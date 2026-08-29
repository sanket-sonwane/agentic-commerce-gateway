"""AP2 crypto authorization & provenance layer tests (PRD §14–31)."""
import json

from merchant_agent.db import mutate
from merchant_agent.models.entities import AuditEvent, CheckoutSession, ExecutionMandate
from merchant_agent.services import crypto_service
from merchant_agent.tests.conftest import complete, make_checkout


# ---------------------------------------------------------------- crypto unit


def test_sign_and_verify_canonical_payload():
    payload = {"cd": "z", "ab": [2, 1], "text": "héllo", "n": 5}
    sig = crypto_service.sign(payload)
    assert crypto_service.verify(payload, sig) is True
    # tampering with the payload must invalidate the signature
    assert crypto_service.verify({"cd": "z", "ab": [2, 1], "text": "héllo", "n": 6}, sig) is False


def test_canonical_json_is_key_sorted():
    a = crypto_service.canonical_json({"b": 1, "a": 2, "c": 3})
    b = crypto_service.canonical_json({"c": 3, "b": 1, "a": 2})
    assert a == b == '{"a":2,"b":1,"c":3}'


def test_object_hash_excludes_signature():
    base = {"id": "x", "value": 1, "signature": "SIG", "db_meta": "ignore"}
    other = {"id": "x", "value": 1, "signature": "DIFFERENT", "db_meta": "ignore"}
    assert crypto_service.object_hash(base) == crypto_service.object_hash(other)
    # changing real content changes the hash
    tampered = {"id": "y", "value": 1, "signature": "SIG", "db_meta": "ignore"}
    assert crypto_service.object_hash(base) != crypto_service.object_hash(tampered)


# ---------------------------------------------------------------- mandate on completion


def test_completion_creates_signed_execution_mandate(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    assert r.status_code == 200, r.text
    body = r.json()
    # session exposes the authorized amount + mandate binding
    assert body["checkout_session"]["payment"]["authorized_amount_paise"] is not None
    assert body["checkout_session"]["payment"]["execution_mandate_id"]

    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        m = db.get(ExecutionMandate, cs.execution_mandate_id)
        assert m is not None
        # signature over canonical payload is valid
        from merchant_agent.services import authorization_service

        payload = authorization_service.execution_mandate_payload(m)
        assert crypto_service.verify(payload, m.signature) is True
        assert m.total_paise == cs.authorized_amount_paise == cs.razorpay_order_amount

        assert any(
            e.action == "execution_mandate_created"
            for e in db.query(AuditEvent).filter_by(checkout_session_id=csid).all()
        )


def test_provenance_verifies_for_completed(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)
    p = client.get(f"/provenance/{csid}").json()
    assert p["provenance_available"] is True
    assert p["status"] == "completed"
    assert all(c["ok"] for c in p["checks"]), p
    assert p["artifacts"]["execution_mandate"]["total_paise"] == p["artifacts"]["decision_receipt"]["authorized_amount_paise"]


def test_provenance_not_available_without_mandate(client):
    """Simulate a legacy transaction that predates AP2 (no execution mandate)."""
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        cs.execution_mandate_id = None
    p = client.get(f"/provenance/{csid}").json()
    assert p["provenance_available"] is False
    assert "NOT_AVAILABLE" in p["summary"]


# ---------------------------------------------------------------- amount binding / tamper


def test_amount_tamper_detected_in_provenance(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)
    # attacker edits the persisted authorized amount in place
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        cs.authorized_amount_paise = cs.authorized_amount_paise + 1
    p = client.get(f"/provenance/{csid}").json()
    amount_checks = [c for c in p["checks"] if c["name"] in ("amount_binding", "order_amount_binding")]
    assert any(c["ok"] is False for c in amount_checks)


def test_mandate_signature_tamper_detected(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        m = db.get(ExecutionMandate, cs.execution_mandate_id)
        m.signature = "0" * 128
    p = client.get(f"/provenance/{csid}").json()
    sig_checks = [c for c in p["checks"] if c["name"] == "execution_mandate_signature"]
    assert sig_checks and sig_checks[0]["ok"] is False


# ---------------------------------------------------------------- callback hardening


def test_duplicate_callback_on_completed_is_blocked(client):
    """A second callback for an already-completed session must be rejected."""
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)
    # fabricate the same callback after completion: state guard must block it
    body = {
        "razorpay_order_id": "order_x_dupe",
        "razorpay_payment_id": "pay_x_dupe",
        "razorpay_signature": "bad",
    }
    r = client.post(f"/checkout_sessions/{csid}/payment_callback", json=body)
    err = r.json().get("error", {})
    assert r.status_code in (400, 409)
    # order mismatch precedes state check, but must never complete
    assert err.get("code") == "PAYMENT_FAILED" or err.get("code") == "INVALID_STATE_TRANSITION"


# ---------------------------------------------------------------- intent mandate


def test_intent_mandate_creation(client):
    r = client.post(
        "/mandates/intents",
        json={
            "buyer_agent_id": "buyer_agent_llm_01",
            "delegation_token": "dlg_9f8e7d",
            "intent": "Buy groceries for my household",
        },
    )
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["type"] == "intent"
    assert m["merchant_id"] == "merchant_grocery_01"
    # signature verifies against the merchant public key
    from merchant_agent.services import authorization_service

    payload = {
        "mandate_id": m["mandate_id"],
        "type": "intent",
        "merchant_id": m["merchant_id"],
        "buyer_agent_id": m["buyer_agent_id"],
        "intent": m["intent"],
        "constraints": m["constraints"],
        "iss_ts": m["issued_at"],
        "exp": m["expires_at"],
        "nonce": m["nonce"],
        "version": m["version"],
    }
    assert crypto_service.verify(payload, m["signature"], public_key_pem_str=m["verifiable_public_key"]) is True


def test_audit_verify_includes_signed_anchor(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)
    v = client.get("/audit_logs/verify").json()
    assert v["intact"] is True
    assert v["anchor"] is not None
    assert v["anchor"]["signature"]


# ---------------------------------------------------------------- fail-closed + replay


def test_execution_mandate_verify_failure_blocks_payment(client, monkeypatch):
    """If Execution Mandate verification fails, completion is blocked and
    NO Razorpay order is created (PRD §24: any check fail -> no call)."""
    from merchant_agent.services import authorization_service

    def _forced_fail(*args, **kwargs):
        return {
            "signature_valid": False, "key_trusted": True, "merchant_id": True,
            "buyer_id": True, "session_id": True, "amount_matches": True,
            "currency": True, "cart_hash": True, "not_expired": True,
            "fresh_issued": True, "nonce_unused": True, "policy_approved": True,
        }

    monkeypatch.setattr(authorization_service, "verify_execution_mandate", _forced_fail)

    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "PAYMENT_BLOCKED"
    # session declined, no order was created
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        assert cs.status == "declined"
        assert cs.razorpay_order_id is None
    # no successful completion
    assert "completed" not in r.json().get("checkout_session", {}).get("status", "")


def test_reused_mandate_nonce_is_detected(client):
    """A mandate referenced by an already-issued decision receipt is a replay."""
    from merchant_agent.models.entities import PolicyDecision
    from merchant_agent.services import authorization_service

    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)

    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        mandate = db.get(ExecutionMandate, cs.execution_mandate_id)
        decision = (
            db.query(PolicyDecision)
            .filter(PolicyDecision.execution_mandate_id == mandate.mandate_id)
            .first()
        )
        # simulate a second settled payment against the SAME mandate -> replay
        authorization_service.issue_decision_receipt(
            db,
            cs,
            mandate=mandate,
            decision=decision,
            outcome="completed",
            razorpay_order_id=cs.razorpay_order_id,
            razorpay_payment_id="pay_replay_probe",
        )
        checks = authorization_service.verify_execution_mandate(
            db, mandate, cs, buyer_agent_id=cs.buyer_agent_id
        )
        assert checks["nonce_unused"] is False

