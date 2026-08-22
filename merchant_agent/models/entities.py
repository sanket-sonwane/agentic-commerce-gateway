"""SQLAlchemy ORM entities for the merchant agent."""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from merchant_agent.db import Base, utcnow


class Product(Base):
    __tablename__ = "products"

    product_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, index=True)
    brand: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(String, default="")
    tags: Mapped[str] = mapped_column(String, default="[]")  # JSON array

    packs: Mapped[list["Pack"]] = relationship(back_populates="product")


class Pack(Base):
    __tablename__ = "packs"

    pack_id: Mapped[str] = mapped_column(String, primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.product_id"), index=True)
    size: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String)  # ml | l | g | kg | pcs
    price: Mapped[int] = mapped_column(Integer)  # INR (whole rupees)
    currency: Mapped[str] = mapped_column(String, default="INR")
    stock_actual: Mapped[int] = mapped_column(Integer, default=0)

    product: Mapped["Product"] = relationship(back_populates="packs")


class Delegation(Base):
    __tablename__ = "delegations"

    delegation_id: Mapped[str] = mapped_column(String, primary_key=True)
    buyer_agent_id: Mapped[str] = mapped_column(String, index=True)
    max_order_value: Mapped[int] = mapped_column(Integer)
    allowed_categories: Mapped[str] = mapped_column(String)  # JSON array
    max_quantity_per_item: Mapped[int] = mapped_column(Integer, default=10)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String, default="active")  # active | revoked


class CheckoutSession(Base):
    __tablename__ = "checkout_sessions"

    checkout_session_id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # created | requires_payment | requires_confirmation | payment_pending
    # completed | declined | cancelled
    status: Mapped[str] = mapped_column(String, index=True)
    buyer_agent_id: Mapped[str] = mapped_column(String, index=True)
    delegation_id: Mapped[str] = mapped_column(String)
    line_items: Mapped[str] = mapped_column(String, default="[]")  # JSON
    totals: Mapped[str] = mapped_column(String, default="{}")  # JSON
    gating_result: Mapped[str] = mapped_column(String, default="{}")  # JSON
    decline_code: Mapped[str | None] = mapped_column(String, nullable=True)
    payment_driver: Mapped[str | None] = mapped_column(String, nullable=True)
    razorpay_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Reservation(Base):
    __tablename__ = "reservations"

    reservation_id: Mapped[str] = mapped_column(String, primary_key=True)
    checkout_session_id: Mapped[str] = mapped_column(String, index=True)
    pack_id: Mapped[str] = mapped_column(String, index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    # active | consumed | released | expired
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String, unique=True)
    checkout_session_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    actor: Mapped[str] = mapped_column(String)  # buyer_agent | merchant_agent | operator | razorpay
    action: Mapped[str] = mapped_column(String)
    decision: Mapped[str | None] = mapped_column(String, nullable=True)  # approved | declined | info
    rules_fired: Mapped[str] = mapped_column(String, default="[]")  # JSON array
    reasoning_summary: Mapped[str] = mapped_column(String, default="")
    payload: Mapped[str] = mapped_column(String, default="{}")  # JSON
    prev_hash: Mapped[str] = mapped_column(String, default="")
    hash: Mapped[str] = mapped_column(String, default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    endpoint: Mapped[str] = mapped_column(String)
    session_id: Mapped[str] = mapped_column(String, index=True)
    response_snapshot: Mapped[str] = mapped_column(String, default="{}")  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PaymentMethod(Base):
    """Merchant-side saved-payment record. Never exposed to buyer agents.

    One row per human user behind a buyer agent scope: customer_id is created
    up-front; token_id appears after the first save-card checkout payment.
    """
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    buyer_agent_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    razorpay_customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    razorpay_token_id: Mapped[str | None] = mapped_column(String, nullable=True)
    card_last4: Mapped[str | None] = mapped_column(String, nullable=True)
    card_network: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending_save")  # pending_save | active
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
