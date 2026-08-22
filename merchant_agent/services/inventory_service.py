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

from merchant_agent.config import settings
from merchant_agent.db import utcnow
from merchant_agent.models.entities import Reservation


def sweep_expired(db: Session) -> int:
    """Mark expired active reservations as expired. Returns count swept."""
    now = utcnow()
    stale = db.execute(
        select(Reservation).where(Reservation.status == "active", Reservation.expires_at <= now)
    ).scalars().all()
    for res in stale:
        res.status = "expired"
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


def release_session_reservations(db: Session, checkout_session_id: str) -> None:
    """Release all active reservations belonging to a session."""
    rows = db.execute(
        select(Reservation).where(
            Reservation.checkout_session_id == checkout_session_id,
            Reservation.status == "active",
        )
    ).scalars().all()
    for res in rows:
        res.status = "released"
    if rows:
        db.flush()


def consume_session_reservations(db: Session, checkout_session_id: str) -> None:
    """Consume reservations after successful payment (stock is decremented)."""
    rows = db.execute(
        select(Reservation).where(
            Reservation.checkout_session_id == checkout_session_id,
            Reservation.status == "active",
        )
    ).scalars().all()
    for res in rows:
        res.status = "consumed"
    if rows:
        db.flush()
