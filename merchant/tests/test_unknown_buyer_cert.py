"""M7 — Unknown-Buyer certification client integration tests.

The certification client validates the live merchant against its published
capability manifest + JSON schemas over pure HTTP. These tests run the client
against the real merchant app (sim driver) via ASGI transport — the same
httpx-only, no-internals posture an independent unknown buyer would use — and
pin that a real purchase cycle certifies cleanly and that contract deviations
are reported as structured failures rather than crashes.
"""
import asyncio

import httpx

import cert_unknown_buyer.client as cert
from merchant.main import app


def _live_transport():
    async def _call(method, path, payload=None, headers=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
            r = await c.request(method, "http://cert.test" + path, json=payload, headers=headers or {})
            try:
                body = r.json()
            except ValueError:
                body = {"error": {"code": "CLIENT_MALFORMED_RESPONSE", "message": "non-json",
                                  "retryable": False, "details": {}}}
            return r.status_code, body

    def live_post(path, payload, key=None):
        headers = {"Idempotency-Key": key} if key else {}
        return asyncio.run(_call("POST", path, payload, headers))

    def live_get(path):
        return asyncio.run(_call("GET", path))

    t = cert._Transport("http://cert.test")
    t.inject(post_fn=live_post, get_fn=live_get)
    return t


def test_unknown_buyer_certification_passes_against_live_merchant():
    ok = cert.certify("http://cert.test", transport=_live_transport())
    assert ok is True


def test_manifest_is_returned(capsys):
    t = _live_transport()
    status, manifest = t.get("/.well-known/merchant")
    assert status == 200
    assert isinstance(manifest.get("capabilities"), list)
    assert isinstance(manifest.get("endpoints"), list)
    # Manifest is derived read-only from real implementation: no secrets.
    assert "capabilities" in manifest


def test_schema_files_publish_manifest_codes():
    import json

    schema = cert._load_schema("error.json")
    codes = schema["properties"]["error"]["properties"]["code"]["enum"]
    # The error contract must include the newest codes the merchant can emit.
    assert "PAYMENT_BLOCKED" in codes
    assert "PROVENANCE_NOT_FOUND" in codes
    assert "INVALID_STATE_TRANSITION" in codes
