"""C-1 payment amount binding tests.

Prove the FINAL AUTHORIZED == PERSISTED AUTHORIZED == RAZORPAY ORDER
== VERIFIED PAYMENT invariant, and that any mismatch FAILS CLOSED
(no successful completion, no merchant/LLM/buyer/request control over amount).
"""
import json

import pytest

from merchant.persistence.db import mutate
from merchant.persistence.models.entities import CheckoutSession, ExecutionMandate
from merchant.tests.conftest import complete, make_checkout
from merchant.payment.payment_service import PaymentOutcome


def _session_binding(client, csid):
    """Serialized payment-binding fields as dict."""
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        return {
            "authorized": cs.authorized_amount_paise,
            "order": cs.razorpay_order_amount,
            "verified": cs.verified_payment_amount_paise,
        }


def _audit_actions(client, csid):
    ev = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
    return [e["action"] for e in ev]


# ---------------------------------------------------------------- Test 1 — normal


def test_1_normal_amount_completes_and_amounts_bind(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkout_session"]["status"] == "completed"
    pay = body["checkout_session"]["payment"]
    # all three amounts identical and positive; amount_match true
    assert pay["authorized_amount_paise"] == pay["razorpay_order_amount_paise"] \
        == pay["verified_payment_amount_paise"]
    assert pay["authorized_amount_paise"] > 0
    assert pay["amount_match"] is True
    # DB keeps the same invariant
    b = _session_binding(client, csid)
    assert b["authorized"] == b["order"] == b["verified"] > 0


# ---------------------------------------------------------------- Test 2 — order amount mismatch


def test_2_order_amount_mismatch_fails_closed(client, monkeypatch):
    from merchant.payment import payment_service
    real = payment_service.create_order

    def _bad_order(csid, amount_rupees, **kw):
        order = real(csid, amount_rupees, **kw)
        order["amount"] = order["amount"] + 3900  # echo a disagreeing order amount
        return order

    monkeypatch.setattr(payment_service, "create_order", _bad_order)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "PAYMENT_BLOCKED"
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        # fail closed: no completed state, no stray order persisted
        assert cs.status == "declined"
        assert cs.razorpay_order_id is None  # diverged order never persisted


# ---------------------------------------------------------------- Test 3 — payment amount mismatch


def test_3_payment_amount_mismatch_fails_closed(client, monkeypatch):
    from merchant.payment import payment_service

    def _bad_capture(order_id, amount_rupees):
        # Sim-completed but reports a verified amount LARGER than authorized.
        return PaymentOutcome(
            status="completed",
            order_id=order_id,
            payment_id="pay_c1_mismatch",
            message="simulated",
            simulated=True,
            raw={"amount": amount_rupees * 100 + 3900},
        )

    monkeypatch.setattr(payment_service, "execute_sim_capture", _bad_capture)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    # gateway said completed, but verified != authorized -> NO successful completion
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "PAYMENT_BLOCKED"
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        assert cs.status == "declined"
        assert cs.razorpay_payment_id == "pay_c1_mismatch"
        assert cs.verified_payment_amount_paise == cs.razorpay_order_amount + 3900
    assert "payment_amount_mismatch" in _audit_actions(client, csid)


# ---------------------------------------------------------------- Test 4 — retry / idempotency


def test_4_retry_same_order_returns_same_result_no_duplicate(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r1 = complete(client, csid, key="c1-idem")
    assert r1.status_code == 200
    pay1 = r1.json()["checkout_session"]["payment"]["payment_id"]
    order1 = r1.json()["checkout_session"]["payment"]["order_id"]

    for _ in range(3):
        r2 = complete(client, csid, key="c1-idem")
        assert r2.status_code == 200
        p2 = r2.json()["checkout_session"]["payment"]
        assert p2["payment_id"] == pay1
        assert p2["order_id"] == order1  # same order, no duplicate
        assert p2["amount_match"] is True

    acts = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
    assert len([a for a in acts if a["action"] == "razorpay_order_created"]) == 1
    assert len([a for a in acts if a["action"] == "payment_success"]) == 1


# ---------------------------------------------------------------- Test 5 — session total mutated after authorization


def test_5_mutated_session_total_after_authorization_detected(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        # mutate the persisted session total AFTER authorization
        t = json.loads(cs.totals)
        t["total"] = t["total"] + 1
        cs.totals = json.dumps(t)
        em = db.get(ExecutionMandate, cs.execution_mandate_id)
        from merchant.ap2 import authorization_service
        checks = authorization_service.verify_execution_mandate(
            db, em, cs, buyer_agent_id=cs.buyer_agent_id
        )
        # the independent authorization binding detects the session-total mutation
        assert checks["amount_matches_persisted"] is False
        # (the naive mandate-vs-authorized check does NOT — it only compares the
        #  two sanctioned fields, so tampering persisted totals hides)
        assert checks["amount_matches"] is True


# ---------------------------------------------------------------- Test 6 — buyer amount manipulation ignored


def test_6_buyer_supplied_amount_is_ignored(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    # Buyer tries to slip a payment amount into the complete request.
    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={
            "buyer_agent_id": "buyer_agent_llm_01",
            "delegation_token": "dlg_9f8e7d",
            "amount": 200,          # ignored
            "total": 99999,         # ignored
            "payment_amount_paise": 2000000,  # ignored
        },
    )
    assert r.status_code == 200, r.text
    pay = r.json()["checkout_session"]["payment"]
    assert pay["amount_match"] is True
    # the authorized amount is the SERVER-computed total from the catalog
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        server_total = round(json.loads(cs.totals)["total"] * 100)
        assert cs.authorized_amount_paise == server_total
        assert cs.razorpay_order_amount == server_total
        assert cs.verified_payment_amount_paise == server_total


# ---------------------------------------------------------------- Test 7 — Merchant LLM cannot influence amount


def test_7_server_authority_fixes_amount_ignores_any_external_value(client):
    # The completion request carries no amount field in the protocol; the amount
    # is derived solely from the authoritative catalog + deterministic policy.
    # Prove the persisted amount equals the server recomputation, never an input.
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    assert r.status_code == 200, r.text
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        em = db.get(ExecutionMandate, cs.execution_mandate_id)
        assert cs.authorized_amount_paise == em.total_paise
        # authorized amount equals the authoritative catalog-derived total
        totals = json.loads(cs.totals)
        assert em.total_paise == round(totals["total"] * 100)


# ---------------------------------------------------------------- Test 8 — AP2 execution mandate mismatch


def test_8_execution_mandate_mismatch_detected(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        em = db.get(ExecutionMandate, cs.execution_mandate_id)
        # force the authorized binding to disagree with the mandate
        cs.authorized_amount_paise = em.total_paise + 100
        from merchant.ap2 import authorization_service
        checks = authorization_service.verify_execution_mandate(
            db, em, cs, buyer_agent_id=cs.buyer_agent_id
        )
        # the execution authorization cannot disagree with the final amount
        assert checks["amount_matches"] is False
        # but the persisted catalog total is untouched (only authorized was tampered),
        # so the independent persistence cross-check still agrees.
        assert checks["amount_matches_persisted"] is True
    p = client.get(f"/provenance/{csid}").json()
    amt = [c for c in p["checks"] if c["name"] in ("amount_binding", "order_amount_binding")]
    assert any(c["ok"] is False for c in amt)
