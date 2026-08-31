"""SQLAlchemy ORM entities for the merchant agent."""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from merchant.persistence.db import Base, utcnow


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
    # AP2 amount binding: the exact amount authorized for payment, in paise,
    # frozen at the moment the Execution Mandate is created (PRD §16). Must
    # equal the Razorpay order/payment amount or payment is blocked.
    authorized_amount_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # AP2: the amount (paise) of the actual Razorpay Order, persisted at creation
    # so the callback can prove Order amount == authorized amount (PRD §17).
    razorpay_order_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # AP2: the amount (paise) that the gateway independently reported as settled
    # (when available). C-1: must equal razorpay_order_amount == authorized amount,
    # or the transaction fails closed. None in hermetic sim mode (no gateway).
    verified_payment_amount_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_mandate_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
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


class IntentMandate(Base):
    """Signed authorization of buyer scope (AP2 §10–12)."""

    __tablename__ = "intent_mandates"

    mandate_id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(String, default="intent")
    merchant_id: Mapped[str] = mapped_column(String)
    buyer_agent_id: Mapped[str] = mapped_column(String, index=True)
    intent: Mapped[str] = mapped_column(String, default="{}")  # JSON
    constraints: Mapped[str] = mapped_column(String, default="{}")  # JSON
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    nonce: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    canonical_payload_hash: Mapped[str] = mapped_column(String, default="")
    key_id: Mapped[str] = mapped_column(String, default="")
    signature: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExecutionMandate(Base):
    """Signed binding of the exact transaction about to be charged (AP2 §13–15)."""

    __tablename__ = "execution_mandates"

    mandate_id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(String, default="execution")
    intent_mandate_id: Mapped[str | None] = mapped_column(String, nullable=True)
    merchant_id: Mapped[str] = mapped_column(String)
    buyer_agent_id: Mapped[str] = mapped_column(String, index=True)
    checkout_session_id: Mapped[str] = mapped_column(String, index=True)
    items: Mapped[str] = mapped_column(String, default="[]")  # JSON
    total_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String, default="INR")
    cart_hash: Mapped[str] = mapped_column(String, default="")
    # Workstream E: the user-signed intent assertion (id + canonical hash) that
    # authorized this execution. NULL = legacy pre-assertion authorization path.
    user_assertion_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_assertion_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    nonce: Mapped[str] = mapped_column(String)
    canonical_payload_hash: Mapped[str] = mapped_column(String, default="")
    key_id: Mapped[str] = mapped_column(String, default="")
    signature: Mapped[str] = mapped_column(String, default="")
    # AP2 single-use: set when the mandate's payment is settled (or declined) so a
    # consumed mandate can never authorize a second payment (replay protection).
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PolicyDecision(Base):
    """Signed deterministic gating decision that authorizes the execution mandate."""

    __tablename__ = "policy_decisions"

    decision_id: Mapped[str] = mapped_column(String, primary_key=True)
    execution_mandate_id: Mapped[str] = mapped_column(String, index=True)
    checkout_session_id: Mapped[str] = mapped_column(String, index=True)
    decision: Mapped[str] = mapped_column(String)  # approved | rejected | hold
    checks: Mapped[str] = mapped_column(String, default="[]")  # JSON
    authorized_amount_paise: Mapped[int] = mapped_column(Integer)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    canonical_payload_hash: Mapped[str] = mapped_column(String, default="")
    key_id: Mapped[str] = mapped_column(String, default="")
    signature: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DecisionReceipt(Base):
    """Signed binding of mandate + policy + Razorpay order/payment (AP2 §27–28)."""

    __tablename__ = "decision_receipts"

    receipt_id: Mapped[str] = mapped_column(String, primary_key=True)
    intent_mandate_id: Mapped[str | None] = mapped_column(String, nullable=True)
    execution_mandate_id: Mapped[str] = mapped_column(String, index=True)
    decision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    checkout_session_id: Mapped[str] = mapped_column(String, index=True)
    authorized_amount_paise: Mapped[int] = mapped_column(Integer)
    razorpay_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payment_status: Mapped[str] = mapped_column(String)  # verified
    outcome: Mapped[str] = mapped_column(String)  # completed | declined | pending
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    canonical_payload_hash: Mapped[str] = mapped_column(String, default="")
    key_id: Mapped[str] = mapped_column(String, default="")
    signature: Mapped[str] = mapped_column(String, default="")


class UserKey(Base):
    """A buying human's registered Ed25519 public key (Workstream E).

    Registered via POST /user_keys with the PUBLIC key only. The private key
    lives exclusively in the user's browser localStorage and is never sent to
    the server (the server can cryptographically verify a user signature but
    can never forge one).
    """

    __tablename__ = "user_keys"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    key_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    public_key_pem: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")  # active | revoked
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserIntentAssertion(Base):
    """A user-signed intent assertion (Workstream E).

    Root of authority: the PII-owning human signs an intent assertion binding
    their identity (user_id, user_key_id), the merchant, the trusted delegate,
    the checkout action and a spend ceiling. The merchant verifies the Ed25519
    signature against the registered UserKey BEFORE any execution mandate /
    order / payment is created. Single-use: consumed_at set on first use.
    """

    __tablename__ = "user_intent_assertions"

    assertion_id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[str] = mapped_column(String, default="1.0")
    type: Mapped[str] = mapped_column(String, default="UserIntentAssertion")
    user_id: Mapped[str] = mapped_column(String, index=True)
    user_key_id: Mapped[str] = mapped_column(String, index=True)
    merchant_id: Mapped[str] = mapped_column(String)
    delegate_id: Mapped[str] = mapped_column(String, index=True)
    intent: Mapped[str] = mapped_column(String, default="{}")  # JSON
    constraint_ceiling: Mapped[str] = mapped_column(String, default="{}")  # JSON
    issued_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    nonce: Mapped[str] = mapped_column(String)
    canonical_payload_hash: Mapped[str] = mapped_column(String, default="")
    key_id: Mapped[str] = mapped_column(String, default="")
    signature: Mapped[str] = mapped_column(String, default="")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
