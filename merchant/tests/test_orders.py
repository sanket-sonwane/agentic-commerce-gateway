"""M3/M5 — Order domain + consumer order API tests.

Proves a completed checkout materializes a durable, authoritative Order; that
the order survives and is readable via the public protocol (never dashboard
only); that the list is buyer-scoped; and that the fulfillment state machine is
deterministic and validated.
"""
from merchant.persistence.db import SessionLocal, mutate
from merchant.persistence.models.entities import Order


def _complete(client, csid, delegation="dlg_9f8e7d", buyer="buyer_agent_llm_01", key=None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": buyer, "delegation_token": delegation},
        headers=headers,
    )


def _order_count(checkout_session_id=None):
    with SessionLocal() as db:
        q = db.query(Order)
        if checkout_session_id:
            q = q.filter(Order.checkout_session_id == checkout_session_id)
        return q.count()


def test_order_created_on_successful_completion(client):
    csid = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}]},
    ).json()["checkout_session"]["checkout_session_id"]
    r = _complete(client, csid, key="ordkey-1")
    assert r.status_code == 200, r.text
    assert r.json()["checkout_session"]["status"] == "completed"
    assert _order_count(csid) == 1


def test_order_creation_is_idempotent(client):
    csid = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
    ).json()["checkout_session"]["checkout_session_id"]
    key = "ordkey-idem"
    r1 = _complete(client, csid, key=key)
    assert r1.status_code == 200
    # Replay with the same idempotency key -> still completed, still one order.
    r2 = _complete(client, csid, key=key)
    assert r2.status_code == 200
    assert r2.json()["checkout_session"]["status"] == "completed"
    assert _order_count(csid) == 1


def test_order_has_authoritative_references(client):
    csid = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}]},
    ).json()["checkout_session"]["checkout_session_id"]
    _complete(client, csid)
    with SessionLocal() as db:
        order = db.query(Order).filter(Order.checkout_session_id == csid).first()
    assert order is not None
    assert order.checkout_session_id == csid
    assert order.buyer_agent_id == "buyer_agent_llm_01"
    assert order.delegation_id == "dlg_9f8e7d"
    assert order.authorized_amount_paise > 0
    assert order.order_status == "placed"
    assert order.fulfillment_status == "pending"


def test_order_read_via_public_api(client):
    csid = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
    ).json()["checkout_session"]["checkout_session_id"]
    _complete(client, csid)

    r = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_eggs_farm", "pack_id": "pack_eggs_12", "quantity": 1}]},
    ).json()["checkout_session"]["checkout_session_id"]
    _complete(client, r)

    # List scoped to buyer_agent_llm_01 / dlg_9f8e7d.
    lst = client.get("/orders", params={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"})
    assert lst.status_code == 200, lst.text
    body = lst.json()
    assert body["count"] >= 2
    order_id = body["orders"][0]["order_id"]
    assert body["orders"][0]["checkout_session_id"]

    # Single order read returns fulfillment state (public path).
    od = client.get(
        f"/orders/{order_id}",
        params={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"},
    )
    assert od.status_code == 200, od.text
    o = od.json()
    assert o["order_id"] == order_id
    assert o["fulfillment_status"] in ("pending", "placed", "packed", "shipped", "delivered")
    assert o["authorized_amount_paise"] > 0
    assert o["payment"]["provider"] == "Razorpay"


def test_orders_list_is_buyer_scoped(client):
    csid = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
    ).json()["checkout_session"]["checkout_session_id"]
    _complete(client, csid)

    # A different buyer/delegation must not see buyer_agent_llm_01's orders.
    lst = client.get("/orders", params={"buyer_agent_id": "buyer_agent_b_deterministic", "delegation_token": "dlg_buyerb_001"})
    assert lst.status_code == 200, lst.text
    # The Buyer-B delegation has never placed orders in this clean state.
    assert lst.json()["count"] == 0


def test_other_buyer_cannot_read_order(client):
    csid = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
    ).json()["checkout_session"]["checkout_session_id"]
    _complete(client, csid)
    with SessionLocal() as db:
        order = db.query(Order).filter(Order.checkout_session_id == csid).first()
    order_id = order.order_id

    # Wrong buyer for the same delegation token -> forbidden.
    r = client.get(
        f"/orders/{order_id}",
        params={"buyer_agent_id": "buyer_agent_b_deterministic", "delegation_token": "dlg_9f8e7d"},
    )
    assert r.status_code == 403


def test_no_order_on_cancelled(client):
    # A cancelled checkout must not materialize an order.
    csid = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
    ).json()["checkout_session"]["checkout_session_id"]
    r = client.post(
        f"/checkout_sessions/{csid}/cancel",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["checkout_session"]["status"] == "cancelled"
    assert _order_count(csid) == 0
    # And the order read for a cancelled session -> 404 (no order exists).
    assert _order_count() == 0


def _make_order(client):
    csid = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
    ).json()["checkout_session"]["checkout_session_id"]
    _complete(client, csid)
    with SessionLocal() as db:
        return db.query(Order).filter(Order.checkout_session_id == csid).first().order_id


def test_fulfillment_state_machine(client, admin_headers):
    order_id = _make_order(client)

    for target in ("placed", "packed", "shipped", "delivered"):
        r = client.post(
            f"/admin/orders/{order_id}/fulfillment",
            json={"order_id": order_id, "target": target},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["order"]["fulfillment_status"] == target

    # Public read reflects the advanced state (Buyer-B able to query progress).
    od = client.get(
        f"/orders/{order_id}",
        params={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"},
    )
    assert od.json()["fulfillment_status"] == "delivered"


def test_illegal_fulfillment_transition_rejected(client, admin_headers):
    order_id = _make_order(client)
    headers = admin_headers
    # Skip ahead -> illegal (pending -> shipped).
    r = client.post(
        f"/admin/orders/{order_id}/fulfillment",
        json={"order_id": order_id, "target": "shipped"},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "INVALID_STATE_TRANSITION"
    # Fulfillment unchanged.
    od = client.get(
        f"/orders/{order_id}",
        params={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"},
    )
    assert od.json()["fulfillment_status"] == "pending"


def test_fulfillment_requires_admin(client):
    order_id = _make_order(client)
    r = client.post(
        f"/admin/orders/{order_id}/fulfillment",
        json={"order_id": order_id, "target": "placed"},
    )
    assert r.status_code == 401, r.text

    r2 = client.post(
        f"/admin/orders/{order_id}/fulfillment",
        json={"order_id": order_id, "target": "placed"},
        headers={"X-Admin-Token": "wrong"},
    )
    assert r2.status_code == 401, r2.text


def test_delegation_revocation_blocks_new_purchases(client, admin_headers):
    # Buyer-B places an order with its own delegation.
    csid = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_b_deterministic", "delegation_token": "dlg_buyerb_001",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
    ).json()["checkout_session"]["checkout_session_id"]
    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_b_deterministic", "delegation_token": "dlg_buyerb_001"},
        headers={"Idempotency-Key": "revoke-pre"},
    )
    assert r.status_code == 200, r.text

    # Revoke the delegation.
    rev = client.post("/admin/delegations/dlg_buyerb_001/revoke", headers=admin_headers)
    assert rev.status_code == 200, rev.text
    assert rev.json()["status"] == "revoked"

    # New purchase with the revoked delegation fails closed (unauthorized).
    r2 = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_b_deterministic", "delegation_token": "dlg_buyerb_001",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
    )
    assert r2.status_code == 401, r2.text
    assert r2.json()["error"]["code"] == "UNAUTHORIZED_AGENT"

    # Existing order idempotent replay of an already-completed session still
    # returns the same completed result (terminal-state protection, not a new
    # authorization).
    r3 = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_b_deterministic", "delegation_token": "dlg_buyerb_001"},
        headers={"Idempotency-Key": "revoke-replay"},
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["checkout_session"]["status"] == "completed"


def test_delegation_revocation_requires_admin(client):
    r = client.post("/admin/delegations/dlg_buyerb_001/revoke")
    assert r.status_code == 401, r.text


# ------------------------------------------------------------------ Phase 7
# Fulfillment binding: buyer-declared delivery method + destination at checkout
# must become part of the commercial agreement and carry through to the Order.


DELIVERY_ADDR = {"name": "Ada Buyer", "line1": "1 MG Road", "city": "Blr", "pin": "560001", "phone": "9999999999"}


def test_fulfillment_binding_carries_to_order(client):
    # Create a checkout declaring delivery + an address.
    resp = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}],
              "fulfillment_method": "delivery", "destination": DELIVERY_ADDR},
    )
    assert resp.status_code == 201, resp.text
    session = resp.json()["checkout_session"]
    # The session exposes the declared terms.
    assert session["fulfillment_method"] == "delivery"
    assert session["destination"] == DELIVERY_ADDR

    csid = session["checkout_session_id"]
    r = _complete(client, csid, key="ff-delivery-1")
    assert r.status_code == 200, r.text

    # The resulting order carries the same fulfillment terms (bound agreement).
    od = client.get(
        f"/orders/{_order_id_for(client, csid)}",
        params={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"},
    )
    assert od.status_code == 200, od.text
    o = od.json()
    assert o["fulfillment_method"] == "delivery"
    assert o["destination"] == DELIVERY_ADDR


def test_fulfillment_method_defaults_to_none_when_not_declared(client):
    resp = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
    )
    assert resp.status_code == 201, resp.text
    session = resp.json()["checkout_session"]
    # Backward-compatible: no declared method -> empty/none, no address.
    assert session["fulfillment_method"] in ("", None)
    assert session["destination"] in ({}, None)


def test_unsupported_fulfillment_method_rejected(client):
    resp = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}],
              "fulfillment_method": "drone"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_fulfillment_binding_survives_idempotent_replay(client):
    # Create with delivery terms; verify the terms are preserved, and that a
    # second create with the SAME idempotency key replays without duplicating.
    key = "ff-idem"
    payload = {"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
               "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}],
               "fulfillment_method": "pickup"}
    r1 = client.post("/checkout_sessions", json=payload, headers={"Idempotency-Key": key})
    r2 = client.post("/checkout_sessions", json=payload, headers={"Idempotency-Key": key})
    assert r1.status_code == 201
    assert r2.status_code == 200  # idempotent replay
    assert r1.json()["checkout_session"]["checkout_session_id"] == \
        r2.json()["checkout_session"]["checkout_session_id"]
    assert r2.json()["checkout_session"]["fulfillment_method"] == "pickup"


def test_pickup_method_also_binds(client):
    resp = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_eggs_farm", "pack_id": "pack_eggs_12", "quantity": 1}],
              "fulfillment_method": "pickup"},
    )
    assert resp.status_code == 201, resp.text
    csid = resp.json()["checkout_session"]["checkout_session_id"]
    r = _complete(client, csid, key="ff-pickup-1")
    assert r.status_code == 200, r.text
    od = client.get(
        f"/orders/{_order_id_for(client, csid)}",
        params={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"},
    )
    assert od.json()["fulfillment_method"] == "pickup"


def _order_id_for(client, checkout_session_id):
    lst = client.get("/orders", params={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"})
    for o in lst.json()["orders"]:
        if o["checkout_session_id"] == checkout_session_id:
            return o["order_id"]
    raise AssertionError(f"no order for {checkout_session_id}")


# ------------------------------------------------------------------ Phase 2
# Commercial-term invariant: the fulfillment terms declared at checkout must be
# identical to the terms that are authorized (post update+confirm) and to the
# terms bound on the completed Order. No path may silently alter them.


def _set_price(pack_id, price):
    from merchant.persistence.db import mutate
    from merchant.persistence.models.entities import Pack

    with mutate() as db:
        db.get(Pack, pack_id).price = price


def _session(client, csid):
    r = client.get(f"/checkout_sessions/{csid}")
    assert r.status_code == 200, r.text
    return r.json()["checkout_session"]


def _confirm(client, csid, version):
    return client.post(
        f"/checkout_sessions/{csid}/confirm",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "accept_new_terms": True, "version": version},
    )


def test_fulfillment_terms_survive_update_then_confirm(client):
    """Declared delivery+address survive a basket update AND a price confirmation."""
    resp = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}],
              "fulfillment_method": "delivery", "destination": DELIVERY_ADDR},
    )
    assert resp.status_code == 201, resp.text
    csid = resp.json()["checkout_session"]["checkout_session_id"]

    # 1. Basket update: switch to a different pack (bumps version). Fulfillment
    #    must not reset to defaults.
    upd = client.put(
        f"/checkout_sessions/{csid}",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_eggs_farm", "pack_id": "pack_eggs_12", "quantity": 1}]},
    )
    assert upd.status_code == 200, upd.text
    assert _session(client, csid)["fulfillment_method"] == "delivery"
    assert _session(client, csid)["destination"] == DELIVERY_ADDR

    # 2. Force a price change on the updated pack so completion needs confirm.
    _set_price("pack_eggs_12", 110)
    r = _complete(client, csid, key="ff-updconfirm-1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PRICE_CHANGED"
    v_after_change = _session(client, csid)["version"]

    # 3. Confirm the current version; terms must persist across the confirm.
    ok = _confirm(client, csid, version=v_after_change)
    assert ok.status_code == 200, ok.text
    after_confirm = _session(client, csid)
    assert after_confirm["fulfillment_method"] == "delivery"
    assert after_confirm["destination"] == DELIVERY_ADDR
    assert after_confirm["status"] == "requires_payment"

    # 4. Complete; the Order must bind the same declared terms.
    done = _complete(client, csid, key="ff-updconfirm-2")
    assert done.status_code == 200, done.text
    assert done.json()["checkout_session"]["status"] == "completed"
    od = client.get(
        f"/orders/{_order_id_for(client, csid)}",
        params={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"},
    )
    assert od.status_code == 200, od.text
    assert od.json()["fulfillment_method"] == "delivery"
    assert od.json()["destination"] == DELIVERY_ADDR


def test_confirm_version_mismatch_does_not_alter_fulfillment_terms(client):
    """A stale confirmation (CONFIRMATION_STALE) leaves pickup terms untouched."""
    resp = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_eggs_farm", "pack_id": "pack_eggs_12", "quantity": 1}],
              "fulfillment_method": "pickup"},
    )
    assert resp.status_code == 201, resp.text
    csid = resp.json()["checkout_session"]["checkout_session_id"]
    v0 = _session(client, csid)["version"]
    assert _session(client, csid)["fulfillment_method"] == "pickup"

    # Price change #1 -> PRICE_CHANGED (version stays v0).
    _set_price("pack_eggs_12", 120)
    r = _complete(client, csid, key="ff-stale-1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PRICE_CHANGED"
    # Confirm correctly -> bumps to v0+1, back to requires_payment.
    ok1 = _confirm(client, csid, version=v0)
    assert ok1.status_code == 200, ok1.text
    assert _session(client, csid)["status"] == "requires_payment"

    # Price change #2 -> PRICE_CHANGED again (version stays v0+1).
    _set_price("pack_eggs_12", 130)
    r2 = _complete(client, csid, key="ff-stale-2")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "PRICE_CHANGED"
    cur_v = _session(client, csid)["version"]
    assert cur_v == v0 + 1

    # Stale confirmation (old version) must be rejected and must NOT change terms.
    stale = _confirm(client, csid, version=v0)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CONFIRMATION_STALE"
    still = _session(client, csid)
    assert still["fulfillment_method"] == "pickup"
    assert still["version"] == cur_v

    # Version-matched confirm then complete; order carries pickup.
    ok = _confirm(client, csid, version=cur_v)
    assert ok.status_code == 200, ok.text
    done = _complete(client, csid, key="ff-stale-3")
    assert done.status_code == 200, done.text
    assert done.json()["checkout_session"]["status"] == "completed"
    od = client.get(
        f"/orders/{_order_id_for(client, csid)}",
        params={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"},
    )
    assert od.json()["fulfillment_method"] == "pickup"


# ------------------------------------------------------------------ Phase 4
# Server-side consumer order proxy: the dashboard reads authoritative Orders
# without ever holding the buyer's delegation token (resolved server-side).


def test_consumer_orders_proxy_returns_demo_buyer_orders(client):
    csid = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}],
              "fulfillment_method": "delivery", "destination": DELIVERY_ADDR},
    ).json()["checkout_session"]["checkout_session_id"]
    r = _complete(client, csid, key="proxy-1")
    assert r.status_code == 200, r.text

    # No delegation token passed — the proxy resolves the acting buyer server-side.
    lst = client.get("/consumer/orders")
    assert lst.status_code == 200, lst.text
    orders = lst.json()["orders"]
    assert len(orders) == 1
    o = orders[0]
    assert o["checkout_session_id"] == csid
    assert o["fulfillment_method"] == "delivery"
    assert o["destination"] == DELIVERY_ADDR

    single = client.get(f"/consumer/orders/{o['order_id']}")
    assert single.status_code == 200, single.text
    assert single.json()["order_id"] == o["order_id"]


def test_consumer_orders_proxy_scoped_to_acting_buyer(client):
    # An order belonging to a DIFFERENT buyer must not surface through the proxy.
    other = client.post(
        "/checkout_sessions",
        json={"buyer_agent_id": "buyer_agent_b_deterministic", "delegation_token": "dlg_buyerb_001",
              "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
    ).json()["checkout_session"]["checkout_session_id"]
    rother = client.post(
        f"/checkout_sessions/{other}/complete",
        json={"buyer_agent_id": "buyer_agent_b_deterministic", "delegation_token": "dlg_buyerb_001"},
        headers={"Idempotency-Key": "proxy-other"},
    )
    assert rother.status_code == 200, rother.text

    # Proxy only sees the acting demo buyer's orders (strict ownership).
    lst = client.get("/consumer/orders")
    assert lst.status_code == 200, lst.text
    assert lst.json()["count"] == 0
    assert all(o["buyer_agent_id"] == "buyer_agent_llm_01" for o in lst.json()["orders"])

    # A direct single-order read of the foreign buyer's order is rejected.
    single = client.get("/consumer/orders/ord_does_not_matter")
    assert single.status_code in (403, 404), single.text
