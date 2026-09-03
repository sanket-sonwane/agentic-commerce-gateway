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
