"""Hermetic end-to-end test over the public Merchant Protocol.

Runs a complete transaction lifecycle (discover -> intent -> checkout -> complete ->
provenance -> audit verify -> explain) plus the fail-closed adversarial path, all
through TestClient against the seeded test DB with the pytest-only sim pilot flag
(no real Razorpay, no network). Exercises the AP2 intent->execution->policy->receipt
chain and the append-only audit trail as one journey.
"""
import json

from merchant.persistence.db import mutate
from merchant.persistence.models.entities import (
    CheckoutSession,
    DecisionReceipt,
    ExecutionMandate,
)
from merchant.tests.conftest import make_checkout

BUYER = "buyer_agent_llm_01"
DLG = "dlg_9f8e7d"
MILK = [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}]


def _create_intent(client, constraints_override=None):
    body = {"buyer_agent_id": BUYER, "delegation_token": DLG, "intent": "Buy groceries for my household"}
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


def _session_id(client, items=None):
    resp = make_checkout(client, items=items)
    assert resp.status_code == 201, resp.text
    return resp.json()["checkout_session"]["checkout_session_id"]


# =============================================================== A. happy path


def test_full_lifecycle_e2e(client):
    # 1. discover
    d = client.post("/discover", json={"query": "milk", "buyer_agent_id": BUYER})
    assert d.status_code == 200, d.text
    assert d.json()["basket_proposal"], d.text

    # 2. signed intent mandate
    im = _create_intent(client)

    # 3. create checkout (state requires_payment, stock reserved, gating passes)
    csid = _session_id(client, items=MILK)
    cs_out = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    assert cs_out["status"] == "requires_payment"
    assert all(c["result"] == "pass" for c in cs_out["gating_result"]["checks"]), cs_out

    # 4. complete with the linked intent -> signed execution mandate, paid
    r = _complete(client, csid, im)
    assert r.status_code == 200, r.text
    assert r.json()["checkout_session"]["status"] == "completed"
    assert r.json()["checkout_session"]["payment"]["execution_mandate_id"]

    # 5. DB invariants: chains + amount binding + single-use
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        em = db.get(ExecutionMandate, cs.execution_mandate_id)
        assert em.intent_mandate_id == im, "execution mandate must link the intent"
        assert cs.authorized_amount_paise == cs.razorpay_order_amount == em.total_paise
        assert em.consumed_at is not None, "mandate must be single-use consumed"
        receipt = (
            db.query(DecisionReceipt)
            .filter(DecisionReceipt.execution_mandate_id == em.mandate_id)
            .first()
        )
        assert receipt is not None and receipt.intent_mandate_id == im
        assert receipt.razorpay_order_id == cs.razorpay_order_id

    # 6. provenance: every check ok (incl. intent_scope, decision_receipt)
    p = client.get(f"/provenance/{csid}").json()
    assert p["provenance_available"] is True and p["status"] == "completed"
    assert all(c["ok"] for c in p["checks"]), p
    names = [c["name"] for c in p["checks"]]
    assert "intent_scope" in names and "decision_receipt" in names and "policy_approved" in names

    # 7. audit hash chain intact + signed anchor
    v = client.get("/audit_logs/verify").json()
    assert v["intact"] is True and v["entries_checked"] >= 10
    assert v["anchor"] and v["anchor"]["signature"]

    # 8. explanation references the AP2 artifacts
    ex = client.get(f"/explain/{csid}").json()
    joined = " ".join(ex["timeline_facts"])
    assert "Execution Mandate" in joined and "Decision Receipt" in joined
    assert "completed successfully" in ex["explanation"]

    # 9. replaying the callback on a completed session is blocked (no state change)
    cb = client.post(
        f"/checkout_sessions/{csid}/payment_callback",
        json={"razorpay_order_id": "x", "razorpay_payment_id": "y", "razorpay_signature": "z"},
    )
    assert cb.status_code in (400, 409)
    with mutate() as db:
        assert db.get(CheckoutSession, csid).status == "completed"


# =============================================================== B. fail closed


def test_e2e_intent_budget_violation_blocks_payment(client):
    im = _create_intent(client, constraints_override={"max_order_value_paise": 100})
    csid = _session_id(client, items=MILK)  # basket > ₹1.00 -> must be blocked
    r = _complete(client, csid, im)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "PAYMENT_BLOCKED"
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        assert cs.status == "declined"
        assert cs.razorpay_order_id is None
        assert cs.execution_mandate_id is None  # never authorized, never charged


def test_e2e_unknown_intent_blocks_payment(client):
    csid = _session_id(client, items=MILK)
    r = _complete(client, csid, "im_bogus")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PAYMENT_BLOCKED"


def test_e2e_intent_category_violation_blocks_payment(client):
    im = _create_intent(client, constraints_override={"allowed_categories": ["bakery"]})
    csid = _session_id(client, items=MILK)  # milk is dairy -> blocked
    r = _complete(client, csid, im)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PAYMENT_BLOCKED"


def test_e2e_tampered_persisted_total_detected_in_provenance(client):
    csid = _session_id(client, items=MILK)
    assert _complete(client, csid).status_code == 200
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        # tamper ONLY the persisted totals JSON (authoritative snapshot): the
        # independent RE-1 cross-check must catch it even though the session's
        # authorized_amount_paise column is untouched.
        t = json.loads(cs.totals)
        t["total"] = t["total"] + 1
        cs.totals = json.dumps(t)
        em = db.get(ExecutionMandate, cs.execution_mandate_id)
        from merchant.ap2 import authorization_service
        checks = authorization_service.verify_execution_mandate(
            db, em, cs, buyer_agent_id=BUYER
        )
        assert checks["amount_matches_persisted"] is False  # RE-1 catches it
        assert checks["amount_matches"] is True  # the naive check does not


def test_e2e_authorized_amount_tamper_detected_in_provenance(client):
    csid = _session_id(client, items=MILK)
    assert _complete(client, csid).status_code == 200
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        cs.authorized_amount_paise = (cs.authorized_amount_paise or 0) + 1
    p = client.get(f"/provenance/{csid}").json()
    amt = [c for c in p["checks"] if c["name"] in ("amount_binding", "order_amount_binding")]
    assert any(c["ok"] is False for c in amt)


def test_e2e_tampered_signature_detected_in_provenance(client):
    csid = _session_id(client, items=MILK)
    assert _complete(client, csid).status_code == 200
    with mutate() as db:
        cs = db.get(CheckoutSession, csid)
        em = db.get(ExecutionMandate, cs.execution_mandate_id)
        em.signature = "0" * 128
    p = client.get(f"/provenance/{csid}").json()
    sig = [c for c in p["checks"] if c["name"] == "execution_mandate_signature"]
    assert sig and sig[0]["ok"] is False
