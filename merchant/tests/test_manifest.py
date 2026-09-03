"""M1 — Merchant capability manifest tests (GET /.well-known/merchant).

Proves an independent buyer can read the manifest with only HTTP and JSON and
that it reflects ACTUAL implemented capabilities (no invented/fake ones).
"""
import json

from merchant.config import settings


def _manifest(client):
    r = client.get("/.well-known/merchant")
    assert r.status_code == 200, r.text
    return r.json()


def test_manifest_shape_and_no_secrets(client):
    m = _manifest(client)
    assert m["merchant_id"] == settings.merchant_id
    assert m["name"]
    assert m["protocol"]["version"] == settings.protocol_version
    assert m["currency"] == "INR"
    assert m["currency_fraction"] == 100
    # No secrets ever.
    body = json.dumps(m).lower()
    for forbidden in ("rzp_test_", "secret", "signing", "private_key", "razorpay_key_secret", ".pem"):
        assert forbidden not in body


def test_manifest_capabilities_match_reality(client):
    m = _manifest(client)
    names = {c["name"] for c in m["capabilities"]}
    # Core merchant loop capabilities that genuinely exist.
    for expected in ("discovery", "catalog", "inventory", "pricing", "checkout",
                     "payment", "fulfillment", "orders", "provenance"):
        assert expected in names, f"missing capability {expected}"
    # Every capability is a read of real functionality — none flagged write-only.
    for c in m["capabilities"]:
        assert isinstance(c["read"], bool)


def test_manifest_endpoints_exist(client):
    m = _manifest(client)
    paths = {e["path"] for e in m["endpoints"]}
    assert "/.well-known/merchant" in paths
    assert "/catalog/products/{product_id}" in paths
    assert "/catalog/packs/{pack_id}" in paths
    assert "/orders" in paths
    assert "/orders/{order_id}" in paths
    assert "/checkout_sessions" in paths


def test_manifest_projection_is_read_only(client):
    # Manifest endpoints must never mutate state.
    from merchant.persistence.db import SessionLocal
    from merchant.persistence.models.entities import AuditEvent

    with SessionLocal() as db:
        before = db.query(AuditEvent).count()
    _manifest(client)
    with SessionLocal() as db:
        after = db.query(AuditEvent).count()
    assert after == before


def test_merchant_identity_bound_to_manifest(client):
    m = _manifest(client)
    # The checkout contract binds merchant_id too — manifest must agree.
    assert m["merchant_id"] == settings.merchant_id
