"""Buyer service — chat API for the human, agent loop behind it.

    POST /chat                      {conversation_id?, message}  -> SSE stream
    POST /conversations/{id}/decision {approved}                 -> SSE stream (resume)
    GET  /conversations/{id}                                     -> transcript replay
"""
import queue
import re
import threading

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from buyer_service.agent import BILL_PAUSE, BuyerAgent
from buyer_service.config import settings
from buyer_service.conversations import CONVERSATIONS, Conversation, get_conversation, new_conversation

app = FastAPI(title="Agentic Commerce Gateway — Buyer Service", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

agent = BuyerAgent()

# Deterministic small-talk/acknowledgment gate: pure greetings AND short
# reactions ("nice", "perfect", "ok") never reach the LLM — 8B models respond
# to them by copying the prompt's example queries into discover_products.
# Rule: every word of a SHORT message must belong to this whitelist.
_ACK_WORDS = {
    # greetings
    "hi", "hii", "hiii", "hey", "hello", "yo", "hlo", "sup",
    "namaste", "namaskar", "morning", "afternoon", "evening",
    # acknowledgments / reactions
    "ok", "okay", "k", "kk", "nice", "perfect", "great", "awesome",
    "cool", "wow", "lol", "super", "amazing", "osm", "good", "fine",
    "alright", "right", "sahi", "acha", "accha", "achha", "theek",
    "thik", "haan", "haanji", "haa", "han", "yes", "yeah", "yep",
    "yup", "no", "nope", "nahi", "done", "thanks", "thank", "thx",
    "ty", "welcome", "bilkul", "sahi", "hai", "hain",
}
_ACK_FILLERS = {"bhai", "ji", "yaar", "bro", "sir", "na"}
_ACK_MAX_WORDS = 4


def _is_smalltalk(text: str) -> bool:
    words = re.findall(r"[a-z']+", (text or "").lower())
    if not words or len(words) > _ACK_MAX_WORDS:
        return False
    return all(w in _ACK_WORDS or w in _ACK_FILLERS for w in words)


def _smalltalk_gen(text: str):
    words = set(re.findall(r"[a-z']+", (text or "").lower()))
    if words & {"hi", "hii", "hiii", "hey", "hello", "yo", "hlo", "sup", "namaste", "namaskar"}:
        lead = "Hello! 👋"
    elif words & {"thanks", "thank", "thx", "ty"}:
        lead = "Anytime! 😊"
    else:
        lead = "Glad you like it! 😊"

    def gen():
        yield {
            "type": "message",
            "content": (
                f"{lead} What would you like to buy? Tell me the items or a recipe — "
                'e.g. "milk, bread and eggs under ₹300" — and I\'ll build your cart.'
            ),
        }

    return gen()


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


class DecisionRequest(BaseModel):
    approved: bool


class ChooseRequest(BaseModel):
    choice: object  # int index | "other" | "cancel"


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
                if event is not BILL_PAUSE:
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
            if item is BILL_PAUSE:
                continue
            import json

            yield f"data: {json.dumps(item)}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")


@app.post("/chat")
def chat(body: ChatRequest):
    conv = get_conversation(body.conversation_id) if body.conversation_id else None
    if conv is None:
        conv = new_conversation()
        conv.emit({"type": "conversation_started", "conversation_id": conv.id})
    # F2: canned smalltalk only for fresh conversations — mid-chat acks
    # ("ok", "nice") must reach the agent, never a template.
    has_prior_exchange = any(
        e.get("type") not in ("conversation_started", "user_message") for e in conv.events
    )
    with conv.lock:
        if conv.busy:
            raise HTTPException(status_code=409, detail="Agent is still working on the previous message.")
        if conv.pending_approval is not None:
            raise HTTPException(status_code=409, detail="A bill is awaiting your approval below.")
        if conv.pending_substitution is not None:
            raise HTTPException(status_code=409, detail="An item substitution is awaiting your answer above.")
        conv.busy = True
    conv.emit({"type": "user_message", "content": body.message})
    prelude = [{"type": "conversation_started", "conversation_id": conv.id}]
    if not has_prior_exchange and _is_smalltalk(body.message):
        return _stream(conv, _smalltalk_gen(body.message), prelude=prelude)
    return _stream(
        conv,
        agent.llm_loop(conv, body.message),
        prelude=prelude,
    )


@app.post("/conversations/{conversation_id}/decision")
def decision(conversation_id: str, body: DecisionRequest):
    conv = get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    with conv.lock:
        if conv.pending_approval is None:
            raise HTTPException(status_code=409, detail="No bill is awaiting approval.")
        if conv.busy:
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
        if conv.busy:
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
        "pending_approval": conv.pending_approval,
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
