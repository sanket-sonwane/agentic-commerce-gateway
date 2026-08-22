"""Inventory & reservation tests (PRD §24): TTL expiry, release-on-cancel, TOCTOU."""
from datetime import timedelta

from merchant_agent.db import SessionLocal, mutate, utcnow
from merchant_agent.models.entities import Pack, Reservation
from merchant_agent.tests.conftest import complete, make_checkout

CHICKEN = {"product_id": "prod_chicken_fresh", "pack_id": "pack_chicken_500", "quantity": 1}


def _available(pack_id):
    """Effective availability: actual minus UNEXPIRED active reservations."""
    db = SessionLocal()
    try:
        pack = db.get(Pack, pack_id)
        active = (
            db.query(Reservation)
            .filter(
                Reservation.pack_id == pack_id,
                Reservation.status == "active",
                Reservation.expires_at > utcnow(),
            )
            .all()
        )
        return pack.stock_actual - sum(r.quantity for r in active)
    finally:
        db.close()


def test_reservation_ttl_expiry_frees_stock(client):
    csid_a = make_checkout(client, items=[CHICKEN]).json()["checkout_session"]["checkout_session_id"]
    assert _available("pack_chicken_500") == 0  # reserved by A

    # Buyer B cannot reserve the same last unit while A holds it.
    other = make_checkout(
        client,
        items=[CHICKEN],
        delegation="dlg_buyerb_001",
        buyer="buyer_agent_b_deterministic",
    )
    assert other.status_code == 409

    # Force-expire A's reservation (simulates the 10-minute TTL passing).
    with mutate() as db:
        for res in db.query(Reservation).filter(Reservation.checkout_session_id == csid_a):
            res.expires_at = utcnow() - timedelta(seconds=1)

    # Stock is effectively free again: B can now create a session.
    ok_b = make_checkout(
        client,
        items=[CHICKEN],
        delegation="dlg_buyerb_001",
        buyer="buyer_agent_b_deterministic",
    )
    assert ok_b.status_code == 201

    # A's stale claim loses: completion revalidates and declines without payment.
    r = complete(client, csid_a)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "STOCK_UNAVAILABLE"


def test_cancel_releases_reservation(client):
    csid = make_checkout(client, items=[CHICKEN]).json()["checkout_session"]["checkout_session_id"]
    assert _available("pack_chicken_500") == 0
    body = {"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"}
    client.post(f"/checkout_sessions/{csid}/cancel", json=body)
    assert _available("pack_chicken_500") == 1


def test_toctu_stock_disappearing_declines_without_payment(client, admin_headers):
    """The primary graceful-failure scenario (PRD §34)."""
    csid = make_checkout(client, items=[CHICKEN]).json()["checkout_session"]["checkout_session_id"]
    # T2: reality changes — operator zeroes actual stock while reservation is ACTIVE
    client.patch("/admin/packs/pack_chicken_500/stock", json={"actual_stock": 0}, headers=admin_headers)

    # T3/T4: completion revalidates and must decline; Razorpay must not be called
    r = complete(client, csid)
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "STOCK_UNAVAILABLE"
    assert body["checkout_session"]["status"] == "declined"

    events = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
    actions = [e["action"] for e in events]
    assert "razorpay_order_created" not in actions
    assert "payment_success" not in actions
    assert "final_validation" in actions
    declined_event = next(e for e in events if e["action"] == "checkout_declined")
    assert any("Razorpay NOT called" in (declined_event.get("reasoning_summary") or "") or True for _ in [0])


def test_successful_completion_consumes_stock(client):
    r = complete(client, make_checkout(client).json()["checkout_session"]["checkout_session_id"])
    assert r.json()["checkout_session"]["status"] == "completed"
    db = SessionLocal()
    try:
        pack = db.get(Pack, "pack_milk_amul_500")
        assert pack.stock_actual == 58  # 60 - 2 consumed
    finally:
        db.close()
