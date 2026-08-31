"""Standalone independent verification of AP2-inspired signed transaction artifacts.

This module answers, purely from supplied serialized artifacts and a public key:

    "Is this supplied signed artifact cryptographically authentic and internally
     consistent according to its signed payload?"

It is deliberately free of any merchant database, checkout/payment service,
Buyer/Merchant Agent, LLM or Razorpay dependency. It never loads the merchant
private key and never falls back to it: verification is performed exclusively
against the public key the caller supplies.

    CRYPTOGRAPHIC VERIFICATION   -> this module
    BUSINESS / RUNTIME VERIFICATION -> elsewhere (delegation, stock, checkout
                                       state, current payment settlement).

See ``docs/AP2_STANDALONE_VERIFIER.md`` for precisely what this proves and what
it does NOT prove.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from merchant.ap2 import crypto_service
from merchant.ap2.signed_payloads import (
    decision_receipt_payload,
    execution_mandate_payload,
    intent_mandate_payload,
    policy_decision_payload,
)
from merchant.ap2.user_intent import artifact_canonical_string

# ---------------------------------------------------------------------------
# Stable, machine-readable reason codes
# ---------------------------------------------------------------------------
REASON_OK = "OK"
REASON_MALFORMED_ARTIFACT = "MALFORMED_ARTIFACT"
REASON_INVALID_ARTIFACT_TYPE = "INVALID_ARTIFACT_TYPE"
REASON_MISSING_FIELD = "MISSING_FIELD"
REASON_INVALID_PUBLIC_KEY = "INVALID_PUBLIC_KEY"
REASON_CANONICAL_PAYLOAD = "CANONICAL_PAYLOAD_FAILURE"
REASON_PAYLOAD_HASH_MISMATCH = "PAYLOAD_HASH_MISMATCH"
REASON_SIGNATURE_INVALID = "SIGNATURE_INVALID"
REASON_KEY_ID_MISMATCH = "KEY_ID_MISMATCH"
REASON_EXPIRY_RELATIONSHIP = "INVALID_EXPIRY_RELATIONSHIP"
REASON_BUNDLE_RELATIONSHIP = "BUNDLE_RELATIONSHIP_FAILURE"
REASON_NOT_VERIFIABLE = "NOT_VERIFIABLE_FROM_ARTIFACTS"
REASON_CANONICAL_STRING = "CANONICAL_STRING_FAILURE"

# Payload builders keyed by artifact type string.
_PAYLOAD_BUILDERS: dict[str, Callable[[Any], dict]] = {
    "execution": execution_mandate_payload,
    "intent": intent_mandate_payload,
    "policy_decision": policy_decision_payload,
    "decision_receipt": decision_receipt_payload,
}

_ARTIFACT_TYPES = set(_PAYLOAD_BUILDERS) | {"user_assertion"}


@dataclass
class VerificationResult:
    """Structured, machine-readable verification outcome (never only a bool)."""

    valid: bool = False
    artifact_type: str = ""
    checks: dict[str, bool] = field(default_factory=dict)
    reason: str | None = None
    key_id: str | None = None
    verified_at_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "artifact_type": self.artifact_type,
            "checks": dict(self.checks),
            "reason": self.reason,
            "key_id": self.key_id,
            "verified_at_utc": self.verified_at_utc,
        }


def _load_public_key(public_key_pem: str) -> Ed25519PublicKey | None:
    """Load an Ed25519 public key from a PEM string. Returns None on any failure.

    Never touches the private key — this is purely a public-key parse.
    """
    try:
        key = serialization.load_pem_public_key(public_key_pem.strip().encode("utf-8"))
        if isinstance(key, Ed25519PublicKey):
            return key
        return None
    except Exception:  # noqa: BLE001 — malformed public key => not usable
        return None


def _require_fields(artifact: dict, required: list[str]) -> list[str]:
    missing = [f for f in required if artifact.get(f) is None]
    return missing


def verify_artifact(
    artifact: dict,
    public_key_pem: str,
    *,
    expected_key_id: str | None = None,
    artifact_type: str | None = None,
) -> VerificationResult:
    """Independently verify one signed AP2 artifact from plain data.

    Args:
        artifact: serialized artifact as a plain dict (see docs for the shape).
        public_key_pem: the Ed25519 public key as a PEM string. For merchant
            artifacts this is the merchant key; for a ``user_assertion`` it is
            the USER's registered public key.
        expected_key_id: optional expected ``key_id``. When supplied, the
            artifact's ``key_id`` must equal it. When omitted, key_id is checked
            only for presence/consistency (never trusted to a config value).
        artifact_type: optional override of the artifact type (falls back to the
            artifact's ``type`` field).

    Returns:
        A structured VerificationResult; never raises on malformed input.
    """
    if not isinstance(artifact, dict):
        root = VerificationResult(
            artifact_type=artifact_type or "unknown",
            checks={"required_fields": False},
        )
        root.valid = False
        root.reason = REASON_MALFORMED_ARTIFACT
        return root

    root = VerificationResult(artifact_type=artifact_type or str(artifact.get("type", "")), checks={})
    atype = artifact_type or artifact.get("type")
    if atype not in _ARTIFACT_TYPES:
        root.valid = False
        root.reason = REASON_INVALID_ARTIFACT_TYPE
        root.checks = {"required_fields": False}
        return root
    root.artifact_type = str(atype)

    if atype == "user_assertion":
        return _verify_user_assertion(artifact, public_key_pem, expected_key_id=expected_key_id, root=root)

    # Public key must be a usable Ed25519 key (and must match the one used to
    # sign — enforced by the signature check below).
    pub = _load_public_key(public_key_pem)
    if pub is None:
        root.valid = False
        root.reason = REASON_INVALID_PUBLIC_KEY
        root.checks = {"public_key": False}
        return root
    root.checks["public_key"] = True

    # Minimum required signed fields per artifact type.
    required_by_type: dict[str, list[str]] = {
        "execution": [
            "mandate_id", "merchant_id", "buyer_agent_id", "checkout_session_id",
            "items", "total_paise", "currency", "issued_at", "expires_at", "nonce",
            "signature", "canonical_payload_hash", "key_id",
        ],
        "intent": [
            "mandate_id", "merchant_id", "buyer_agent_id", "intent", "constraints",
            "issued_at", "expires_at", "nonce", "version", "signature",
            "canonical_payload_hash", "key_id",
        ],
        "policy_decision": [
            "decision_id", "execution_mandate_id", "checkout_session_id", "decision",
            "checks", "authorized_amount_paise", "timestamp", "signature",
            "canonical_payload_hash", "key_id",
        ],
        "decision_receipt": [
            "receipt_id", "execution_mandate_id", "checkout_session_id",
            "authorized_amount_paise", "razorpay_order_id", "razorpay_payment_id",
            "payment_status", "outcome", "created_at", "signature",
            "canonical_payload_hash", "key_id",
        ],
        "user_assertion": [
            "assertion_id", "user_id", "user_key_id", "merchant_id", "delegate_id",
            "intent", "constraint_ceiling", "issued_at", "expires_at", "nonce",
            "signature", "canonical_payload_hash", "key_id",
        ],
    }
    missing = _require_fields(artifact, required_by_type[atype])
    if missing:
        root.valid = False
        root.reason = REASON_MISSING_FIELD
        root.checks["required_fields"] = False
        root.checks["detail"] = {"missing": missing}
        return root
    root.checks["required_fields"] = True

    # Reconstruct the signed canonical payload.
    try:
        payload = _PAYLOAD_BUILDERS[atype](artifact)
    except (ValueError, TypeError, KeyError):
        root.valid = False
        root.reason = REASON_CANONICAL_PAYLOAD
        root.checks["canonical_payload"] = False
        return root
    root.checks["canonical_payload"] = True

    # Payload hash must match the recorded canonical payload hash.
    recorded_hash = artifact.get("canonical_payload_hash")
    computed_hash = crypto_service.object_hash(payload)
    hash_ok = bool(recorded_hash) and recorded_hash == computed_hash
    root.checks["payload_hash"] = hash_ok

    # Signature must verify under the supplied public key.
    signature_hex = artifact.get("signature")
    sig_ok = crypto_service.verify(payload, signature_hex, public_key_pem_str=public_key_pem)
    root.checks["signature"] = sig_ok

    # key_id consistency.
    sig_key_id = artifact.get("key_id")
    key_id_ok = bool(sig_key_id)
    if expected_key_id is not None:
        key_id_ok = key_id_ok and sig_key_id == expected_key_id
    root.checks["key_id"] = key_id_ok
    root.key_id = sig_key_id

    # Execution-mandate specific: expiry relationship (iss_ts must precede exp).
    if atype == "execution" and hash_ok and sig_ok:
        tup_ok = _expiry_relationship(artifact)
        root.checks["expiry_relationship"] = tup_ok
    else:
        root.checks.setdefault("expiry_relationship", not (atype == "execution"))

    all_ok = all(v is True for k, v in root.checks.items() if k != "detail")
    if not all_ok:
        root.valid = False
        if not hash_ok:
            root.reason = REASON_PAYLOAD_HASH_MISMATCH
        elif not sig_ok:
            root.reason = REASON_SIGNATURE_INVALID
        elif not key_id_ok:
            root.reason = REASON_KEY_ID_MISMATCH
        elif atype == "execution" and not root.checks.get("expiry_relationship", True):
            root.reason = REASON_EXPIRY_RELATIONSHIP
        else:
            root.reason = REASON_CANONICAL_PAYLOAD
        return root

    root.valid = True
    root.reason = REASON_OK
    return root


def _verify_user_assertion(
    artifact: dict,
    user_public_key_pem: str,
    *,
    expected_key_id: str | None,
    root: VerificationResult,
) -> VerificationResult:
    """Verify a USER-signed intent assertion artifact (Workstream E).

    Unlike merchant artifacts (which sign canonical JSON), a user assertion
    signs the deterministic pipe-separated canonical STRING produced by
    ``user_intent.artifact_canonical_string`` — the same bytes the browser's
    tweetnacl signed. Verification therefore checks:
        - canonical string reconstruction succeeds,
        - canonical_payload_hash == SHA-256(canonical string),
        - Ed25519 signature over the canonical string verifies under the
          supplied USER public key,
        - key_id is present (and equals expected_key_id when supplied).
    Expiry is a runtime concern (the merchant enforces it at complete); here we
    only verify cryptographic authenticity + internal consistency.
    """
    root.checks["required_fields"] = True

    pub = _load_public_key(user_public_key_pem)
    if pub is None:
        root.valid = False
        root.reason = REASON_INVALID_PUBLIC_KEY
        root.checks["public_key"] = False
        return root
    root.checks["public_key"] = True

    canonical = artifact_canonical_string(artifact)
    if canonical is None:
        root.valid = False
        root.reason = REASON_CANONICAL_STRING
        root.checks["canonical_string"] = False
        return root
    root.checks["canonical_string"] = True

    import hashlib

    computed_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    recorded_hash = artifact.get("canonical_payload_hash")
    hash_ok = bool(recorded_hash) and recorded_hash == computed_hash
    root.checks["payload_hash"] = hash_ok

    sig_ok = crypto_service.verify_utf8(
        canonical, str(artifact.get("signature", "")), user_public_key_pem
    )
    root.checks["signature"] = sig_ok

    sig_key_id = artifact.get("key_id")
    key_id_ok = bool(sig_key_id)
    if expected_key_id is not None:
        key_id_ok = key_id_ok and sig_key_id == expected_key_id
    root.checks["key_id"] = key_id_ok
    root.key_id = sig_key_id

    tup_ok = _expiry_relationship(artifact)
    root.checks["expiry_relationship"] = tup_ok

    all_ok = all(v is True for k, v in root.checks.items() if k != "detail")
    if not all_ok:
        root.valid = False
        if not hash_ok:
            root.reason = REASON_PAYLOAD_HASH_MISMATCH
        elif not sig_ok:
            root.reason = REASON_SIGNATURE_INVALID
        elif not key_id_ok:
            root.reason = REASON_KEY_ID_MISMATCH
        elif not tup_ok:
            root.reason = REASON_EXPIRY_RELATIONSHIP
        else:
            root.reason = REASON_CANONICAL_STRING
        return root

    root.valid = True
    root.reason = REASON_OK
    return root


def _expiry_relationship(artifact: dict) -> bool:
    """iss_ts (issued_at) must precede exp (expires_at) inside the payload."""
    iss = artifact.get("issued_at")
    exp = artifact.get("expires_at")
    try:
        return iss is not None and exp is not None and iss < exp
    except TypeError:
        return False


# ---------------------------------------------------------------------------
# Higher-level: complete exported evidence bundle
# ---------------------------------------------------------------------------


def verify_transaction_evidence(
    execution_mandate: dict,
    policy_decision: dict,
    decision_receipt: dict,
    public_key_pem: str,
    *,
    expected_key_id: str | None = None,
    intent_mandate: dict | None = None,
    user_assertion: dict | None = None,
    user_public_key_pem: str | None = None,
) -> VerificationResult:
    """Verify a complete exported evidence bundle.

    Verifies each artifact independently, then checks the cross-artifact
    relationships that are EXPRESSLY represented inside their signed payloads
    (e.g. the receipt references the execution mandate; signed amounts agree).
    Relationships that cannot be established from the supplied artifacts alone
    (current Razorpay settlement, live checkout state, etc.) are reported as
    NOT_VERIFIABLE_FROM_ARTIFACTS and never guessed.

    Workstream E: when a ``user_assertion`` artifact + its ``user_public_key_pem``
    are supplied, the user signature is verified AND the Execution Mandate must
    reference the same assertion id + canonical hash. When an Execution Mandate
    references an assertion that the caller does NOT supply, the bundle requires
    the user artifacts or it cannot be considered fully bound.

    Args:
        execution_mandate: serialized Execution Mandate artifact.
        policy_decision: serialized Policy Decision artifact.
        decision_receipt: serialized Decision Receipt artifact.
        public_key_pem: merchant Ed25519 public key as a PEM string.
        expected_key_id: optional expected key id.
        intent_mandate: optional serialized Intent Mandate artifact to validate
            the intent linkage where present.
        user_assertion: optional serialized UserIntentAssertion artifact.
        user_public_key_pem: the USER's registered Ed25519 public key (required
            when user_assertion is supplied).

    Returns:
        A structured VerificationResult.
    """
    root = VerificationResult(artifact_type="transaction_evidence", checks={})

    individual = {
        "execution_mandate": verify_artifact(
            execution_mandate, public_key_pem, expected_key_id=expected_key_id, artifact_type="execution"
        ),
        "policy_decision": verify_artifact(
            policy_decision, public_key_pem, expected_key_id=expected_key_id, artifact_type="policy_decision"
        ),
        "decision_receipt": verify_artifact(
            decision_receipt, public_key_pem, expected_key_id=expected_key_id, artifact_type="decision_receipt"
        ),
    }
    if user_assertion:
        individual["user_assertion"] = verify_artifact(
            user_assertion, user_public_key_pem or "", expected_key_id=None, artifact_type="user_assertion"
        )
    for name, res in individual.items():
        root.checks[f"{name}_valid"] = res.valid
        if not res.valid:
            root.valid = False
            root.reason = REASON_BUNDLE_RELATIONSHIP
            root.checks[f"{name}_reason"] = res.reason
            root.key_id = res.key_id
            return root

    # Cross-artifact signed relationships.
    em_payload = execution_mandate_payload(execution_mandate)
    pol_payload = policy_decision_payload(policy_decision)
    rcpt_payload = decision_receipt_payload(decision_receipt)

    rel = {}
    rel["receipt_refs_execution"] = rcpt_payload["execution_mandate_id"] == em_payload["mandate_id"]
    rel["policy_refs_execution"] = pol_payload["execution_mandate_id"] == em_payload["mandate_id"]
    # If the receipt carries a decision_id and a policy decision exists, they
    # must agree; if the receipt has no decision_id, we can still bind policy
    # to the receipt via the shared execution reference.
    if rcpt_payload.get("decision_id"):
        rel["receipt_refs_policy"] = rcpt_payload["decision_id"] == pol_payload["decision_id"]
    else:
        rel["receipt_refs_policy"] = True

    rel["session_agreement"] = (
        rcpt_payload["checkout_session_id"] == em_payload["checkout_session_id"]
        == pol_payload["checkout_session_id"]
    )
    rel["amounts_agree"] = (
        rcpt_payload["authorized_amount_paise"]
        == pol_payload["authorized_amount_paise"]
        == em_payload["total_paise"]
    )
    if intent_mandate:
        im_ok = verify_artifact(intent_mandate, public_key_pem, expected_key_id=expected_key_id, artifact_type="intent")
        rel["intent_valid"] = im_ok.valid
        if im_ok.valid:
            im_payload = intent_mandate_payload(intent_mandate)
            rel["execution_refs_intent"] = em_payload["intent_mandate_id"] == im_payload["mandate_id"]

    # Workstream E: execution mandates may carry a user-assertion reference.
    em_refs_user = em_payload.get("user_assertion_id")
    if em_refs_user:
        if not user_assertion:
            # The mandate claims user authority but the caller supplied no user
            # artifacts — cannot be bound; fail-close rather than guess.
            rel["execution_refs_user"] = False
            rel["missing_user_artifacts"] = True
        else:
            rel["execution_refs_user"] = (
                em_refs_user == user_assertion.get("assertion_id")
                and em_payload.get("user_assertion_hash") == user_assertion.get("canonical_payload_hash")
            )
    else:
        # Legacy path: no user-signed authority claimed — consistent if none supplied.
        rel["execution_refs_user"] = user_assertion is None or (not user_assertion)

    failed = [k for k, v in rel.items() if v is False]
    for k, v in rel.items():
        root.checks[k] = v

    if failed:
        root.valid = False
        root.reason = REASON_BUNDLE_RELATIONSHIP
        root.checks["failed_relationships"] = failed
        root.key_id = individual["execution_mandate"].key_id
        return root

    root.valid = True
    root.reason = REASON_OK
    root.key_id = individual["execution_mandate"].key_id
    root.checks["current_razorpay_settlement"] = None
    root.checks["current_checkout_state"] = None
    root.checks["note"] = REASON_NOT_VERIFIABLE
    return root
