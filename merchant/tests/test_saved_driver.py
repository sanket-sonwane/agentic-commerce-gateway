"""Saved-driver behaviour without real credentials (hermetic).

With PAYMENT_DRIVER=saved but blank Razorpay keys the flow must still park the
session for authorization, expose the hints consistently on GET, and reuse the
open order on retries instead of creating duplicates. Token-rejection handling
(needs_reauth lifecycle), the mandate-order shape and the headless recurring
charge contract are covered with mocked gateways.
"""
from merchant_agent.config import settings
from merchant_agent.db import mutate
from merchant_agent.models.entities import PaymentMethod
from merchant_agent.services import payment_service
from merchant_agent.services.payment_service import PaymentOutcome, classify_token_rejection
from merchant_agent.tests.conftest import complete, make_checkout


BUYER = "buyer_agent_llm_01"


def _driver_saved(monkeypatch):
    monkeypatch.setattr(settings, "payment_driver", "saved")


def _seed_token_method(customer_id="cust_TEST", token_id="token_OLD"):
    """Insert (or reset) a saved-card row for the demo buyer."""
    with mutate() as db:
        method = (
            db.query(PaymentMethod).filter(PaymentMethod.buyer_agent_id == BUYER).first()
        )
        if method is None:
            method = PaymentMethod(buyer_agent_id=BUYER)
            db.add(method)
            db.flush()
        method.razorpay_customer_id = customer_id
        method.razorpay_token_id = token_id
        method.card_last4 = "3168"
        method.card_network = "Visa"
        method.status = "active"


def _drop_token_method():
    with mutate() as db:
        db.query(PaymentMethod).filter(PaymentMethod.buyer_agent_id == BUYER).delete()


def _with_credentials(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_key")
    monkeypatch.setattr(settings, "razorpay_key_secret", "secret")


def test_create_order_carries_mandate_block_when_saved(monkeypatch):
    """Authorization orders must include customer_id + token{max_amount,
    expire_at, frequency} per Razorpay Recurring Payments docs."""
    _with_credentials(monkeypatch)
    captured: dict = {}

    class FakeOrdersApi:
        def create(self, payload):
            captured.update(payload)
            return {"id": "order_MANDATE"}

    class FakeClient:
        order = FakeOrdersApi()

    monkeypatch.setattr(payment_service, "_get_client", lambda: FakeClient())

    out = payment_service.create_order(
        "cs_mandate_check", 100, mandate={"customer_id": "cust_T1"}
    )
    assert out["id"] == "order_MANDATE"
    assert captured["customer_id"] == "cust_T1"
    assert captured["method"] == "card"
    token = captured["token"]
    assert token["max_amount"] == 1500000
    assert token["frequency"] == "as_presented"
    assert int(token["expire_at"]) > 1800000000

    # Without mandate: plain order, no card/token fields.
    captured.clear()
    payment_service.create_order("cs_plain", 100)
    assert "customer_id" not in captured and "token" not in captured


def test_headless_charge_hits_recurring_endpoint_with_correct_contract(monkeypatch):
    _with_credentials(monkeypatch)
    calls: dict = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"id": "pay_HEADLESS", "status": "captured"}

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls["payload"] = kwargs.get("json")
        return FakeResp()

    monkeypatch.setattr(payment_service.httpx, "post", fake_post)

    outcome = payment_service.charge_with_token(
        "order_HEADLESS",
        50000,
        "cust_HEADLESS",
        "token_HEADLESS",
        buyer_agent_id=BUYER,
    )
    assert outcome.status == "completed"
    assert outcome.payment_id == "pay_HEADLESS"
    assert calls["url"].endswith("/payments/create/recurring")
    body = calls["payload"]
    assert body["token"] == "token_HEADLESS"
    assert body["recurring"] is True
    assert body["customer_id"] == "cust_HEADLESS"
    assert body["order_id"] == "order_HEADLESS"
    assert body["amount"] == 50000
    assert body["email"] and body["contact"]


def test_headless_charge_retries_once_on_server_error(monkeypatch):
    """SERVER_ERROR is transient: one retry, then the structured failure."""
    _with_credentials(monkeypatch)
    attempts = []

    class FakeResp:
        status_code = 500

        def json(self):
            return {"error": {"code": "SERVER_ERROR", "description": "try again shortly"}}

    def fake_post(url, **kwargs):
        attempts.append(kwargs.get("json"))
        return FakeResp()

    monkeypatch.setattr(payment_service.httpx, "post", fake_post)
    monkeypatch.setattr(payment_service.time, "sleep", lambda s: None)

    outcome = payment_service.charge_with_token(
        "order_R", 50000, "cust_R", "token_R", buyer_agent_id=BUYER
    )
    assert len(attempts) == 2  # exactly one retry
    assert outcome.status == "declined"
    assert "try again" in outcome.message.lower()


def test_classify_token_rejection():
    auth_failed = {"error": {"code": "BAD_REQUEST_ERROR", "description": "Authentication failed"}}
    mandate = {"error": {"reason": "mandate_missing", "description": "token cannot be charged"}}
    normal = {"error": {"code": "GATEWAY_ERROR", "description": "Issuer declined the transaction"}}
    assert classify_token_rejection(auth_failed) == "auth_required"
    assert classify_token_rejection(mandate) == "auth_required"
    assert classify_token_rejection(normal) == "declined"
    assert classify_token_rejection({}) == "declined"


def test_auth_required_decline_marks_needs_reauth(client, monkeypatch):
    _driver_saved(monkeypatch)
    _seed_token_method()
    try:
        calls = []

        def fake_charge(order_id, amount_paise, customer_id, token_id, buyer_agent_id=""):
            calls.append(token_id)
            return PaymentOutcome(
                status="declined",
                order_id=order_id,
                error_code="PAYMENT_FAILED",
                message="Token charge rejected by gateway: Authentication failed",
                raw={"error": {"code": "BAD_REQUEST_ERROR", "description": "Authentication failed"}},
                rejection_class="auth_required",
            )

        monkeypatch.setattr(payment_service, "charge_with_token", fake_charge)

        csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
        r = complete(client, csid)
        body = r.json()["checkout_session"]
        assert len(calls) == 1
        assert body["status"] == "requires_payment"
        assert body["payment"]["awaiting_authorization"] is True

        with mutate() as db:
            method = db.query(PaymentMethod).filter(PaymentMethod.buyer_agent_id == BUYER).first()
            assert method.status == "needs_reauth"

        events = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
        reauth = [e for e in events if e["action"] == "token_needs_reauth"]
        assert reauth and reauth[0]["payload"].get("gateway_error", {}).get("error", {}).get(
            "description"
        ) == "Authentication failed"

        # Second completion must skip the doomed token charge entirely.
        r2 = complete(client, csid)
        assert r2.json()["checkout_session"]["payment"]["awaiting_authorization"] is True
        assert len(calls) == 1
    finally:
        _drop_token_method()


def test_new_callback_token_resets_needs_reauth_to_active(client, monkeypatch):
    _driver_saved(monkeypatch)
    _seed_token_method()
    try:
        # Park the method in needs_reauth first.
        with mutate() as db:
            method = db.query(PaymentMethod).filter(PaymentMethod.buyer_agent_id == BUYER).first()
            method.status = "needs_reauth"

        csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
        complete(client, csid)  # parks, creates open order
        from merchant_agent.models.entities import CheckoutSession

        with mutate() as db:
            order_id = db.get(CheckoutSession, csid).razorpay_order_id

        monkeypatch.setattr(payment_service, "verify_checkout_signature", lambda *a, **k: True)
        monkeypatch.setattr(
            payment_service,
            "fetch_payment",
            lambda payment_id: {
                "id": payment_id,
                "token_id": "token_NEW",
                "card": {"last4": "3168", "network": "Visa"},
            },
        )

        cb = client.post(
            f"/checkout_sessions/{csid}/payment_callback",
            json={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": "pay_TEST",
                "razorpay_signature": "sig",
            },
        )
        assert cb.status_code == 200
        session = cb.json()["checkout_session"]
        assert session["status"] == "completed"

        with mutate() as db:
            method = db.query(PaymentMethod).filter(PaymentMethod.buyer_agent_id == BUYER).first()
            assert method.status == "active"
            assert method.razorpay_token_id == "token_NEW"

        events = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
        saved = [e for e in events if e["action"] == "payment_token_saved"]
        assert saved and "recurring mandate" in saved[-1]["reasoning_summary"]

        # Next order must charge headless through the ladder (mock completes it).
        def fake_charge_ok(order_id, amount_paise, customer_id, token_id, buyer_agent_id=""):
            assert token_id == "token_NEW"
            return PaymentOutcome(
                status="completed",
                order_id=order_id,
                payment_id="pay_HEADLESS",
                message="Charged using saved card token (server-side)",
            )

        monkeypatch.setattr(payment_service, "charge_with_token", fake_charge_ok)
        csid2 = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
        r2 = complete(client, csid2)
        final = r2.json()["checkout_session"]
        assert final["status"] == "completed"
        assert final["payment"]["payment_id"] == "pay_HEADLESS"
        assert not final["payment"].get("awaiting_authorization")
    finally:
        _drop_token_method()


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
