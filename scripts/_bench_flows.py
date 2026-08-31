"""Hermetic buyer-flow latency benchmark (no live LLM, no live merchant server).

Drives the real BuyerAgent.llm_loop with a scripted fake LLM against the real
merchant app wired in-process via httpx ASGITransport. Measures true
user-perceived latency for representative flows and reports a breakdown of
LLM calls / tool calls / discovery calls. This is a dev helper, not pytest.
"""
import json
import os
import sys
import time
from types import SimpleNamespace as F

os.environ.setdefault("PAYMENT_DRIVER", "sim")
os.environ.setdefault("ACG_TEST_ALLOW_SIM", "1")
os.environ.setdefault("RAZORPAY_KEY_ID", "")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "")
os.environ.setdefault("MA_LLM_BRAIN", "false")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import buyer_service.agent as agent_mod  # noqa: E402
from buyer_service.conversations import new_conversation  # noqa: E402

# ---- wire the buyer's MerchantClient to the in-process merchant app ----
from merchant.main import app as merchant_app  # noqa: E402

AGENT = agent_mod.BuyerAgent()

# Route the buyer's MerchantClient (which calls module-level httpx.post/put/get)
# through the in-process merchant app over ASGI, so the real protocol client
# code runs against the real merchant app without a live server or network.
import buyer_service.tools as _tools  # noqa: E402

from fastapi.testclient import TestClient as _TestClient  # noqa: E402

_TCLIENT = _TestClient(merchant_app, raise_server_exceptions=False)


def _asgi_call(method):
    def wrapped(*args, **kwargs):
        headers = kwargs.get("headers") or {}
        json_body = kwargs.get("json")
        url = args[0] if args else kwargs.get("url")
        # httpx paths like /checkout_sessions/{id} are already fully formed.
        req = {"headers": headers if headers else None, "json": json_body}
        if method == "POST":
            resp = _TCLIENT.post(url, **req)
        elif method == "PUT":
            resp = _TCLIENT.put(url, **req)
        else:
            resp = _TCLIENT.get(url)
        return _R(resp.status_code, _json_or_empty(resp))
    return wrapped


class _R:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


def _json_or_empty(resp):
    try:
        return resp.json()
    except Exception:
        return {}


_ORIG_POST = _tools.httpx.post
_ORIG_PUT = _tools.httpx.put
_ORIG_GET = _tools.httpx.get
_tools.httpx.post = _asgi_call("POST")
_tools.httpx.put = _asgi_call("PUT")
_tools.httpx.get = _asgi_call("GET")


_LLM_CALL_COUNT = {"n": 0}


class _FakeCompletions:
    def __init__(self, script):
        self.script = script

    def create(self, **kwargs):
        _LLM_CALL_COUNT["n"] += 1
        if not self.script:
            raise RuntimeError("fake script exhausted")
        return self.script.pop(0)


class _FakeChat:
    def __init__(self, script):
        self.completions = _FakeCompletions(script)


class _FakeOpenAI:
    def __init__(self, script):
        self.chat = _FakeChat(script)


def msg_content(content):
    return F(choices=[F(message=F(content=content, tool_calls=None))])


def msg_toolcall(name, args, call_id="call_x"):
    tc = F(id=call_id, function=F(name=name, arguments=json.dumps(args)))
    return F(choices=[F(message=F(content=None, tool_calls=[tc]))])


class FlowResult:
    def __init__(self, name):
        self.name = name
        self.total_ms = 0
        self.llm_calls = 0
        self.tool_calls = 0
        self.discovery_calls = 0
        self.events = []
        self.outcome = ""

    def summary(self):
        out = (
            f"{self.name:<24} total={self.total_ms/1000:6.2f}s  "
            f"llm={self.llm_calls}  tools={self.tool_calls}  "
            f"discovery={self.discovery_calls}  events={self.events}\n"
        )
        stages = getattr(self, "stages", None) or []
        if stages:
            breakdown = "    ".join(f"{s.get('stage')}={s.get('elapsed_ms', 0):.0f}ms" for s in stages)
            out += f"{'':24}  [timings] {breakdown}\n"
        return out


def run_flow(name, script, instruction, *, extra_events=False):
    """Drive llm_loop with a fake LLM script; count + time everything."""
    if extra_events:
        # 'other' tool toggles a second tool call after first (counts both)
        pass
    orig_client = agent_mod._client
    agent_mod._client = lambda: _FakeOpenAI(script)
    _LLM_CALL_COUNT["n"] = 0
    res = FlowResult(name)
    conv = new_conversation()
    try:
        # Reset merchant demo state (restore stock, clear transactions).
        try:
            from scripts.seed_catalog import main as _seed
            _seed()
        except Exception:
            pass
        t0 = time.perf_counter()
        for ev in AGENT.llm_loop(conv, instruction):
            if ev is agent_mod.BILL_PAUSE:
                break
            if not isinstance(ev, dict):
                continue
            res.events.append(ev.get("type"))
            if ev.get("type") == "tool":
                res.tool_calls += 1
                if ev.get("tool") == "discover_products":
                    res.discovery_calls += 1
            if ev.get("type") == "message" and isinstance(ev.get("content"), str):
                res.outcome = ev["content"][:80]
        res.total_ms = (time.perf_counter() - t0) * 1000
    finally:
        agent_mod._client = orig_client
    res.llm_calls = _LLM_CALL_COUNT["n"]
    # Capture measured stage timings from the thread-local recorder.
    from buyer_service.timing import recorder as _rec
    res.stages = _rec.stages(conv.id)
    return res


def main():
    from scripts.seed_catalog import main as seed
    seed()
    print(f"== Hermetic Buyer-Flow Latency Baseline (in-process merchant, fake LLM) ==")

    a_script = [
        msg_toolcall("discover_products", {"query": "milk"}, "a1"),
        msg_toolcall(
            "create_checkout_session",
            {"items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}]},
            "a2",
        ),
    ]
    ra = run_flow("FLOW A: simple purchase", list(a_script), "Buy 2 packs of Amul toned milk 500ml")
    print(ra.summary())

    b_script = [
        msg_toolcall("discover_products", {"query": "milk bread eggs"}, "b1"),
        msg_content("Here's what I found: Amul milk, bread and eggs are available. Would you like me to build a cart?"),
    ]
    rb = run_flow("FLOW B: info/ambiguous", list(b_script), "What milk do you have?")
    print(rb.summary())

    c_script = [
        msg_toolcall("discover_products", {"query": "chicken"}, "c1"),
        msg_toolcall(
            "create_checkout_session",
            {"items": [{"product_id": "prod_chicken_fresh", "pack_id": "pack_chicken_500", "quantity": 1}]},
            "c2",
        ),
    ]
    rc = run_flow("FLOW C: adaptive purchase", list(c_script), "order fresh chicken breast")
    print(rc.summary())

    print("(LLM call count = scripted choice-turns; see agent loop for nudges)")


if __name__ == "__main__":
    main()
