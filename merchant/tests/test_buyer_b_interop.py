"""Buyer-B independent protocol client — outcome handling regressions.

The merchant serializes the optional ``error`` field of a successful/non-error
completion as JSON ``null`` (``CompleteResponse.error: Optional[dict] = None``).
Buyer-B previously did ``err = data.get("error", {})`` then ``err.get(...)``,
which yields ``None`` for an explicit ``"error": null`` and crashed with
``AttributeError: 'NoneType' object has no attribute 'get'`` whenever a
HTTP-200 response carried a non-``completed`` session plus a null error field.

These tests pin the corrected contract: SUCCESS is first-class, DECLINE/ERROR
is reported from a real dict envelope, MALFORMED bodies fail safely and are
never reported as success, transport failures degrade to a structured
client-side error, and the live merchant flow (sim driver) completes cleanly.
"""
import httpx
import pytest

import buyer_b.client as bb
from merchant.main import app

PROPOSAL = [
    {
        "product_id": "prod_milk_amul",
        "pack_id": "pack_milk_amul_500",
        "name": "Amul Milk 500ml",
        "matched_for": "milk",
        "match_reasons": ["exact"],
        "quantity": 1,
        "price": 27,
        "size": 500.0,
        "unit": "ml",
        "available_stock": 10,
        "category": "dairy",
        "tags": ["dairy", "milk"],
    }
]

DISCOVERY_OK = (200, {"basket_proposal": PROPOSAL, "estimated_total": 27, "currency": "INR"})

CHECKOUT_CREATED = (
    201,
    {
        "checkout_session": {
            "checkout_session_id": "cs_buyerb_test",
            "version": 1,
            "status": "requires_payment",
            "buyer_agent_id": bb.BUYER_AGENT_ID,
            "delegation_id": bb.DEFAULT_DELEGATION,
            "line_items": [],
            "totals": {"subtotal": 27, "tax": 0, "shipping": 0, "total": 27, "currency": "INR"},
            "gating_result": {"passed": True, "checks": []},
            "reservation": {"status": "active", "expires_at": None},
            "payment": {},
            "razorpay_order_id": None,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
    },
)


def _patch_transport(monkeypatch, *, discover=DISCOVERY_OK, create=CHECKOUT_CREATED, complete):
    monkeypatch.setattr(bb, "post", lambda path, payload: discover if path == "/discover" else create)
    monkeypatch.setattr(bb, "post_with_key", lambda path, payload, key: complete)
    monkeypatch.setattr(bb, "get", lambda path: (200, {}))


# ---------------------------------------------------------------- T1 SUCCESS
# Successful completion even though the merchant body carries "error": null.


def test_successful_checkout_is_first_class(monkeypatch, capsys):
    completed = (
        200,
        {
            "checkout_session": {
                "checkout_session_id": "cs_buyerb_test",
                "version": 1,
                "status": "completed",
                "buyer_agent_id": bb.BUYER_AGENT_ID,
                "delegation_id": bb.DEFAULT_DELEGATION,
                "line_items": [],
                "totals": {"subtotal": 27, "tax": 0, "shipping": 0, "total": 27, "currency": "INR"},
                "gating_result": {"passed": True, "checks": []},
                "reservation": {"status": "none", "expires_at": None},
                "payment": {"order_id": "order_x", "payment_id": "pay_x"},
                "razorpay_order_id": "order_x",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            },
            "change": None,
            "error": None,  # the exact seam that used to crash the client
        },
    )
    replay = (200, completed[1])
    _patch_transport(monkeypatch, complete=completed)
    monkeypatch.setattr(bb, "post_with_key", lambda path, payload, key: completed if key and "buyerb-" in key else replay)

    assert bb.run("milk", 300, bb.DEFAULT_DELEGATION) is True
    out = capsys.readouterr().out
    assert "outcome: completed" in out
    assert "paid ₹27" in out
    assert "error=None" not in out


# ---------------------------------------------------------------- T2 DECLINE
# Structured decline: reason code + message preserved, no crash.


def test_structured_decline_is_preserved(monkeypatch, capsys):
    declined = (
        409,
        {
            "error": {
                "code": "STOCK_UNAVAILABLE",
                "message": "Amul Milk 500ml: requested=1, available=0",
                "retryable": True,
                "details": {},
            },
            "checkout_session": {"checkout_session_id": "cs_buyerb_test", "status": "declined"},
        },
    )
    _patch_transport(monkeypatch, complete=declined)

    assert bb.run("milk", 300, bb.DEFAULT_DELEGATION) is False
    out = capsys.readouterr().out
    assert "STOCK_UNAVAILABLE" in out
    assert "Amul Milk 500ml: requested=1" in out
    assert "outcome: declined" in out


# ---------------------------------------------------------------- T3 MALFORMED
# Incomplete success response: must NOT be reported as success and must not raise.


def test_malformed_proposal_item_does_not_crash(monkeypatch, capsys):
    mixed = (200, {"basket_proposal": [PROPOSAL[0], "not-an-object", {"price": "abc"}], "currency": "INR"})
    completed = (
        200,
        {
            "checkout_session": {
                "checkout_session_id": "cs_buyerb_test",
                "version": 1,
                "status": "completed",
                "buyer_agent_id": bb.BUYER_AGENT_ID,
                "delegation_id": bb.DEFAULT_DELEGATION,
                "line_items": [],
                "totals": {"subtotal": 27, "tax": 0, "shipping": 0, "total": 27, "currency": "INR"},
                "gating_result": {"passed": True, "checks": []},
                "reservation": {"status": "none", "expires_at": None},
                "payment": {"order_id": "order_x", "payment_id": "pay_x"},
                "razorpay_order_id": "order_x",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            },
            "change": None,
            "error": None,
        },
    )
    _patch_transport(monkeypatch, discover=mixed, complete=completed)

    assert bb.run("milk", 300, bb.DEFAULT_DELEGATION) is True  # valid item still selected
    out = capsys.readouterr().out
    assert "outcome: completed" in out


def test_malformed_success_missing_totals_is_rejected(monkeypatch, capsys):
    malformed = (200, {"checkout_session": {"status": "completed"}})  # totals missing
    _patch_transport(monkeypatch, complete=malformed)

    assert bb.run("milk", 300, bb.DEFAULT_DELEGATION) is False
    out = capsys.readouterr().out
    assert "MALFORMED" in out
    assert "outcome: completed" not in out


def test_malformed_missing_checkout_session_is_rejected(monkeypatch, capsys):
    malformed = (200, {"some_other_key": 1})
    _patch_transport(monkeypatch, complete=malformed)

    assert bb.run("milk", 300, bb.DEFAULT_DELEGATION) is False
    out = capsys.readouterr().out
    assert "no merchant error envelope" in out
    assert "outcome: completed" not in out


def test_malformed_non_object_body_is_rejected(monkeypatch, capsys):
    _patch_transport(monkeypatch, complete=(200, ["not", "an", "object"]))

    assert bb.run("milk", 300, bb.DEFAULT_DELEGATION) is False
    out = capsys.readouterr().out
    assert "MALFORMED" in out
    assert "outcome: completed" not in out


# ---------------------------------------------------------------- T4 ORIGINAL BUG
# 200 + requires_payment + "error": null — the exact NoneType .get() seam.


def test_missing_error_on_nonterminal_success_does_not_crash(monkeypatch, capsys):
    requires_payment = (
        200,
        {
            "checkout_session": {
                "checkout_session_id": "cs_buyerb_test",
                "version": 1,
                "status": "requires_payment",
                "buyer_agent_id": bb.BUYER_AGENT_ID,
                "delegation_id": bb.DEFAULT_DELEGATION,
                "line_items": [],
                "totals": {"subtotal": 27, "tax": 0, "shipping": 0, "total": 27, "currency": "INR"},
                "gating_result": {"passed": True, "checks": []},
                "reservation": {"status": "active", "expires_at": None},
                "payment": {"order_id": "order_x"},
                "razorpay_order_id": "order_x",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            },
            "change": None,
            "error": None,
        },
    )
    _patch_transport(monkeypatch, complete=requires_payment)

    assert bb.run("milk", 300, bb.DEFAULT_DELEGATION) is False
    out = capsys.readouterr().out
    assert "requires_payment" in out
    assert "no merchant error envelope" in out
    assert "outcome: completed" not in out


# ---------------------------------------------------------------- T5 PROTOCOL ERROR
# Non-2xx with a structured ProtocolError payload.


def test_merchant_http_protocol_error_is_reported(monkeypatch, capsys):
    internal = (
        500,
        {
            "error": {
                "code": "INVALID_REQUEST",
                "message": "Internal merchant error; transaction not executed",
                "retryable": False,
                "details": {},
            }
        },
    )
    _patch_transport(monkeypatch, complete=internal)

    assert bb.run("milk", 300, bb.DEFAULT_DELEGATION) is False
    out = capsys.readouterr().out
    assert "Internal merchant error; transaction not executed" in out
    assert "INVALID_REQUEST" in out


# ---------------------------------------------------------------- T6 TRANSPORT FAILURE
# Real connection-refused request: must degrade to a structured client error.


def test_transport_failure_is_structured(monkeypatch, capsys):
    monkeypatch.setattr(bb, "BASE", "http://127.0.0.1:9")  # refuse the connection

    assert bb.run("milk", 300, bb.DEFAULT_DELEGATION) is False
    out = capsys.readouterr().out
    assert "CLIENT_TRANSPORT_ERROR" in out


# ---------------------------------------------------------------- T7 LIVE FLOW
# Full Buyer-B flow against the real merchant app (sim driver) via ASGI transport.


def test_live_merchant_flow_completes(monkeypatch, capsys):
    import asyncio

    import httpx as hx

    async def _call(method, path, payload=None, headers=None):
        async with hx.AsyncClient(transport=hx.ASGITransport(app=app)) as c:
            r = await c.request(method, "http://buyerb.test" + path, json=payload, headers=headers or {})
            return r.status_code, r.json()

    def live_request(method, path, payload=None, headers=None):
        return asyncio.run(_call(method, path, payload, headers))

    def live_post(path, payload):
        return live_request("POST", path, payload)

    def live_post_with_key(path, payload, key):
        return live_request("POST", path, payload, {"Idempotency-Key": key})

    monkeypatch.setattr(bb, "post", live_post)
    monkeypatch.setattr(bb, "post_with_key", live_post_with_key)
    monkeypatch.setattr(bb, "get", lambda path: (200, {}))

    assert bb.run("milk", 300, bb.DEFAULT_DELEGATION) is True
    out = capsys.readouterr().out
    assert "outcome: completed" in out
    assert "same_payment=True" in out
    assert "CLIENT_" not in out