"""AP2 user-key + user-intent-assertion service (Workstream E).

Owns the buying human's Ed25519 public-key registry and the verification of
user-signed intent assertions — the root of authority that authorizes money
movement. Extracted from ``authorization_service`` to keep the mandate/
decision/receipt concerns separate from the user-key concern.

Fail-closed guarantee preserved: any failed check returns ``checks`` with a
false entry and detail naming every failed check; the CALLER must block
completion entirely (PAYMENT_BLOCKED).
"""
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from merchant.config import settings
from merchant.persistence.models.entities import UserIntentAssertion, UserKey
from merchant.ap2 import crypto_service
from merchant.ap2.iso import utcnow, parse_dt
from merchant.ap2.user_intent import (
    VERSION as USER_ASSERTION_VERSION,
    TYPE as USER_ASSERTION_TYPE,
    assertion_canonical_string,
    assertion_hash,
)


def register_user_key(
    db: Session,
    *,
    user_id: str,
    key_id: str,
    public_key_pem: str,
) -> UserKey:
    """Register a buying human's Ed25519 PUBLIC key (root of authority, E).

    The private key lives only in the user's browser localStorage and is never
    transmitted. Registration replaces a previous key for the same user (key
    rotation); it never weakens any check downstream (verification requires the
    registered key to be present AND active).
    """
    if not crypto_service.is_ed25519_public_key_pem(public_key_pem):
        raise ValueError("public key PEM could not be parsed as an Ed25519 public key")

    row = db.get(UserKey, user_id)
    if row is None:
        row = UserKey(user_id=user_id, key_id=key_id, public_key_pem=public_key_pem, status="active")
        db.add(row)
    else:
        row.key_id = key_id
        row.public_key_pem = public_key_pem
        row.status = "active"
    db.flush()
    return row


def resolve_user_key(db: Session, user_key_id: str) -> UserKey | None:
    return (
        db.query(UserKey).filter(UserKey.key_id == user_key_id, UserKey.status == "active").first()
    )


def store_user_assertion(
    db: Session,
    artifact: dict,
    *,
    key_id: str,
    signature: str,
) -> UserIntentAssertion:
    """Persist a user-signed UserIntentAssertion (single-use; consumed on verify).

    The canonical string + hash are recomputed locally (never trusted from the
    wire); verify_user_assertion has already proven the wire-supplied hash
    equals this same recomputation and the signature verifies under the trusted
    key.
    """
    from datetime import datetime as _dt

    canon_ok = True
    canonical = None
    try:
        canonical = assertion_canonical_string(
            assertion_id=str(artifact["assertion_id"]),
            user_id=str(artifact["user_id"]),
            user_key_id=str(artifact["user_key_id"]),
            merchant_id=str(artifact["merchant_id"]),
            delegate_id=str(artifact["delegate_id"]),
            intent=artifact.get("intent") or {},
            constraint_ceiling=artifact.get("constraint_ceiling") or {},
            issued_at=artifact["issued_at"],
            expires_at=artifact["expires_at"],
            nonce=str(artifact["nonce"]),
        )
    except (KeyError, TypeError):
        canon_ok = False
    canonical_hash = (
        assertion_hash(
            assertion_id=str(artifact["assertion_id"]),
            user_id=str(artifact["user_id"]),
            user_key_id=str(artifact["user_key_id"]),
            merchant_id=str(artifact["merchant_id"]),
            delegate_id=str(artifact["delegate_id"]),
            intent=artifact.get("intent") or {},
            constraint_ceiling=artifact.get("constraint_ceiling") or {},
            issued_at=artifact["issued_at"],
            expires_at=artifact["expires_at"],
            nonce=str(artifact["nonce"]),
        )
        if canon_ok
        else ""
    )
    row = UserIntentAssertion(
        assertion_id=str(artifact["assertion_id"]),
        version=str(artifact.get("version", USER_ASSERTION_VERSION)),
        type=str(artifact.get("type", USER_ASSERTION_TYPE)),
        user_id=str(artifact["user_id"]),
        user_key_id=str(artifact["user_key_id"]),
        merchant_id=str(artifact["merchant_id"]),
        delegate_id=str(artifact["delegate_id"]),
        intent=json.dumps(artifact.get("intent"), separators=(",", ":")),
        constraint_ceiling=json.dumps(artifact.get("constraint_ceiling", {}), separators=(",", ":")),
        issued_at=_dt.fromisoformat(str(artifact["issued_at"])) if isinstance(artifact["issued_at"], str) else artifact["issued_at"],
        expires_at=_dt.fromisoformat(str(artifact["expires_at"])) if isinstance(artifact["expires_at"], str) else artifact["expires_at"],
        nonce=str(artifact["nonce"]),
        canonical_payload_hash=canonical_hash,
        key_id=key_id,
        signature=signature,
        consumed_at=utcnow(),  # single-use: consumed the moment it authorizes
    )
    db.add(row)
    db.flush()
    return row


def verify_user_assertion(
    db: Session,
    artifact: dict,
    *,
    checkout_session_id: str,
    buyer_agent_id: str,
    total_paise: int,
    now: datetime | None = None,
) -> tuple[dict[str, bool], str, str | None]:
    """Verify a user-signed intent assertion (fail-closed) — root of authority (E).

    Check order mirrors the risk order:
        1. signature valid under the registered user public key
        2. registered key trusted (present + active + key_id/user_id ownership)
        3. canonical payload hash matches the recomputation
        4. expiry window (not expired, not future-skewed)
        5. single-use (nonce not consumed already)
        6. subject binding (assertion user == registered key owner)
        7. merchant binding (assertion merchant == this merchant)
        8. delegate binding (assertion delegate == the buyer_agent executing)
        9. session binding (intent.action == checkout AND intent.checkout_session_id)
       10. constraint ceiling (currency + max_amount_paise >= execution total)

    Returns (checks, detail, assertion_id). On any failure the CALLER must
    block completion entirely: no Execution Mandate, no Razorpay order, no
    payment (PAYMENT_BLOCKED) — the merchant never trusts an unverified user
    signature to authorize money movement.
    """
    now = now or utcnow()
    checks: dict[str, bool] = {}

    # --- 1. + 2. signature + key trust (resolve key BEFORE touching fields) ---
    key = resolve_user_key(db, str(artifact.get("user_key_id", "")))
    canonical = None
    try:
        canonical = assertion_canonical_string(
            assertion_id=str(artifact["assertion_id"]),
            user_id=str(artifact["user_id"]),
            user_key_id=str(artifact["user_key_id"]),
            merchant_id=str(artifact["merchant_id"]),
            delegate_id=str(artifact["delegate_id"]),
            intent=artifact.get("intent") or {},
            constraint_ceiling=artifact.get("constraint_ceiling") or {},
            issued_at=artifact["issued_at"],
            expires_at=artifact["expires_at"],
            nonce=str(artifact["nonce"]),
        )
    except (KeyError, TypeError):
        canonical = None
    checks["signature_valid"] = bool(
        canonical is not None
        and key is not None
        and crypto_service.verify_utf8(canonical, str(artifact.get("signature", "")), key.public_key_pem)
    )
    checks["key_trusted"] = key is not None and key.status == "active"

    # --- 3. hash integrity ---
    got = assertion_hash(
        assertion_id=str(artifact["assertion_id"]),
        user_id=str(artifact["user_id"]),
        user_key_id=str(artifact["user_key_id"]),
        merchant_id=str(artifact["merchant_id"]),
        delegate_id=str(artifact["delegate_id"]),
        intent=artifact.get("intent") or {},
        constraint_ceiling=artifact.get("constraint_ceiling") or {},
        issued_at=artifact["issued_at"],
        expires_at=artifact["expires_at"],
        nonce=str(artifact["nonce"]),
    )
    checks["hash_integrity"] = str(artifact.get("canonical_payload_hash", "")) == got

    # --- 4. expiry window ---
    try:
        issued = parse_dt(artifact["issued_at"])
        expires = parse_dt(artifact["expires_at"])
    except (KeyError, TypeError, ValueError):
        issued, expires = None, None
    checks["not_expired"] = (
        issued is not None and expires is not None and issued < expires and expires > now
        and (now - issued) <= timedelta(minutes=settings.user_assertion_ttl_minutes)
    )

    # --- 5. single-use (replay protection) ---
    existing = (
        db.query(UserIntentAssertion)
        .filter(UserIntentAssertion.assertion_id == str(artifact.get("assertion_id", "")))
        .first()
        if artifact.get("assertion_id")
        else None
    )
    checks["nonce_unused"] = existing is None
    if existing is not None:
        checks["nonce_unused"] = existing.consumed_at is None

    # --- 6. subject: assertion user == registered key owner ---
    checks["subject_match"] = key is not None and str(artifact.get("user_id", "")) == key.user_id

    # --- 7. merchant binding ---
    checks["merchant_match"] = str(artifact.get("merchant_id", "")) == settings.merchant_id

    # --- 8. delegate binding ---
    checks["delegate_match"] = str(artifact.get("delegate_id", "")) == buyer_agent_id

    # --- 9. checkout-session binding ---
    intent = artifact.get("intent") or {}
    if not isinstance(intent, dict):
        intent = {}
    checks["session_match"] = (
        intent.get("action") == "checkout"
        and str(intent.get("checkout_session_id", "")) == checkout_session_id
    )

    # --- 10. constraint ceiling ---
    ceiling = artifact.get("constraint_ceiling") or {}
    if not isinstance(ceiling, dict):
        ceiling = {}
    max_paise = ceiling.get("max_amount_paise")
    currency_ok = str(ceiling.get("currency", "INR")) == "INR"
    amount_ok = isinstance(max_paise, int) and max_paise > 0 and total_paise <= max_paise
    checks["constraint_ceiling"] = currency_ok and amount_ok

    detail = (
        "User intent assertion verified: one-time user consent binds subject, "
        "merchant, delegate, checkout session and spend ceiling"
        if all(checks.values())
        else "User intent assertion verification FAILED: " + ", ".join(
            name for name, ok in checks.items() if not ok
        )
    )
    return checks, detail, str(artifact.get("assertion_id", ""))
