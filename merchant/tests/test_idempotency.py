"""Idempotency tests (PRD §26 / FR-13 / M4)."""
from merchant.tests.conftest import complete, make_checkout


def test_complete_replay_same_result_no_double_charge(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r1 = complete(client, csid, key="idem-1")
    assert r1.json()["checkout_session"]["status"] == "completed"
    pay1 = r1.json()["checkout_session"]["payment"]["payment_id"]

    for _ in range(3):
        r2 = complete(client, csid, key="idem-1")
        assert r2.status_code == 200
        assert r2.json()["checkout_session"]["payment"]["payment_id"] == pay1

    events = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
    # exactly one payment success despite four completion calls
    assert len([e for e in events if e["action"] == "payment_success"]) == 1
    assert len([e for e in events if e["action"] == "razorpay_order_created"]) == 1


def test_completed_without_key_returns_existing_result(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r1 = complete(client, csid)
    pay1 = r1.json()["checkout_session"]["payment"]["payment_id"]
    r2 = complete(client, csid)  # no key: terminal-state path returns existing result (PRD §26)
    assert r2.status_code == 200
    assert r2.json()["checkout_session"]["status"] == "completed"
    assert r2.json()["checkout_session"]["payment"]["payment_id"] == pay1
    events = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
    assert len([e for e in events if e["action"] == "razorpay_order_created"]) == 1


def test_create_replay_does_not_duplicate_sessions(client):
    payload = {
        "buyer_agent_id": "buyer_agent_llm_01",
        "delegation_token": "dlg_9f8e7d",
        "line_items": [{"product_id": "prod_eggs_farm", "pack_id": "pack_eggs_6", "quantity": 1}],
    }
    r1 = client.post("/checkout_sessions", json=payload, headers={"Idempotency-Key": "create-1"})
    r2 = client.post("/checkout_sessions", json=payload, headers={"Idempotency-Key": "create-1"})
    assert r1.status_code == 201 and r2.status_code == 200
    sid1 = r1.json()["checkout_session"]["checkout_session_id"]
    sid2 = r2.json()["checkout_session"]["checkout_session_id"]
    assert sid1 == sid2
