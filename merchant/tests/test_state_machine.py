"""State machine + terminal-state protection tests (PRD §14)."""
from merchant.tests.conftest import complete, make_checkout

INVALID_COMPLETIONS = ["completed", "declined", "cancelled"]


def test_happy_path_states(client):
    r = make_checkout(client)
    assert r.status_code == 201
    cs = r.json()["checkout_session"]
    # gating passes at creation -> straight to requires_payment
    assert cs["status"] == "requires_payment"
    r = complete(client, cs["checkout_session_id"])
    assert r.status_code == 200
    assert r.json()["checkout_session"]["status"] == "completed"


def test_terminal_completed_returns_existing_result(client):
    """PRD §26: repeated completion attempts return the existing result, never re-charge."""
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r1 = complete(client, csid)  # no idempotency key at all
    assert r1.status_code == 200
    pay1 = r1.json()["checkout_session"]["payment"]["payment_id"]
    r2 = complete(client, csid)
    assert r2.status_code == 200
    assert r2.json()["checkout_session"]["payment"]["payment_id"] == pay1
    events = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
    assert len([e for e in events if e["action"] == "razorpay_order_created"]) == 1


def test_terminal_declined_cannot_complete(client, admin_headers):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    # zero out stock -> completion declines
    client.patch("/admin/packs/pack_milk_amul_500/stock", json={"actual_stock": 0}, headers=admin_headers)
    r = complete(client, csid)
    assert r.json()["checkout_session"]["status"] == "declined"
    r2 = complete(client, csid)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


def test_cancelled_is_terminal(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    body = {"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"}
    r = client.post(f"/checkout_sessions/{csid}/cancel", json=body)
    assert r.json()["checkout_session"]["status"] == "cancelled"
    # idempotent cancel
    r = client.post(f"/checkout_sessions/{csid}/cancel", json=body)
    assert r.status_code == 200
    # complete must be refused
    r = complete(client, csid)
    assert r.status_code == 409


def test_update_requires_live_state(client):
    body = {
        "buyer_agent_id": "buyer_agent_llm_01",
        "delegation_token": "dlg_9f8e7d",
        "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_1l", "quantity": 1}],
    }
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    ok = client.put(f"/checkout_sessions/{csid}", json=body)
    assert ok.status_code == 200
    assert ok.json()["checkout_session"]["version"] == 2
