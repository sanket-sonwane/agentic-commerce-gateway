"""Regression tests for the LATEST audit (BUYER_AGENT_AUDIT.md) P0 findings:

- H1/E1 — deterministic outcomes invisible to the LLM: approval/cancel/complete
          must be written back into conv.llm_history so follow-ups are answered
          from ground truth.
- H2/E2 — create_checkout_session is not idempotent client-side: the buyer must
          send a stable basket-derived Idempotency-Key and auto-cancel a prior
          live session when the basket changes.
- H3/E3 — buyer complete_checkout used a fresh idempotency key per call (double
          -charge exposure): it must reuse a stable per-bill key so a buyer/user
          retry replays instead of re-running the money ladder.

Money safety (C-1/C-2/C-3) is untouched; these changes are buyer-client /
conversation-context only. The adversarial H3 test proves a replayed complete
key cannot double-charge.
"""
from unittest import mock

from buyer_service.agent import BuyerAgent, _stable_basket_key
from buyer_service.conversations import Conversation
from buyer_service.tools import MerchantClient
from merchant.tests.conftest import complete, make_checkout


# ---------------------------------------------------------------------------
# H1 — deterministic outcomes written back into llm_history
# ---------------------------------------------------------------------------


def _conv() -> Conversation:
    return Conversation(id="conv_test")


def test_h1_cancel_writes_state_note():
    agent = BuyerAgent(merchant=mock.Mock())
    agent.merchant.cancel_checkout.return_value = (
        200,
        {"checkout_session": {"checkout_session_id": "cs_c", "status": "cancelled"}},
    )
    conv = _conv()
    conv.pending_approval = {"checkout_session_id": "cs_c", "kind": "initial"}
    outs = list(agent.resume_after_decision(conv, approved=False))
    assert any(o["type"] == "status" for o in outs)
    notes = [m for m in conv.llm_history if m.get("role") == "system"]
    assert notes, "no deterministic state note written back on cancel"
    payload = notes[-1]["content"]
    assert "order_cancelled" in payload
    assert "cs_c" in payload


def test_h1_complete_writes_state_note():
    agent = BuyerAgent(merchant=mock.Mock())
    agent.merchant.complete_checkout.return_value = (
        200,
        {
            "checkout_session": {
                "checkout_session_id": "cs_done",
                "status": "completed",
                "totals": {"total": 104.0},
                "payment": {"driver": "sim", "saved_card": None},
            }
        },
    )
    conv = _conv()
    conv.intent_mandate_id = "im_1"
    conv.pending_approval = {"checkout_session_id": "cs_done", "kind": "initial"}
    outs = list(agent.resume_after_decision(conv, approved=True))
    assert any(o["type"] == "message" and "Order placed" in o["content"] for o in outs)
    notes = [m for m in conv.llm_history if m.get("role") == "system"]
    assert notes, "no deterministic state note written back on completion"
    payload = notes[-1]["content"]
    assert "order_completed" in payload
    assert "cs_done" in payload


def test_h1_decline_writes_state_note():
    agent = BuyerAgent(merchant=mock.Mock())
    agent.merchant.complete_checkout.return_value = (
        409,
        {
            "error": {"code": "SPEND_LIMIT_EXCEEDED", "message": "over budget"},
            "checkout_session": {"checkout_session_id": "cs_x", "status": "declined"},
        },
    )
    conv = _conv()
    conv.pending_approval = {"checkout_session_id": "cs_x", "kind": "initial"}
    list(agent.resume_after_decision(conv, approved=True))
    notes = [m for m in conv.llm_history if m.get("role") == "system"]
    assert any("order_declined" in n["content"] for n in notes)


# ---------------------------------------------------------------------------
# H2 — stable create idempotency key + stale-session auto-cancel
# ---------------------------------------------------------------------------


def _li(a, b, n=1):
    return [{"product_id": a, "pack_id": b, "quantity": n}]


def test_h2_stable_key_is_order_insensitive_and_reused():
    k1 = _stable_basket_key(_li("p_milk", "pack_milk", 2) + _li("p_bread", "pack_bread", 1))
    k2 = _stable_basket_key(_li("p_bread", "pack_bread", 1) + _li("p_milk", "pack_milk", 2))
    assert k1 == k2
    assert k1 != _stable_basket_key(_li("p_milk", "pack_milk", 3))
    assert k1.startswith("create_")


def test_h2_create_sends_stable_idempotency_key():
    client = mock.Mock()
    client.create_checkout.return_value = (
        201,
        {
            "checkout_session": {
                "checkout_session_id": "cs_a",
                "status": "requires_payment",
                "line_items": [],
                "totals": {},
            }
        },
    )
    client.create_intent_mandate.return_value = (201, {"mandate_id": "im_x"})
    agent = BuyerAgent(merchant=client)
    conv = _conv()
    items = _li("p_milk", "pack_milk", 2)
    result = agent._execute_tool(conv, "create_checkout_session", {"items": items})
    assert result["created"] is True
    # the idempotency header must echo the basket-derived key (stable, not random)
    sent = client.create_checkout.call_args.kwargs["idempotency_key"]
    assert sent == _stable_basket_key(items)
    assert conv.last_create_key == sent


def test_h2_stale_session_cancelled_when_basket_changes():
    client = mock.Mock()
    client.create_checkout.return_value = (
        201,
        {
            "checkout_session": {
                "checkout_session_id": "cs_new",
                "status": "requires_payment",
                "line_items": [],
                "totals": {},
            }
        },
    )
    client.create_intent_mandate.return_value = (201, {"mandate_id": "im_x"})
    agent = BuyerAgent(merchant=client)
    conv = _conv()
    conv.active_session_id = "cs_stale"
    conv.last_create_key = _stable_basket_key(_li("p_milk", "pack_milk", 2))  # different basket
    agent._execute_tool(conv, "create_checkout_session", {"items": _li("p_bread", "pack_bread", 1)})
    client.cancel_checkout.assert_called_once_with("cs_stale")
    assert conv.active_session_id == "cs_new"


def test_h2_same_basket_is_not_cancelled_and_replays():
    client = mock.Mock()
    # replay: server returns the existing session with HTTP 200 (created=False)
    client.create_checkout.return_value = (
        200,
        {
            "checkout_session": {
                "checkout_session_id": "cs_same",
                "status": "requires_payment",
                "line_items": [],
                "totals": {},
            }
        },
    )
    client.create_intent_mandate.return_value = (201, {"mandate_id": "im_x"})
    agent = BuyerAgent(merchant=client)
    conv = _conv()
    conv.active_session_id = "cs_same"
    conv.last_create_key = _stable_basket_key(_li("p_milk", "pack_milk", 2))
    result = agent._execute_tool(conv, "create_checkout_session", {"items": _li("p_milk", "pack_milk", 2)})
    # same basket -> no cancel, recognized as the (replayed) active session
    client.cancel_checkout.assert_not_called()
    assert result["created"] is True
    assert conv.active_session_id == "cs_same"


# ---------------------------------------------------------------------------
# H3 — stable per-bill complete key + adversarial no-double-charge
# ---------------------------------------------------------------------------


def test_h3_complete_reuses_stable_per_session_key(monkeypatch):
    """The real MerchantClient.complete_checkout must send a stable per-bill
    Idempotency-Key (comp_{csid}), NOT a fresh random uuid per call, so a buyer
    retry of the same approved bill replays instead of re-running the ladder."""
    captured = []
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = {"checkout_session": {"status": "completed"}}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured.append(headers.get("Idempotency-Key") if headers else None)
        return resp

    monkeypatch.setattr("buyer_service.tools.httpx.post", _fake_post)
    client = MerchantClient()
    csid = "cs_abc"
    client.complete_checkout(csid, intent_mandate_id="im_1")  # retry #1 of same bill
    client.complete_checkout(csid, intent_mandate_id="im_1")  # retry #2 of same bill
    assert len(captured) == 2
    assert captured[0] == captured[1] == f"comp_{csid}"


def _count_success_audits(client, csid):
    ev = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
    return sum(1 for e in ev if e["action"] == "payment_success")


def test_h3_buyer_retry_never_double_charges(client, admin_headers):
    """Adversarial: the buyer now sends one stable per-bill key. Simulate a
    network-level retry of the SAME complete request (same key) — the merchant
    must replay the original result with a single payment, no double charge."""
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    key = f"comp_{csid}"  # the per-bill key the buyer client now derives

    def _stock():
        packs = client.get("/admin/packs", headers=admin_headers).json()["packs"]
        return {p["pack_id"]: p["stock_actual"] for p in packs}

    r1 = complete(client, csid, key=key)
    assert r1.status_code == 200, r1.text
    assert r1.json()["checkout_session"]["status"] == "completed"
    pay1 = r1.json()["checkout_session"]["payment"]["payment_id"]
    stock1 = _stock()
    success1 = _count_success_audits(client, csid)

    # retry the identical request (same key) — must be an idempotent replay
    r2 = complete(client, csid, key=key)
    assert r2.status_code == 200, r2.text
    assert r2.json()["checkout_session"]["status"] == "completed"
    pay2 = r2.json()["checkout_session"]["payment"]["payment_id"]
    stock2 = _stock()
    success2 = _count_success_audits(client, csid)

    # one and only one payment / stock consumption / success audit
    assert pay1 == pay2
    assert stock1 == stock2
    assert success2 == success1 == 1
