"""Audit trail + hash chain integrity tests (PRD §31–32) and error contract (§27)."""
from merchant.persistence.db import mutate
from merchant.persistence.models.entities import AuditEvent
from merchant.tests.conftest import complete, make_checkout


def test_critical_events_recorded(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)
    events = [e["action"] for e in client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]]
    for required in (
        "checkout_create_requested",
        "gating_evaluated",
        "reservation_created",
        "checkout_created",
        "complete_requested",
        "final_validation",
        "razorpay_order_created",
        "payment_success",
        "checkout_completed",
    ):
        assert required in events, f"missing audit event: {required}"


def test_hash_chain_verifies_intact(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)
    r = client.get("/audit_logs/verify").json()
    assert r["intact"] is True
    assert r["entries_checked"] > 0


def test_tampering_is_detected(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)
    assert client.get("/audit_logs/verify").json()["intact"] is True

    # simulate an attacker editing a stored reasoning summary in place
    with mutate() as db:
        victim = db.query(AuditEvent).filter(AuditEvent.action == "payment_success").first()
        victim.reasoning_summary = "totally legitimate payment"

    r = client.get("/audit_logs/verify").json()
    assert r["intact"] is False
    assert r["first_broken_seq"] == victim.seq


def test_explanation_from_facts(client, admin_headers):
    """Declined transaction yields a rule-grounded human explanation (PRD §33)."""
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    client.patch("/admin/packs/pack_milk_amul_500/stock", json={"actual_stock": 0}, headers=admin_headers)
    complete(client, csid)
    x = client.get(f"/explain/{csid}").json()
    assert "declined" in x["explanation"].lower()
    assert any("available in stock" in f or "stock" in f.lower() for f in x["timeline_facts"])


def test_error_envelope_shape(client):
    r = make_checkout(client, delegation="dlg_expired_001")
    err = r.json()["error"]
    assert set(err.keys()) >= {"code", "message", "retryable", "details"}
    assert isinstance(err["retryable"], bool)
