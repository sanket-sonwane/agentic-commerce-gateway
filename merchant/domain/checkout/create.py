"""Checkout session creation (PRD §15)."""
import json
import uuid

from sqlalchemy.orm import Session

from merchant.control import gating_service
from merchant.control.delegation_service import validate_delegation
from merchant.domain import inventory_service
from merchant.domain.checkout.gating import _decline_error, _gate
from merchant.domain.checkout.idempotency import _idempotent_replay, _store_idempotent
from merchant.domain.checkout.pricing import _compute_totals, _resolve_items
from merchant.domain.checkout.session import _session_ref
from merchant.evidence import audit_service
from merchant.persistence.models.entities import CheckoutSession


def _validate_fulfillment(method: str | None) -> str:
    """Validate the buyer-declared fulfillment method ("" if unspecified)."""
    if not method:
        return ""
    if method not in ("delivery", "pickup"):
        from merchant.persistence.models.dto import ErrorCode, ProtocolError

        raise ProtocolError(
            ErrorCode.INVALID_REQUEST,
            f"Unsupported fulfillment_method '{method}'; expected 'delivery' or 'pickup'",
            http_status=400,
            details={"fulfillment_method": method, "supported": ["delivery", "pickup"]},
        )
    return method


def create_checkout(
    db: Session,
    *,
    buyer_agent_id: str,
    delegation_token: str,
    line_items: list[dict],
    idempotency_key: str | None = None,
    fulfillment_method: str | None = None,
    destination: dict | None = None,
) -> tuple[CheckoutSession, bool]:
    """Returns (session, created_bool). Replays return the existing session."""
    replay = _idempotent_replay(db, idempotency_key)
    if replay:
        existing = db.get(CheckoutSession, replay["checkout_session_id"])
        if existing is not None:
            return existing, False

    delegation = validate_delegation(db, buyer_agent_id, delegation_token)
    method = _validate_fulfillment(fulfillment_method)

    # Merge duplicate lines targeting the same pack.
    merged: dict[tuple[str, str], int] = {}
    for item in line_items:
        key = (item["product_id"], item["pack_id"])
        merged[key] = merged.get(key, 0) + int(item["quantity"])
    raw_items = [
        {"product_id": k[0], "pack_id": k[1], "quantity": v} for k, v in merged.items()
    ]

    resolved = _resolve_items(db, raw_items)
    totals = _compute_totals(resolved)
    gating = _gate(db, delegation, resolved, totals)

    cs = CheckoutSession(
        checkout_session_id=f"cs_{uuid.uuid4().hex[:10]}",
        version=1,
        status="created",
        buyer_agent_id=buyer_agent_id,
        delegation_id=delegation_token,
        line_items=json.dumps(resolved),
        totals=json.dumps(totals),
        gating_result=gating.model_dump_json(),
        idempotency_key=idempotency_key,
        fulfillment_method=method,
        destination=json.dumps(destination or {}),
    )
    db.add(cs)
    db.flush()

    audit_service.append_event(
        db,
        actor="buyer_agent",
        action="checkout_create_requested",
        checkout_session_id=cs.checkout_session_id,
        payload={"line_items": resolved, "totals": totals},
    )

    if gating.passed:
        expires_at = inventory_service.create_reservations(db, cs.checkout_session_id, resolved)
        cs.status = "requires_payment"
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="gating_evaluated",
            checkout_session_id=cs.checkout_session_id,
            decision="approved",
            rules_fired=[f"{c.rule}:{c.result}" for c in gating.checks],
            reasoning_summary="All deterministic policy checks passed at creation",
            payload={"checks": [c.model_dump() for c in gating.checks]},
        )
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="reservation_created",
            checkout_session_id=cs.checkout_session_id,
            decision="info",
            reasoning_summary=f"Reserved {len(resolved)} line item(s)",
            payload={"expires_at": expires_at},
        )
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="checkout_created",
            checkout_session_id=cs.checkout_session_id,
            decision="approved",
            reasoning_summary=f"Session requires_payment, total ₹{totals['total']}",
        )
    else:
        cs.status = "declined"
        cs.decline_code = gating_service.first_failure_code(gating).value
        err = _decline_error(gating)
        err.session_ref = _session_ref(cs)
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="gating_evaluated",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=[f"{c.rule}:{c.result}" for c in gating.checks],
            reasoning_summary="Deterministic policy rejected basket at creation",
            payload={"checks": [c.model_dump() for c in gating.checks]},
        )
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="checkout_declined",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=[cs.decline_code],
            reasoning_summary=f"Create declined: {cs.decline_code}; Razorpay NOT called",
        )

    db.flush()
    _store_idempotent(db, idempotency_key, "create", cs,
                      {"checkout_session_id": cs.checkout_session_id})
    return cs, True