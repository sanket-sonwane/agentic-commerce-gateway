"""Task C — inventory reservation lifecycle audit evidence.

Reservation *creation* was already audited (checkout_service emits
`reservation_created`). These tests pin the newly-added lifecycle transitions
(release, consume, expire) that inventory_service performs, and verify:
  - each durable transition emits an audit event with exact references
  - a failed mutation emits NO false success event
  - the audit hash chain stays intact across lifecycle operations
  - multiple lifecycle operations preserve append ordering
"""
from datetime import timedelta

from merchant.persistence.db import mutate, utcnow
from merchant.persistence.models.entities import Reservation
from merchant.domain import inventory_service
from merchant.tests.conftest import complete, make_checkout

MILK = [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}]


def _events(client, csid):
    return client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]


def _actions(client, csid):
    return [e["action"] for e in _events(client, csid)]


def test_reservation_created_event_exists(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    assert "reservation_created" in _actions(client, csid)


def test_reservation_consumed_event_exists(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    assert complete(client, csid).status_code == 200
    actions = _actions(client, csid)
    assert "reservation_consumed" in actions
    consumed = [e for e in _events(client, csid) if e["action"] == "reservation_consumed"]
    assert consumed and consumed[0]["payload"]["new_status"] == "consumed"
    assert consumed[0]["payload"]["reservation_id"]
    assert consumed[0]["payload"]["pack_id"] == "pack_milk_amul_500"
    assert consumed[0]["payload"]["previous_status"] == "active"


def test_reservation_released_event_exists_on_cancel(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    body = {"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"}
    client.post(f"/checkout_sessions/{csid}/cancel", json=body)
    actions = _actions(client, csid)
    assert "reservation_released" in actions
    released = [e for e in _events(client, csid) if e["action"] == "reservation_released"]
    assert released and released[0]["payload"]["new_status"] == "released"
    assert released[0]["actor"] == "buyer_agent"


def test_reservation_expired_event_exists(client):
    """A durable sweep (inside mutate) emits reservation_expired for each."""
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    with mutate() as db:
        res = (
            db.query(Reservation).filter(Reservation.checkout_session_id == csid).first()
        )
        res.expires_at = utcnow() - timedelta(seconds=1)
        db.flush()
        swept = inventory_service.sweep_expired(db)
    assert swept == 1
    assert "reservation_expired" in _actions(client, csid)


def test_failed_mutation_emits_no_false_success_event(client):
    """A declined completion releases stock but never claims consumption."""
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r = complete(client, csid)
    assert r.status_code == 200, r.text
    actions = _actions(client, csid)
    # Successful completion consumes; now force a declined path on a NEW session
    # and assert it releases but does NOT claim the reservation was consumed.
    csid2 = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    with mutate() as db:
        cs = _session_row(db, csid2)
        cs.status = "declined"
        cs.decline_code = "PAYMENT_FAILED"
        inventory_service.release_session_reservations(db, csid2, actor="razorpay")
        db.flush()
    actions2 = _actions(client, csid2)
    assert "reservation_released" in actions2
    assert "reservation_consumed" not in actions2


def _session_row(db, csid):
    from merchant.persistence.models.entities import CheckoutSession
    return db.get(CheckoutSession, csid)


def test_audit_chain_intact_after_lifecycle(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    assert complete(client, csid).status_code == 200
    assert "reservation_consumed" in _actions(client, csid)
    v = client.get("/audit_logs/verify").json()
    assert v["intact"] is True
    assert v["entries_checked"] > 0


def test_multiple_lifecycle_ops_preserve_ordering(client):
    # update (release old + re-reserve more milk) then complete (consume). The
    # audit chain must remain intact and sequence must stay contiguous.
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    ur = client.put(
        f"/checkout_sessions/{csid}",
        json={
            "buyer_agent_id": "buyer_agent_llm_01",
            "delegation_token": "dlg_9f8e7d",
            "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 3}],
        },
    )
    assert ur.status_code == 200, ur.text
    cr = complete(client, csid)
    assert cr.status_code == 200, cr.text
    actions = _actions(client, csid)
    assert "reservation_released" in actions
    assert "reservation_consumed" in actions
    v = client.get("/audit_logs/verify").json()
    assert v["intact"] is True
    # consumed must be strictly ordered after the latest released.
    seq_by_action = {e["seq"]: e["action"] for e in _events(client, csid)}
    last_released = max(seq for seq, a in seq_by_action.items() if a == "reservation_released")
    consumed_seq = next(seq for seq, a in seq_by_action.items() if a == "reservation_consumed")
    assert consumed_seq > last_released
