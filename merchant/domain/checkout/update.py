"""Checkout basket updates (PRD §16)."""
import json

from sqlalchemy.orm import Session

from merchant.control.delegation_service import validate_delegation
from merchant.domain import inventory_service
from merchant.domain.checkout.gating import _decline_error, _gate
from merchant.domain.checkout.pricing import _compute_totals, _resolve_items
from merchant.domain.checkout.session import _check_ownership, _session_ref, get_session
from merchant.evidence import audit_service
from merchant.persistence.models.dto import ErrorCode, ProtocolError


def update_checkout(
    db: Session,
    checkout_session_id: str,
    *,
    buyer_agent_id: str,
    delegation_token: str,
    line_items: list[dict],
) -> "CheckoutSession | ProtocolError":
    """Apply a basket update. Returns the updated session, or a ProtocolError
    (already recorded in audit) that the caller must raise AFTER commit."""
    cs = get_session(db, checkout_session_id)
    _check_ownership(cs, buyer_agent_id, delegation_token)
    if cs.status != "requires_payment":
        raise ProtocolError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Only live sessions can be edited; session is {cs.status}",
            http_status=409,
            details={"current_status": cs.status},
        )

    delegation = validate_delegation(db, buyer_agent_id, delegation_token)

    merged: dict[tuple[str, str], int] = {}
    for item in line_items:
        key = (item["product_id"], item["pack_id"])
        merged[key] = merged.get(key, 0) + int(item["quantity"])
    raw_items = [{"product_id": k[0], "pack_id": k[1], "quantity": v} for k, v in merged.items()]

    resolved = _resolve_items(db, raw_items)
    totals = _compute_totals(resolved)
    gating = _gate(db, delegation, resolved, totals)

    audit_service.append_event(
        db,
        actor="buyer_agent",
        action="checkout_update_requested",
        checkout_session_id=cs.checkout_session_id,
        payload={"new_line_items": resolved, "new_totals": totals},
    )

    if not gating.passed:
        # Session stays intact with its original items/reservation; buyer adapts again.
        err = _decline_error(gating)
        err.session_ref = _session_ref(cs)
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="gating_evaluated",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=[f"{c.rule}:{c.result}" for c in gating.checks],
            reasoning_summary="Update rejected by policy; previous basket preserved",
            payload={"checks": [c.model_dump() for c in gating.checks]},
        )
        db.flush()
        return err

    inventory_service.release_session_reservations(
        db,
        cs.checkout_session_id,
        reason="Basket update; previous reservation was released before re-reserving",
    )
    expires_at = inventory_service.create_reservations(db, cs.checkout_session_id, resolved)
    cs.line_items = json.dumps(resolved)
    cs.totals = json.dumps(totals)
    cs.gating_result = gating.model_dump_json()
    cs.version += 1
    cs.status = "requires_payment"

    audit_service.append_event(
        db,
        actor="merchant_agent",
        action="gating_evaluated",
        checkout_session_id=cs.checkout_session_id,
        decision="approved",
        rules_fired=[f"{c.rule}:{c.result}" for c in gating.checks],
        reasoning_summary="Updated basket passed all policy checks",
        payload={"checks": [c.model_dump() for c in gating.checks]},
    )
    audit_service.append_event(
        db,
        actor="merchant_agent",
        action="checkout_updated",
        checkout_session_id=cs.checkout_session_id,
        decision="approved",
        reasoning_summary=f"Basket updated to version {cs.version}, total ₹{totals['total']}",
        payload={"expires_at": expires_at},
    )
    db.flush()
    return cs