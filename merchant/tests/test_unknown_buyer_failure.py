"""Phase 3 — Unknown-Buyer failure + recovery lifecycle.

A focused integration test proving that when a transaction hits a realistic
failure (a product becomes unavailable after checkout but before payment), the
merchant fails safely and the buyer can recover over the public HTTP surface
with NO unauthorized charge, NO invalid/duplicate Order, NO stale success, and
NO misleading transaction state.

The buyer only ever uses public HTTP. The stock-unavailability is injected as
an *external event* (a pack's stock drops to zero) — like reality, the product
sold out to another buyer or was pulled — never as buyer knowledge of
merchant internals.
"""
from merchant.persistence.db import mutate
from merchant.persistence.models.entities import Order, Pack

BUYER = "buyer_agent_llm_01"
DELEG = "dlg_9f8e7d"
PRIMARY = "pack_milk_amul_500"      # the product that becomes unavailable
ALTERNATIVE = "pack_milk_md_500"    # the authoritative catalog alternative


def _set_stock(pack_id, qty):
    with mutate() as db:
        db.get(Pack, pack_id).stock_actual = qty


def _orders(client):
    r = client.get(f"/orders?buyer_agent_id={BUYER}&delegation_token={DELEG}")
    assert r.status_code == 200, r.text
    return r.json()["orders"]


def test_failure_then_alternative_recovery(client):
    # 1. Discover the merchant's capability manifest (public entry point).
    mani = client.get("/.well-known/merchant")
    assert mani.status_code == 200
    assert isinstance(mani.json().get("endpoints"), list)

    # 2. Inspect the product in the authoritative public catalog.
    pack = client.get(f"/catalog/packs/{PRIMARY}")
    assert pack.status_code == 200
    assert pack.json()["availability"] == "in_stock"

    # 3. Create a checkout for the primary product.
    cs = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": BUYER, "delegation_token": DELEG,
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": PRIMARY, "quantity": 1}],
              "fulfillment_method": "delivery",
              "destination": {"name": "Ada", "line1": "1 MG Rd", "city": "Blr", "pin": "560001"}},
    )
    assert cs.status_code == 201, cs.text
    csid = cs.json()["checkout_session"]["checkout_session_id"]

    # 4. The product becomes unavailable before payment (external stock event).
    _set_stock(PRIMARY, 0)

    # 5. Completion fails safely with a structured protocol error.
    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": BUYER, "delegation_token": DELEG},
        headers={"Idempotency-Key": "f1"},
    )
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "STOCK_UNAVAILABLE"
    assert err["retryable"] is True  # availability may return; session itself is moot
    failed = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    assert failed["status"] == "declined"
    assert failed["razorpay_order_id"] is None  # no charge was ever initiated

    # 6. The failure must not create an Order / duplicate / stale success.
    assert _orders(client) == []
    with mutate() as db:
        assert db.query(Order).count() == 0

    # 7. Re-read the authoritative catalog: primary is out, alternative is in.
    re_pack = client.get(f"/catalog/packs/{PRIMARY}")
    assert re_pack.status_code == 200
    assert re_pack.json()["available"] == 0
    assert re_pack.json()["availability"] == "out_of_stock"
    alt = client.get(f"/catalog/packs/{ALTERNATIVE}")
    assert alt.status_code == 200
    assert alt.json()["availability"] == "in_stock"

    # 8. Choose the alternative and create a fresh checkout.
    cs2 = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": BUYER, "delegation_token": DELEG,
              "line_items": [{"product_id": "prod_milk_mother_dairy", "pack_id": ALTERNATIVE, "quantity": 1}],
              "fulfillment_method": "delivery",
              "destination": {"name": "Ada", "line1": "1 MG Rd", "city": "Blr", "pin": "560001"}},
    )
    assert cs2.status_code == 201, cs2.text
    csid2 = cs2.json()["checkout_session"]["checkout_session_id"]

    # 9. Authorize + pay (sim driver, hermetic).
    done = client.post(
        f"/checkout_sessions/{csid2}/complete",
        json={"buyer_agent_id": BUYER, "delegation_token": DELEG},
        headers={"Idempotency-Key": "f2"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["checkout_session"]["status"] == "completed"

    # 10. Retrieve the authoritative Order — exactly ONE, bound to the
    #     alternative checkout, NOT the failed one.
    orders = _orders(client)
    assert len(orders) == 1
    o = orders[0]
    assert o["checkout_session_id"] == csid2
    assert o["fulfillment_status"] == "pending"
    assert o["fulfillment_method"] == "delivery"
    assert o["authorized_amount_paise"] > 0
    with mutate() as db:
        assert db.query(Order).count() == 1

    # 11. Read the single order + verify provenance for the successful one.
    single = client.get(
        f"/orders/{o['order_id']}?buyer_agent_id={BUYER}&delegation_token={DELEG}")
    assert single.status_code == 200
    assert single.json()["order_id"] == o["order_id"]

    prov = client.get(f"/provenance/{csid2}")
    assert prov.status_code == 200
    report = prov.json()
    assert report.get("provenance_available") is True
    assert all(c.get("ok") for c in report.get("checks", []))


def test_stock_out_at_create_declines_with_no_order(client):
    """A product already unavailable at checkout creation is declined immediately."""
    _set_stock(ALTERNATIVE, 0)
    r = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": BUYER, "delegation_token": DELEG,
              "line_items": [{"product_id": "prod_milk_mother_dairy", "pack_id": ALTERNATIVE, "quantity": 1}]},
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "STOCK_UNAVAILABLE"
    assert _orders(client) == []
    with mutate() as db:
        assert db.query(Order).count() == 0
