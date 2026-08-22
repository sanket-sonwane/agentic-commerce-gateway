"""Saved-driver behaviour without real credentials (hermetic).

With PAYMENT_DRIVER=saved but blank Razorpay keys the flow must still park the
session for authorization, expose the hints consistently on GET, and reuse the
open order on retries instead of creating duplicates.
"""
from merchant_agent.config import settings
from merchant_agent.tests.conftest import complete, make_checkout


def _driver_saved(monkeypatch):
    monkeypatch.setattr(settings, "payment_driver", "saved")


def test_saved_driver_parks_for_authorization(client, monkeypatch):
    _driver_saved(monkeypatch)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    assert r.status_code == 200
    body = r.json()["checkout_session"]
    assert body["status"] == "requires_payment"
    assert body["payment"]["awaiting_authorization"] is True
    assert body["payment"].get("customer_id")
    assert body["payment"].get("save_card") is True

    # GET must surface identical authorization hints (Ops view depends on this)
    g = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    assert g["payment"]["awaiting_authorization"] is True


def test_saved_driver_reuses_open_order_on_retry(client, monkeypatch):
    _driver_saved(monkeypatch)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r1 = complete(client, csid)
    order1 = r1.json()["checkout_session"]["razorpay_order_id"]
    r2 = complete(client, csid)  # agent retried while modal open
    assert r2.status_code == 200
    order2 = r2.json()["checkout_session"]["razorpay_order_id"]
    assert order1 == order2

    events = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
    assert len([e for e in events if e["action"] == "razorpay_order_created"]) == 1
