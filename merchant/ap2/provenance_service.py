"""AP2 provenance verification (PRD §30, §37, and GET /provenance/{id})."""
import json

from sqlalchemy.orm import Session

from merchant.persistence.models.entities import (
    CheckoutSession,
    DecisionReceipt,
    ExecutionMandate,
    PolicyDecision,
)
from merchant.ap2 import authorization_service, crypto_service


def _provenance_available(checkout_session: CheckoutSession, mandate: ExecutionMandate | None) -> bool:
    return mandate is not None


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
            "execution_mandate": authorization_service.execution_mandate_payload(mandate),
            "execution_mandate_signature": mandate.signature,
            "policy_decision": authorization_service.policy_decision_payload(decision)
            if decision else None,
            "decision_receipt": authorization_service.decision_receipt_payload(receipt)
            if receipt else None,
            "merchant_key_id": crypto_service.key_id(),
        },
    }
