"""Checkout confirmation after a price/terms change (PRD §18)."""
import json

from sqlalchemy.orm import Session

from merchant.domain import inventory_service
from merchant.domain.checkout.pricing import _compute_totals, _resolve_items
from merchant.domain.checkout.session import _check_ownership, _session_ref, get_session
from merchant.evidence import audit_service
from merchant.persistence.models.dto import ErrorCode, ProtocolError


def confirm_checkout(
    db: Session,
    checkout_session_id: str,
    *,
    buyer_agent_id: str,
    delegation_token: str,
    accept_new_terms: bool,
    version: int,
) -> "CheckoutSession | ProtocolError":
    """Apply buyer confirmation. Returns the updated session or a recorded
    ProtocolError that the caller must raise AFTER commit."""
    cs = get_session(db, checkout_session_id)
    _check_ownership(cs, buyer_agent_id, delegation_token)

    audit_service.append_event(
        db,
        actor="buyer_agent",
        action="confirmation_requested",
        checkout_session_id=cs.checkout_session_id,
        payload={"accept_new_terms": accept_new_terms, "version": version, "session_version": cs.version},
    )

    if version != cs.version:
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="confirmation_rejected",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=["confirmation_stale"],
            reasoning_summary=f"Confirmation referenced version {version}; current is {cs.version}",
        )
        db.flush()
        return ProtocolError(
            ErrorCode.CONFIRMATION_STALE,
            f"Confirmation references version {version} but session is version {cs.version}",
            http_status=409,
            details={"expected_version": cs.version, "provided_version": version},
            session_ref=_session_ref(cs),
        )

    if cs.status != "requires_confirmation":
        db.flush()
        return ProtocolError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Session {cs.status} does not require confirmation",
            http_status=409,
            details={"current_status": cs.status},
        )

    if not accept_new_terms:
        inventory_service.release_session_reservations(
            db,
            cs.checkout_session_id,
            actor="buyer_agent",
            reason="Buyer declined new terms; reserved stock was released",
        )
        cs.status = "cancelled"
        audit_service.append_event(
            db,
            actor="buyer_agent",
            action="confirmation_rejected",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            reasoning_summary="Buyer declined new terms; session cancelled",
        )
        db.flush()
        return cs

    # Acceptance persists the NEW terms: re-resolve authoritative prices/totals
    # so the next completion validates against what the buyer just approved.
    resolved = _resolve_items(db, [{"product_id": i["product_id"], "pack_id": i["pack_id"],
                                    "quantity": i["quantity"]} for i in json.loads(cs.line_items)])
    new_totals = _compute_totals(resolved)
    cs.line_items = json.dumps(resolved)
    cs.totals = json.dumps(new_totals)
    cs.version += 1
    cs.status = "requires_payment"
    audit_service.append_event(
        db,
        actor="merchant_agent",
        action="confirmation_accepted",
        checkout_session_id=cs.checkout_session_id,
        decision="approved",
        reasoning_summary=(
            f"Buyer accepted new terms; session updated to ₹{new_totals['total']} "
            f"(v{cs.version}) and returned to requires_payment"
        ),
    )
    db.flush()
    return cs