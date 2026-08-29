"""Transaction-scoped Merchant Agent state (PRD §12).

State lives for the duration of one agent session and is never shared across
buyers. The delegation token is held server-side only and is never placed
into LLM-visible context.
"""
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

# running | awaiting_buyer_confirmation | ready_for_completion |
# awaiting_payment_authorization | completed | declined | unable_to_fulfill
ACTIVE_STATUSES = {"running"}
PAUSED_STATUSES = {"awaiting_buyer_confirmation", "ready_for_completion", "awaiting_payment_authorization"}
TERMINAL_STATUSES = {"completed", "declined", "unable_to_fulfill"}

SESSIONS: dict[str, "MerchantAgentState"] = {}
_SESSION_LOCK = threading.Lock()
_BUSY: dict[str, threading.Lock] = {}


def new_session_id() -> str:
    return f"mas_{uuid.uuid4().hex[:10]}"


def session_busy_lock(agent_session_id: str) -> threading.Lock:
    with _SESSION_LOCK:
        return _BUSY.setdefault(agent_session_id, threading.Lock())


def register_session(state: "MerchantAgentState") -> None:
    with _SESSION_LOCK:
        SESSIONS[state.agent_session_id] = state


def get_session_state(agent_session_id: str) -> Optional["MerchantAgentState"]:
    return SESSIONS.get(agent_session_id)


@dataclass
class MerchantAgentState:
    agent_session_id: str
    buyer_request: str
    buyer_agent_id: str
    delegation_token: str  # server-side only; never exposed to the LLM
    execution_mode: str = "buyer_completes"  # buyer_completes | agent_completed

    requested_items: list[dict] = field(default_factory=list)
    current_items: list[dict] = field(default_factory=list)

    observations: list[dict] = field(default_factory=list)
    actions_taken: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)

    checkout_session_id: Optional[str] = None
    pending_proposal: Optional[dict] = None
    rejected_proposals: list[dict] = field(default_factory=list)
    excluded_pack_ids: list[str] = field(default_factory=list)

    iteration_count: int = 0
    tool_call_count: int = 0

    status: str = "running"
    outcome: dict = field(default_factory=dict)
    llm_history: list[dict] = field(default_factory=list)
    fallback_ctx: dict = field(default_factory=dict)

    # -------------------------------------------------- helpers

    def observe(self, source: str, content: dict) -> None:
        self.observations.append({"source": source, "content": content})

    def record_action(self, action: str, args: dict, reasoning: str = "") -> None:
        self.actions_taken.append(
            {"action": action, "args": args, "reasoning": reasoning, "iteration": self.iteration_count}
        )

    def set_outcome(self, status: str, reason_code: str, message: str, **extra: Any) -> None:
        self.status = status
        self.outcome = {
            "status": status,
            "reason_code": reason_code,
            "message": message,
            **extra,
        }

    def public_view(self) -> dict:
        """LLM-safe projection of the state (no delegation token)."""
        return {
            "agent_session_id": self.agent_session_id,
            "status": self.status,
            "buyer_request": self.buyer_request,
            "execution_mode": self.execution_mode,
            "requested_items": self.requested_items,
            "current_items": self.current_items,
            "checkout_session_id": self.checkout_session_id,
            "pending_proposal": self.pending_proposal,
            "excluded_pack_ids": self.excluded_pack_ids,
            "iterations": self.iteration_count,
            "tool_calls": self.tool_call_count,
            "outcome": self.outcome or None,
        }
