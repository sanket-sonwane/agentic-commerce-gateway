"""Consumer provenance projection tests (backend, presentation-only).

The projection is a thin serializer over authoritative evidence. These tests
assert the shape and truthful content WITHOUT re-verifying crypto (that is the
AP2 layer's job, already covered elsewhere).
"""
import json

from merchant.persistence.db import mutate
from merchant.persistence.models.entities import CheckoutSession, ExecutionMandate
from merchant.tests.conftest import complete, make_checkout


def _get(client, csid):
    r = client.get(f"/consumer/transactions/{csid}/provenance")
    assert r.status_code == 200, r.text
    return r.json()


def _create_intent(client, buyer="buyer_agent_llm_01", delegation="dlg_9f8e7d", intent="Buy groceries for my household"):
    r = client.post(
        "/mandates/intents",
        json={"buyer_agent_id": buyer, "delegation_token": delegation, "intent": intent},
    )
    assert r.status_code == 201, r.text
    return r.json()["mandate_id"]


def _complete_with_intent(client, csid, im):
    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d",
              "intent_mandate_id": im},
    )
    return r


# ---------------------------------------------------------------- 1. success


def test_successful_transaction(client):
    im = _create_intent(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = _complete_with_intent(client, csid, im)
    assert r.status_code == 200, r.text

    p = _get(client, csid)
    assert p["transaction"]["status"] == "completed"
    assert p["razorpay_called"] is True
    assert p["payment"]["status"] == "verified"
    assert p["payment"]["razorpay_called"] is True
    assert p["stopped_at"] is None
    assert p["current_stage"] == "completed"
    assert p["proof"]["provenance"] == "verified"
    assert p["proof"]["audit_chain"] == "verified"
    assert p["proof"]["verifier_status"] == "VALID"
    assert p["transaction"]["merchant"]  # display name present


# ---------------------------------------------------------------- 2. policy decline


def test_policy_decline(client):
    # spend_limit failure: saffron (₹2,600) under restricted ₹500 delegation
    # is declined at gating time, before any money movement.
    csb = make_checkout(
        client,
        items=[{"product_id": "prod_saffron_premium", "pack_id": "pack_saffron_5g", "quantity": 1}],
        delegation="dlg_restricted_50",
        buyer="buyer_agent_llm_01",
    )
    assert csb.status_code == 409, csb.text
    csid = csb.json()["checkout_session"]["checkout_session_id"]

    p = _get(client, csid)
    assert p["transaction"]["status"] == "declined"
    assert p["razorpay_called"] is False
    assert p["payment"]["status"] == "not_called"
    assert p["stopped_at"] == "safety_checks"
    assert p["failure_stage"] == "safety_checks"
    assert any(c["status"] == "fail" for c in p["checks"])


# ---------------------------------------------------------------- 3. amount / intent decline


def test_amount_decline_via_intent_scope(client):
    # Intent with a tiny max order -> execution blocked (PAYMENT_BLOCKED)
    # BEFORE any mandate link or money movement.
    r = client.post(
        "/mandates/intents",
        json={
            "buyer_agent_id": "buyer_agent_llm_01",
            "delegation_token": "dlg_9f8e7d",
            "intent": "Buy only cheap things",
            "constraints_override": {"max_order_value_paise": 100},
        },
    )
    assert r.status_code == 201, r.text
    im = r.json()["mandate_id"]
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = _complete_with_intent(client, csid, im)
    assert r.status_code == 409, r.text

    p = _get(client, csid)
    assert p["transaction"]["status"] == "declined"
    assert p["razorpay_called"] is False
    assert p["payment"]["status"] == "not_called"
    assert p["stopped_at"] in ("authorization", "safety_checks")
    # Honest failure framing: intent scope blocked this execution.
    assert any(w in (p["failure_reason"] or "") for w in ("intent", "scope", "not authorize"))


# ---------------------------------------------------------------- 4. payment decline


def test_payment_decline_razorpay_called(client):
    # simulate a gateway decline: complete normally (sim) then override to declined via callback
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    assert r.status_code == 200, r.text

    # Drive a gateway decline through the authoritative path: payment_callback with
    # a failed signature/order after an order was created. Simpler: reuse sim by
    # marking a payment_failure event + declined state through a reconcile-callback.
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        cs.status = "declined"
        cs.decline_code = "PAYMENT_FAILED"
        cs.razorpay_payment_id = "pay_fail"
        db.flush()
        from merchant.evidence import audit_service
        audit_service.append_event(
            db, actor="razorpay", action="payment_failure", checkout_session_id=csid,
            decision="declined", rules_fired=["PAYMENT_FAILED"],
            reasoning_summary="Gateway declined the payment",
        )

    p = _get(client, csid)
    assert p["razorpay_called"] is True
    assert p["payment"]["status"] == "declined"
    assert p["payment"]["payment_id"] == "pay_fail"
    assert p["stopped_at"] == "razorpay"
    assert p["failure_stage"] == "razorpay"


# ---------------------------------------------------------------- 5. payment pending


def test_payment_pending(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    assert r.status_code == 200, r.text
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        cs.status = "payment_pending"
        db.flush()
        from merchant.evidence import audit_service
        audit_service.append_event(
            db, actor="razorpay", action="payment_pending", checkout_session_id=csid,
            decision="pending", reasoning_summary="Payment unresolved; reconciliation required",
        )

    p = _get(client, csid)
    assert p["payment"]["status"] == "pending"
    assert p["razorpay_called"] is True
    assert p["stopped_at"] == "razorpay"
    assert p["failure_stage"] == "razorpay"


# ---------------------------------------------------------------- 6. Razorpay not called


def test_razorpay_not_called_not_inferred_from_missing_id(client):
    # Session that never reached the gateway: pending authorization only.
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    with mutate() as db:
        # Wait until order created, then NOT verify — park as requires_payment
        db.get(CheckoutSession, csid).status = "requires_payment"
    p = _get(client, csid)
    # No order id, no razorpay event -> authoritative False (not inferred from payment_id)
    assert p["razorpay_called"] is False
    assert p["payment"]["status"] == "not_called"


# ---------------------------------------------------------------- 7. authorization info


def test_authorization_information(client):
    im = _create_intent(client, intent="Weekly grocery run")
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    _complete_with_intent(client, csid, im)

    p = _get(client, csid)
    auth = p["authorization"]
    assert auth["authorized_by"]  # present
    assert "signed by you" not in json.dumps(auth).lower()
    assert "delegation" in json.dumps(auth).lower()  # truthful authority framing
    assert auth["scope"] == "Weekly grocery run"
    assert auth["agent"]
    # signed intent cap surfaced from the linked Intent Mandate
    assert auth["max_amount_paise"] is not None and auth["max_amount_paise"] > 0
    # merchant display present
    assert p["transaction"]["merchant"]


# ---------------------------------------------------------------- 8. proof status


def test_proof_status(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    assert r.status_code == 200, r.text
    p = _get(client, csid)
    assert p["proof"]["execution_mandate"] == "verified"
    assert p["proof"]["decision_receipt"] == "verified"
    assert p["proof"]["provenance"] == "verified"
    assert p["proof"]["audit_chain"] == "verified"
    assert p["proof"]["mandate_id"]


# ---------------------------------------------------------------- 9. missing optional artifacts


def test_missing_optional_artifacts_no_intent(client):
    # A completed transaction with no linked Intent Mandate must degrade gracefully.
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)  # no intent_mandate_id
    assert r.status_code == 200, r.text
    p = _get(client, csid)
    assert p["authorization"]["scope"]  # falls back to delegation framing
    assert p["proof"]["provenance"] == "verified"
    assert "merchant_signed" not in p["authorization"].get("scope", "")


def test_missing_optional_artifacts_predates_ap2(client):
    # Simulate a session with no execution mandate (pre-AP2) -> provenance unavailable.
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    with mutate() as db:
        db.get(CheckoutSession, csid).execution_mandate_id = None
    p = _get(client, csid)
    assert p["proof"]["provenance"] == "unavailable"
    assert p["proof"]["verifier_status"] == "NOT_AVAILABLE"
    assert p["transaction"]["status"] in ("requires_payment", "created")


# ---------------------------------------------------------------- 10. unknown session


def test_unknown_session(client):
    r = client.get("/consumer/transactions/cs_does_not_exist/provenance")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "CHECKOUT_NOT_FOUND"


def test_no_secrets_exposed(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    assert r.status_code == 200, r.text
    raw = json.dumps(_get(client, csid))
    assert "BEGIN PRIVATE KEY" not in raw
    assert "rzp_" not in raw
    assert "admin_demo_token" not in raw
    assert "cvv" not in raw.lower()
