"""C-2 / C-3 payment state-security tests.

C-2: a late callback must never reopen a terminal checkout
     (cancelled/declined/completed -> completed), and a Razorpay signature
     alone is NOT sufficient authorization.
C-3: `_finalize_payment()` is the last line of defense -- it independently
     rejects sessions that are not legally finalizable, and never consumes
     stock/reservations, writes receipts, or emits success audits for an
     invalid finalization.

Joint invariant:
    STATE VALID -> AMOUNT VALID (C-1) -> PAYMENT VALID -> FINALIZE
"""
import pytest

from merchant.domain import checkout_service
from merchant.persistence.db import mutate
from merchant.persistence.models.entities import CheckoutSession, Pack
from merchant.payment import payment_service
from merchant.payment.payment_service import PaymentOutcome
from merchant.tests.conftest import complete, make_checkout


def _audit_actions(client, csid):
    ev = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
    return [e["action"] for e in ev]


def _audit_payloads(client, csid, action):
    ev = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
    return [e.get("payload", {}) for e in ev if e["action"] == action]


def _stock(client, pack_id="pack_milk_amul_500"):
    with mutate() as db:
        return db.get(Pack, pack_id).stock_actual


def _callback(client, csid, order_id, payment_id="pay_sec", signature="sig", monkeypatch=None):
    """Send a payment callback with a valid (monkeypatched) signature."""
    if monkeypatch is not None:
        monkeypatch.setattr(payment_service, "verify_checkout_signature", lambda *a, **k: True)
    return client.post(
        f"/checkout_sessions/{csid}/payment_callback",
        json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        },
    )


def _given(client, csid, *, state, order_id="order_sec_amount"):
    """Deterministically place a session in an exact state with a known order
    and a valid C-1 amount binding (so a callback/direct call isolates the
    STATE logic rather than tripping on order-mismatch or amount-mismatch)."""
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        cs.status = state
        cs.razorpay_order_id = order_id
        cs.authorized_amount_paise = 10400
        cs.razorpay_order_amount = 10400
    return order_id


# ================================================================ C-2: callbacks


def test_c2_1_cancelled_session_callback_never_completes(client, monkeypatch):
    stock0 = _stock(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    # realistic cancel via the API
    r = client.post(
        f"/checkout_sessions/{csid}/cancel",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"},
    )
    assert r.json()["checkout_session"]["status"] == "cancelled"

    # a successful (valid-signature) callback races in afterwards
    order = _given(client, csid, state="cancelled")
    cb = _callback(client, csid, order, monkeypatch=monkeypatch)
    assert cb.status_code == 409
    assert cb.json()["error"]["code"] == "INVALID_STATE_TRANSITION"

    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        assert cs.status == "cancelled"          # never opened to completed
        assert cs.razorpay_payment_id is None    # payment not attached
    # stock untouched by the cancelled session
    assert _stock(client) == stock0
    acts = _audit_actions(client, csid)
    assert "payment_success" not in acts


def test_c2_2_declined_session_callback_never_completes(client, monkeypatch):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    # realistically decline: dead stock at completion
    with mutate() as db:
        db.get(Pack, "pack_milk_amul_500").stock_actual = 0
    stock0 = _stock(client)  # baseline AFTER the dead-stock is set (0)
    complete(client, csid)
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "declined"

    order = _given(client, csid, state="declined")
    cb = _callback(client, csid, order, monkeypatch=monkeypatch)
    assert cb.status_code == 409
    assert cb.json()["error"]["code"] == "INVALID_STATE_TRANSITION"

    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "declined"
    assert _stock(client) == stock0
    assert "payment_success" not in _audit_actions(client, csid)


def test_c2_3_completed_session_duplicate_callback_is_safe(client, monkeypatch):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        assert cs.status == "completed"
        order = cs.razorpay_order_id
    stock_after_first = _stock(client)

    # duplicate successful callback must not finalize a second time
    cb = _callback(client, csid, order, monkeypatch=monkeypatch)
    assert cb.status_code == 409
    assert cb.json()["error"]["code"] == "INVALID_STATE_TRANSITION"

    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "completed"
    assert _stock(client) == stock_after_first                     # no 2nd stock use
    acts = _audit_actions(client, csid)
    assert len([a for a in acts if a == "payment_success"]) == 1   # no 2nd completion
    assert len([a for a in acts if a == "decision_receipt_issued"]) <= 1


def test_c2_4_invalid_non_completable_state_callback_rejected(client, monkeypatch):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    # requires_confirmation is not callback-finalizable
    order = _given(client, csid, state="requires_confirmation")
    cb = _callback(client, csid, order, monkeypatch=monkeypatch)
    assert cb.status_code == 409
    assert cb.json()["error"]["code"] == "INVALID_STATE_TRANSITION"
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "requires_confirmation"


def test_c2_5_valid_signature_but_wrong_state_is_rejected(client, monkeypatch):
    # signature is valid (monkeypatched True) -- but that alone is NOT authority.
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)  # -> completed with a real order
    with mutate() as db:
        order = db.get(CheckoutSession, csid).razorpay_order_id

    # valid signature, already-completed session -> REJECT (not a second charge)
    cb = _callback(client, csid, order, monkeypatch=monkeypatch)
    assert cb.status_code == 409
    assert cb.json()["error"]["code"] == "INVALID_STATE_TRANSITION"
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "completed"
    assert len([a for a in _audit_actions(client, csid) if a == "payment_success"]) == 1


def test_c2_order_and_session_mismatch_rejected(client, monkeypatch):
    # callback references order_A but the session belongs to order_B -> reject, do
    # not finalize (order/session binding; never finalize a foreign order's payment).
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    _given(client, csid, state="requires_payment", order_id="order_B")
    cb = _callback(client, csid, "order_A", monkeypatch=monkeypatch)
    assert cb.status_code == 400                      # order mismatch
    assert cb.json()["error"]["code"] in ("PAYMENT_FAILED", "INVALID_STATE_TRANSITION")
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "requires_payment"
    assert "payment_success" not in _audit_actions(client, csid)


# ================================================================ C-3: finalizer


def _finalize(db, cs, outcome):
    """Raw call to the internal finalizer (bypasses /complete & callback)."""
    return checkout_service._finalize_payment(db, cs, outcome)


def test_c3_valid_requires_payment_to_completed_succeeds(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    _given(client, csid, state="requires_payment")
    out = PaymentOutcome(
        status="completed", order_id="order_sec", payment_id="pay_sec",
        message="direct", simulated=True, raw={"amount": 10400},
    )
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        resp = _finalize(db, cs, out)
        assert cs.status == "completed"


def test_c3_requires_payment_amount_mismatch_still_fails_closed(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    _given(client, csid, state="requires_payment")
    out = PaymentOutcome(
        status="completed", order_id="order_sec", payment_id="pay_sec",
        message="direct", simulated=True, raw={"amount": 10400 + 3900},  # mismatched
    )
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        resp = _finalize(db, cs, out)     # C-1 binding routes to declined
        assert cs.status == "declined"
        assert cs.decline_code == "PAYMENT_BLOCKED"
    assert "payment_amount_mismatch" in _audit_actions(client, csid)


@pytest.mark.parametrize("terminal", ["cancelled", "declined", "completed"])
def test_c3_terminal_state_never_refinalizes(client, terminal):
    stock0 = _stock(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    _given(client, csid, state=terminal)
    out = PaymentOutcome(
        status="completed", order_id="order_sec", payment_id="pay_sec",
        message="direct", simulated=True, raw={"amount": 10400},
    )
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        with pytest.raises(Exception) as ei:
            _finalize(db, cs, out)
        exc = ei.value
        from merchant.domain.checkout_service import ProtocolError
        assert isinstance(exc, ProtocolError)
        assert exc.code == "INVALID_STATE_TRANSITION"
        # nothing mutated: still terminal, no payment attached, no stock consumed
        assert cs.status == terminal
        assert cs.razorpay_payment_id is None
    assert _stock(client) == stock0
    acts = _audit_actions(client, csid)
    assert "payment_success" not in acts
    assert "decision_receipt_issued" not in acts
    assert "checkout_completed" not in acts


@pytest.mark.parametrize("illegal", ["created", "requires_confirmation"])
def test_c3_non_completable_state_rejected(client, illegal):
    stock0 = _stock(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    _given(client, csid, state=illegal)
    out = PaymentOutcome(status="completed", order_id="o", payment_id="p",
                         message="x", simulated=True, raw={"amount": 10400})
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        from merchant.domain.checkout_service import ProtocolError
        with pytest.raises(ProtocolError) as ei:
            _finalize(db, cs, out)
        assert ei.value.code == "INVALID_STATE_TRANSITION"
        assert cs.status == illegal
    assert _stock(client) == stock0
    assert "payment_success" not in _audit_actions(client, csid)


def test_c3_created_to_completed_rejected_even_with_valid_binding(client):
    # defense: a session that never reached a payment-gateway state cannot jump
    # straight to completed even if amounts are present.
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    _given(client, csid, state="created")
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        from merchant.domain.checkout_service import ProtocolError
        with pytest.raises(ProtocolError):
            _finalize(db, cs, PaymentOutcome(status="completed", order_id="o",
                                             payment_id="p", raw={"amount": 10400}))
        assert cs.status == "created"


def test_c3_payment_pending_to_completed_reconcile_succeeds(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    _given(client, csid, state="payment_pending")
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        resp = _finalize(db, cs, PaymentOutcome(
            status="completed", order_id="order_sec", payment_id="pay_sec",
            raw={"amount": 10400},
        ))
        assert cs.status == "completed"      # legitimate reconciliation preserved


def test_c3_payment_pending_reconcile_still_pending_is_noop(client):
    # reconcile returns "pending" because the gateway is still unresolved -> the
    # session must STAY payment_pending (legal no-op), not error or regress.
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    _given(client, csid, state="payment_pending")
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        resp = _finalize(db, cs, PaymentOutcome(
            status="pending", order_id="order_sec", error_code="PAYMENT_PENDING",
        ))
        assert cs.status == "payment_pending"   # preserved
        assert resp.error["code"] == "PAYMENT_PENDING"


# ================================================================ reconcile endpoint


def test_reconcile_payment_pending_to_completed_via_api(client, monkeypatch):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        cs.status = "payment_pending"
        cs.razorpay_order_id = "order_sec"
        cs.authorized_amount_paise = 10400
        cs.razorpay_order_amount = 10400
    from merchant.payment import payment_service as ps
    monkeypatch.setattr(ps, "fetch_order_payments", lambda order_id: [
        {"id": "pay_sec", "status": "captured", "amount": 10400},
    ])
    r = client.post(f"/checkout_sessions/{csid}/reconcile")
    assert r.status_code == 200, r.text
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "completed"


def test_reconcile_cancelled_never_completes(client, monkeypatch):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        cs.status = "cancelled"
        cs.razorpay_order_id = "order_sec"
    from merchant.payment import payment_service as ps
    monkeypatch.setattr(ps, "fetch_order_payments", lambda order_id: [
        {"id": "pay_sec", "status": "captured", "amount": 10400},
    ])
    r = client.post(f"/checkout_sessions/{csid}/reconcile")
    # reconciliation refuses non-payment_pending sessions -> stays cancelled
    assert r.status_code == 409
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "cancelled"


def test_reconcile_declined_never_completes(client, monkeypatch):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        cs.status = "declined"
        cs.razorpay_order_id = "order_sec"
    from merchant.payment import payment_service as ps
    monkeypatch.setattr(ps, "fetch_order_payments", lambda order_id: [
        {"id": "pay_sec", "status": "captured", "amount": 10400},
    ])
    r = client.post(f"/checkout_sessions/{csid}/reconcile")
    assert r.status_code == 409
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "declined"


# ================================================================ idempotency


def test_terminal_session_cannot_trigger_another_payment(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)                 # completed once
    with mutate() as db:
        first_pid = db.get(CheckoutSession, csid).razorpay_payment_id
        first_order = db.get(CheckoutSession, csid).razorpay_order_id

    # a DIFFERENT idempotency key on the now-terminal session must not re-charge
    r = complete(client, csid, key="c2c3-new-key")
    assert r.status_code == 200
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        assert cs.status == "completed"
        assert cs.razorpay_payment_id == first_pid   # no new payment
        assert cs.razorpay_order_id == first_order   # no new order
    acts = _audit_actions(client, csid)
    assert len([a for a in acts if a == "payment_success"]) == 1
    assert len([a for a in acts if a == "razorpay_order_created"]) == 1


# ================================================================ adversarial seqs


def test_seq_A_callback_then_callback_again(client, monkeypatch):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    order = _given(client, csid, state="requires_payment")
    r1 = _callback(client, csid, order, monkeypatch=monkeypatch)
    assert r1.status_code == 200
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "completed"
    r2 = _callback(client, csid, order, monkeypatch=monkeypatch)
    assert r2.status_code == 409                       # second callback rejected
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "completed"
    acts = _audit_actions(client, csid)
    assert len([a for a in acts if a == "payment_success"]) == 1


def test_seq_B_cancel_then_callback(client, monkeypatch):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    order = _given(client, csid, state="requires_payment")
    client.post(f"/checkout_sessions/{csid}/cancel",
                json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"})
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "cancelled"
    cb = _callback(client, csid, order, monkeypatch=monkeypatch)
    assert cb.status_code == 409
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "cancelled"  # never opened
    assert "payment_success" not in _audit_actions(client, csid)


def test_seq_C_callback_then_cancel_then_callback(client, monkeypatch):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    order = _given(client, csid, state="requires_payment")
    _callback(client, csid, order, monkeypatch=monkeypatch)          # -> completed
    # cancel on a completed (terminal) session is rejected by cancel_checkout
    r = client.post(f"/checkout_sessions/{csid}/cancel",
                    json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"})
    assert r.status_code == 409
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "completed"   # still completed
    cb2 = _callback(client, csid, order, monkeypatch=monkeypatch)    # duplicate
    assert cb2.status_code == 409
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "completed"
    assert len([a for a in _audit_actions(client, csid) if a == "payment_success"]) == 1


def test_seq_D_payment_pending_cancel_reconcile(client, monkeypatch):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    order = _given(client, csid, state="payment_pending")
    r = client.post(f"/checkout_sessions/{csid}/cancel",
                    json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"})
    assert r.status_code == 200                                      # pending is cancellable
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "cancelled"
    from merchant.payment import payment_service as ps
    monkeypatch.setattr(ps, "fetch_order_payments", lambda order_id: [
        {"id": "pay_sec", "status": "captured", "amount": 10400},
    ])
    rr = client.post(f"/checkout_sessions/{csid}/reconcile")
    assert rr.status_code == 409                                     # cancelled not reconciliable
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "cancelled"   # never reopens


def test_seq_E_declined_then_callback(client, monkeypatch):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    order = _given(client, csid, state="declined")
    cb = _callback(client, csid, order, monkeypatch=monkeypatch)
    assert cb.status_code == 409
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "declined"
    assert "payment_success" not in _audit_actions(client, csid)


def test_seq_F_completed_then_callback(client, monkeypatch):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    order = _given(client, csid, state="completed")
    cb = _callback(client, csid, order, monkeypatch=monkeypatch)
    assert cb.status_code == 409
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "completed"
    assert len([a for a in _audit_actions(client, csid) if a == "payment_success"]) == 0
