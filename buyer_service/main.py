"""Buyer service — chat API for the human, agent loop behind it.

    POST /chat                      {conversation_id?, message}  -> SSE stream
    POST /conversations/{id}/decision {approved}                 -> SSE stream (resume)
    GET  /conversations/{id}                                     -> transcript replay

Cart-centric flow: chat stays open while a cart is live. Text consent is
classified deterministically (buyer_service/consent.py) BEFORE any LLM call,
so only an explicit short affirmative can trigger money movement.
"""
import queue
import re
import threading

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from buyer_service.agent import BuyerAgent
from buyer_service.consent import classify_reply, is_explicit_cancel
from buyer_service.config import settings
from buyer_service.conversations import CONVERSATIONS, Conversation, get_conversation, new_conversation

app = FastAPI(title="Agentic Commerce Gateway — Buyer Service", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

agent = BuyerAgent()


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


class DecisionRequest(BaseModel):
    approved: bool


class ChooseRequest(BaseModel):
    choice: object  # int index | "other" | "cancel"


# Deterministic purchase-intent detection: user asks to place the order before
# the agent asked. We route to the LLM with a directive to ask the final
# yes/no question — money still only moves after an explicit "yes".
_ORDER_INTENT = re.compile(
    r"\b(?:place|complete|finish|confirm|final(?:ize|ise)?|go)\s+(?:the\s+|my\s+|this\s+)?order\b"
    r"|\border\s+karo?\b|\border\s+kar\s*do\b"
    r"|\bbuy\s+(?:it\s+)?now\b"
    r"|\bcheckout\b"
    r"|\bproceed\s+(?:to\s+)?(?:payment|checkout)\b",
    re.IGNORECASE,
)
_INTENT_VETO = re.compile(
    r"\b(add|remove|delete|change|swap|replace|set|increase|decrease|quantity|qty|"
    r"less|more|not|don't|dont|cancel|stop|wait|instead|but)\b",
    re.IGNORECASE,
)

_DIRECTIVE_PLACE_ORDER = (
    "The user wants to place the order NOW. Do NOT call any tools. Reply with ONE "
    "short confirmation question stating the current cart's final total and ending "
    "with the exact marker [ASK_APPROVAL]. Example: 'Final total ₹110 — shall I "
    "place this order? [ASK_APPROVAL]'"
)


def _wants_order_placed(text: str) -> bool:
    words = re.findall(r"\S+", text or "")
    if not words or len(words) > 10:
        return False
    return bool(_ORDER_INTENT.search(text)) and not _INTENT_VETO.search(text)


def _stream(conv: Conversation, gen, prelude: list[dict] | None = None):
    """Run a sync event generator on a worker thread; stream events as SSE."""
    q: queue.Queue = queue.Queue()
    sentinel = object()

    def worker():
        try:
            for event in prelude or []:
                conv.emit(event)
                q.put(event)
            for event in gen:
                conv.emit(event)
                q.put(event)
        except Exception as exc:  # noqa: BLE001
            err = {"type": "message", "content": f"Something went wrong on my side ({exc})."}
            conv.emit(err)
            q.put(err)
        finally:
            q.put(sentinel)
            conv.busy = False

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        while True:
            item = q.get()
            if item is sentinel:
                yield "event: done\ndata: {}\n\n"
                break
            import json

            yield f"data: {json.dumps(item)}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")


@app.post("/chat")
def chat(body: ChatRequest):
    conv = get_conversation(body.conversation_id) if body.conversation_id else None
    if conv is None:
        conv = new_conversation()
        conv.emit({"type": "conversation_started", "conversation_id": conv.id})
    with conv.lock:
        if conv.busy or conv.payment_in_flight:
            raise HTTPException(status_code=409, detail="Agent is still working on the previous message.")
        if conv.pending_substitution is not None:
            raise HTTPException(status_code=409, detail="An item substitution is awaiting your answer above.")

        route_decision: bool | None = None
        if conv.awaiting_payment_consent:
            intent = classify_reply(body.message)
            if intent == "approve" and conv.active_session_id:
                route_decision = True  # explicit textual approval -> charge
            elif intent == "cancel":
                route_decision = False  # explicit cancel while asked -> drop order
            # approve without a live cart, soft_no, edits, questions -> normal turn
            conv.awaiting_payment_consent = False
        elif (
            is_explicit_cancel(body.message)
            and conv.active_session_id is not None
            and not conv.payment_in_flight
        ):
            route_decision = False
            conv.awaiting_payment_consent = False

        conv.busy = True

    conv.emit({"type": "user_message", "content": body.message})
    prelude = [{"type": "conversation_started", "conversation_id": conv.id}]
    if route_decision is True:
        return _stream(conv, agent.resume_after_decision(conv, True), prelude=prelude)
    if route_decision is False:
        return _stream(conv, agent.resume_after_decision(conv, False), prelude=prelude)
    directive = (
        _DIRECTIVE_PLACE_ORDER
        if conv.active_session_id and not conv.payment_in_flight and _wants_order_placed(body.message)
        else None
    )
    return _stream(conv, agent.llm_loop(conv, body.message, directive=directive), prelude=prelude)


@app.post("/conversations/{conversation_id}/decision")
def decision(conversation_id: str, body: DecisionRequest):
    """Legacy/programmatic approval endpoint (UI now approves via chat text)."""
    conv = get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    with conv.lock:
        if conv.pending_approval is None:
            raise HTTPException(status_code=409, detail="No cart is awaiting confirmation.")
        if conv.busy or conv.payment_in_flight:
            raise HTTPException(status_code=409, detail="Agent is busy.")
        conv.busy = True
    return _stream(conv, agent.resume_after_decision(conv, body.approved))


@app.post("/conversations/{conversation_id}/choose")
def choose(conversation_id: str, body: ChooseRequest):
    conv = get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    with conv.lock:
        if conv.pending_substitution is None:
            raise HTTPException(status_code=409, detail="No substitution is awaiting your answer.")
        if conv.busy or conv.payment_in_flight:
            raise HTTPException(status_code=409, detail="Agent is busy.")
        conv.busy = True
    return _stream(conv, agent.choose(conv, body.choice))


@app.get("/conversations/{conversation_id}")
def replay(conversation_id: str):
    conv = get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    return {
        "conversation_id": conv.id,
        "events": conv.events,
        "active_cart": bool(conv.active_session_id),
        "awaiting_payment_consent": conv.awaiting_payment_consent,
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": settings.nim_model,
        "buyer_agent_id": settings.buyer_agent_id,
        "merchant": settings.merchant_base_url,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("buyer_service.main:app", host="0.0.0.0", port=settings.buyer_service_port, reload=False)
