"""P1 handoff-reliability tests (8B-model failure regression).

Covers the audited failure modes without a live LLM:
  F1  invented checkout ids + wrong termination after basket creation
  F2  inventory numbers copied into quantity arguments
  F3  disallowed self-completion in buyer_completes mode

All tests drive the deterministic fallback brain or scripted _next_action
sequences through the production loop/tools — no prompt-dependent behavior.
"""
import json

import merchant_agent.agent.loop as loop_mod
from merchant_agent.agent.loop import MerchantAgent, _exposed_tool_schemas, _transaction_state_note
from merchant_agent.agent.state import MerchantAgentState, new_session_id
from merchant_agent.agent import tools as agent_tools
from merchant_agent.config import settings
from merchant_agent.db import mutate, SessionLocal
from merchant_agent.models.entities import CheckoutSession, Pack

BUYER = "buyer_agent_llm_01"
DELEGATION = "dlg_9f8e7d"
RESTRICTED = "dlg_restricted_50"

MILK = {"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500"}
MILK_1L = {"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_1l"}
SAFFRON = {"product_id": "prod_saffron_premium", "pack_id": "pack_saffron_5g"}


def _make_state(mode="buyer_completes", items=None, delegation=DELEGATION):
    return MerchantAgentState(
        agent_session_id=new_session_id(),
        buyer_request="Buy milk",
        buyer_agent_id=BUYER,
        delegation_token=delegation,
        execution_mode=mode,
        requested_items=items or [],
    )


def _zero_stock(pack_id: str):
    with mutate() as db:
        db.get(Pack, pack_id).stock_actual = 0


def _audit_payloads(agent_session_id: str):
    from merchant_agent.services import audit_service

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


def _start(client, mode, request="Buy 2 packs of Amul toned milk 500ml", items=None, delegation=DELEGATION):
    return client.post(
        "/agent_sessions",
        json={
            "buyer_request": request,
            "buyer_agent_id": BUYER,
            "delegation_token": delegation,
            "execution_mode": mode,
            **({"requested_items": items} if items else {}),
        },
    ).json()


# ---------------------------------------------------------------- T1/T3 happy handoff


def test_buyer_completes_handoff_preserves_real_csid(client):
    """Basket creation must end ready_for_completion carrying the server id,
    and that exact id must reach the deterministic completion ladder."""
    body = _start(client, "buyer_completes", items=[{**MILK, "quantity": 2}])
    assert body["status"] == "ready_for_completion", body
    real = body["checkout_session"]["checkout_session_id"]
    assert body["outcome"]["checkout_session_id"] == real
    assert body["outcome"]["reason_code"] == "basket_ready_for_buyer_payment"
    assert body["checkout_session"]["status"] == "requires_payment"

    comp = client.post(
        f"/checkout_sessions/{real}/complete",
        json={"buyer_agent_id": BUYER, "delegation_token": DELEGATION},
    )
    assert comp.status_code == 200
    final = comp.json()["checkout_session"]
    assert final["status"] == "completed"
    assert final["checkout_session_id"] == real


# ---------------------------------------------------------------- T2 fake identifiers


def test_complete_with_invented_csid_is_structurally_rejected(client):
    r = client.post(
        "/checkout_sessions/cs_fake_123/complete",
        json={"buyer_agent_id": BUYER, "delegation_token": DELEGATION},
    )
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "CHECKOUT_NOT_FOUND"
    assert client.get("/audit_logs/verify").json()["intact"] is True


def test_fake_run_gating_id_rejected_and_audited_via_loop(client, monkeypatch):
    """F1 part 1: an invented 'session_123' must be rejected server-side and
    leave a declined audit trace — with zero payment activity."""
    state = _make_state("buyer_completes")
    state.llm_history.append({"role": "user", "content": "ctx"})
    script = iter([
        ("run_gating", {"checkout_session_id": "session_123"}, "checking policy", "tc1"),
        ("finish", {"outcome": "declined", "reason_code": "gave_up", "message": "done"}, "r", None),
    ])
    monkeypatch.setattr(loop_mod, "_next_action", lambda st: next(script))

    result = MerchantAgent().run(state)
    assert result.status == "declined"

    events = _audit_payloads(state.agent_session_id)
    gate_events = [e for e in events if e[0] == "agent_run_gating"]
    assert gate_events and gate_events[0][1] == "declined"
    assert gate_events[0][2]["args"] == {"checkout_session_id": "session_123"}
    assert not any("razorpay_order_created" in e[0] or e[0] == "payment_success" for e in events)
    assert client.get("/audit_logs/verify").json()["intact"] is True


def test_run_gating_tool_rejects_unknown_id_directly():
    state = _make_state()
    res = agent_tools.execute_tool("run_gating", {"checkout_session_id": "session_123"}, state)
    assert res["success"] is False
    assert res["error"]["code"] == "CHECKOUT_NOT_FOUND"


# ---------------------------------------------------------------- T3 tool exposure


def test_tool_exposure_is_mode_aware():
    buyer_names = {s["function"]["name"] for s in _exposed_tool_schemas(_make_state("buyer_completes"))}
    agent_names = {s["function"]["name"] for s in _exposed_tool_schemas(_make_state("agent_completed"))}
    assert "request_completion" not in buyer_names
    assert "request_completion" in agent_names
    assert "create_checkout" in buyer_names and "update_checkout" in buyer_names


def test_request_completion_refusal_names_correct_action():
    state = _make_state("buyer_completes")
    res = agent_tools.execute_tool("request_completion", {}, state)
    assert res["success"] is False
    assert "ready_for_completion" in res["error"]["message"]


# ---------------------------------------------------------------- T4 state header


def test_transaction_state_note_carries_real_csid():
    state = _make_state("buyer_completes", items=[{**MILK, "quantity": 2}])
    MerchantAgent().run(state)
    assert state.status == "ready_for_completion"
    note = _transaction_state_note(state)
    assert state.checkout_session_id in note
    assert "ready_for_completion" in note
    assert "requested_quantity: 2" in note
    assert "YOU CANNOT REQUEST PAYMENT" in note


# ---------------------------------------------------------------- T5 decline guardrail


def _live_state_from(client, monkeypatch=None):
    body = _start(client, "buyer_completes", items=[{**MILK, "quantity": 1}])
    assert body["status"] == "ready_for_completion"
    state = _make_state("buyer_completes", items=[{**MILK, "quantity": 1}])
    state.checkout_session_id = body["checkout_session"]["checkout_session_id"]
    state.llm_history.append({"role": "user", "content": "ctx"})
    return state, body["checkout_session"]["checkout_session_id"]


def test_decline_with_live_checkout_held_once_then_honored(client, monkeypatch):
    state, csid = _live_state_from(client)
    script = iter([
        ("finish", {"outcome": "declined", "reason_code": "no_matching_product", "message": "m"}, "r1", None),
        ("finish", {"outcome": "declined", "reason_code": "explicit_decline", "message": "sure"}, "r2", None),
    ])
    monkeypatch.setattr(loop_mod, "_next_action", lambda st: next(script))

    MerchantAgent().run(state)

    assert state.status == "declined"
    assert state.outcome["reason_code"] == "explicit_decline"
    actions = [a for a, _, _ in _audit_payloads(state.agent_session_id)]
    assert "agent_decline_hold" in actions
    # hold consumed exactly one extra turn, second decision terminated: nothing paid
    assert not any(a == "agent_request_completion" for a in actions)
    # the underlying basket remains live and buyer-completable
    cs = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    assert cs["status"] == "requires_payment"


def test_decline_hold_respects_iteration_budget(client, monkeypatch):
    state, _csid = _live_state_from(client)
    script = iter([
        ("finish", {"outcome": "declined", "reason_code": "no_matching_product", "message": "m"}, "r", None),
    ])
    monkeypatch.setattr(loop_mod, "_next_action", lambda st: next(script))
    monkeypatch.setattr(settings, "ma_max_iterations", 1)

    MerchantAgent().run(state)

    assert state.status == "unable_to_fulfill"
    assert state.outcome["reason_code"] == "merchant_agent_unable_to_fulfill"
    assert client.get("/audit_logs/verify").json()["intact"] is True


def test_wrong_mode_ready_for_completion_held_then_request_completion(client, monkeypatch):
    """Mirror of the decline hold: in agent_completed mode a premature
    finish(ready_for_completion) is corrected once toward request_completion."""
    body = _start(client, "agent_completed", items=[{**MILK, "quantity": 1}])
    # force the wrong-mode finish by intercepting before completion would run:
    state = _make_state("agent_completed", items=[{**MILK, "quantity": 1}])
    assert body["status"] in ("completed", "awaiting_payment_authorization", "ready_for_completion")
    csid = body["checkout_session"]["checkout_session_id"]

    # Build a live-basket state whose scripted brain mis-terminates ready_for_completion.
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        if cs.status != "requires_payment":
            cs.status = "requires_payment"  # park it again (sim may have completed it)
    state.checkout_session_id = csid
    state.llm_history.append({"role": "user", "content": "ctx"})
    script = iter([
        ("finish", {"outcome": "ready_for_completion", "reason_code": "confused", "message": "hand off"}, "r1", None),
        ("request_completion", {}, "executing gated payment", "tc2"),
        ("finish", {"outcome": "completed", "reason_code": "fulfilled_after_gated_completion", "message": "paid"}, "r2", None),
    ])
    monkeypatch.setattr(loop_mod, "_next_action", lambda st: next(script))

    MerchantAgent().run(state)

    actions = [a for a, _, _ in _audit_payloads(state.agent_session_id)]
    assert "agent_mode_hold" in actions
    assert any(a == "agent_request_completion" for a in actions)
    assert state.status == "completed"


def test_mode_hold_honors_second_decision_if_model_insists(client, monkeypatch):
    from fastapi.testclient import TestClient as _TC  # noqa: F401

    state = _make_state("agent_completed", items=[{**MILK, "quantity": 1}])
    # create a live checkout through the normal tool path first
    agent_tools.execute_tool("create_checkout", {"items": [MILK]}, state)
    assert state.checkout_session_id
    state.llm_history.append({"role": "user", "content": "ctx"})
    script = iter([
        ("finish", {"outcome": "ready_for_completion", "reason_code": "insist", "message": "hand off"}, "r1", None),
        ("finish", {"outcome": "ready_for_completion", "reason_code": "explicit_handoff", "message": "sure"}, "r2", None),
    ])
    monkeypatch.setattr(loop_mod, "_next_action", lambda st: next(script))

    MerchantAgent().run(state)

    assert state.status == "ready_for_completion"
    assert state.outcome["reason_code"] == "explicit_handoff"
    assert "agent_mode_hold" in [a for a, _, _ in _audit_payloads(state.agent_session_id)]


# ------------------------------------------------- anti-silent-substitution


def test_silent_substitution_blocked_without_proposal():
    """F-new: model must never swap an unrequested pack into a basket —
    deterministic block, corrective message, zero checkout created."""
    state = _make_state("agent_completed", items=[{**MILK, "quantity": 1}])
    agent_tools.execute_tool("check_inventory", {"pack_id": MILK["pack_id"]}, state)

    res = agent_tools.execute_tool("create_checkout", {"items": [{**SAFFRON, "quantity": 1}]}, state)

    assert res["success"] is False
    assert res["error"]["code"] == "INVALID_REQUEST"
    assert "SILENT_SUBSTITUTION_BLOCKED" in res["error"]["message"]
    assert res["error"]["details"]["blocked_packs"] == ["pack_saffron_5g"]
    assert "request_confirmation" in res["error"]["message"]
    assert state.checkout_session_id is None
    assert client_verify_intact()


def test_accepted_proposal_pack_passes_substitution_guard():
    state = _make_state("agent_completed", items=[dict(MILK_1L)])
    state.fallback_ctx["accepted_proposal_packs"] = [MILK["pack_id"]]
    db = SessionLocal()
    try:
        res = agent_tools.execute_tool("create_checkout", {"items": [{**MILK, "quantity": 2}]}, state)
        assert res["success"], res
        assert state.checkout_session_id
    finally:
        db.close()


def test_browse_flow_without_known_intent_unaffected():
    state = _make_state("agent_completed")
    db = SessionLocal()
    try:
        res = agent_tools.execute_tool("create_checkout", {"items": [{**SAFFRON, "quantity": 1}]}, state)
        assert res["success"], res
    finally:
        db.close()


def test_silent_quantity_reduction_blocked():
    """Model must not shrink the buyer's requested quantity to dodge the gate."""
    state = _make_state("agent_completed", items=[{**MILK, "quantity": 50}])
    agent_tools.execute_tool("check_inventory", {"pack_id": MILK["pack_id"]}, state)

    res = agent_tools.execute_tool("create_checkout", {"items": [{**MILK, "quantity": 10}]}, state)

    assert res["success"] is False
    assert res["error"]["code"] == "INVALID_REQUEST"
    assert "SILENT_MODIFICATION_BLOCKED" in res["error"]["message"]
    assert res["error"]["details"]["requested_quantity"] == 50
    assert res["error"]["details"]["basket_quantity"] == 10
    # the honest attempt at the real quantity still reaches (and fails) gating
    honest = agent_tools.execute_tool("create_checkout", {"items": [{**MILK, "quantity": 50}]}, state)
    assert honest["success"] is False
    assert honest["error"]["code"] == "DELEGATION_SCOPE_VIOLATION"


def test_exact_requested_quantity_passes_guard():
    state = _make_state("agent_completed", items=[{**MILK, "quantity": 2}])
    agent_tools.execute_tool("check_inventory", {"pack_id": MILK["pack_id"]}, state)
    db = SessionLocal()
    try:
        res = agent_tools.execute_tool("create_checkout", {"items": [{**MILK, "quantity": 2}]}, state)
        assert res["success"], res
    finally:
        db.close()


def test_find_alternative_keeps_explicit_buyer_quantities():
    """A quantity the buyer actually typed (e.g. 'buy 6 eggs') is never clamped."""
    state = _make_state("agent_completed")
    state.buyer_request = "buy 6 eggs"
    db = SessionLocal()
    try:
        res = agent_tools.execute_tool(
            "find_alternative",
            {"product_id": "prod_eggs_farm", "pack_id": "pack_eggs_6", "quantity": 6},
            state,
        )
        assert res["success"], res
        assert res["result"]["requested"]["requested_quantity"] == 6
        assert "quantity_note" not in res["result"]
    finally:
        db.close()


def test_request_confirmation_rejects_doomed_quantities():
    """Proposals that the deterministic gate is guaranteed to reject never
    reach the buyer."""
    state = _make_state("agent_completed", items=[{**MILK, "quantity": 50}])
    res = agent_tools.execute_tool(
        "request_confirmation",
        {
            "reason_code": "requested_pack_unavailable",
            "original": dict(MILK),
            "proposal": {**MILK, "quantity": 50},
            "message": "take 50?",
        },
        state,
    )
    assert res["success"] is False
    assert "max_quantity_per_item=10" in res["error"]["message"]
    assert state.pending_proposal is None

    ok = agent_tools.execute_tool(
        "request_confirmation",
        {
            "reason_code": "requested_pack_unavailable",
            "original": dict(MILK),
            "proposal": {**MILK, "quantity": 10},
            "message": "take 10?",
        },
        state,
    )
    assert ok["success"], ok
    assert state.pending_proposal["proposal"]["quantity"] == 10


def client_verify_intact():
    from fastapi.testclient import TestClient

    from merchant_agent.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        return c.get("/audit_logs/verify").json()["intact"]


# ------------------------------------------- budget windows across pause/resume


def test_buyer_decision_grants_fresh_iteration_window(client):
    """A pre-pause dithered session must still complete after the buyer
    accepts: the resume leg gets its own window (tool caps stay global)."""
    from merchant_agent.agent.state import get_session_state
    from merchant_agent.config import settings as cfg

    _zero_stock(MILK_1L["pack_id"])
    body = _start(client, "agent_completed", request="Buy 1L Amul milk")
    assert body["status"] == "awaiting_buyer_confirmation"

    st = get_session_state(body["agent_session_id"])
    assert st is not None
    st.iteration_count = cfg.ma_max_iterations  # simulate a fully-dithered first leg

    final = client.post(
        f"/agent_sessions/{body['agent_session_id']}/decision",
        json={"decision": "accept", "note": "fits"},
    ).json()

    assert final["status"] == "completed", final
    assert final["outcome"]["payment"]["payment_id"]


def test_budget_windows_are_hard_capped(client, monkeypatch):
    from merchant_agent.agent.state import get_session_state
    from merchant_agent.config import settings as cfg

    _zero_stock(MILK_1L["pack_id"])
    body = _start(client, "agent_completed", request="Buy 1L Amul milk")
    st = get_session_state(body["agent_session_id"])
    st.fallback_ctx["budget_windows"] = 99  # attempted abuse

    monkeypatch.setattr(cfg, "ma_max_iterations", 2)
    st.iteration_count = 0
    # even with the cap, runaway loops die safely:
    script = iter([
        ("run_gating", {}, "thinking", None),
    ])
    monkeypatch.setattr(loop_mod, "_next_action", lambda s: next(script))

    MerchantAgent().run(st)
    assert st.status in ("unable_to_fulfill", "awaiting_buyer_confirmation")
    assert not any(
        e[0] == "payment_success" for e in _audit_payloads(st.agent_session_id)
    )


# ---------------------------------------------------------------- payment truth guard


def test_finish_completed_without_payment_held_then_downgraded(client, monkeypatch):
    """A model claiming 'completed' with no gateway-confirmed payment is held
    once, then downgraded to the honest handoff. No payment events may exist."""
    state = _make_state("agent_completed", items=[{**MILK, "quantity": 1}])
    agent_tools.execute_tool("create_checkout", {"items": [MILK]}, state)
    assert state.checkout_session_id
    state.llm_history.append({"role": "user", "content": "ctx"})
    script = iter([
        ("finish", {"outcome": "completed", "reason_code": "fulfilled_as_requested", "message": "paid!"}, "r1", None),
        ("finish", {"outcome": "completed", "reason_code": "fulfilled_as_requested", "message": "really paid"}, "r2", None),
    ])
    monkeypatch.setattr(loop_mod, "_next_action", lambda st: next(script))

    MerchantAgent().run(state)

    # downgraded: never terminal-completed without money moving
    assert state.status == "ready_for_completion"
    assert state.outcome["reason_code"] == "unverified_completion_downgraded"
    events = _audit_payloads(state.agent_session_id)
    assert any(a == "agent_unpaid_completed_hold" for a, _, _ in events)
    assert not any(e[0] == "payment_success" for e in events)

    cs = client.get(f"/checkout_sessions/{state.checkout_session_id}").json()["checkout_session"]
    assert cs["status"] == "requires_payment"  # basket intact and completable


def test_finish_completed_accepted_only_after_real_payment(client, monkeypatch):
    state = _make_state("agent_completed", items=[{**MILK, "quantity": 1}])
    agent_tools.execute_tool("create_checkout", {"items": [MILK]}, state)
    csid = state.checkout_session_id

    # gateway-confirmed completion (sim capture) recorded in the DB
    from merchant_agent.models.entities import CheckoutSession

    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        cs.status = "completed"

    state.llm_history.append({"role": "user", "content": "ctx"})
    script = iter([
        ("finish", {"outcome": "completed", "reason_code": "fulfilled_as_requested", "message": "ok"}, "r", None),
    ])
    monkeypatch.setattr(loop_mod, "_next_action", lambda st: next(script))

    MerchantAgent().run(state)

    assert state.status == "completed"
    assert state.outcome["reason_code"] == "fulfilled_as_requested"
    assert not any(
        a == "agent_unpaid_completed_hold" for a, _, _ in _audit_payloads(state.agent_session_id)
    )


# ---------------------------------------------------------------- T6/T7 tool semantics


def test_find_alternative_reports_quantity_drops():
    state = _make_state("agent_completed", items=[{**MILK, "quantity": 40}])
    agent_tools.execute_tool("check_inventory", {"pack_id": MILK["pack_id"]}, state)
    db = SessionLocal()
    try:
        res = agent_tools.execute_tool(
            "find_alternative",
            {"product_id": MILK["product_id"], "pack_id": MILK["pack_id"], "quantity": 40},
            state,
        )
        assert res["success"], res
        body = res["result"]
        assert body["count"] == 0
        assert body["requested"]["requested_quantity"] == 40
        assert body["drop_summary"]["quantity_over_limit"] >= 1
        assert "max_quantity_per_item=10" in body["note"]

        ok = agent_tools.execute_tool(
            "find_alternative",
            {"product_id": MILK["product_id"], "pack_id": MILK["pack_id"], "quantity": 1},
            state,
        )
        assert ok["result"]["count"] >= 1
        assert ok["result"]["note"] == ""
    finally:
        db.close()


def test_check_inventory_semantics_are_explicit():
    state = _make_state()
    db = SessionLocal()
    try:
        res = agent_tools.execute_tool("check_inventory", {"pack_id": MILK["pack_id"]}, state)
        assert res["success"], res
        body = res["result"]
        assert body["available"] == body["available_quantity"] >= 0
        assert body["availability"] in ("in_stock", "out_of_stock")
        assert "NOT" in body["note"]
    finally:
        db.close()


# ---------------------------------------------------------------- T8 gating authority


def test_spend_limit_rejection_through_agent_path(client):
    state = _make_state("agent_completed", items=[{**SAFFRON, "quantity": 1}], delegation=RESTRICTED)
    MerchantAgent().run(state)

    assert state.status == "declined"
    assert state.outcome["reason_code"] == "policy_declined"
    events = _audit_payloads(state.agent_session_id)
    assert any(a == "agent_create_checkout" and d == "declined" for a, d, _ in events)
    assert not any(a == "agent_request_completion" for a, _, _ in events)
    assert client.get("/audit_logs/verify").json()["intact"] is True


# ---------------------------------------------------------------- T9 pending ≠ success


def test_payment_pending_surfaces_as_non_success(client, monkeypatch):
    from merchant_agent.services import payment_service as ps
    from merchant_agent.services.payment_service import PaymentOutcome

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


# ------------------------------------------- alternative flow keeps server id authoritative


def test_alternative_accept_flow_keeps_server_csid_authoritative(client):
    _zero_stock(MILK_1L["pack_id"])
    body = _start(
        client,
        "agent_completed",
        request="Buy 1L Amul milk",
    )
    assert body["status"] == "awaiting_buyer_confirmation"
    proposal_pack = body["proposal"]["proposal"]["pack_id"]
    original_pack = body["proposal"]["original"]["pack_id"]
    assert original_pack == MILK_1L["pack_id"]
    assert proposal_pack != original_pack

    final = client.post(
        f"/agent_sessions/{body['agent_session_id']}/decision",
        json={"decision": "accept", "note": "fits intent"},
    ).json()

    assert final["status"] == "completed"
    paid_csid = final["outcome"]["checkout_session_id"]
    assert paid_csid == final["checkout_session"]["checkout_session_id"]
    assert final["outcome"]["payment"]["payment_id"]
