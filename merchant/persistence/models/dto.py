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
    PAYMENT_BLOCKED = "PAYMENT_BLOCKED"  # AP2: amount/mandate binding mismatch
    PROVENANCE_NOT_FOUND = "PROVENANCE_NOT_FOUND"


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
    # AP2: optional signed Intent Mandate authorizing the buyer's scope. When
    # supplied, the Execution Mandate is linked to it and the transaction must
    # fall within its constraints or completion is blocked (fail closed).
    intent_mandate_id: Optional[str] = None
    # Workstream E: optional user-signed UserIntentAssertion (root of authority).
    # The browser signs a canonical consent string with the user's own Ed25519
    # key (private key stays in localStorage, never sent). The merchant verifies
    # the signature, hash, expiry, single-use, subject, merchant, delegate and
    # constraint ceiling BEFORE any Execution Mandate / Razorpay order / payment
    # exists. Absent = legacy authorization path (explicitly labeled as such).
    user_assertion: Optional[dict[str, Any]] = None


class UserKeyRegister(BaseModel):
    """Register a buying human's Ed25519 PUBLIC key (never the private key)."""

    user_id: str = Field(min_length=1)
    key_id: str = Field(min_length=1)
    public_key_pem: str = Field(min_length=16)
    status: str = "active"


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
    # Public identity of the merchant serving this session (used by the
    # user-signed intent assertion to bind consent to the correct merchant).
    merchant_id: str
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
    anchor: Optional[dict[str, Any]] = None


# ---------- AP2 provenance ----------


class IntentMandateCreate(BaseModel):
    buyer_agent_id: str
    delegation_token: str
    intent: str = Field(min_length=1, description="Human-readable intent, e.g. 'Buy groceries for my household'")
    constraints_override: Optional[dict[str, Any]] = None


class ProvenanceCheck(BaseModel):
    name: str
    ok: bool
    detail: str


class ProvenanceVerifyResponse(BaseModel):
    checkout_session_id: str
    provenance_available: bool
    status: str
    checks: list[ProvenanceCheck]
    summary: str
    artifacts: dict[str, Any] = {}


# ---------- consumer provenance projection ----------


class ConsumerTransaction(BaseModel):
    id: str
    status: str
    amount: int  # whole rupees, from authoritative totals
    currency: str = "INR"
    merchant: str
    agent: str
    created_at: datetime
    updated_at: datetime


class ConsumerJourneyStage(BaseModel):
    stage: str
    status: str  # completed | reached | failed | not_reached
    title: str
    summary: str
    timestamp: Optional[datetime] = None
    evidence_ref: Optional[str] = None


class ConsumerAuthorization(BaseModel):
    authorized_by: str
    agent: str
    scope: str
    max_amount_paise: Optional[int] = None
    merchant_scope: list[str] = []
    expires_at: Optional[datetime] = None
    verified: bool = True
    authority_note: str = ""
    # user_signed | delegation | none — how this transaction's authority root
    # was established. user_signed means a user-owned key cryptographically
    # verified a UserIntentAssertion for this transaction.
    authorization_source: str = "delegation"


class ConsumerCheck(BaseModel):
    name: str
    status: str  # pass | fail | skipped
    summary: str


class ConsumerPayment(BaseModel):
    provider: str = "Razorpay"
    status: str  # not_called | order_created | pending | authorized | verified | declined
    razorpay_called: bool = False
    order_id: Optional[str] = None
    payment_id: Optional[str] = None
    amount: Optional[int] = None  # whole rupees
    amount_paise: Optional[int] = None


class ConsumerProof(BaseModel):
    execution_mandate: str = "unavailable"  # verified | failed | unavailable
    policy_decision: str = "unavailable"
    decision_receipt: str = "unavailable"
    provenance: str = "unavailable"
    audit_chain: str = "unavailable"
    key_id: Optional[str] = None
    mandate_id: Optional[str] = None
    receipt_id: Optional[str] = None
    verifier_status: str = "unverified"  # VALID | INVALID | NOT_AVAILABLE


class ConsumerProvenanceResponse(BaseModel):
    checkout_session_id: str
    transaction: ConsumerTransaction
    journey: list[ConsumerJourneyStage] = []
    authorization: ConsumerAuthorization
    checks: list[ConsumerCheck] = []
    payment: ConsumerPayment
    proof: ConsumerProof
    current_stage: str
    last_successful_stage: Optional[str] = None
    failure_stage: Optional[str] = None
    failure_reason: Optional[str] = None
    stopped_at: Optional[str] = None
    razorpay_called: bool = False


# ---------- merchant agent (PRD §9–14, §19) ----------


class AgentSessionCreate(BaseModel):
    buyer_request: str = Field(min_length=1)
    buyer_agent_id: str
    delegation_token: str
    execution_mode: str = Field(
        default="buyer_completes",
        description="buyer_completes: buyer triggers payment via protocol | agent_completed: agent runs the gated completion",
    )
    requested_items: list[LineItemIn] = Field(default_factory=list)
    brain: Optional[str] = Field(
        default=None,
        description="Optional per-session brain override: 'llm' | 'fallback' (defaults to MA_LLM_BRAIN)",
    )


class AgentDecisionRequest(BaseModel):
    decision: str = Field(description="accept | reject")
    note: str = ""


class AgentSessionOut(BaseModel):
    agent_session_id: str
    status: str
    buyer_request: str
    execution_mode: str
    requested_items: list[dict[str, Any]] = []
    current_items: list[dict[str, Any]] = []
    checkout_session_id: Optional[str] = None
    proposal: Optional[dict[str, Any]] = None
    outcome: Optional[dict[str, Any]] = None
    checkout_session: Optional[dict[str, Any]] = None
    iterations: int = 0
    tool_calls: int = 0


class AgentTraceResponse(BaseModel):
    agent_session_id: str
    events: list[AuditEventOut] = []
