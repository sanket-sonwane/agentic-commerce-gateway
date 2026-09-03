"""Order domain: durable merchant order + minimal fulfillment state machine.

A Checkout Session is the payment/session object. The Order is the long-lived
commercial object created once a checkout payment verifies. This module owns:

- materializing an Order from a completed CheckoutSession (idempotent: at most
  one order per checkout; replaying a completed session never double-creates).
- the deterministic fulfillment state machine (pending -> placed -> packed ->
  shipped -> delivered) with validated transitions.
- operator-authorized fulfillment advancement (audited).
- serialization for the consumer order APIs.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from merchant.config import settings
from merchant.persistence.db import utcnow
from merchant.persistence.models.dto import ErrorCode, ProtocolError
from merchant.persistence.models.entities import CheckoutSession, Order

FULFILLMENT_STATES = ["pending", "placed", "packed", "shipped", "delivered"]
FULFILLMENT_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"placed"},
    "placed": {"packed"},
    "packed": {"shipped"},
    "shipped": {"delivered"},
    "delivered": set(),
}

ORDER_STATUS = ["placed"]
TERMINAL_FULFILLMENT = {"delivered"}


def _audit(db: Session, csid: str | None, actor: str, action: str, decision: str,
           summary: str, payload: dict | None = None) -> None:
    from merchant.evidence import audit_service

    audit_service.append_event(
        db,
        actor=actor,
        action=action,
        checkout_session_id=csid,
        decision=decision,
        reasoning_summary=summary,
        payload=payload or {},
    )


def _validate_fulfillment_transition(current: str, target: str) -> None:
    if target not in FULFILLMENT_TRANSITIONS.get(current, set()):
        raise ProtocolError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Illegal fulfillment transition {current} -> {target}",
            http_status=409,
            details={"current_fulfillment_status": current, "target_fulfillment_status": target},
        )


def create_order_from_checkout(db: Session, cs: CheckoutSession) -> Order:
    """Materialize the durable Order for a completed checkout (idempotent).

    MUST be called inside the same `mutate()` transaction that marked the
    session completed, so the order + success audit share the commit with the
    payment finalization. Returns the existing order if one already exists for
    this checkout (never double-creates on replay).
    """
    existing = (
        db.execute(select(Order).where(Order.checkout_session_id == cs.checkout_session_id))
        .scalars()
        .first()
    )
    if existing is not None:
        return existing

    order = Order(
        order_id=f"ord_{uuid.uuid4().hex[:10]}",
        checkout_session_id=cs.checkout_session_id,
        buyer_agent_id=cs.buyer_agent_id,
        delegation_id=cs.delegation_id,
        merchant_id=settings.merchant_id,
        line_items=cs.line_items,
        authorized_amount_paise=cs.authorized_amount_paise or int((json.loads(cs.totals) or {}).get("total", 0) * 100),
        currency="INR",
        payment_driver=cs.payment_driver,
        razorpay_order_id=cs.razorpay_order_id,
        razorpay_payment_id=cs.razorpay_payment_id,
        execution_mandate_id=cs.execution_mandate_id,
        order_status="placed",
        fulfillment_status="pending",
        fulfillment_method=cs.fulfillment_method or "",
        destination=cs.destination or "{}",
    )
    db.add(order)
    db.flush()

    _audit(
        db,
        cs.checkout_session_id,
        actor="merchant_agent",
        action="order_created",
        decision="approved",
        summary=f"Order {order.order_id} created for paid checkout {cs.checkout_session_id}",
        payload={
            "order_id": order.order_id,
            "checkout_session_id": cs.checkout_session_id,
            "authorized_amount_paise": order.authorized_amount_paise,
            "payment_id": order.razorpay_payment_id,
        },
    )
    return order


def get_order(db: Session, order_id: str) -> Order:
    order = db.get(Order, order_id)
    if order is None:
        raise ProtocolError(
            ErrorCode.CHECKOUT_NOT_FOUND,
            f"Order {order_id} not found",
            http_status=404,
            details={"order_id": order_id},
        )
    return order


def _check_order_ownership(order: Order, buyer_agent_id: str, delegation_token: str) -> None:
    if order.buyer_agent_id != buyer_agent_id or order.delegation_id != delegation_token:
        raise ProtocolError(
            ErrorCode.UNAUTHORIZED_AGENT,
            "Order belongs to a different buyer agent/delegation",
            http_status=403,
        )


def list_orders_for_buyer(db: Session, buyer_agent_id: str, delegation_token: str) -> list[Order]:
    """List orders scoped to the requesting buyer/delegation only.

    Ownership is enforced bound to the delegation token — a caller cannot read
    another buyer's orders by supplying an arbitrary buyer_agent_id.
    """
    rows = (
        db.execute(
            select(Order)
            .where(Order.buyer_agent_id == buyer_agent_id, Order.delegation_id == delegation_token)
            .order_by(Order.created_at.desc())
        )
        .scalars()
        .all()
    )
    return rows


def advance_fulfillment(
    db: Session,
    order_id: str,
    target: str,
    *,
    method: str = "",
    destination: str = "",
    estimated_delivery: str | None = None,
    tracking_reference: str | None = None,
) -> Order:
    """Operator-authorized fulfillment advancement (validated + audited)."""
    order = get_order(db, order_id)
    current = order.fulfillment_status
    _validate_fulfillment_transition(current, target)

    order.fulfillment_status = target
    if method:
        order.fulfillment_method = method
    if destination:
        order.destination = destination
    if estimated_delivery is not None:
        order.estimated_delivery = estimated_delivery
    if tracking_reference is not None:
        order.tracking_reference = tracking_reference

    _audit(
        db,
        order.checkout_session_id,
        actor="operator",
        action="fulfillment_advanced",
        decision="approved",
        summary=f"Fulfillment {current} -> {target} for order {order.order_id}",
        payload={"order_id": order.order_id, "from": current, "to": target},
    )
    db.flush()
    return order


def _order_line_items(raw: str) -> list[dict]:
    return json.loads(raw) if raw else []


def _order_destination(raw: str | None) -> dict | None:
    """Decode the stored JSON address block; None when empty/absent."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) and parsed else None


def serialize_order(order: Order) -> dict:
    line_items = _order_line_items(order.line_items)
    # Normalize line items to the protocol shape (no ORM/internal leakage).
    items = []
    for it in line_items:
        items.append(
            {
                "product_id": it.get("product_id"),
                "pack_id": it.get("pack_id"),
                "name": it.get("name"),
                "category": it.get("category", ""),
                "quantity": int(it.get("quantity", 0)),
                "unit_price": int(it.get("unit_price", 0)),
            }
        )
    return {
        "order_id": order.order_id,
        "checkout_session_id": order.checkout_session_id,
        "buyer_agent_id": order.buyer_agent_id,
        "merchant_id": order.merchant_id,
        "merchant": settings.merchant_id,
        "currency": order.currency,
        "authorized_amount_paise": order.authorized_amount_paise,
        "amount": order.authorized_amount_paise / 100 if order.authorized_amount_paise else None,
        "line_items": items,
        "order_status": order.order_status,
        "fulfillment_status": order.fulfillment_status,
        "fulfillment_method": order.fulfillment_method or None,
        "destination": _order_destination(order.destination),
        "estimated_delivery": order.estimated_delivery,
        "tracking_reference": order.tracking_reference,
        "payment": {
            "provider": "Razorpay",
            "driver": order.payment_driver,
            "order_id": order.razorpay_order_id,
            "payment_id": order.razorpay_payment_id,
        },
        "execution_mandate_id": order.execution_mandate_id,
        "decision_receipt_id": order.decision_receipt_id,
        "created_at": order.created_at.isoformat() + "Z",
        "updated_at": order.updated_at.isoformat() + "Z",
    }
