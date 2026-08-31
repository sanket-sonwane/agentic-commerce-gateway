"""Hermetic buyer harness helpers (fake LLM + in-process merchant over ASGI).

Lets pytest drive the real BuyerAgent against the real merchant app offline
(no live LLM, no live merchant server). Used by the conversational and
telemetry regression suites.
"""
import json
from types import SimpleNamespace as F

import httpx
from fastapi.testclient import TestClient

import buyer_service.agent as agent_mod
import buyer_service.tools as tools
from merchant.main import app as merchant_app


class _Expr:
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


def _route_to_merchant(url, headers=None, json_body=None, method="POST"):
    tclient = _TC
    req = {"headers": headers if headers else None, "json": json_body}
    if method == "POST":
        resp = tclient.post(url, **req)
    elif method == "PUT":
        resp = tclient.put(url, **req)
    else:
        resp = tclient.get(url)
    return _Expr(resp.status_code, _json_or_empty(resp))


_TC = None
_ORIG_POST = _ORIG_PUT = _ORIG_GET = None


def install(tclient: TestClient):
    """Route buyer MerchantClient HTTP calls through the given TestClient."""
    global _TC, _ORIG_POST, _ORIG_PUT, _ORIG_GET
    _TC = tclient
    if _ORIG_POST is None:
        _ORIG_POST = tools.httpx.post
        _ORIG_PUT = tools.httpx.put
        _ORIG_GET = tools.httpx.get
    tools.httpx.post = lambda *a, **k: _route_to_merchant(
        (a[0] if a else k.get("url")), k.get("headers"), k.get("json"), "POST"
    )
    tools.httpx.put = lambda *a, **k: _route_to_merchant(
        (a[0] if a else k.get("url")), k.get("headers"), k.get("json"), "PUT"
    )
    tools.httpx.get = lambda *a, **k: _route_to_merchant(
        (a[0] if a else k.get("url")), k.get("headers"), k.get("json"), "GET"
    )


def uninstall():
    global _ORIG_POST, _ORIG_PUT, _ORIG_GET
    if _ORIG_POST is not None:
        tools.httpx.post = _ORIG_POST
        tools.httpx.put = _ORIG_PUT
        tools.httpx.get = _ORIG_GET
    _ORIG_POST = _ORIG_PUT = _ORIG_GET = None


# ---------------------------------------------------------------- fake LLM


class _FakeCompletions:
    def __init__(self, script):
        self.script = script
        self.calls = 0
        self.last_messages = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_messages = kwargs.get("messages")
        if not self.script:
            raise RuntimeError("fake script exhausted")
        return self.script.pop(0)


class _FakeChat:
    def __init__(self, script):
        self.completions = _FakeCompletions(script)


class _FakeOpenAI:
    def __init__(self, script):
        self.chat = _FakeChat(script)


class FakeDriver:
    """Installs a fake LLM in front of the buyer agent and tracks call count."""

    def __init__(self, script):
        self.script = list(script)
        self.fake = _FakeOpenAI(self.script)
        self._orig = None
        self.calls = 0

    def _factory(self):
        return self.fake

    def __enter__(self):
        self._orig = agent_mod._client
        agent_mod._client = self._factory
        return self

    def __exit__(self, *exc):
        agent_mod._client = self._orig
        return False

    @property
    def call_count(self):
        return self.fake.chat.completions.calls

    @property
    def seen_messages(self):
        return self.fake.chat.completions.last_messages


def msg_content(content):
    return F(choices=[F(message=F(content=content, tool_calls=None))])


def msg_toolcall(name, args, call_id="call_x"):
    tc = F(id=call_id, function=F(name=name, arguments=json.dumps(args)))
    return F(choices=[F(message=F(content=None, tool_calls=[tc]))])


def drain_sse(response):
    events = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            payload = line[len("data: ") :].strip()
            if payload and payload != "{}":
                events.append(json.loads(payload))
    return events


def run_chat_turn(app, message, conversation_id=None, client=None):
    c = client or TestClient(app)
    with c.stream(
        "POST", "/chat", json={"message": message, "conversation_id": conversation_id}
    ) as r:
        return drain_sse(r)
