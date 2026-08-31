"""Checkout session endpoints (PRD §15–21).

Error-raising discipline: services may report failures, but ProtocolError is
raised OUTSIDE db.mutate() so committed decline states + audit events persist.

Razorpay payment callback + reconciliation live in ``merchant.payment.callback``.
"""
from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.orm import Session

from merchant.persistence.db import mutate, read_only
from merchant.persistence.models.dto import (
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
from merchant.domain import checkout as checkout_service

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
            intent_mandate_id=body.intent_mandate_id,
            user_assertion=body.user_assertion,
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
