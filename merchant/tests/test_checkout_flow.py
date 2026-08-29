"""Checkout flow tests: price-change -> confirmation, stale confirmation, update semantics."""
import json

from merchant.tests.conftest import complete, make_checkout


def _set_price(admin_headers, pack_id, price, client):
    # operator-style price change via direct DB (no public price endpoint by design)
    from merchant.persistence.db import mutate
    from merchant.persistence.models.entities import Pack

    with mutate() as db:
        db.get(Pack, pack_id).price = price


def test_price_change_requires_confirmation_then_completes(client, admin_headers):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]  # 2x₹32 + ₹40 ship = 104
    _set_price(admin_headers, "pack_milk_amul_500", 45, client)  # new total: 90+40=130

    r = complete(client, csid)
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "PRICE_CHANGED"
    assert body["error"]["details"]["old_total"] == 104
    assert body["error"]["details"]["new_total"] == 130
    assert body["checkout_session"]["status"] == "requires_confirmation"

    session = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    current_version = session["version"]

    # stale confirmation must be rejected (threat: stale approval)
    stale = client.post(
        f"/checkout_sessions/{csid}/confirm",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "accept_new_terms": True, "version": current_version - 1},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CONFIRMATION_STALE"

    # version-matched confirmation returns to requires_payment
    ok = client.post(
        f"/checkout_sessions/{csid}/confirm",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "accept_new_terms": True, "version": current_version},
    )
    assert ok.status_code == 200
    assert ok.json()["checkout_session"]["status"] == "requires_payment"

    # completing now charges the NEW terms without further prompting
    done = complete(client, csid)
    assert done.status_code == 200
    final = done.json()["checkout_session"]
    assert final["status"] == "completed"
    assert final["totals"]["total"] == 130


def test_rejecting_new_terms_cancels(client, admin_headers):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    _set_price(admin_headers, "pack_milk_amul_500", 50, client)
    r = complete(client, csid)
    assert r.json()["checkout_session"]["status"] == "requires_confirmation"
    version = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]["version"]
    no = client.post(
        f"/checkout_sessions/{csid}/confirm",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "accept_new_terms": False, "version": version},
    )
    assert no.status_code == 200
    assert no.json()["checkout_session"]["status"] == "cancelled"


def test_update_policy_failure_preserves_session(client):
    body = {
        "buyer_agent_id": "buyer_agent_llm_01",
        "delegation_token": "dlg_9f8e7d",
        "line_items": [{"product_id": "prod_saffron_premium", "pack_id": "pack_saffron_5g", "quantity": 3}],  # 7800 > 5000
    }
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = client.put(f"/checkout_sessions/{csid}", json=body)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SPEND_LIMIT_EXCEEDED"
    # original basket preserved and still completable
    session = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    assert session["line_items"][0]["pack_id"] == "pack_milk_amul_500"
    assert session["status"] == "requires_payment"
    done = complete(client, csid)
    assert done.json()["checkout_session"]["status"] == "completed"


def test_price_authority_server_side(client):
    """Client-supplied prices must be ignored/absent — merchant computes totals."""
    r = make_checkout(client)  # request contains NO price fields at all
    items = r.json()["checkout_session"]["line_items"]
    totals = r.json()["checkout_session"]["totals"]
    assert items[0]["unit_price"] == 32
    assert totals["subtotal"] == 64 and totals["shipping"] == 40 and totals["total"] == 104
