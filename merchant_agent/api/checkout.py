"""Checkout session endpoints (PRD §15–21) + payment callback/reconciliation.

Error-raising discipline: services may report failures, but ProtocolError is
raised OUTSIDE db.mutate() so committed decline states + audit events persist.
"""
from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.orm import Session

from merchant_agent.db import mutate, read_only
from merchant_agent.models.dto import (
    CancelCheckoutRequest,
    CompleteCheckoutRequest,
    CompleteResponse,
    ConfirmRequest,
    CreateCheckoutRequest,
    CreateCheckoutResponse,
    CheckoutSessionOut,
    ErrorCode,
    ProtocolError,
    UpdateCheckoutRequest,
)
from merchant_agent.services import checkout_service, payment_service

router = APIRouter()


@router.post("/checkout_sessions", status_code=201)
def create_checkout(body: CreateCheckoutRequest, response: Response,
                    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    error: ProtocolError | None = None
    with mutate() as db:
        cs, created = checkout_service.create_checkout(
            db,
            buyer_agent_id=body.buyer_agent_id,
            delegation_token=body.delegation_token,
            line_items=[i.model_dump() for i in body.line_items],
            idempotency_key=idempotency_key,
        )
        out = CreateCheckoutResponse(checkout_session=checkout_service.serialize_session(db, cs))
        if not created:
            response.status_code = 200  # idempotent replay
        if cs.status == "declined":
            error = _declined_at_create(out.checkout_session)
    if error is not None:
        raise error
    return out


def _declined_at_create(session_out: CheckoutSessionOut) -> ProtocolError:
    code = ErrorCode.INVALID_REQUEST
    for check in session_out.gating_result.get("checks", []):
        if check.get("result") == "fail":
            code = {
                "spend_limit": ErrorCode.SPEND_LIMIT_EXCEEDED,
                "stock_check": ErrorCode.STOCK_UNAVAILABLE,
                "category_allowed": ErrorCode.CATEGORY_BLOCKED,
                "delegation_expiry": ErrorCode.DELEGATION_EXPIRED,
                "quantity_limit": ErrorCode.DELEGATION_SCOPE_VIOLATION,
            }.get(check["rule"], ErrorCode.INVALID_REQUEST)
            break
    failed = [c["detail"] for c in session_out.gating_result.get("checks", []) if c.get("result") == "fail"]
    return ProtocolError(
        code,
        "; ".join(failed) or "Policy rejected basket",
        http_status=409,
        retryable=code == ErrorCode.STOCK_UNAVAILABLE,
        details={"gating": session_out.gating_result},
        session_ref={
            "checkout_session_id": session_out.checkout_session_id,
            "status": session_out.status,
        },
    )


@router.put("/checkout_sessions/{checkout_session_id}")
def update_checkout(checkout_session_id: str, body: UpdateCheckoutRequest):
    error: ProtocolError | None = None
    with mutate() as db:
        result = checkout_service.update_checkout(
            db,
            checkout_session_id,
            buyer_agent_id=body.buyer_agent_id,
            delegation_token=body.delegation_token,
            line_items=[i.model_dump() for i in body.line_items],
        )
        if isinstance(result, ProtocolError):
            error = result  # policy rejection; session state already persisted
        else:
            out = CreateCheckoutResponse(checkout_session=checkout_service.serialize_session(db, result))
    if error is not None:
        raise error
    return out


@router.get("/checkout_sessions/{checkout_session_id}")
def get_checkout(checkout_session_id: str, db: Session = Depends(read_only)):
    cs = checkout_service.get_session(db, checkout_session_id)
    return CreateCheckoutResponse(checkout_session=checkout_service.serialize_session(db, cs))


@router.post("/checkout_sessions/{checkout_session_id}/complete")
def complete_checkout(checkout_session_id: str, body: CompleteCheckoutRequest,
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    error: ProtocolError | None = None
    with mutate() as db:
        result, replayed = checkout_service.complete_checkout(
            db,
            checkout_session_id,
            buyer_agent_id=body.buyer_agent_id,
            delegation_token=body.delegation_token,
            idempotency_key=idempotency_key,
        )
        if idempotency_key and not replayed:
            checkout_service.store_complete_idempotency(db, idempotency_key, checkout_session_id, result)
        session_out = result.checkout_session
        status = session_out.status

        if status == "declined":
            err = result.error or {"code": "INVALID_REQUEST", "message": "Declined",
                                   "retryable": False, "details": {}}
            error = ProtocolError(
                err["code"], err["message"], http_status=409,
                retryable=err.get("retryable", False), details=err.get("details", {}),
                session_ref={"checkout_session_id": session_out.checkout_session_id, "status": status},
            )
        elif status == "requires_confirmation":
            change = result.change.model_dump() if result.change else {}
            error = ProtocolError(
                "PRICE_CHANGED",
                "Transaction terms changed; buyer confirmation required",
                http_status=409, retryable=True, details=change,
                session_ref={"checkout_session_id": session_out.checkout_session_id, "status": status},
            )
    if error is not None:
        raise error
    return result


@router.post("/checkout_sessions/{checkout_session_id}/confirm")
def confirm_checkout(checkout_session_id: str, body: ConfirmRequest):
    error: ProtocolError | None = None
    with mutate() as db:
        result = checkout_service.confirm_checkout(
            db,
            checkout_session_id,
            buyer_agent_id=body.buyer_agent_id,
            delegation_token=body.delegation_token,
            accept_new_terms=body.accept_new_terms,
            version=body.version,
        )
        if isinstance(result, ProtocolError):
            error = result
        else:
            out = CreateCheckoutResponse(checkout_session=checkout_service.serialize_session(db, result))
    if error is not None:
        raise error
    return out


@router.post("/checkout_sessions/{checkout_session_id}/cancel")
def cancel_checkout(checkout_session_id: str, body: CancelCheckoutRequest):
    error: ProtocolError | None = None
    with mutate() as db:
        result = checkout_service.cancel_checkout(
            db,
            checkout_session_id,
            buyer_agent_id=body.buyer_agent_id,
            delegation_token=body.delegation_token,
        )
        if isinstance(result, ProtocolError):
            error = result
        else:
            out = CreateCheckoutResponse(checkout_session=checkout_service.serialize_session(db, result))
    if error is not None:
        raise error
    return out


@router.post("/checkout_sessions/{checkout_session_id}/payment_callback")
def payment_callback(checkout_session_id: str, body: dict):
    """Razorpay Checkout.js success handler -> HMAC-verified completion."""
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
        elif not payment_service.verify_checkout_signature(order_id, payment_id, signature):
            error = ProtocolError(
                "PAYMENT_FAILED", "Signature verification failed",
                http_status=400, retryable=False,
            )
        else:
            from merchant_agent.services import audit_service
            from merchant_agent.services.payment_service import PaymentOutcome

            outcome = PaymentOutcome(
                status="completed", order_id=order_id, payment_id=payment_id,
                message="Authorized via Razorpay Checkout; signature verified",
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
                            # Diagnostics: which checkout stage ran + mandate flag
                            # as echoed by Razorpay on the payment entity.
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
