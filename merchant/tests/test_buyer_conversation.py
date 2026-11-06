"""Workstream B — hermetic conversational regression suite (fake LLM + mocked
merchant). Exercises the buyer agent + deterministic-first router across 10
scenarios: information, purchase, modification, cancellation, substitution,
payment-status, completed-order follow-up, failed-order follow-up, ambiguous,
and unrelated-during-checkout.

All offline: the buyer LLM is scripted and the merchant is routed in-process.
"""
import pytest
from fastapi.testclient import TestClient

from buyer_service.main import app as buyer_app
from merchant.main import app as merchant_app

from merchant.tests._buyer_harness import (
    install,
    uninstall,
    FakeDriver,
    msg_content,
    msg_toolcall,
    drain_sse,
)


@pytest.fixture
def routed_merchant():
    tclient = TestClient(merchant_app)
    install(tclient)
    try:
        yield tclient
    finally:
        uninstall()


@pytest.fixture
def buy_client(routed_merchant):
    with TestClient(buyer_app) as c:
        yield c


def chat(buy_client, message, conversation_id=None):
    with buy_client.stream(
        "POST", "/chat", json={"message": message, "conversation_id": conversation_id}
    ) as r:
        return drain_sse(r)


def conv_id(events):
    for e in events:
        if e.get("type") == "conversation_started":
            return e["conversation_id"]
    return None


def types(events):
    return [e.get("type") for e in events if isinstance(e, dict)]


# ---------------------------------------------------------------- 1 info

def test_information_browse(routed_merchant, buy_client):
    script = [
        msg_toolcall("discover_products", {"query": "milk"}),
        msg_content("Here are some milk options for you."),
    ]
    with FakeDriver(script) as drv:
        events = chat(buy_client, "show me what milks you have")
    t = types(events)
    assert "tool" in t and "found" in t and "message" in t
    found = [e for e in events if e.get("type") == "found"]
    assert found and found[0].get("candidates")


# ------------------------------------------------------------- 2 purchase

def test_purchase_custom_basket_pauses_for_bill(routed_merchant, buy_client):
    script = [
        msg_toolcall("discover_products", {"query": "amul milk"}),
        msg_toolcall("create_checkout_session", {
            "items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}]
        }),
    ]
    with FakeDriver(script) as drv:
        events = chat(buy_client, "buy 2 amul milk")
    t = types(events)
    assert "bill" in t
    bill = [e for e in events if e.get("type") == "bill"]
    assert bill and bill[0]["totals"]["total"] > 0


# ------------------------------------------------------------- 3 modify

def test_modification_reorder_replaces_bill(routed_merchant, buy_client):
    script = [
        msg_toolcall("discover_products", {"query": "milk"}),
        msg_toolcall("create_checkout_session", {
            "items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}]
        }),
    ]
    with FakeDriver(script) as drv:
        e1 = chat(buy_client, "add 2 amul milk")
    cid = conv_id(e1)
    assert "bill" in types(e1)

    # Reject the first bill (cancels it), then a revised order creates a new bill.
    with buy_client.stream("POST", f"/conversations/{cid}/decision", json={"approved": False}) as r:
        drain_sse(r)

    script2 = [
        msg_toolcall("discover_products", {"query": "bread eggs"}),
        msg_toolcall("create_checkout_session", {
            "items": [{"product_id": "prod_bread_massimo", "pack_id": "pack_bread_massimo_400", "quantity": 1}]
        }),
    ]
    with FakeDriver(script2) as drv2:
        e2_events = chat(buy_client, "actually lets get bread and eggs", conversation_id=cid)
    assert "bill" in types(e2_events)
    found = [e for e in e2_events if e.get("type") == "bill"]
    assert found


# ------------------------------------------------------------- 4 cancel

def test_cancel_routes_deterministically_without_llm(routed_merchant, buy_client):
    script = [
        msg_toolcall("discover_products", {"query": "amul milk"}),
        msg_toolcall("create_checkout_session", {
            "items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}]
        }),
    ]
    with FakeDriver(script) as drv:
        e1 = chat(buy_client, "buy 2 amul milk")
    cid = conv_id(e1)
    llm_before = drv.call_count
    assert "bill" in types(e1)

    # Now cancel — must be handled by the deterministic router (no new LLM call).
    with FakeDriver([]) as drv2:
        e2 = chat(buy_client, "cancel my order", conversation_id=cid)
    assert drv2.call_count == 0
    t = types(e2)
    assert "status" in t and "message" in t
    st = [e for e in e2 if e.get("type") == "status"]
    assert st and st[0]["status"] == "cancelled"
    text = " ".join(e.get("content", "") for e in e2 if e.get("type") == "message")
    assert "charged" in text.lower()


# ------------------------------------------------------------- 5 substitute

def test_substitution_proposed_when_stock_unavailable(routed_merchant, buy_client, clean_state_reset):
    # Create a valid bill first (stock available at reservation time).
    script = [
        msg_toolcall("discover_products", {"query": "amul milk"}),
        msg_toolcall("create_checkout_session", {
            "items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}]
        }),
    ]
    with FakeDriver(script) as drv:
        events = chat(buy_client, "buy 2 amul milk")
    cid = conv_id(events)
    assert "bill" in types(events)

    # Simulate the stock-race: the item goes out of stock between bill creation
    # and the user approving completion, so the deterministic substitute flow
    # engages at completion time (no LLM decides the replacement).
    from merchant.persistence.db import mutate
    from merchant.persistence.models.entities import Pack
    with mutate() as db:
        db.get(Pack, "pack_milk_amul_500").stock_actual = 0

    with buy_client.stream("POST", f"/conversations/{cid}/decision", json={"approved": True}) as r:
        e2 = drain_sse(r)
    t = types(e2)
    assert "substitute_proposal" in t
    prop = [e for e in e2 if e.get("type") == "substitute_proposal"]
    assert prop and prop[0].get("options")


# --------------------------------------------------------- 6 payment status

def test_status_reports_active_unpaid_checkout(routed_merchant, buy_client):
    script = [
        msg_toolcall("discover_products", {"query": "milk"}),
        msg_toolcall("create_checkout_session", {
            "items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]
        }),
    ]
    with FakeDriver(script) as drv:
        e1 = chat(buy_client, "i want amul milk")
    cid = conv_id(e1)
    with FakeDriver([]) as drv2:
        e2 = chat(buy_client, "what's my order status", conversation_id=cid)
    assert drv2.call_count == 0
    text = " ".join(e.get("content", "") for e in e2 if e.get("type") == "message")
    assert "approval" in text.lower() or "not" in text.lower()


# ------------------------------------------- 7 completed-order follow-up

def test_completed_order_followup(routed_merchant, buy_client):
    from buyer_service.conversations import new_conversation
    conv = new_conversation()
    conv.llm_history.append({
        "role": "system",
        "content": '{"state": {"event": "order_completed", "total": 95, "checkout_session_id": "c1"}}',
    })
    from merchant.tests._buyer_harness import FakeDriver as _FD
    with FakeDriver([]) as drv:
        e2 = chat(buy_client, "where is my order", conversation_id=conv.id)
    assert drv.call_count == 0
    text = " ".join(e.get("content", "") for e in e2 if e.get("type") == "message")
    assert "paid" in text.lower() or "completed" in text.lower()


# --------------------------------------------- 8 failed/declined follow-up

def test_declined_order_followup(routed_merchant, buy_client):
    from buyer_service.conversations import new_conversation
    conv = new_conversation()
    conv.llm_history.append({
        "role": "system",
        "content": '{"state": {"event": "order_declined", "code": "SPEND_LIMIT_EXCEEDED"}}',
    })
    with FakeDriver([]) as drv:
        e = chat(buy_client, "what happened to my order", conversation_id=conv.id)
    assert drv.call_count == 0
    text = " ".join(x.get("content", "") for x in e if x.get("type") == "message")
    assert "declined" in text.lower() or "charged" in text.lower()


# ----------------------------------------------------------- 9 ambiguous

def test_ambiguous_uses_llm(routed_merchant, buy_client):
    script = [
        msg_toolcall("discover_products", {"query": "dinner"}),
        msg_content("How about chicken and rice for dinner?"),
    ]
    with FakeDriver(script) as drv:
        events = chat(buy_client, "i want something for dinner")
    assert drv.call_count > 0
    assert "message" in types(events)


# -------------------------------------------- 10 unrelated (grounded by state)

def test_unrelated_conversation_stays_grounded_in_state(routed_merchant, buy_client):
    from buyer_service.conversations import new_conversation
    conv = new_conversation()
    conv.llm_history.append({
        "role": "system",
        "content": '{"state": {"event": "order_completed", "total": 95, "checkout_session_id": "c1"}}',
    })
    # Unrelated chit-chat -> falls through to the LLM (not routed, no pending
    # bill), but every turn is bound to the injected CURRENT STATE block so the
    # model cannot reconstruct or invent order/payment facts.
    script2 = [msg_content("It's sunny here. Happy to help with your next order."),
               msg_content("It's sunny here. Happy to help with your next order.")]
    with FakeDriver(script2) as drv2:
        e2 = chat(buy_client, "what's the weather like outside", conversation_id=conv.id)
    assert drv2.call_count >= 1
    msgs = drv2.seen_messages or []
    state_lines = [m.get("content") for m in msgs if m.get("role") == "system"]
    assert any("CURRENT STATE" in (c or "") for c in state_lines)
    assert any("COMPLETED" in (c or "") for c in state_lines)
    assert "message" in types(e2)


# -------------------------------- C9b authorized-awaiting-capture truthfulness

def _bill_then_decision(buy_client, monkeypatch, complete_payload, reconcile_payload):
    import buyer_service.tools as bt
    script = [
        msg_toolcall("discover_products", {"query": "milk"}),
        msg_toolcall("create_checkout_session", {
            "items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]
        }),
    ]
    with FakeDriver(script) as drv:
        e1 = chat(buy_client, "buy amul milk please")
    cid = conv_id(e1)
    assert "bill" in types(e1)

    calls = {}

    def fake_complete(self, *a, **k):
        calls["complete"] = True
        return 200, {"checkout_session": complete_payload}

    def fake_reconcile(self, *a, **k):
        calls["reconcile"] = True
        return 200, {"checkout_session": reconcile_payload}

    monkeypatch.setattr(bt.MerchantClient, "complete_checkout", fake_complete)
    monkeypatch.setattr(bt.MerchantClient, "reconcile", fake_reconcile)
    with buy_client.stream("POST", f"/conversations/{cid}/decision", json={"approved": True}) as r:
        return cid, calls, drain_sse(r)


def test_payment_pending_with_payment_object_does_not_claim_unresolved(routed_merchant, buy_client, monkeypatch):
    """A payment_pending session that already has a payment object (authorized,
    awaiting capture) must NEVER be narrated as 'none of the message
    connection dropped / nothing was charged / no order exists'."""
    pay = {"payment_id": "pay_authorized_1", "order_id": "order_authorized_1"}
    pending = {
        "checkout_session_id": "cs_auth",
        "status": "payment_pending",
        "payment": pay,
        "totals": {"total": 95},
    }
    _, calls, e2 = _bill_then_decision(buy_client, monkeypatch, pending, pending)
    assert calls.get("reconcile")
    text = " ".join(x.get("content", "") for x in e2 if x.get("type") == "message")
    assert "awaiting" in text.lower() or "authorized" in text.lower()
    assert "nothing was charged" not in text.lower()
    assert "connection to the payment gateway dropped" not in text.lower()
    assert "no order exists" not in text.lower()


def test_payment_pending_with_order_no_payment_tells_auto_settle_story(routed_merchant, buy_client, monkeypatch):
    """A payment_pending session with a gateway ORDER but no payment object yet
    is the async auto-pay window: the recurring debit was created and is in
    flight at the bank ("amount yet to be authenticated"), and the background
    reconcile worker settles it automatically. The narration must tell that
    story — NEVER claim nothing happened / no order exists."""
    pending_no_pay = {
        "checkout_session_id": "cs_pend",
        "status": "payment_pending",
        "payment": {"order_id": "order_pending_1"},
        "totals": {"total": 95},
    }
    _, calls, e2 = _bill_then_decision(buy_client, monkeypatch, pending_no_pay, pending_no_pay)
    assert calls.get("reconcile")
    text = " ".join(x.get("content", "") for x in e2 if x.get("type") == "message")
    tl = text.lower()
    assert "recurring" in tl or "auto-pay" in tl
    assert "automatically" in tl
    assert "receipt" in tl
    assert "nothing was charged" not in tl
    assert "no order exists" not in tl
    assert "connection dropped" not in tl


def test_payment_pending_without_order_object_stays_truthful(routed_merchant, buy_client, monkeypatch):
    """A payment_pending session with NO gateway order and NO payment object is
    the honest nothing-created story: gateway initiation failed before anything
    was created, so nothing was charged."""
    pending_created_fail = {
        "checkout_session_id": "cs_pend",
        "status": "payment_pending",
        "payment": {},
        "totals": {"total": 95},
    }
    _, calls, e2 = _bill_then_decision(buy_client, monkeypatch, pending_created_fail, pending_created_fail)
    assert calls.get("reconcile")
    text = " ".join(x.get("content", "") for x in e2 if x.get("type") == "message")
    tl = text.lower()
    assert "nothing was charged" in tl
    assert "awaiting" not in tl
    assert "automatically" not in tl
    assert "no order exists" in tl


# ----------------------------------- C9 gateway-unreachable truthfulness

def test_gateway_unreachable_is_truthful(routed_merchant, buy_client, monkeypatch):
    """When the payment gateway cannot be reached at completion, the agent must
    say nothing was charged and no order placed — never claim success."""
    script = [
        msg_toolcall("discover_products", {"query": "milk"}),
        msg_toolcall("create_checkout_session", {
            "items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]
        }),
    ]
    with FakeDriver(script) as drv:
        e1 = chat(buy_client, "buy amul milk please")
    cid = conv_id(e1)
    assert "bill" in types(e1)

    # Simulate the gateway being unreachable at completion time (transport error).
    import buyer_service.tools as bt

    def boom(self, *a, **k):
        raise ConnectionError("gateway timeout")

    monkeypatch.setattr(bt.MerchantClient, "complete_checkout", boom)
    with buy_client.stream("POST", f"/conversations/{cid}/decision", json={"approved": True}) as r:
        e2 = drain_sse(r)
    text = " ".join(x.get("content", "") for x in e2 if x.get("type") == "message")
    assert "nothing was charged" in text.lower() or ("charged" in text.lower() and "no order" in text.lower())
    assert "paid" not in text.lower()


@pytest.fixture
def clean_state_reset():
    from merchant.persistence.db import mutate
    from merchant.persistence.models.entities import (Reservation, IdempotencyKey, CheckoutSession, AuditEvent)
    yield
    with mutate() as db:
        for model in (Reservation, IdempotencyKey, CheckoutSession, AuditEvent):
            db.query(model).delete()
