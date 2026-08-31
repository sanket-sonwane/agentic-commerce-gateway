"""Checkout cancellation (PRD §17)."""
from sqlalchemy.orm import Session

from merchant.domain import inventory_service
from merchant.domain.checkout.session import TERMINAL_STATES, _check_ownership, get_session
from merchant.evidence import audit_service
from merchant.persistence.models.dto import ErrorCode, ProtocolError


def cancel_checkout(
    db: Session,
    checkout_session_id: str,
    *,
    buyer_agent_id: str,
    delegation_token: str,
) -> "CheckoutSession | ProtocolError":
    cs = get_session(db, checkout_session_id)
    _check_ownership(cs, buyer_agent_id, delegation_token)

    if cs.status == "cancelled":
        return cs  # idempotent
    if cs.status in TERMINAL_STATES:
        db.flush()
        return ProtocolError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Cannot cancel a {cs.status} session",
            http_status=409,
            details={"current_status": cs.status},
        )

    inventory_service.release_session_reservations(
        db,
        cs.checkout_session_id,
        actor="buyer_agent",
        reason="Checkout was cancelled and reserved stock was released",
    )
    cs.status = "cancelled"
    audit_service.append_event(
        db,
        actor="buyer_agent",
        action="checkout_cancelled",
        checkout_session_id=cs.checkout_session_id,
        decision="info",
        reasoning_summary="Buyer cancelled; reservations released; no payment attempted",
    )
    db.flush()
    return cs