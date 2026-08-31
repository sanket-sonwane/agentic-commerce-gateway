"""Razorpay payment callback + reconciliation endpoints.

Owns the HTTP-facing side of payment settlement: the Checkout.js success
callback that HMAC-verifies a Razorpay signature, enforces AP2 amount binding
against the Execution Mandate, and finalizes the session; plus the manual
reconcile endpoint that asks Razorpay for the truth on a payment_pending
session.

All business rules live in ``merchant.domain.checkout`` and
``merchant.payment.payment_service``; this module only marshals HTTP → domain.
"""
from fastapi import APIRouter
from sqlalchemy.orm import Session

from merchant.persistence.db import mutate
from merchant.persistence.models.dto import ProtocolError
from merchant.domain import checkout as checkout_service
from merchant.payment import payment_service

router = APIRouter()


@router.post("/checkout_sessions/{checkout_session_id}/payment_callback")
def payment_callback(checkout_session_id: str, body: dict):
    """Razorpay Checkout.js success handler -> HMAC-verified completion.

    AP2 hardening: besides the existing HMAC verification, the callback also
    enforces (a) state — a session must be awaiting authorization, and (b) amount
    binding — the truly-settled payment amount must equal the amount authorized
    by the Execution Mandate. Any failure blocks completion (PAYMENT_BLOCKED).
    """
    order_id = str(body.get("razorpay_order_id", ""))
    payment_id = str(body.get("razorpay_payment_id", ""))
    signature = str(body.get("razorpay_signature", ""))
    error: ProtocolError | None = None
    with mutate() as db:
        cs = checkout_service.get_session(db, checkout_session_id)
        if cs.razorpay_order_id != order_id:
            error = ProtocolError(
                "PAYMENT_FAILED", "Order mismatch on callback",
                http_status=400, details={"expected": cs.razorpay_order_id},
            )
        elif cs.status != "requires_payment":
            error = ProtocolError(
                "INVALID_STATE_TRANSITION",
                f"Session is {cs.status}; not awaiting authorization to be finalized",
                http_status=409,
                details={"current_status": cs.status},
            )
        elif not payment_service.verify_checkout_signature(order_id, payment_id, signature):
            error = ProtocolError(
                "PAYMENT_FAILED", "Signature verification failed",
                http_status=400, retryable=False,
            )
        else:
            from merchant.evidence import audit_service
            from merchant.payment.payment_service import PaymentOutcome

            # AP2 amount binding: the settled payment must equal the amount the
            # Execution Mandate authorized, and that must equal the Razorpay order.
            # fetch_payment returns {} with no credentials (sim/test mode), in
            # which case we fall back to the persisted order amount.
            settled_amount = checkout_service.settle_probe(cs, payment_id)
            if settled_amount is not None and cs.authorized_amount_paise is not None \
                    and settled_amount != cs.authorized_amount_paise:
                audit_service.append_event(
                    db,
                    actor="razorpay",
                    action="payment_amount_mismatch",
                    checkout_session_id=cs.checkout_session_id,
                    decision="declined",
                    rules_fired=["PAYMENT_BLOCKED"],
                    reasoning_summary=(
                        f"Amount binding failed: authorized {cs.authorized_amount_paise}paise "
                        f"vs settled {settled_amount}paise"
                    ),
                )
                error = ProtocolError(
                    "PAYMENT_BLOCKED",
                    "Settled amount does not match the authorized execution amount; "
                    "completion blocked",
                    http_status=409, retryable=False,
                    details={
                        "authorized_amount_paise": cs.authorized_amount_paise,
                        "settled_amount_paise": settled_amount,
                    },
                )
            else:
                outcome = PaymentOutcome(
                    status="completed", order_id=order_id, payment_id=payment_id,
                    message="Authorized via Razorpay Checkout; signature verified",
                    # Carry the settled amount (from settle_probe) into
                    # _finalize_payment so it persists as verified_payment_amount_paise.
                    raw={"amount": settled_amount} if isinstance(settled_amount, int) and settled_amount > 0 else {},
                )
                out = checkout_service.finalize_with_outcome(db, cs, outcome)

                # Best-effort: capture the saved-card token so future orders are zero-UI.
                try:
                    pay_obj = payment_service.fetch_payment(payment_id)
                    token_id = pay_obj.get("token_id")
                    card = pay_obj.get("card") or {}
                    if token_id:
                        method = checkout_service.get_or_create_payment_method(db, cs.buyer_agent_id)
                        reauth_repaired = method.status == "needs_reauth"
                        method.razorpay_token_id = token_id
                        method.card_last4 = card.get("last4")
                        method.card_network = card.get("network")
                        method.status = "active"
                        audit_service.append_event(
                            db,
                            actor="merchant_agent",
                            action="payment_token_saved",
                            checkout_session_id=cs.checkout_session_id,
                            decision="info",
                            reasoning_summary=(
                                f"Saved {card.get('network') or 'card'} ••••{card.get('last4')} "
                                + ("with recurring mandate for future one-tap payments"
                                 if reauth_repaired else "for future one-tap payments")
                            ),
                            payload={
                                **({"replaced_unchargeable_token": True} if reauth_repaired else {}),
                                "payment_notes_mode": (pay_obj.get("notes") or {}).get("mode"),
                                "payment_recurring_flag": pay_obj.get("recurring"),
                            },
                        )
                    if card:
                        out.checkout_session.payment["saved_card"] = {
                            "last4": card.get("last4"),
                            "network": card.get("network"),
                        }
                except Exception:  # noqa: BLE001 — token persistence must never fail a paid order
                    pass
    if error is not None:
        raise error
    return out


@router.post("/checkout_sessions/{checkout_session_id}/reconcile")
def reconcile(checkout_session_id: str):
    """Resolve a payment_pending session by asking Razorpay for the truth."""
    with mutate() as db:
        return checkout_service.reconcile_session(db, checkout_session_id)
