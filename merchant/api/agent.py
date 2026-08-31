"""Merchant Agent protocol endpoints (PRD §19, §29, §43).

    POST /agent_sessions                    -> run the agent on a buyer request
    GET  /agent_sessions/{id}               -> current session view
    POST /agent_sessions/{id}/decision      -> buyer accept/reject of a proposal
    GET  /agent_sessions/{id}/trace         -> reconstructable decision trace

The Buyer Agent reaches the Merchant Agent ONLY through these routes
(FR-MA-18/19). Existing deterministic endpoints remain untouched (G6).
"""
import json

from fastapi import APIRouter, HTTPException

from merchant.agent.orchestration import MerchantAgent
from merchant.agent import timing as agent_timing
from merchant.agent.state import (
    MerchantAgentState,
    get_session_state,
    new_session_id,
    register_session,
    session_busy_lock,
)
from merchant.config import settings
from merchant.persistence.models.dto import (
    AgentDecisionRequest,
    AgentSessionCreate,
    AgentSessionOut,
    AgentTraceResponse,
)
from merchant.domain import checkout_service
from merchant.evidence import audit_service

router = APIRouter()

EXECUTION_MODES = {"buyer_completes", "agent_completed"}


def _serialize_event(e) -> dict:
    return {
        "seq": e.seq,
        "event_id": e.event_id,
        "checkout_session_id": e.checkout_session_id,
        "actor": e.actor,
        "action": e.action,
        "decision": e.decision,
        "rules_fired": json.loads(e.rules_fired),
        "reasoning_summary": e.reasoning_summary,
        "payload": json.loads(e.payload),
        "prev_hash": e.prev_hash,
        "hash": e.hash,
        "timestamp": e.timestamp.isoformat() + "Z",
    }


def _session_out(state: MerchantAgentState) -> dict:
    out = AgentSessionOut(
        agent_session_id=state.agent_session_id,
        status=state.status,
        buyer_request=state.buyer_request,
        execution_mode=state.execution_mode,
        requested_items=state.requested_items,
        current_items=state.current_items,
        checkout_session_id=state.checkout_session_id,
        proposal=state.pending_proposal,
        outcome=state.outcome or None,
        iterations=state.iteration_count,
        tool_calls=state.tool_call_count,
    )
    body = out.model_dump()
    # Workstream A: structured per-stage latency breakdown for this session.
    body["latency_breakdown"] = {
        "stages": agent_timing.stages(state.agent_session_id),
        "total_ms": agent_timing.session_total_ms(state.agent_session_id),
    }
    if state.checkout_session_id:
        from merchant.persistence.db import SessionLocal

        db = SessionLocal()
        try:
            try:
                cs = checkout_service.get_session(db, state.checkout_session_id)
                body["checkout_session"] = checkout_service.serialize_session(db, cs).model_dump(mode="json")
            except Exception:  # noqa: BLE001 — declined sessions always resolvable; safety net
                body["checkout_session"] = None
        finally:
            db.close()
    return body


def _require_session(agent_session_id: str) -> MerchantAgentState:
    state = get_session_state(agent_session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Unknown agent session")
    return state


@router.post("/agent_sessions", status_code=201)
def create_agent_session(body: AgentSessionCreate):
    """Run the Merchant Agent loop on a buyer request until pause/terminal."""
    if body.execution_mode not in EXECUTION_MODES:
        raise HTTPException(status_code=422, detail="execution_mode must be buyer_completes or agent_completed")
    if body.brain not in (None, "llm", "fallback"):
        raise HTTPException(status_code=422, detail="brain must be 'llm' or 'fallback'")

    state = MerchantAgentState(
        agent_session_id=new_session_id(),
        buyer_request=body.buyer_request.strip(),
        buyer_agent_id=body.buyer_agent_id,
        delegation_token=body.delegation_token,
        execution_mode=body.execution_mode,
        requested_items=[i.model_dump() for i in body.requested_items],
    )
    if body.brain:
        state.fallback_ctx["brain_override"] = body.brain
    register_session(state)

    lock = session_busy_lock(state.agent_session_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Agent session is busy")
    try:
        MerchantAgent().run(state)
    finally:
        lock.release()
    return _session_out(state)


@router.get("/agent_sessions/{agent_session_id}")
def get_agent_session(agent_session_id: str):
    return _session_out(_require_session(agent_session_id))


@router.post("/agent_sessions/{agent_session_id}/decision")
def decide_agent_session(agent_session_id: str, body: AgentDecisionRequest):
    """Buyer answers a structured proposal: accept | reject."""
    state = _require_session(agent_session_id)
    decision = body.decision.strip().lower()
    if decision not in ("accept", "reject"):
        raise HTTPException(status_code=422, detail="decision must be 'accept' or 'reject'")
    if state.status != "awaiting_buyer_confirmation" or state.pending_proposal is None:
        raise HTTPException(status_code=409, detail="No proposal is awaiting a buyer decision")

    lock = session_busy_lock(agent_session_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Agent session is busy")
    try:
        MerchantAgent().resume(state, accepted=(decision == "accept"), note=body.note)
    finally:
        lock.release()
    return _session_out(state)


@router.get("/agent_sessions/{agent_session_id}/trace", response_model=AgentTraceResponse)
def trace_agent_session(agent_session_id: str):
    """Reconstructable decision trace (PRD §38) from hash-chained audit events."""
    from merchant.persistence.db import SessionLocal

    state = _require_session(agent_session_id)
    db = SessionLocal()
    try:
        events = audit_service.all_events(db, limit=2000)
        matched = []
        for e in reversed(events):  # seq ascending
            try:
                payload = json.loads(e.payload)
            except json.JSONDecodeError:
                payload = {}
            if payload.get("agent_session_id") == agent_session_id or (
                state.checkout_session_id and e.checkout_session_id == state.checkout_session_id
            ):
                matched.append(_serialize_event(e))
        return AgentTraceResponse(agent_session_id=agent_session_id, events=matched)
    finally:
        db.close()


@router.get("/health/agent")
def agent_health():
    return {
        "brain": "llm" if settings.ma_llm_brain else "fallback",
        "max_iterations": settings.ma_max_iterations,
        "max_tool_calls": settings.ma_max_tool_calls,
    }
