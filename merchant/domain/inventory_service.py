"""Inventory + reservation management (PRD §24).

Model:
    actual_stock     -> packs.stock_actual
    reserved_stock   -> sum of active, unexpired reservations
    available_stock  -> actual - reserved

A reservation does NOT protect against actual stock disappearing: final
validation at completion re-checks actual stock (this enables the deliberate
TOCTOU failure demo).
"""
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from merchant.config import settings
from merchant.persistence.db import utcnow
from merchant.persistence.models.entities import Reservation
from merchant.evidence import audit_service


def _reservation_event(
    db: Session,
    *,
    actor: str,
    action: str,
    res: Reservation,
    prev_status: str,
    new_status: str,
    reason: str = "",
) -> None:
    """Record a reservation state transition in the serialized audit trail.

    MUST be called from inside a `mutate()` block (``_begin_immediate`` set), so
    the audit event shares the transaction with the state change: if the
    transaction rolls back, neither the transition nor the evidence persists.
    """
    if not db.info.get("_begin_immediate"):
        return
    audit_service.append_event(
        db,
        actor=actor,
        action=action,
        checkout_session_id=res.checkout_session_id,
        decision="info",
        reasoning_summary=reason or _default_summary(action),
        payload={
            "reservation_id": res.reservation_id,
            "pack_id": res.pack_id,
            "quantity": int(res.quantity),
            "previous_status": prev_status,
            "new_status": new_status,
        },
    )


def _default_summary(action: str) -> str:
    return {
        "reservation_released": "Reserved stock was released",
        "reservation_consumed": "Reserved stock was consumed",
        "reservation_expired": "Reservation expired",
    }.get(action, "Reservation state changed")


def sweep_expired(db: Session) -> int:
    """Mark expired active reservations as expired. Returns count swept.

    Lifecycle audit: each durable expiry is recorded as ``reservation_expired``.
    `sweep_expired` is ALSO invoked from read-only/agent inventory checks where
    the change is transient (rolled back, no ``_begin_immediate``); those are
    never audited, because they are not real transitions.
    """
    now = utcnow()
    stale = db.execute(
        select(Reservation).where(Reservation.status == "active", Reservation.expires_at <= now)
    ).scalars().all()
    for res in stale:
        prev = res.status
        res.status = "expired"
        _reservation_event(
            db, actor="merchant_agent", action="reservation_expired",
            res=res, prev_status=prev, new_status="expired",
            reason="Reservation exceeded its expiry window",
        )
    if stale:
        db.flush()
    return len(stale)


def reserved_quantity(db: Session, pack_id: str) -> int:
    rows = db.execute(
        select(Reservation.quantity).where(
            Reservation.pack_id == pack_id,
            Reservation.status == "active",
        )
    ).scalars().all()
    return sum(rows)


def available_stock(db: Session, pack_id: str, stock_actual: int) -> int:
    sweep_expired(db)
    return max(0, stock_actual - reserved_quantity(db, pack_id))


def create_reservations(db: Session, checkout_session_id: str, items: list[dict]) -> str:
    """Reserve each line item. `items` = resolved line items with pack_id+quantity."""
    expires_at = utcnow() + timedelta(minutes=settings.reservation_ttl_minutes)
    for item in items:
        db.add(
            Reservation(
                reservation_id=f"res_{uuid.uuid4().hex[:10]}",
                checkout_session_id=checkout_session_id,
                pack_id=item["pack_id"],
                quantity=int(item["quantity"]),
                status="active",
                expires_at=expires_at,
            )
        )
    db.flush()
    return expires_at.isoformat() + "Z"


def release_session_reservations(
    db: Session,
    checkout_session_id: str,
    *,
    actor: str = "merchant_agent",
    reason: str = "",
) -> None:
    """Release all active reservations belonging to a session."""
    rows = db.execute(
        select(Reservation).where(
            Reservation.checkout_session_id == checkout_session_id,
            Reservation.status == "active",
        )
    ).scalars().all()
    for res in rows:
        prev = res.status
        res.status = "released"
        _reservation_event(
            db, actor=actor, action="reservation_released",
            res=res, prev_status=prev, new_status="released",
            reason=reason or "Reserved stock was released",
        )
    if rows:
        db.flush()


def consume_session_reservations(
    db: Session,
    checkout_session_id: str,
    *,
    actor: str = "merchant_agent",
    reason: str = "",
) -> None:
    """Consume reservations after successful payment (stock is decremented)."""
    rows = db.execute(
        select(Reservation).where(
            Reservation.checkout_session_id == checkout_session_id,
            Reservation.status == "active",
        )
    ).scalars().all()
    for res in rows:
        prev = res.status
        res.status = "consumed"
        _reservation_event(
            db, actor=actor, action="reservation_consumed",
            res=res, prev_status=prev, new_status="consumed",
            reason=reason or "Reserved stock was consumed after payment",
        )
    if rows:
        db.flush()
