"""Checkout session lifecycle state: constants, guards, serialization.

Holds the only valid state transitions (VALID_TRANSITIONS / TERMINAL_STATES)
and the guards that enforce them at every mutation boundary. serialize_session
returns the wire shape the API/agent layer exposes (no secrets — coarse state
and payment truth only).
"""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from merchant.config import settings
from merchant.persistence.models.dto import (
    CheckoutSessionOut,
    ErrorCode,
    ProtocolError,
    ReservationInfo,
)
from merchant.persistence.models.entities import CheckoutSession, PaymentMethod, Reservation

VALID_TRANSITIONS: dict[str, set[str]] = {
    "created": {"requires_payment", "declined", "cancelled"},
    "requires_payment": {"requires_confirmation", "payment_pending", "completed", "declined", "cancelled"},
    "requires_confirmation": {"requires_payment", "declined", "cancelled"},
    "payment_pending": {"completed", "declined"},
    "completed": set(),
    "declined": set(),
    "cancelled": set(),
}

TERMINAL_STATES = {"completed", "declined", "cancelled"}

# Per payment outcome.status the finalizer's intended (non-terminal) target state.
_FINALIZE_TARGET: dict[str, str] = {
    "completed": "completed",
    "pending": "payment_pending",
    "declined": "declined",
}


def _session_ref(cs: CheckoutSession) -> dict:
    return {"checkout_session_id": cs.checkout_session_id, "status": cs.status}


def serialize_session(db: Session, cs: CheckoutSession) -> CheckoutSessionOut:
    active_res = (
        db.execute(
            select(Reservation).where(
                Reservation.checkout_session_id == cs.checkout_session_id,
                Reservation.status == "active",
            )
        )
        .scalars()
        .first()
    )

    reservation = (
        ReservationInfo(status="active", expires_at=active_res.expires_at)
        if active_res
        else ReservationInfo(status="none")
    )

    payment: dict = {}
    if cs.razorpay_order_id:
        payment = {
            "driver": cs.payment_driver,
            "order_id": cs.razorpay_order_id,
            "payment_id": cs.razorpay_payment_id,
            "amount": json.loads(cs.totals).get("total"),
            "currency": "INR",
        }
        if cs.authorized_amount_paise is not None:
            payment["authorized_amount_paise"] = cs.authorized_amount_paise
            payment["execution_mandate_id"] = cs.execution_mandate_id
        if cs.razorpay_order_amount is not None:
            payment["razorpay_order_amount_paise"] = cs.razorpay_order_amount
        if cs.verified_payment_amount_paise is not None:
            payment["verified_payment_amount_paise"] = cs.verified_payment_amount_paise
        if cs.razorpay_order_amount is not None and cs.verified_payment_amount_paise is not None:
            payment["amount_match"] = (
                cs.authorized_amount_paise is not None
                and cs.razorpay_order_amount == cs.authorized_amount_paise
                and cs.verified_payment_amount_paise == cs.authorized_amount_paise
            )
        # A live order on a non-terminal session means we're waiting for the card UI.
        if cs.status == "requires_payment" and settings.payment_driver in ("checkout", "saved"):
            payment["awaiting_authorization"] = True
            payment["key_id"] = settings.razorpay_key_id
            if settings.payment_driver == "saved":
                method = (
                    db.execute(
                        select(PaymentMethod).where(PaymentMethod.buyer_agent_id == cs.buyer_agent_id)
                    )
                    .scalars()
                    .first()
                )
                if method and method.razorpay_customer_id:
                    payment["customer_id"] = method.razorpay_customer_id
                    payment["save_card"] = True

    return CheckoutSessionOut(
        checkout_session_id=cs.checkout_session_id,
        version=cs.version,
        status=cs.status,
        buyer_agent_id=cs.buyer_agent_id,
        delegation_id=cs.delegation_id,
        merchant_id=settings.merchant_id,
        line_items=json.loads(cs.line_items),
        totals=json.loads(cs.totals),
        gating_result=json.loads(cs.gating_result),
        reservation=reservation,
        payment=payment,
        razorpay_order_id=cs.razorpay_order_id,
        fulfillment_method=cs.fulfillment_method or "",
        destination=json.loads(cs.destination) if cs.destination else {},
        created_at=cs.created_at,
        updated_at=cs.updated_at,
    )


def get_session(db: Session, checkout_session_id: str) -> CheckoutSession:
    cs = db.get(CheckoutSession, checkout_session_id)
    if cs is None:
        raise ProtocolError(
            ErrorCode.CHECKOUT_NOT_FOUND,
            f"Checkout session {checkout_session_id} not found",
            http_status=404,
        )
    return cs


def get_session_status(db: Session, checkout_session_id: str) -> str | None:
    """Status-only read (None when the session does not exist) — used by the
    Merchant Agent's payment-truth / live-basket checks."""
    cs = db.get(CheckoutSession, checkout_session_id)
    return cs.status if cs is not None else None


def _check_ownership(cs: CheckoutSession, buyer_agent_id: str, delegation_token: str) -> None:
    if cs.buyer_agent_id != buyer_agent_id or cs.delegation_id != delegation_token:
        raise ProtocolError(
            ErrorCode.UNAUTHORIZED_AGENT,
            "Session belongs to a different buyer agent/delegation",
            http_status=403,
        )


def _assert_transition(current: str, target: str) -> None:
    if target not in VALID_TRANSITIONS.get(current, set()):
        raise ProtocolError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Illegal transition {current} -> {target}",
            http_status=409,
            details={"current_status": current},
        )


def _assert_finalizable_state(db: Session, cs: CheckoutSession, outcome: "PaymentOutcome") -> None:
    """C-3 defense-in-depth: the finalizer independently proves the session is in
    a legally finalizable state for the supplied payment outcome, BEFORE any
    irreversible effect (stock/reservation consumption, status mutation, receipts,
    success audits). Never mutates; raises INVALID_STATE_TRANSITION otherwise.
    """
    origin = cs.status
    target = _FINALIZE_TARGET.get(outcome.status)

    # Terminal states are immutable: a late callback or direct call must never
    # re-complete, re-decline, or re-cancel (no duplicate charge/stock/receipt).
    if origin in TERMINAL_STATES:
        raise ProtocolError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Cannot finalize a {origin} session to {target or outcome.status}",
            http_status=409,
            details={"current_status": origin, "target": target or outcome.status},
        )

    if target is None:
        raise ProtocolError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Unsupported payment outcome status '{outcome.status}'",
            http_status=409,
            details={"current_status": origin, "outcome_status": outcome.status},
        )

    # Legitimate reconciliation no-op: a still-unresolved payment_pending session
    # being reconciled (reconcile_pending returned pending) stays payment_pending.
    if origin == "payment_pending" and target == "payment_pending":
        return

    _assert_transition(origin, target)