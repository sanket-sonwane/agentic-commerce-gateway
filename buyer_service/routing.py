"""Deterministic-first routing + authoritative CURRENT STATE (Workstream B).

Improves conversational quality and latency by handling a small, well-defined
set of user intents WITHOUT an extra LLM round-trip, always from authoritative
conversation state:

  * `cancel`      — cancel the active checkout/negotiation if we genuinely own
                    one; never invent a session id.
  * `order_status`— answer from the deterministic state notes + active session;
                    never claim a payment or order that the server hasn't
                    confirmed.

Everything else (purchases, browsing, questions, unrelated chit-chat) falls
through to the ordinary LLM tool loop, but the turn is annotated with a
CURRENT STATE block so the model can't hallucinate order/payment status.

Scope guardrails honoured: no LLM money authority, no payment/state-machine
redesign, payments only executed by the deterministic merchant path, and the
money truth ("paid" only when the server says `completed`) is never weakened.
"""
import json
import re
from collections.abc import Generator
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from buyer_service.conversations import Conversation
    from buyer_service.tools import MerchantClient


# ---------------------------------------------------------------- intent

_CANCEL_RE = re.compile(
    r"\b(?:cancel|cancel\s+my\s+order|cancel\s+this|stop\s+the\s+order|"
    r"never\s+mind|forget\s+it|scrap\s+it|take\s+it\s+off|i\s+don'?t\s+want\s+"
    r"(?:it|this|them)\s+anymore|remove\s+the\s+order|ditch\s+it)\b",
    re.IGNORECASE,
)

_STATUS_RE = re.compile(
    r"\b(?:where\s+is\s+my\s+order|order\s+status|what.?s\s+the\s+status|"
    r"my\s+order\s+status|status\s+of\s+my\s+order|has\s+my\s+order|is\s+my\s+"
    r"order|track\s+my\s+order|did\s+my\s+order|where\s+has\s+my\s+order|"
    r"delivery\s+status|was\s+my\s+order|what\s+happened\s+to\s+my\s+order|"
    r"how\s+is\s+my\s+order|how\s+do\s+i\s+track|order\s+update)\b",
    re.IGNORECASE,
)


def intent(text: str) -> str | None:
    """Classify a user message with cheap deterministic heuristics.

    Returns 'cancel', 'order_status', or None (fall through to the LLM).
    """
    if not text:
        return None
    if _CANCEL_RE.search(text):
        return "cancel"
    if _STATUS_RE.search(text):
        return "order_status"
    return None


def _state_note(payload: dict) -> dict:
    """Deterministic ground-truth note (mirrors the buyer agent's format)."""
    return {"role": "system", "content": json.dumps({"state": payload}, default=str)}


# ------------------------------------------------------------ state notes

def _state_notes(conv) -> list[dict]:
    """Deterministic server-verified facts written as system `state` notes."""
    notes: list[dict] = []
    for m in getattr(conv, "llm_history", []) or []:
        if m.get("role") != "system":
            continue
        raw = m.get("content", "")
        if not isinstance(raw, str):
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("state"), dict):
            notes.append(data["state"])
    return notes


def current_state_block(conv) -> str:
    """Authoritative, model-visible summary of the conversation's server state.

    Injected into the LLM turn so follow-ups about order/payment status can only
    be grounded in deterministic truth. Exposes no secrets — only coarse state.
    """
    lines: list[str] = []
    notes = _state_notes(conv)

    order_events = []
    for n in notes:
        ev = n.get("event")
        if ev in ("order_completed", "order_cancelled", "order_declined",
                  "stock_unavailable", "substitution_accepted"):
            order_events.append(n)

    if order_events:
        last = order_events[-1]
        e = last.get("event")
        if e == "order_completed":
            lines.append("Last order: COMPLETED (server-confirmed paid).")
        elif e == "order_cancelled":
            lines.append("Last order: CANCELLED. Nothing was charged.")
        elif e == "order_declined":
            lines.append("Last order: DECLINED. Nothing was charged.")
        elif e == "stock_unavailable":
            lines.append("Some items went out of stock; an alternative was proposed.")
        elif e == "substitution_accepted":
            lines.append("A substitution was accepted; a new checkout was proposed.")

    if conv.pending_approval:
        lines.append("There is a checkout bill currently awaiting YOUR approval.")
        lines.append("It is NOT paid until you approve it and the gateway confirms.")
    if conv.pending_substitution:
        lines.append("There is an item-substitution question currently awaiting YOUR answer.")
    if conv.active_session_id and not conv.pending_approval:
        lines.append("There is an active (unapproved) checkout session — not yet paid.")
    if conv.active_agent_session_id:
        lines.append("A merchant-agent negotiation is in progress — not yet paid.")

    if not lines:
        lines.append("No active or completed order in this conversation yet.")
    return "CURRENT STATE (deterministic, authoritative): " + " ".join(lines)


# ------------------------------------------------------------- short-circuits

def _last_order_outcome(conv) -> tuple[str | None, dict | None]:
    notes = _state_notes(conv)
    for n in reversed(notes):
        if n.get("event") in ("order_completed", "order_cancelled", "order_declined"):
            return n["event"], n
    return None, None


def _handle_status(conv) -> Generator[dict, None, None]:
    event, note = _last_order_outcome(conv)
    if event == "order_completed":
        total = note.get("total")
        msg = f"Your last order was completed and paid" + (f" for ₹{total}." if total else ".")
    elif event == "order_cancelled":
        msg = "Your last order was cancelled and nothing was charged."
    elif event == "order_declined":
        msg = "Your last order was declined and nothing was charged."
    elif conv.pending_approval:
        msg = "There's a bill awaiting your approval — I'll pause for that before any payment."
    elif conv.pending_substitution:
        msg = "There's a substitution choice awaiting your answer."
    elif conv.active_session_id:
        msg = "You have a checkout prepared but it isn't paid yet. Approve the bill to finish."
    else:
        msg = "You don't have any active order right now. What would you like to buy?"
    yield {"type": "message", "content": msg}


def _handle_cancel(conv, merchant) -> Generator[dict, None, None]:
    # Cancel the substitution question if that's what's pending.
    if conv.pending_substitution:
        conv.pending_substitution = None
        conv.llm_history.append(_state_note({"event": "substitution_cancelled"}))
        yield {"type": "message", "content": "Got it — I've dropped that substitution question. Nothing was changed or charged."}
        return

    csid = conv.active_session_id
    if not csid:
        if conv.pending_approval:
            conv.pending_approval = None
            conv.active_session_id = None
            yield {"type": "message", "content": "Sure — I've cancelled that pending order. Nothing was charged."}
        else:
            yield {"type": "message", "content": "There's no active order to cancel right now. What would you like to do?"}
        return

    try:
        status_code, payload = merchant.cancel_checkout(csid)
    except Exception as exc:  # noqa: BLE001 - gateway unreachable, be truthful
        yield {"type": "message",
               "content": f"I couldn't reach the order service to cancel right now ({exc}). Try again in a moment."}
        return
    session = payload.get("checkout_session", {}) if isinstance(payload, dict) else {}
    ok = status_code == 200 and session.get("status") == "cancelled"
    conv.pending_approval = None
    conv.active_session_id = None
    conv.llm_history.append(
        _state_note({"event": "order_cancelled", "checkout_session_id": csid})
    )
    if ok:
        yield {
            "type": "status",
            "checkout_session_id": csid,
            "status": "cancelled",
        }
        yield {"type": "message", "content": "Done — I cancelled that order. Nothing was charged."}
    else:
        yield {"type": "message", "content": "That order was already closed or couldn't be cancelled — nothing was charged."}


def respond(conv, text: str, merchant) -> Generator[dict, None, None] | None:
    """Deterministic short-circuit for a classified intent.

    Returns an event generator when handled without the LLM, else None so the
    caller falls through to the normal tool loop.
    """
    kind = intent(text)
    if kind == "cancel":
        return _handle_cancel(conv, merchant)
    if kind == "order_status":
        return _handle_status(conv)
    return None


__all__ = ["intent", "current_state_block", "respond"]
