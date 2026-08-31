"""AP2 authorization & provenance service (PRD §10–31).

Builds the cryptographic artifacts that bind intent -> execution -> policy ->
payment, persists the authorized amount, and verifies mandates before money
movement. The policy engine (gating_service) remains the authority; this layer
only proves and binds the decision — it never manufactures one.

Signed-payload construction lives in ``merchant.ap2.signed_payloads`` so the
runtime and the standalone verifier share the exact same canonicalization.
"""
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from merchant.config import settings
from merchant.persistence.models.entities import (
    CheckoutSession,
    DecisionReceipt,
    ExecutionMandate,
    IntentMandate,
    PolicyDecision,
    UserIntentAssertion,
    UserKey,
)
from merchant.ap2 import crypto_service
from merchant.ap2.iso import utcnow, parse_dt
from merchant.ap2.user_intent import (
    VERSION as USER_ASSERTION_VERSION,
    TYPE as USER_ASSERTION_TYPE,
    assertion_canonical_string,
    assertion_hash,
)
from merchant.ap2.signed_payloads import (
    _iso,
    decision_receipt_payload,
    execution_mandate_payload,
    intent_mandate_payload,
    policy_decision_payload,
)


# ---------------------------------------------------------------- intent mandate


def build_intent_mandate(
    db: Session,
    *,
    buyer_agent_id: str,
    merchant_id: str,
    intent: dict,
    constraints: dict,
    expires_at: datetime | None = None,
) -> IntentMandate:
    """Create + sign an Intent Mandate defining the buyer's authorized scope.

    The mandate answers "what was the buyer authorized to accomplish?" — it
    must not claim any specific purchase (that is the Execution Mandate's job).
    """
    now = utcnow()
    if expires_at is None:
        expires_at = now + timedelta(days=30)
    m = IntentMandate(
        mandate_id=f"im_{crypto_service.new_nonce()[:16]}",
        type="intent",
        merchant_id=merchant_id,
        buyer_agent_id=buyer_agent_id,
        intent=json.dumps(intent, separators=(",", ":")),
        constraints=json.dumps(constraints, separators=(",", ":")),
        issued_at=now,
        expires_at=expires_at,
        nonce=crypto_service.new_nonce(),
        version=1,
        key_id=crypto_service.key_id(),
    )
    payload = intent_mandate_payload(m)
    m.canonical_payload_hash = crypto_service.object_hash(payload)
    m.signature = crypto_service.sign(payload)
    db.add(m)
    db.flush()
    return m


# ---------------------------------------------------------------- execution mandate


def build_execution_mandate(
    db: Session,
    cs: CheckoutSession,
    *,
    buyer_agent_id: str,
    items: list[dict],
    total_paise: int,
    intent_mandate_id: str | None = None,
    user_assertion_id: str | None = None,
    user_assertion_hash: str | None = None,
) -> ExecutionMandate:
    """Create + sign an Execution Mandate binding the exact transaction.

    Must be called AFTER final validation and immediately before money movement
    (PRD §14–15), so the mandate freezes the basket actually about to be charged.
    """
    from merchant.domain.checkout import computes_cart_hash

    now = utcnow()
    expires = now + timedelta(minutes=settings.mandate_ttl_minutes)

    mandate = ExecutionMandate(
        mandate_id=f"em_{crypto_service.new_nonce()[:16]}",
        type="execution",
        intent_mandate_id=intent_mandate_id,
        merchant_id=settings.merchant_id,
        buyer_agent_id=buyer_agent_id,
        checkout_session_id=cs.checkout_session_id,
        items=json.dumps(items, separators=(",", ":")),
        total_paise=total_paise,
        currency="INR",
        cart_hash=computes_cart_hash(items),
        user_assertion_id=user_assertion_id,
        user_assertion_hash=user_assertion_hash,
        issued_at=now,
        expires_at=expires,
        nonce=crypto_service.new_nonce(),
        key_id=crypto_service.key_id(),
    )
    payload = execution_mandate_payload(mandate)
    mandate.canonical_payload_hash = crypto_service.object_hash(payload)
    mandate.signature = crypto_service.sign(payload)
    db.add(mandate)
    db.flush()

    cs.authorized_amount_paise = total_paise
    cs.execution_mandate_id = mandate.mandate_id
    return mandate


def verify_execution_mandate(
    db: Session,
    mandate: ExecutionMandate,
    cs: CheckoutSession,
    *,
    buyer_agent_id: str,
) -> dict[str, bool]:
    """12-check verification (PRD §24). Returns {name: ok} per check.

    Caller decides: if ALL mandatory checks pass the payment proceeds.
    """
    now = utcnow()
    payload = execution_mandate_payload(mandate)
    checks: dict[str, bool] = {}

    checks["signature_valid"] = crypto_service.verify(payload, mandate.signature)
    checks["key_trusted"] = mandate.key_id == settings.merchant_key_id
    checks["merchant_id"] = mandate.merchant_id == settings.merchant_id
    checks["buyer_id"] = mandate.buyer_agent_id == buyer_agent_id
    checks["session_id"] = mandate.checkout_session_id == cs.checkout_session_id
    checks["amount_matches"] = mandate.total_paise == (cs.authorized_amount_paise or 0)

    # Independent persistence cross-check (breaks the build/verify tautology):
    # the mandated total must also equal the AUTHORITATIVE total persisted on
    # the session at create/update time, NOT merely the value this request just
    # wrote into authorized_amount_paise microseconds earlier.
    try:
        persisted_total = json.loads(cs.totals).get("total")
        checks["amount_matches_persisted"] = (
            persisted_total is not None and mandate.total_paise == round(persisted_total * 100)
        )
    except (ValueError, TypeError):
        checks["amount_matches_persisted"] = False

    checks["currency"] = mandate.currency == "INR"

    # cart hash must equal the current session's line items
    current_items = json.loads(cs.line_items)
    from merchant.domain.checkout import computes_cart_hash

    checks["cart_hash"] = mandate.cart_hash == computes_cart_hash(current_items)
    checks["not_expired"] = mandate.expires_at > now
    checks["fresh_issued"] = (now - mandate.issued_at) <= timedelta(minutes=settings.mandate_ttl_minutes)

    # single use: a consumed mandate (already settled/declined) can never
    # authorize another payment; also refuse if a receipt already exists
    checks["not_consumed"] = mandate.consumed_at is None
    used = db.query(DecisionReceipt).filter(
        DecisionReceipt.execution_mandate_id == mandate.mandate_id
    ).first()
    checks["nonce_unused"] = used is None

    # AP2 intent binding: if this execution is linked to an Intent Mandate, the
    # transaction MUST fall within the intent's signed constraints. This is the
    # enforcement that connects "what the buyer authorized" to "what is executed".
    if mandate.intent_mandate_id:
        ok, detail = verify_intent_scope(
            db,
            mandate.intent_mandate_id,
            buyer_agent_id=mandate.buyer_agent_id,
            items=json.loads(mandate.items),
            total_paise=mandate.total_paise,
        )
        checks["intent_scope_ok"] = ok
    else:
        checks["intent_scope_ok"] = True  # no intent mandate supplied -> nothing to violate

    decision = db.query(PolicyDecision).filter(
        PolicyDecision.execution_mandate_id == mandate.mandate_id,
        PolicyDecision.decision == "approved",
    ).first()
    checks["policy_approved"] = decision is not None

    # Workstream E: a mandate that claims a user-signed assertion must reference
    # a REAL, single-use-consumed assertion whose hash matches the mandate's.
    # Absent reference == legacy path, which is silently allowed here.
    if mandate.user_assertion_id:
        ua = db.get(UserIntentAssertion, mandate.user_assertion_id)
        checks["user_assertion_bound"] = (
            ua is not None
            and ua.consumed_at is not None
            and ua.canonical_payload_hash == mandate.user_assertion_hash
        )
    else:
        checks["user_assertion_bound"] = True

    return checks


def verify_intent_scope(
    db: Session,
    intent_mandate_id: str,
    *,
    buyer_agent_id: str,
    items: list[dict],
    total_paise: int,
) -> tuple[bool, str]:
    """Enforce that an Execution Mandate falls within a signed Intent Mandate.

    Returns (ok, human-readable detail). Any violation means the execution is
    NOT authorized by the buyer's intent and must be blocked (fail closed).
    """
    m = db.get(IntentMandate, intent_mandate_id)
    if m is None:
        return False, f"Unknown intent mandate {intent_mandate_id}"
    payload = intent_mandate_payload(m)
    if not crypto_service.verify(payload, m.signature):
        return False, f"Intent mandate {intent_mandate_id} signature invalid"
    if m.buyer_agent_id != buyer_agent_id:
        return False, "Intent mandate belongs to a different buyer agent"
    if m.expires_at <= utcnow():
        return False, f"Intent mandate {intent_mandate_id} expired"

    constraints = json.loads(m.constraints) or {}
    allowed = set(constraints.get("allowed_categories", []))
    cats = {i.get("category") for i in items}
    if cats and not cats.issubset(allowed) and allowed:
        return False, f"Execution category {sorted(cats)} outside intent categories {sorted(allowed)}"

    max_order_paise = constraints.get("max_order_value_paise")
    if max_order_paise is not None and total_paise > int(max_order_paise):
        return False, (
            f"Execution total {total_paise}paise exceeds intent max {int(max_order_paise)}paise"
        )

    max_qty = constraints.get("max_quantity_per_item")
    if max_qty is not None:
        over = [i for i in items if int(i.get("quantity", 0)) > int(max_qty)]
        if over:
            return False, f"Item quantity exceeds intent max {max_qty} per item"

    return True, f"Execution within signed intent scope {intent_mandate_id}"


def mark_mandate_consumed(db: Session, mandate: ExecutionMandate) -> None:
    """Mark an Execution Mandate as single-use-consumed after its payment settles."""
    if mandate.consumed_at is None:
        mandate.consumed_at = utcnow()
        db.flush()


# ---------------------------------------------------------------- user keys (Workstream E)


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


# ---------------------------------------------------------------- policy decision


def sign_policy_decision(
    db: Session,
    cs: CheckoutSession,
    mandate_id: str,
    *,
    decision: str,
    checks: list[dict],
    authorized_amount_paise: int,
) -> PolicyDecision:
    """Wrap a deterministic gating result into a signed Policy Decision artifact."""
    dec = PolicyDecision(
        decision_id=f"dec_{crypto_service.new_nonce()[:16]}",
        execution_mandate_id=mandate_id,
        checkout_session_id=cs.checkout_session_id,
        decision=decision,
        checks=json.dumps(checks, separators=(",", ":")),
        authorized_amount_paise=authorized_amount_paise,
        timestamp=utcnow(),
        key_id=crypto_service.key_id(),
    )
    payload = policy_decision_payload(dec)
    dec.canonical_payload_hash = crypto_service.object_hash(payload)
    dec.signature = crypto_service.sign(payload)
    db.add(dec)
    db.flush()
    return dec


# ---------------------------------------------------------------- decision receipt


def issue_decision_receipt(
    db: Session,
    cs: CheckoutSession,
    *,
    mandate: ExecutionMandate,
    decision: PolicyDecision | None,
    outcome: str,
    razorpay_order_id: str | None,
    razorpay_payment_id: str | None,
) -> DecisionReceipt:
    """Create + sign a Decision Receipt after payment completes/declines (AP2 §27)."""
    receipt = DecisionReceipt(
        receipt_id=f"rcpt_{crypto_service.new_nonce()[:16]}",
        intent_mandate_id=mandate.intent_mandate_id,
        execution_mandate_id=mandate.mandate_id,
        decision_id=decision.decision_id if decision else None,
        checkout_session_id=cs.checkout_session_id,
        authorized_amount_paise=cs.authorized_amount_paise or mandate.total_paise,
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id,
        payment_status="verified",
        outcome=outcome,
        created_at=utcnow(),
        key_id=crypto_service.key_id(),
    )
    payload = decision_receipt_payload(receipt)
    receipt.canonical_payload_hash = crypto_service.object_hash(payload)
    receipt.signature = crypto_service.sign(payload)
    db.add(receipt)
    db.flush()
    return receipt
