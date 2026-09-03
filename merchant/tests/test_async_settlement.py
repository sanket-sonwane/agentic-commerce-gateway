"""Async-settlement hardening: customer-setup retries, idempotent-quirk parse
fix, async token-charge reconciliation, background reconcile worker, and the
retryable (non-declining) transient customer-setup path.

These cover the real Razorpay behavior: /payments/create/recurring returns the
payment id WITHOUT a status, the payment sits at the bank in `created`, and
capture settles minutes later.
"""
import json

import requests

from merchant.config import settings
from merchant.persistence.db import mutate
from merchant.persistence.models.entities import CheckoutSession
from merchant.payment import payment_service
from merchant.payment.payment_service import PaymentOutcome
from merchant.domain.reconcile_worker import reconcile_all_pending_once
from merchant.tests.conftest import complete, make_checkout

BUYER = "buyer_agent_llm_01"


def _with_credentials(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_key")
    monkeypatch.setattr(settings, "razorpay_key_secret", "secret")


def _driver_saved(monkeypatch):
    monkeypatch.setattr(settings, "payment_driver", "saved")


# ================================================================ customer setup


def test_ensure_customer_retries_transient_then_succeeds(monkeypatch):
    _with_credentials(monkeypatch)
    monkeypatch.setattr(payment_service.time, "sleep", lambda s: None)

    class FakeCustomerApi:
        def __init__(self):
            self.calls = 0

        def create(self, payload):
            self.calls += 1
            if self.calls < 3:
                raise requests.exceptions.ConnectionError(
                    "Remote end closed connection without response"
                )
            return {"id": "cust_RETRY"}

    class FakeClient:
        customer = FakeCustomerApi()

    monkeypatch.setattr(payment_service, "_get_client", lambda: FakeClient())

    customer = payment_service.ensure_customer(BUYER)
    assert customer["id"] == "cust_RETRY"
    assert FakeClient.customer.calls == 3


def test_ensure_customer_transient_exhausted_still_raises(monkeypatch):
    _with_credentials(monkeypatch)
    monkeypatch.setattr(payment_service.time, "sleep", lambda s: None)

    class FakeCustomerApi:
        def create(self, payload):
            raise requests.exceptions.ConnectionError(
                "Remote end closed connection without response"
            )

    class FakeClient:
        customer = FakeCustomerApi()

    monkeypatch.setattr(payment_service, "_get_client", lambda: FakeClient())

    import pytest

    with pytest.raises(requests.exceptions.ConnectionError):
        payment_service.ensure_customer(BUYER)


def test_ensure_customer_non_transient_does_not_retry(monkeypatch):
    _with_credentials(monkeypatch)

    class FakeCustomerApi:
        def create(self, payload):
            raise ValueError("bad configuration")

    class FakeClient:
        customer = FakeCustomerApi()

    monkeypatch.setattr(payment_service, "_get_client", lambda: FakeClient())

    import pytest

    with pytest.raises(ValueError):
        payment_service.ensure_customer(BUYER)


# ================================================================ headless charge


def test_headless_charge_parses_razorpay_payment_id_without_status(monkeypatch):
    """create/recurring returns razorpay_payment_id and NO status — the real
    cause of the 'Gateway state None' pending. Must park with the real id."""
    _with_credentials(monkeypatch)

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "razorpay_payment_id": "pay_ASYNC",
                "razorpay_order_id": "order_ASYNC",
                "razorpay_signature": "sig",
            }

    monkeypatch.setattr(payment_service.httpx, "post", lambda url, **kw: FakeResp())

    outcome = payment_service.charge_with_token(
        "order_ASYNC", 5000, "cust_ASYNC", "token_ASYNC", buyer_agent_id=BUYER
    )
    assert outcome.status == "pending"
    assert outcome.payment_id == "pay_ASYNC"
    assert outcome.error_code == "PAYMENT_PENDING"


def test_headless_charge_created_status_parks_with_payment_id(monkeypatch):
    _with_credentials(monkeypatch)

    class FakeResp:
        status_code = 200

        def json(self):
            return {"id": "pay_CREATED", "status": "created"}

    monkeypatch.setattr(payment_service.httpx, "post", lambda url, **kw: FakeResp())

    outcome = payment_service.charge_with_token(
        "order_C", 5000, "cust_C", "token_C", buyer_agent_id=BUYER
    )
    assert outcome.status == "pending"
    assert outcome.payment_id == "pay_CREATED"


# ================================================================ reconciliation


def test_reconcile_polls_until_captured(monkeypatch):
    _with_credentials(monkeypatch)
    monkeypatch.setattr(payment_service.time, "sleep", lambda s: None)
    states = iter(
        [
            PaymentOutcome(
                status="pending", order_id="order_P", error_code="PAYMENT_PENDING"
            ),
            PaymentOutcome(
                status="completed", order_id="order_P", payment_id="pay_P",
                raw={"amount": 5000},
            ),
        ]
    )
    monkeypatch.setattr(payment_service, "_reconcile_once", lambda *a, **k: next(states))

    outcome = payment_service.reconcile_pending(
        "order_P", payment_id="pay_P", poll_for_seconds=20.0
    )
    assert outcome.status == "completed"
    assert outcome.payment_id == "pay_P"


def test_reconcile_polls_stops_pending_at_deadline(monkeypatch):
    _with_credentials(monkeypatch)
    monkeypatch.setattr(payment_service.time, "sleep", lambda s: None)

    def still_pending(*a, **k):
        return PaymentOutcome(
            status="pending", order_id="order_P", error_code="PAYMENT_PENDING"
        )

    monkeypatch.setattr(payment_service, "_reconcile_once", still_pending)

    outcome = payment_service.reconcile_pending(
        "order_P", payment_id="pay_P", poll_for_seconds=8.0
    )
    assert outcome.status == "pending"


def test_reconcile_enriches_by_payment_id_when_order_list_lags(monkeypatch):
    _with_credentials(monkeypatch)
    monkeypatch.setattr(payment_service, "fetch_order_payments", lambda oid: [])
    monkeypatch.setattr(
        payment_service,
        "fetch_payment",
        lambda pid: {"id": pid, "status": "captured", "amount": 9600},
    )

    outcome = payment_service.reconcile_pending("order_L", payment_id="pay_L")
    assert outcome.status == "completed"
    assert outcome.payment_id == "pay_L"
    assert outcome.raw["amount"] == 9600


# ================================================== transient customer setup path


def test_transient_customer_setup_keeps_session_retryable(client, monkeypatch):
    _driver_saved(monkeypatch)

    def flaky_ensure(buyer_agent_id):
        raise requests.exceptions.ConnectionError(
            "Remote end closed connection without response"
        )

    monkeypatch.setattr(payment_service, "ensure_customer", flaky_ensure)

    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["retryable"] is True
    assert body["error"]["code"] == "PAYMENT_FAILED"
    session = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    assert session["status"] == "requires_payment"


def test_non_transient_customer_setup_still_declines(client, monkeypatch):
    _driver_saved(monkeypatch)
    monkeypatch.setattr(
        payment_service, "ensure_customer", lambda b: (_ for _ in ()).throw(ValueError("bad creds"))
    )

    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PAYMENT_FAILED"
    session = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    assert session["status"] == "declined"


def test_transient_customer_setup_not_pinned_by_idempotency(client, monkeypatch):
    """A retryable transient failure must NOT be idempotently replayed: retrying
    with the same key re-runs the ladder and can succeed."""
    _driver_saved(monkeypatch)
    calls = []

    def once_flaky_ensure(buyer_agent_id):
        calls.append(1)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("Remote end closed connection")
        return {"id": "cust_RETRY"}

    monkeypatch.setattr(payment_service, "ensure_customer", once_flaky_ensure)

    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r1 = complete(client, csid, key="comp_same_key")
    assert r1.status_code == 200
    assert r1.json()["error"]["retryable"] is True

    r2 = complete(client, csid, key="comp_same_key")
    assert r2.status_code == 200
    assert r2.json()["error"] is None or not r2.json()["error"].get("retryable")
    assert len(calls) == 2  # re-ran; not a byte-for-byte replay of the error


# ============================================================== reconcile worker


def _insert_pending(csid="cs_async_1", order="order_ASYNC", pay="pay_ASYNC"):
    with mutate() as db:
        db.add(
            CheckoutSession(
                checkout_session_id=csid,
                status="payment_pending",
                buyer_agent_id=BUYER,
                delegation_id="dlg_9f8e7d",
                line_items=json.dumps(
                    [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}]
                ),
                totals=json.dumps({"total": 44}),
                payment_driver="saved",
                razorpay_order_id=order,
                razorpay_payment_id=pay,
                authorized_amount_paise=4400,
                razorpay_order_amount=4400,
            )
        )


def test_reconcile_worker_finalises_captured_pending_session(client, monkeypatch):
    _driver_saved(monkeypatch)
    _with_credentials(monkeypatch)
    _insert_pending()

    def fake_reconcile(order_id, *, payment_id=None, poll_for_seconds=None):
        return PaymentOutcome(
            status="completed", order_id=order_id, payment_id="pay_ASYNC",
            message="Confirmed captured during reconciliation", raw={"amount": 4400},
        )

    monkeypatch.setattr(payment_service, "reconcile_pending", fake_reconcile)

    assert reconcile_all_pending_once() == 1
    with mutate() as db:
        cs = db.get(CheckoutSession, "cs_async_1")
        assert cs.status == "completed"
        assert cs.razorpay_payment_id == "pay_ASYNC"
        assert cs.verified_payment_amount_paise == 4400


def test_reconcile_worker_leaves_sticky_pending(client, monkeypatch):
    _driver_saved(monkeypatch)
    _with_credentials(monkeypatch)
    _insert_pending()

    def fake_reconcile(order_id, *, payment_id=None, poll_for_seconds=None):
        return PaymentOutcome(
            status="pending", order_id=order_id, error_code="PAYMENT_PENDING",
            message="No captured payment yet; still unresolved",
        )

    monkeypatch.setattr(payment_service, "reconcile_pending", fake_reconcile)

    assert reconcile_all_pending_once() == 0
    with mutate() as db:
        assert db.get(CheckoutSession, "cs_async_1").status == "payment_pending"


def test_reconcile_worker_noop_for_sim_driver(client, monkeypatch):
    # Default conftest driver is sim: the worker must not even look.
    _insert_pending()
    assert reconcile_all_pending_once() == 0