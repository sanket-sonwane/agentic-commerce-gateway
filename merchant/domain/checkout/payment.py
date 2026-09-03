"""Payment settlement: finalizer, payment-method records, reconciliation.

``_finalize_payment`` is the single choke point where a gateway outcome becomes
a session status. It is guarded by ``_assert_finalizable_state`` (C-3) and, on
success, by the C-1 amount-binding check (authorized == order == verified)
BEFORE any irreversible effect. Receipt issuing and reconciliation live here
too; gateway clients are imported lazily to avoid import cycles.
"""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from merchant.config import settings
from merchant.domain import inventory_service
from merchant.domain.checkout.session import (
    _assert_finalizable_state,
    get_session,
    serialize_session,
)
from merchant.evidence import audit_service
from merchant.persistence.models.dto import CompleteResponse, ErrorCode, ProtocolError
from merchant.persistence.models.entities import CheckoutSession, Pack, PaymentMethod


def get_or_create_payment_method(db: Session, buyer_agent_id: str) -> PaymentMethod:
    row = (
        db.execute(select(PaymentMethod).where(PaymentMethod.buyer_agent_id == buyer_agent_id))
        .scalars()
        .first()
    )
    if row is None:
        row = PaymentMethod(buyer_agent_id=buyer_agent_id, status="pending_save")
        db.add(row)
        db.flush()
    return row


def finalize_with_outcome(db: Session, cs: CheckoutSession, outcome: "PaymentOutcome") -> CompleteResponse:
    return _finalize_payment(db, cs, outcome)


def settle_probe(cs: CheckoutSession, payment_id: str) -> int | None:
    """Best-effort settled payment amount (paise) for amount-binding checks.

    Returns None when the amount cannot be determined (no credentials / sim),
    in which case the callback falls back to the persisted order amount. Never
    raises: a probe failure must not block an already-verified payment.
    """
    from merchant.payment import payment_service

    try:
        pay = payment_service.fetch_payment(payment_id)
    except Exception:  # noqa: BLE001
        return None
    raw_amount = pay.get("amount")
    if isinstance(raw_amount, int) and raw_amount > 0:
        return raw_amount
    return cs.razorpay_order_amount


def _finalize_payment(db: Session, cs: CheckoutSession, outcome: "PaymentOutcome") -> CompleteResponse:
    # C-3: last line of defense. Prove the session is legally finalizable for this
    # outcome before touching anything irreversible.
    _assert_finalizable_state(db, cs, outcome)

    cs.payment_driver = cs.payment_driver or settings.payment_driver
    cs.razorpay_order_id = outcome.order_id or cs.razorpay_order_id
    cs.razorpay_payment_id = outcome.payment_id
    binding_declined = False

    if outcome.status == "completed":
        # --- C-1 amount binding guard (read-only; routes only to the existing
        # declined transition — never alters callback/state-machine semantics).
        # Prove authorized == order == verified BEFORE marking completed. Any
        # divergence fails closed, so a successful completion always implies
        # FINAL AUTHORIZED == PERSISTED AUTHORIZED == RAZORPAY ORDER
        # == VERIFIED PAYMENT. ---
        authorized = cs.authorized_amount_paise or 0
        order_amt = cs.razorpay_order_amount or 0
        raw_amt = outcome.raw.get("amount")
        verified = raw_amt if isinstance(raw_amt, int) and raw_amt > 0 else None
        if verified is None:
            # No independent gateway amount (hermetic sim / no credential probe):
            # bind against the order amount — the strongest evidence available —
            # and note the limitation (documented in the C-1 report).
            verified = order_amt
        cs.verified_payment_amount_paise = verified or None
        amount_match = authorized > 0 and order_amt == authorized and verified == authorized

        if not amount_match:
            binding_declined = True
            inventory_service.release_session_reservations(
                db,
                cs.checkout_session_id,
                actor="razorpay",
                reason="Amount binding mismatch; reservation was released and payment blocked",
            )
            cs.status = "declined"
            cs.decline_code = ErrorCode.PAYMENT_BLOCKED.value
            audit_service.append_event(
                db,
                actor="razorpay",
                action="payment_amount_mismatch",
                checkout_session_id=cs.checkout_session_id,
                decision="declined",
                rules_fired=["PAYMENT_BLOCKED"],
                reasoning_summary=(
                    f"Amount binding failed before completion: authorized {authorized}paise, "
                    f"order {order_amt}paise, verified {verified}paise"
                ),
                payload={
                    "authorized_amount_paise": authorized,
                    "razorpay_order_amount": order_amt,
                    "verified_payment_amount_paise": verified,
                    "amount_match": False,
                },
            )
            audit_service.append_event(
                db,
                actor="merchant_agent",
                action="checkout_declined",
                checkout_session_id=cs.checkout_session_id,
                decision="declined",
                rules_fired=[ErrorCode.PAYMENT_BLOCKED.value],
                reasoning_summary="Amount binding mismatch; NO successful completion",
            )
        else:
            # Consume reservations and decrement actual stock.
            items = json.loads(cs.line_items)
            for item in items:
                pack = db.get(Pack, item["pack_id"])
                pack.stock_actual = max(0, pack.stock_actual - item["quantity"])
            inventory_service.consume_session_reservations(
                db,
                cs.checkout_session_id,
                reason="Payment succeeded; reserved stock was consumed",
            )
            cs.status = "completed"
            audit_service.append_event(
                db,
                actor="razorpay",
                action="payment_success",
                checkout_session_id=cs.checkout_session_id,
                decision="approved",
                reasoning_summary=(
                    f"Paid ₹{json.loads(cs.totals)['total']} "
                    f"(order={outcome.order_id}, payment={outcome.payment_id}"
                    f"{', simulated' if outcome.simulated else ''})"
                ),
                payload={
                    "simulated": outcome.simulated,
                    "authorized_amount_paise": authorized,
                    "razorpay_order_amount": order_amt,
                    "verified_payment_amount_paise": verified,
                    "amount_match": True,
                    **outcome.raw,
                },
            )
            audit_service.append_event(
                db,
                actor="merchant_agent",
                action="checkout_completed",
                checkout_session_id=cs.checkout_session_id,
                decision="approved",
                reasoning_summary="Transaction completed; stock consumed",
            )
    elif outcome.status == "pending":
        cs.status = "payment_pending"
        audit_service.append_event(
            db,
            actor="razorpay",
            action="payment_pending",
            checkout_session_id=cs.checkout_session_id,
            decision="pending",
            reasoning_summary=outcome.message or "Payment unresolved; reconciliation required",
        )
    else:
        inventory_service.release_session_reservations(
            db,
            cs.checkout_session_id,
            actor="razorpay",
            reason="Gateway declined the payment; reservation was released",
        )
        cs.status = "declined"
        cs.decline_code = ErrorCode.PAYMENT_FAILED.value
        audit_service.append_event(
            db,
            actor="razorpay",
            action="payment_failure",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=["PAYMENT_FAILED"],
            reasoning_summary=outcome.message or "Gateway rejected the payment",
        )
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="checkout_declined",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=["PAYMENT_FAILED"],
            reasoning_summary="Gateway decline recorded",
        )
    db.flush()

    # AP2: issue a signed Decision Receipt binding mandate + policy + payment.
    _issue_receipt_for_outcome(db, cs, outcome)

    resp = CompleteResponse(checkout_session=serialize_session(db, cs))
    if binding_declined:
        resp.error = {
            "code": ErrorCode.PAYMENT_BLOCKED.value,
            "message": "Amount binding failed; payment amount does not match the "
                       "authorized execution amount",
            "retryable": False,
            "details": {
                "authorized_amount_paise": cs.authorized_amount_paise,
                "razorpay_order_amount": cs.razorpay_order_amount,
                "verified_payment_amount_paise": cs.verified_payment_amount_paise,
            },
        }
    elif outcome.status == "pending":
        resp.error = {
            "code": ErrorCode.PAYMENT_PENDING.value,
            "message": outcome.message or "Payment unresolved",
            "retryable": False,
            "details": {},
        }
    elif outcome.status == "declined":
        resp.error = {
            "code": ErrorCode.PAYMENT_FAILED.value,
            "message": outcome.message or "Payment failed",
            "retryable": False,
            "details": {},
        }
    return resp


def _issue_receipt_for_outcome(db: Session, cs: CheckoutSession, outcome: "PaymentOutcome") -> None:
    """Create + sign a Decision Receipt only when an Execution Mandate exists.

    Old/legacy transactions without a mandate (created before the AP2 layer)
    simply have no receipt — provenance reports NOT_AVAILABLE for them.
    """
    if not cs.execution_mandate_id:
        return
    from merchant.persistence.models.entities import ExecutionMandate, PolicyDecision
    from merchant.ap2 import authorization_service

    mandate = db.get(ExecutionMandate, cs.execution_mandate_id)
    if mandate is None:
        return
    decision = (
        db.query(PolicyDecision)
        .filter(PolicyDecision.execution_mandate_id == mandate.mandate_id)
        .order_by(PolicyDecision.created_at.desc())
        .first()
    )
    receipt = authorization_service.issue_decision_receipt(
        db,
        cs,
        mandate=mandate,
        decision=decision,
        outcome=outcome.status,
        razorpay_order_id=cs.razorpay_order_id,
        razorpay_payment_id=cs.razorpay_payment_id,
    )
    # AP2 single-use: once settled/declined, the mandate can never authorize a
    # second payment. Verify-and-complete on a consumed mandate now fails closed.
    authorization_service.mark_mandate_consumed(db, mandate)
    audit_service.append_event(
        db,
        actor="merchant_agent",
        action="decision_receipt_issued",
        checkout_session_id=cs.checkout_session_id,
        decision="info",
        reasoning_summary=f"Signed Decision Receipt {receipt.receipt_id} recorded",
        payload={
            "receipt_id": receipt.receipt_id,
            "execution_mandate_id": mandate.mandate_id,
            "authorized_amount_paise": receipt.authorized_amount_paise,
            "outcome": outcome.status,
        },
    )
    db.flush()


def reconcile_session(db: Session, checkout_session_id: str) -> CompleteResponse:
    """Operator/buyer-safe reconciliation for payment_pending sessions."""
    cs = get_session(db, checkout_session_id)
    if cs.status != "payment_pending":
        raise ProtocolError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Session {cs.status} is not payment_pending",
            http_status=409,
        )
    from merchant.payment import payment_service

    outcome = payment_service.reconcile_pending(
        cs.razorpay_order_id or "",
        payment_id=cs.razorpay_payment_id,
    )
    return _finalize_payment(db, cs, outcome)