"""Workstream A — telemetry safety + exposure tests (hermetic).

Verifies that the buyer timing instrumentation:
  1. records structured per-stage timing for a real /chat turn, and
  2. never leaks sensitive material (prompts, messages, amounts, ids, keys).
Runs fully offline via a fake LLM and an in-process merchant routed over ASGI.
"""
import pytest
from fastapi.testclient import TestClient

from buyer_service.timing import recorder, stage_timer
import buyer_service.agent as agent_mod
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

SENSITIVE_KEYS = {
    "prompt", "message", "content", "instruction", "args", "arguments",
    "amount", "price", "total", "card", "pan", "cvv", "key", "token",
    "secret", "password", "conversation_id", "checkout_session_id",
    "payment_id", "agent_session_id", "question", "text", "request",
}


@pytest.fixture
def routed_merchant():
    """Route the buyer's MerchantClient HTTP calls to the in-process merchant."""
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


def _assert_no_sensitive(entry, where):
    for k in entry:
        assert k not in SENSITIVE_KEYS, f"sensitive key leaked at {where}: {k}"
        if isinstance(entry[k], str) and entry[k]:
            assert len(entry[k]) < 200, f"suspiciously long value at {where}.{k}"


def test_recorder_records_ok_and_error():
    recorder.begin_turn("t1")
    with stage_timer("probe_ok", tool="x") as st:
        st.outcome = "ok"
    recorder.record("probe_error", 0.0, 0.005, outcome="error", tool="y")
    stages = recorder.stages("t1")
    assert [s["stage"] for s in stages] == ["probe_ok", "probe_error"]
    for s in stages:
        _assert_no_sensitive(s, "stage")
        assert s["elapsed_ms"] >= 0


def test_chat_turn_emits_timings_without_secrets(routed_merchant, buy_client):
    script = [
        msg_toolcall("discover_products", {"query": "milk"}),
        msg_content("Here's what I found."),
    ]
    with FakeDriver(script) as drv:
        with buy_client.stream("POST", "/chat", json={"message": "show me milk"}) as r:
            events = drain_sse(r)

    types = [e.get("type") for e in events if isinstance(e, dict)]
    assert "conversation_started" in types
    assert any(t in ("tool", "found") for t in types), types

    conv_id = events[0]["conversation_id"]
    timings_resp = buy_client.get(f"/conversations/{conv_id}/timings")
    assert timings_resp.status_code == 200
    payload = timings_resp.json()
    assert payload["conversation_id"] == conv_id
    stages = payload["timings"]
    assert isinstance(stages, list) and stages
    for s in stages:
        _assert_no_sensitive(s, "timings")
        assert s["elapsed_ms"] >= 0


def test_discovery_uses_skip_llm_parse(routed_merchant):
    """Buyer-driven discovery must not trigger the merchant LLM query parser."""
    from buyer_service.tools import MerchantClient
    mc = MerchantClient()
    status, payload = mc.discover("milk bread eggs", skip_llm_parse=True)
    assert status == 200
    assert payload.get("parser") == "keyword-fallback"
