"""In-memory conversation store with thread-safe event log."""
import threading
import uuid
from dataclasses import dataclass, field


@dataclass
class Conversation:
    id: str
    events: list[dict] = field(default_factory=list)      # full event log for UI replay
    llm_history: list[dict] = field(default_factory=list)  # NIM message history
    pending_approval: dict | None = None                   # {approval_id, checkout_session_id, kind}
    pending_substitution: dict | None = None               # {options, kept_items, unavailable, round}
    active_session_id: str | None = None
    active_agent_session_id: str | None = None             # merchant agent session under negotiation
    last_query: str = ""                                   # most recent discovery query
    intent_mandate_id: str | None = None                   # AP2 signed intent authorizing scope
    last_basket_items: list[dict] = field(default_factory=list)
    last_create_key: str | None = None                     # E2: idempotency key of active basket
    last_candidates: list[dict] = field(default_factory=list)  # most recent discovery result
    declined_packs: list[str] = field(default_factory=list)    # packs declined as out-of-stock
    offered_pack_ids: list[str] = field(default_factory=list)  # substitutes already shown
    substitution_rounds: int = 0
    timings: list[dict] = field(default_factory=list)  # latest turn's measured stages (Workstream A)
    busy: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, event: dict) -> dict:
        event = {"seq": len(self.events), **event}
        self.events.append(event)
        return event

    def events_after(self, seq: int) -> list[dict]:
        return self.events[seq + 1 :]


CONVERSATIONS: dict[str, Conversation] = {}


def new_conversation() -> Conversation:
    conv = Conversation(id=f"conv_{uuid.uuid4().hex[:10]}")
    CONVERSATIONS[conv.id] = conv
    return conv


def get_conversation(conversation_id: str) -> Conversation | None:
    return CONVERSATIONS.get(conversation_id)
