"""Buyer service — chat API for the human, agent loop behind it.

    POST /chat                      {conversation_id?, message}  -> SSE stream
    POST /conversations/{id}/decision {approved}                 -> SSE stream (resume)
    GET  /conversations/{id}                                     -> transcript replay
"""
import queue
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
    with conv.lock:
        if conv.busy:
            raise HTTPException(status_code=409, detail="Agent is still working on the previous message.")
        if conv.pending_approval is not None:
            raise HTTPException(status_code=409, detail="A bill is awaiting your approval below.")
        if conv.pending_substitution is not None:
            raise HTTPException(status_code=409, detail="An item substitution is awaiting your answer above.")
        conv.busy = True
    conv.emit({"type": "user_message", "content": body.message})
    return _stream(
        conv,
        agent.llm_loop(conv, body.message),
        prelude=[{"type": "conversation_started", "conversation_id": conv.id}],
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
