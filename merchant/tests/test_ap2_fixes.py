"""Regression tests for the AP2 post-implementation audit fixes.

Covers the three coordinator gaps identified in the audit:
  RE-3  Intent Mandate is now linked to and ENFORCED for the Execution Mandate.
  RE-1  Verification no longer a tautology (amount cross-checked vs persisted totals).
  RE-4  Mandates are single-use: a consumed mandate can never authorize another payment.
"""
import json

from merchant_agent.db import mutate
from merchant_agent.models.entities import CheckoutSession, ExecutionMandate
from merchant_agent.services import authorization_service, crypto_service
from merchant_agent.tests.conftest import make_checkout

BUYER = "buyer_agent_llm_01"
DLG = "dlg_9f8e7d"


def _create_intent(client, intent="Buy groceries for my household", constraints_override=None):
    body = {
        "buyer_agent_id": BUYER,
        "delegation_token": DLG,
        "intent": intent,
    }
    if constraints_override is not None:
        body["constraints_override"] = constraints_override
    r = client.post("/mandates/intents", json=body)
    assert r.status_code == 201, r.text
    return r.json()["mandate_id"]


def _complete(client, csid, intent_mandate_id=None):
    body = {"buyer_agent_id": BUYER, "delegation_token": DLG}
    if intent_mandate_id is not None:
        body["intent_mandate_id"] = intent_mandate_id
    return client.post(f"/checkout_sessions/{csid}/complete", json=body)


# ---------------------------------------------------------------- RE-3: intent linkage + enforcement


def test_intent_mandate_is_linked_to_execution(client):
    im = _create_intent(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = _complete(client, csid, im)
    assert r.status_code == 200, r.text

    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        m = db.get(ExecutionMandate, cs.execution_mandate_id)
        # RE-3: the execution mandate now references the intent that authorized it
        assert m.intent_mandate_id == im

    # provenance reports the intent-scope check as passing
    p = client.get(f"/provenance/{csid}").json()
    scopes = [c for c in p["checks"] if c["name"] == "intent_scope"]
    assert scopes and scopes[0]["ok"] is True, p
    assert p["artifacts"]["execution_mandate"]["intent_mandate_id"] == im


def test_execution_outside_intent_budget_is_blocked(client):
    # Intent with a budget too small for the milk basket (2x32 INR = 64 => 6400 paise)
    im = _create_intent(client, constraints_override={"max_order_value_paise": 2000})
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = _complete(client, csid, im)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "PAYMENT_BLOCKED"
    assert "intent scope" in err["message"].lower()
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        # fail closed: nothing was charged, no order created
        assert cs.status == "declined"
        assert cs.razorpay_order_id is None
        assert cs.execution_mandate_id is None


def test_execution_with_categories_outside_intent_is_blocked(client):
    # Intent restricted to "bakery" only; milk (dairy) must be rejected.
    im = _create_intent(client, constraints_override={"allowed_categories": ["bakery"]})
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = _complete(client, csid, im)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "PAYMENT_BLOCKED"


def test_unknown_intent_mandate_blocks(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = _complete(client, csid, "im_nonexistent")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "PAYMENT_BLOCKED"


# ---------------------------------------------------------------- RE-1: verification not tautological


def test_amount_mismatch_with_persisted_totals_detected(client):
    """Verification must cross-check the mandated amount against the authoritative
    totals persisted on the session, independent of the just-built mandate."""
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = _complete(client, csid)
    assert r.status_code == 200, r.text

    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        m = db.get(ExecutionMandate, cs.execution_mandate_id)
        # tamper the persisted totals so the persistence cross-check breaks
        totals = json.loads(cs.totals)
        totals["total"] = totals["total"] + 1
        cs.totals = json.dumps(totals)
        if m.consumed_at is None:
            # bypass single-use so we only exercise the amount check
            m.consumed_at = None
        checks = authorization_service.verify_execution_mandate(
            db, m, cs, buyer_agent_id=BUYER
        )
        assert checks["amount_matches_persisted"] is False


# ---------------------------------------------------------------- RE-4: single-use / replay


def test_consumed_mandate_cannot_authorize_again(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = _complete(client, csid)
    assert r.status_code == 200, r.text

    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        m = db.get(ExecutionMandate, cs.execution_mandate_id)
        # RE-4: settlement must have marked the mandate consumed
        assert m.consumed_at is not None
        checks = authorization_service.verify_execution_mandate(
            db, m, cs, buyer_agent_id=BUYER
        )
        assert checks["not_consumed"] is False


def test_intent_mandate_signature_is_verified_in_scope(client):
    im = _create_intent(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = _complete(client, csid, im)
    assert r.status_code == 200, r.text

    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        m = db.get(ExecutionMandate, cs.execution_mandate_id)
        ok, detail = authorization_service.verify_intent_scope(
            db, im, buyer_agent_id=BUYER, items=json.loads(m.items), total_paise=m.total_paise
        )
        assert ok, detail


# ---------------------------------------------------------------- crypto sanity


def test_sign_and_verify_still_works():
    payload = {"a": 1, "b": [2, 3], "nested": {"x": "y"}}
    sig = crypto_service.sign(payload)
    assert crypto_service.verify(payload, sig) is True
    assert crypto_service.verify({"a": 1, "b": [2, 3], "nested": {"x": "z"}}, sig) is False
