"""Pydantic DTOs — the published protocol contract shapes."""
from datetime import datetime
from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNAUTHORIZED_AGENT = "UNAUTHORIZED_AGENT"
    DELEGATION_EXPIRED = "DELEGATION_EXPIRED"
    DELEGATION_SCOPE_VIOLATION = "DELEGATION_SCOPE_VIOLATION"
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    PACK_NOT_FOUND = "PACK_NOT_FOUND"
    STOCK_UNAVAILABLE = "STOCK_UNAVAILABLE"
    CATEGORY_BLOCKED = "CATEGORY_BLOCKED"
    SPEND_LIMIT_EXCEEDED = "SPEND_LIMIT_EXCEEDED"
    CHECKOUT_NOT_FOUND = "CHECKOUT_NOT_FOUND"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    PRICE_CHANGED = "PRICE_CHANGED"
    CONFIRMATION_STALE = "CONFIRMATION_STALE"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_PENDING = "PAYMENT_PENDING"


class ProtocolError(Exception):
    """Raised inside services; converted to the structured error envelope."""

    def __init__(
        self,
        code: "ErrorCode | str",
        message: str,
        *,
        http_status: int = 400,
        retryable: bool = False,
        details: Optional[dict[str, Any]] = None,
        session_ref: Optional[dict[str, Any]] = None,
    ) -> None:
        self.code = ErrorCode(code) if isinstance(code, str) else code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.details = details or {}
        self.session_ref = session_ref
        super().__init__(message)

    def envelope(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
            }
        }
        if self.session_ref:
            body["checkout_session"] = self.session_ref
        return body


# ---------- discovery ----------


class DiscoverRequest(BaseModel):
    query: str = Field(min_length=1)
    buyer_agent_id: str
    exclude_pack_ids: list[str] = Field(default_factory=list)  # substitution support
    deprioritize_product_ids: list[str] = Field(default_factory=list)  # prefer other products
    exclude_product_ids: list[str] = Field(default_factory=list)  # hard product exclusion
    skip_llm_parse: bool = False  # deterministic keyword matching for substitutions


class ProposalItem(BaseModel):
    product_id: str
    pack_id: str
    name: str
    matched_for: str
    match_reasons: list[str] = []
    quantity: int = 1
    price: int
    size: float
    unit: str
    available_stock: int
    category: str = ""
    tags: list[str] = []


class DiscoveryResponse(BaseModel):
    basket_proposal: list[ProposalItem]
    estimated_total: int
    currency: str = "INR"
    unmatched: list[str] = []
    parser: str = "keyword-fallback"  # llm | keyword-fallback (observability)


# ---------- checkout ----------


class LineItemIn(BaseModel):
    product_id: str
    pack_id: str
    quantity: int = Field(ge=1)


class CreateCheckoutRequest(BaseModel):
    buyer_agent_id: str
    delegation_token: str
    line_items: list[LineItemIn] = Field(min_length=1)


class UpdateCheckoutRequest(BaseModel):
    buyer_agent_id: str
    delegation_token: str
    line_items: list[LineItemIn] = Field(min_length=1)


class CompleteCheckoutRequest(BaseModel):
    buyer_agent_id: str
    delegation_token: str


class CancelCheckoutRequest(BaseModel):
    buyer_agent_id: str
    delegation_token: str


class ConfirmRequest(BaseModel):
    buyer_agent_id: str
    delegation_token: str
    accept_new_terms: bool
    version: int


class Totals(BaseModel):
    subtotal: int
    tax: int
    shipping: int
    total: int
    currency: str = "INR"


class GatingCheck(BaseModel):
    rule: str
    result: str  # pass | fail | skipped
    detail: str


class GatingResult(BaseModel):
    passed: bool
    checks: list[GatingCheck] = []


class ReservationInfo(BaseModel):
    status: str
    expires_at: Optional[datetime] = None


class ChangeInfo(BaseModel):
    type: str
    old_total: int
    new_total: int


class CheckoutSessionOut(BaseModel):
    checkout_session_id: str
    version: int
    status: str
    buyer_agent_id: str
    delegation_id: str
    line_items: list[dict[str, Any]]
    totals: dict[str, Any]
    gating_result: dict[str, Any]
    reservation: ReservationInfo
    change: Optional[ChangeInfo] = None
    payment: dict[str, Any] = {}
    razorpay_order_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class CreateCheckoutResponse(BaseModel):
    checkout_session: CheckoutSessionOut


class CompleteResponse(BaseModel):
    checkout_session: CheckoutSessionOut
    change: Optional[ChangeInfo] = None
    error: Optional[dict[str, Any]] = None


# ---------- audit ----------


class AuditEventOut(BaseModel):
    seq: int
    event_id: str
    checkout_session_id: Optional[str]
    actor: str
    action: str
    decision: Optional[str]
    rules_fired: list[str]
    reasoning_summary: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    timestamp: datetime


class AuditVerifyResponse(BaseModel):
    intact: bool
    entries_checked: int
    first_broken_seq: Optional[int] = None
