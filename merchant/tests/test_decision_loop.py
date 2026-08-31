"""Decision-cycle optimization tests (Merchant Agent adaptive-loop seams).

The Merchant Agent must invoke the LLM ONLY when merchant judgment is
genuinely required. These tests prove the deterministic short-circuit seams
that remove redundant reasoning rounds while preserving payment truth, budgets
and the guardrails:

  D1  buyer_completes:  create_checkout success -> deterministic ready_for_completion
  D2  agent_completed:  create_checkout success -> deterministic gated completion
  D3  STOCK_UNAVAILABLE at gated completion keeps the adaptive loop alive
  D4  buyer REJECTS a substitution -> next-ranked candidate proposed without the brain
  D5  buyer keeps rejecting -> observed list exhausts -> graceful decline (no loop)
  D6  duplicate deterministic reads are replayed / repeat-blocked, not re-executed
  D7  tool-call budget now binds real executions (previously inert counter)

All tests run the deterministic fallback brain (conftest MA_LLM_BRAIN=false);
brain-call counts are observed with a recording _next_action wrapper exactly
like the guardrail suites in test_agent_handoff.py.
"""
import json

import merchant.agent.loop as loop_mod
from merchant.agent.loop import MerchantAgent
from merchant.agent.state import MerchantAgentState, get_session_state, new_session_id
from merchant.agent import tools as agent_tools
from merchant.config import settings
from merchant.persistence.db import mutate
from merchant.persistence.models.entities import Pack

BUYER = "buyer_agent_llm_01"
DELEGATION = "dlg_9f8e7d"

MILK = {"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500"}
MILK_1L = {"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_1l"}


def _make_state(mode="buyer_completes", items=None, request=None):
    return MerchantAgentState(
        agent_session_id=new_session_id(),
        buyer_request=request or "Buy milk",
        buyer_agent_id=BUYER,
        delegation_token=DELEGATION,
        execution_mode=mode,
        requested_items=items or [],
    )


def _zero_stock(pack_id: str):
    with mutate() as db:
        db.get(Pack, pack_id).stock_actual = 0


def _recording_next_action(monkeypatch):
    """Wrap the real brain and record every invocation (one entry per turn)."""
    calls = []
    original = loop_mod._next_action

    def wrapped(st):
        calls.append(st.status)
        return original(st)

    monkeypatch.setattr(loop_mod, "_next_action", wrapped)
    return calls


def _audit_payloads(agent_session_id: str):
    from merchant.evidence import audit_service
    from merchant.persistence.db import SessionLocal

    db = SessionLocal()
    try:
        events = [
            e
            for e in audit_service.all_events(db, limit=1000)
            if json.loads(e.payload).get("agent_session_id") == agent_session_id
        ]
        events.reverse()
        return [(e.action, e.decision, json.loads(e.payload)) for e in events]
    finally:
        db.close()


# ---------------------------------------------------------------- D1 buyer_completes


def test_buyer_completes_create_short_circuits_to_ready(monkeypatch):
    """A loop-executed policy-clean checkout ends ready_for_completion on the
    next turn WITHOUT an extra reasoning round for the (deterministic) handoff."""
    state = _make_state("buyer_completes", items=[{**MILK, "quantity": 2}])
    calls = _recording_next_action(monkeypatch)

    result = MerchantAgent().run(state)

    assert result.status == "ready_for_completion"
    assert result.outcome["reason_code"] == "basket_ready_for_buyer_payment"
    assert len(calls) == 2  # observe inventory + create; the handoff was deterministic
    assert result.tool_call_count == 2


def test_buyer_completes_single_brain_call_when_prechecked(monkeypatch):
    """Even when the model creates the basket as its very first action, the
    deterministic handoff follows -- one brain call total."""
    state = _make_state("buyer_completes", items=[{**MILK, "quantity": 1}])
    script = iter([
        ("create_checkout", {"items": [{**MILK, "quantity": 1}]}, "creating", "tc-pre"),
    ])
    monkeypatch.setattr(loop_mod, "_next_action", lambda st: next(script))

    result = MerchantAgent().run(state)

    assert result.status == "ready_for_completion"
    assert result.outcome["checkout_session_id"] == state.checkout_session_id
    assert result.tool_call_count == 1


# ---------------------------------------------------------------- D2 agent_completed auto-complete


def test_agent_completed_auto_completes_live_checkout(monkeypatch):
    """agent_completed: a policy-clean live checkout is completed through the
    SAME gated money ladder deterministically — not through another model turn."""
    state = _make_state("agent_completed", items=[{**MILK, "quantity": 1}])
    calls = _recording_next_action(monkeypatch)

    result = MerchantAgent().run(state)

    assert result.status == "completed"
    assert result.outcome["reason_code"] == "fulfilled_after_gated_completion"
    assert result.outcome["payment"]["payment_id"]
    assert len(calls) == 2  # observe + create; gated completion was deterministic
    assert result.tool_call_count == 3  # check_inventory, create_checkout, request_completion
    events = _audit_payloads(state.agent_session_id)
    assert any(a == "agent_request_completion" for a, _, _ in events)


def test_agent_completed_search_flow_auto_completes(monkeypatch):
    """Full search -> observe -> create -> deterministic complete (no model
    turn for the completion decision)."""
    state = _make_state("agent_completed", request="Buy 2 packs of Amul toned milk 500ml")
    calls = _recording_next_action(monkeypatch)

    result = MerchantAgent().run(state)

    assert result.status == "completed"
    assert result.outcome["payment"]["payment_id"]
    assert len(calls) == 3  # search, observe, create — completion was deterministic


# ---------------------------------------------------------------- D3 adaptation preserved


def test_stock_failure_at_auto_completion_keeps_adaptive_loop(monkeypatch):
    """The seam must NOT swallow recoverable failures: STOCK_UNAVAILABLE at the
    money ladder keeps the loop alive so the next decision cycle adapts
    (multi-cycle LLM deliberation preserved on an agent_completed session)."""
    real_guide = MerchantAgent._guide_next_action

    def zero_stock_after_create(self, st, name, envelope):
        real_guide(self, st, name, envelope)
        if name == "create_checkout" and envelope.get("success"):
            with mutate() as db:
                db.get(Pack, MILK["pack_id"]).stock_actual = 0

    monkeypatch.setattr(MerchantAgent, "_guide_next_action", zero_stock_after_create)

    state = _make_state("agent_completed", items=[{**MILK, "quantity": 1}])
    calls = _recording_next_action(monkeypatch)

    result = MerchantAgent().run(state)

    assert state.fallback_ctx.get("pending_continuation") is None  # seam consumed/cleared
    assert result.status == "awaiting_buyer_confirmation"
    assert result.pending_proposal is not None
    assert result.outcome.get("status") in (None, "awaiting_buyer_confirmation")
    assert len(calls) == 4  # observe, create, then find_alternative + request_confirmation
    events = [a for a, _, _ in _audit_payloads(state.agent_session_id)]
    assert any(a == "agent_find_alternative" for a in events)
    assert any(a == "agent_request_confirmation" for a in events)


def test_agent_completed_payment_pending_still_terminates_honestly(monkeypatch):
    """Hard completion failures are NOT swallowed by the seam: a deterministic
    'pending' outcome still terminates honestly (no false 'completed')."""
    from merchant.payment import payment_service as ps
    from merchant.payment.payment_service import PaymentOutcome

    def forced_pending(order_id, amount_rupees):
        return PaymentOutcome(
            status="pending",
            order_id=order_id,
            error_code="PAYMENT_PENDING",
            message="forced unresolved capture",
        )

    monkeypatch.setattr(ps, "execute_sim_capture", forced_pending)

    state = _make_state("agent_completed", items=[{**MILK, "quantity": 1}])
    MerchantAgent().run(state)

    assert state.status == "declined"
    assert state.outcome["reason_code"] == "payment_pending"
    blob = json.dumps(state.outcome).lower()
    assert "completed" not in blob and "paid rs" not in blob


# ---------------------------------------------------------------- D4 reject seam


def test_buyer_rejection_proposes_next_ranked_without_brain(monkeypatch):
    """A REJECTED substitution uses the ALREADY-OBSERVED ranked candidate list:
    the next candidate is proposed deterministically and the brain is never
    consulted — and the rejected pack is never re-proposed."""
    _zero_stock(MILK_1L["pack_id"])
    state = _make_state("agent_completed", request="Buy 1L Amul milk")
    MerchantAgent().run(state)
    assert state.status == "awaiting_buyer_confirmation"
    first_pack = state.pending_proposal["proposal"]["pack_id"]

    calls = _recording_next_action(monkeypatch)

    MerchantAgent().resume(state, accepted=False, note="not acceptable")

    assert state.status == "awaiting_buyer_confirmation"
    next_pack = state.pending_proposal["proposal"]["pack_id"]
    assert next_pack != first_pack
    assert next_pack not in state.excluded_pack_ids
    assert first_pack in state.excluded_pack_ids
    assert calls == []  # the follow-up proposal was fully deterministic


def test_reject_exhausts_observed_candidates_then_declines(monkeypatch):
    """When every observed candidate has been rejected, the seam yields to the
    adaptive cycle which declines gracefully — never a livelock, never payment."""
    _zero_stock(MILK_1L["pack_id"])
    state = _make_state("agent_completed", request="Buy 1L Amul milk")
    MerchantAgent().run(state)
    assert state.status == "awaiting_buyer_confirmation"

    guards = 0
    while state.status == "awaiting_buyer_confirmation" and guards < 12:
        guards += 1
        MerchantAgent().resume(state, accepted=False, note="reject all")

    assert state.status == "declined"
    assert state.outcome["reason_code"] in ("no_valid_alternative", "no_matching_product")
    assert guards >= 2  # at least one deterministic next-candidate proposal happened
    assert state.tool_call_count <= settings.ma_max_tool_calls


def test_accepted_alternative_still_completes_via_seam(monkeypatch):
    """After a reject seam proposed the next candidate, ACCEPTING it goes
    straight through the deterministic completion (no extra brain turn)."""
    _zero_stock(MILK_1L["pack_id"])
    state = _make_state("agent_completed", request="Buy 1L Amul milk")
    MerchantAgent().run(state)
    merchant_session = state.agent_session_id

    MerchantAgent().resume(state, accepted=False, note="no")
    assert state.status == "awaiting_buyer_confirmation"

    calls = _recording_next_action(monkeypatch)
    MerchantAgent().resume(state, accepted=True, note="take it")

    assert state.status == "completed"
    assert state.outcome["payment"]["payment_id"]
    assert calls == []  # create (applied in _accept_substitution) + gated completion, both deterministic
    assert merchant_session == state.agent_session_id


# ---------------------------------------------------------------- D5 replay / budget


def test_duplicate_read_in_loop_is_replayed_not_reexecuted(monkeypatch):
    """A repeated IDENTICAL read in an unchanged world replays the cached
    envelope (still closing the OpenAI tool round) instead of re-poking the DB."""
    state = _make_state("agent_completed", items=[{**MILK, "quantity": 1}])
    state.llm_history.append({"role": "user", "content": "ctx"})
    script = iter([
        ("check_inventory", {"pack_id": MILK["pack_id"]}, "observe", "tc1"),
        ("check_inventory", {"pack_id": MILK["pack_id"]}, "observe again", "tc2"),
        ("finish", {"outcome": "declined", "reason_code": "gave_up", "message": "m"}, "r", None),
    ])
    monkeypatch.setattr(loop_mod, "_next_action", lambda st: next(script))

    MerchantAgent().run(state)

    assert state.status == "declined"
    events = _audit_payloads(state.agent_session_id)
    executed = [e for e in events if e[0] == "agent_check_inventory"]
    replayed = [e for e in events if e[0] == "agent_check_inventory_replayed"]
    assert len(executed) == 1  # executed exactly once
    assert len(replayed) == 1 and replayed[0][1] == "approved"

    tool_msgs = [m for m in state.llm_history if m.get("role") == "tool"]
    assert tool_msgs[-1]["tool_call_id"] == "tc2"
    assert json.loads(tool_msgs[-1]["content"]).get("replayed_from_observation") is True


def test_duplicate_read_after_write_is_not_replayed():
    """A write bumps the world: the SAME read afterwards must re-execute (safe
    over-invalidation), never return a stale replay."""
    state = _make_state("agent_completed", items=[{**MILK, "quantity": 1}])
    agent_tools.execute_tool("check_inventory", {"pack_id": MILK["pack_id"]}, state)
    env = agent_tools.execute_tool("create_checkout", {"items": [{**MILK, "quantity": 1}]}, state)
    assert env["success"]

    replay = loop_mod._maybe_replay_observation(state, "check_inventory", {"pack_id": MILK["pack_id"]})
    assert replay is None  # world moved -> must re-read


def test_identical_repeat_escalates_to_blocked_repeat():
    """First identical re-request replays; the SECOND escalates to a
    BLOCKED_REPEAT refusal so the brain cannot spin on duplicate observations."""
    state = _make_state()
    args = {"pack_id": MILK["pack_id"]}
    agent_tools.execute_tool("check_inventory", args, state)

    first = loop_mod._maybe_replay_observation(state, "check_inventory", args)
    assert first is not None and first.get("replayed_from_observation") is True

    second = loop_mod._maybe_replay_observation(state, "check_inventory", args)
    assert second is not None and second["error"]["code"] == "BLOCKED_REPEAT"

    third = loop_mod._maybe_replay_observation(state, "check_inventory", args)
    assert third is not None and third["error"]["code"] == "BLOCKED_REPEAT"


def test_tool_budget_now_binds_real_calls(monkeypatch):
    """The tool-call budget is now a REAL ceiling (it was previously an inert
    counter — no increment existed)."""
    monkeypatch.setattr(settings, "ma_max_tool_calls", 2)
    state = _make_state()

    r1 = agent_tools.execute_tool("get_product", {"product_id": "prod_milk_amul"}, state)
    r2 = agent_tools.execute_tool("get_product", {"product_id": "prod_milk_amul"}, state)
    r3 = agent_tools.execute_tool("get_product", {"product_id": "prod_milk_amul"}, state)

    assert r1["success"] and r2["success"]
    assert r3["error"]["code"] == "TOOL_BUDGET_EXHAUSTED"


def test_current_basket_note_in_transaction_state():
    """The per-turn context exposes the resolved live basket in one line so the
    model never has to reconstruct it (STEP-11-minimal context, no redesign)."""
    state = _make_state("buyer_completes", items=[{**MILK, "quantity": 2}])
    assert "current basket: none yet" in loop_mod._transaction_state_note(state)

    state.current_items = [{"pack_id": MILK["pack_id"], "quantity": 2}]
    note = loop_mod._transaction_state_note(state)
    assert f"{MILK['pack_id']} x2" in note