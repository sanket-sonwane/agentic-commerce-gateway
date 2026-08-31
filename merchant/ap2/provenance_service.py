"""AP2 provenance verification (PRD §30, §37, and GET /provenance/{id})."""
import json

from sqlalchemy.orm import Session

from merchant.persistence.models.entities import (
    CheckoutSession,
    DecisionReceipt,
    ExecutionMandate,
    PolicyDecision,
    UserIntentAssertion,
    UserKey,
)
from merchant.ap2 import authorization_service, crypto_service
from merchant.ap2.signed_payloads import _iso
from merchant.ap2.user_intent import artifact_canonical_string


def _provenance_available(checkout_session: CheckoutSession, mandate: ExecutionMandate | None) -> bool:
    return mandate is not None


def _full_artifact(payload: dict, signature: str, canonical_payload_hash: str, key_id: str) -> dict:
    """Fold signature + key_id back over a signed payload dict.

    The signed payload alone is not independently verifiable; the standalone
    verifier needs the full artifact shape (payload fields + ``signature`` +
    ``canonical_payload_hash`` + ``key_id``) exactly like the offline export in
    ``scripts/export_ap2_evidence.py`` produces. Keeping this export in the same
    shape means any consumer can feed ``artifacts.*`` straight to
    ``standalone_verifier.verify_transaction_evidence``.
    """
    d = dict(payload)
    d["signature"] = signature
    d["canonical_payload_hash"] = canonical_payload_hash
    d["key_id"] = key_id
    return d


def verify_provenance(db: Session, checkout_session_id: str) -> dict:
    """Reconstruct + verify the AP2 provenance chain for one checkout session.

    Returns a provenance report with per-check results. When the transaction
    predates the AP2 layer (no Execution Mandate), provenance reports
    NOT_AVAILABLE instead of falsely claiming verification.
    """
    cs = db.get(CheckoutSession, checkout_session_id)
    if cs is None:
        from merchant.persistence.models.dto import ErrorCode, ProtocolError

        raise ProtocolError(
            ErrorCode.CHECKOUT_NOT_FOUND,
            f"Checkout session {checkout_session_id} not found",
            http_status=404,
        )

    checks: list[dict] = []
    mandate = None
    if cs.execution_mandate_id:
        mandate = db.get(ExecutionMandate, cs.execution_mandate_id)

    if not _provenance_available(cs, mandate):
        return {
            "checkout_session_id": checkout_session_id,
            "provenance_available": False,
            "status": cs.status,
            "checks": [],
            "summary": "PROVENANCE: NOT_AVAILABLE — this transaction predates the AP2 "
                       "cryptographic authorization layer or has no execution mandate.",
            "artifacts": {},
        }

    assert mandate is not None

    # 1. Execution Mandate signature + integrity
    em_payload = authorization_service.execution_mandate_payload(mandate)
    em_crc = crypto_service.object_hash(em_payload)
    checks.append({
        "name": "execution_mandate_signature",
        "ok": crypto_service.verify(em_payload, mandate.signature),
        "detail": f"Mandate {mandate.mandate_id} signature over canonical payload",
    })
    checks.append({
        "name": "execution_mandate_integrity",
        "ok": mandate.canonical_payload_hash == em_crc,
        "detail": "Stored canonical payload hash matches recomputation",
    })

    # 2. Mandate binds the session amount
    checks.append({
        "name": "amount_binding",
        "ok": cs.authorized_amount_paise is not None and mandate.total_paise == cs.authorized_amount_paise,
        "detail": (
            f"Mandate total {mandate.total_paise}paise == session authorized "
            f"{cs.authorized_amount_paise}paise"
        ),
    })

    # 3. Cart hash matches the session basket
    from merchant.domain.checkout_service import computes_cart_hash

    checks.append({
        "name": "cart_binding",
        "ok": computes_cart_hash(json.loads(cs.line_items)) == mandate.cart_hash,
        "detail": "Mandate cart_hash matches current session line items",
    })

    # 3b. If linked to an Intent Mandate, execution must stay within its signed
    # scope (RE-3): amount + category + quantity bounds from the buyer's intent.
    from merchant.ap2.authorization_service import verify_intent_scope

    if mandate.intent_mandate_id:
        scope_ok, scope_detail = verify_intent_scope(
            db,
            mandate.intent_mandate_id,
            buyer_agent_id=mandate.buyer_agent_id,
            items=json.loads(mandate.items),
            total_paise=mandate.total_paise,
        )
        checks.append({
            "name": "intent_scope",
            "ok": scope_ok,
            "detail": scope_detail,
        })

    # 3c. Workstream E: user-signed intent assertion — the root of authority.
    # If the Execution Mandate references one, it must exist, be single-use
    # consumed, hash-match, and its signature must verify under the user's
    # registered public key.
    if mandate.user_assertion_id:
        ua = db.get(UserIntentAssertion, mandate.user_assertion_id)
        if ua is not None:
            key = (
                db.query(UserKey).filter(UserKey.key_id == ua.user_key_id).first()
            )
            rebuilt = {
                "assertion_id": ua.assertion_id,
                "version": ua.version,
                "type": ua.type,
                "user_id": ua.user_id,
                "user_key_id": ua.user_key_id,
                "merchant_id": ua.merchant_id,
                "delegate_id": ua.delegate_id,
                "intent": json.loads(ua.intent),
                "constraint_ceiling": json.loads(ua.constraint_ceiling),
                "issued_at": ua.issued_at.isoformat(timespec="seconds"),
                "expires_at": ua.expires_at.isoformat(timespec="seconds"),
                "nonce": ua.nonce,
            }
            canonical = artifact_canonical_string(rebuilt)
            sig_ok = bool(
                canonical is not None
                and key is not None
                and crypto_service.verify_utf8(canonical, ua.signature, key.public_key_pem)
            )
            checks.append({
                "name": "user_assertion",
                "ok": (
                    key is not None
                    and key.status == "active"
                    and ua.consumed_at is not None
                    and ua.canonical_payload_hash == mandate.user_assertion_hash
                    and sig_ok
                ),
                "detail": (
                    f"User assertion {ua.assertion_id} verified under user key "
                    f"{ua.user_key_id} (single-use consumed)"
                    if key is not None and sig_ok and ua.canonical_payload_hash == mandate.user_assertion_hash
                    else "User assertion present but NOT verified"
                ),
            })
        else:
            checks.append({
                "name": "user_assertion",
                "ok": False,
                "detail": f"Referenced user assertion {mandate.user_assertion_id} not found",
            })
    else:
        checks.append({
            "name": "user_assertion",
            "ok": True,
            "detail": "No user-signed assertion on this transaction (legacy authorization path)",
        })

    # 4. Policy decision approved + signed
    decision = (
        db.query(PolicyDecision)
        .filter(PolicyDecision.execution_mandate_id == mandate.mandate_id)
        .order_by(PolicyDecision.created_at.desc())
        .first()
    )
    if decision and decision.decision == "approved":
        dec_payload = authorization_service.policy_decision_payload(decision)
        checks.append({
            "name": "policy_approved",
            "ok": crypto_service.verify(dec_payload, decision.signature)
            and decision.authorized_amount_paise == mandate.total_paise,
            "detail": "Signed policy decision approves the exact mandate amount",
        })
    elif decision:
        checks.append({
            "name": "policy_approved",
            "ok": False,
            "detail": f"Policy decision present but decision={decision.decision}",
        })
    else:
        checks.append({
            "name": "policy_approved",
            "ok": False,
            "detail": "No policy decision artifact found for the mandate",
        })

    # 5. Razorpay order amount == authorized amount
    checks.append({
        "name": "order_amount_binding",
        "ok": cs.razorpay_order_amount is None or cs.razorpay_order_amount == mandate.total_paise,
        "detail": (
            f"Order {cs.razorpay_order_id} amount {cs.razorpay_order_amount}paise "
            f"== authorized {mandate.total_paise}paise"
            if cs.razorpay_order_amount is not None else
            "No persisted order amount for this transaction (sim mode)"
        ),
    })

    # 6. Signed Decision Receipt links the chain
    receipt = (
        db.query(DecisionReceipt)
        .filter(DecisionReceipt.execution_mandate_id == mandate.mandate_id)
        .order_by(DecisionReceipt.created_at.desc())
        .first()
    )
    if receipt:
        rcpt_payload = authorization_service.decision_receipt_payload(receipt)
        checks.append({
            "name": "decision_receipt",
            "ok": crypto_service.verify(rcpt_payload, receipt.signature)
            and receipt.authorized_amount_paise == mandate.total_paise
            and receipt.checkout_session_id == checkout_session_id,
            "detail": f"Signed Decision Receipt {receipt.receipt_id} verified",
        })
    else:
        checks.append({
            "name": "decision_receipt",
            "ok": cs.status != "completed",  # only required for completed transactions
            "detail": "No Decision Receipt yet (transaction not settled/verified)",
        })

    all_ok = all(c["ok"] for c in checks) and cs.status in ("completed", "declined")
    user_assertion_artifact = None
    if mandate.user_assertion_id and db.get(UserIntentAssertion, mandate.user_assertion_id) is not None:
        ua = db.get(UserIntentAssertion, mandate.user_assertion_id)
        key = db.query(UserKey).filter(UserKey.key_id == ua.user_key_id).first()
        user_assertion_artifact = {
            "assertion_id": ua.assertion_id,
            "version": ua.version,
            "type": ua.type,
            "user_id": ua.user_id,
            "user_key_id": ua.user_key_id,
            "merchant_id": ua.merchant_id,
            "delegate_id": ua.delegate_id,
            "intent": json.loads(ua.intent),
            "constraint_ceiling": json.loads(ua.constraint_ceiling),
            "issued_at": ua.issued_at.isoformat(timespec="seconds"),
            "expires_at": ua.expires_at.isoformat(timespec="seconds"),
            "nonce": ua.nonce,
            "canonical_payload_hash": ua.canonical_payload_hash,
            "signature": ua.signature,
            "key_id": ua.user_key_id,
            "user_public_key_pem": key.public_key_pem if key else None,
        }
    merchant_key_id = crypto_service.key_id()
    em_full = _full_artifact(
        em_payload, mandate.signature, mandate.canonical_payload_hash, merchant_key_id
    )
    # Signed payloads store timestamps under their payload keys (iss_ts/exp/ts);
    # the verifier's required-field contract uses the canonical field names:
    # re-add them exactly as the offline export does (bulk field identity).
    em_full["issued_at"] = _iso(mandate.issued_at)
    em_full["expires_at"] = _iso(mandate.expires_at)
    pol_full = None
    if decision:
        pol_full = _full_artifact(
            dec_payload, decision.signature, decision.canonical_payload_hash, merchant_key_id
        )
        pol_full["timestamp"] = _iso(decision.timestamp)
    rcpt_full = None
    if receipt:
        rcpt_full = _full_artifact(
            rcpt_payload, receipt.signature, receipt.canonical_payload_hash, merchant_key_id
        )
        rcpt_full["created_at"] = _iso(receipt.created_at)
    return {
        "checkout_session_id": checkout_session_id,
        "provenance_available": True,
        "status": cs.status,
        "checks": checks,
        "summary": (
            "All provenance checks passed."
            if all_ok else
            "One or more provenance checks failed; chain is not fully verified."
        ),
        "artifacts": {
            "execution_mandate": em_full,
            "execution_mandate_signature": mandate.signature,
            "policy_decision": pol_full,
            "decision_receipt": rcpt_full,
            "user_assertion": user_assertion_artifact,
            "merchant_key_id": merchant_key_id,
        },
    }
