"""Demo-reset vs append-only audit semantics regressions.

``POST /admin/demo/reset`` must NOT silently erase audit history. It clears
transactions / idempotency keys / delegations, restores catalog stock, and
records the reset itself (``demo_reset_requested`` + ``demo_reset_completed``)
appended inside the same serialized transaction. These tests pin that
contract: history is preserved, the hash chain stays intact across resets,
post-reset transactions are valid, admin authorization is required, and
pre-reset provenance honestly degrades to 404 (business rows are gone while
the audit evidence remains).
"""
from merchant.tests.conftest import complete, make_checkout

ADMIN = {"X-Admin-Token": "admin_demo_token"}


def _completed_run(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)
    return csid


def _all_actions(client):
    return {e["action"] for e in client.get("/audit_logs").json()["events"]}


# ---------------------------------------------------------------- T1 preserve


def test_reset_preserves_audit_history(client, admin_headers):
    csid = _completed_run(client)
    before = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
    assert before  # the transaction produced audit events

    total_before = len(client.get("/audit_logs").json()["events"])
    r = client.post("/admin/demo/reset", headers=admin_headers)
    assert r.status_code == 200, r.text

    after = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
    assert [e["event_id"] for e in after] == [e["event_id"] for e in before]
    assert all(a["hash"] == b["hash"] for a, b in zip(after, before))

    total_after = client.get("/audit_logs").json()["events"]
    assert len(total_after) == total_before + 2
    assert {"demo_reset_requested", "demo_reset_completed"} <= _all_actions(client)


# ---------------------------------------------------------------- T2 audited


def test_reset_is_audited_with_request_and_completion(client, admin_headers):
    _completed_run(client)
    prior = len(client.get("/audit_logs").json()["events"])

    r = client.post("/admin/demo/reset", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reset_ok"
    assert body["audit_history"] == "preserved"
    assert body["audit_events_preserved"] is True
    assert body["audit_events_before"] == prior
    assert set(body["deleted"]) == {"reservations", "idempotency_keys", "checkout_sessions", "delegations"}

    events = {e["action"]: e for e in client.get("/audit_logs").json()["events"]}
    req = events["demo_reset_requested"]
    done = events["demo_reset_completed"]
    assert req["seq"] < done["seq"]  # requested precedes completion
    assert req["actor"] == "operator"
    assert req["payload"]["audit_events_before"] == prior
    assert req["payload"]["preserves"] == ["audit_events"]
    assert done["payload"]["audit_history"] == "preserved"
    assert done["payload"]["audit_events_after"] == prior + 2


# ---------------------------------------------------------------- T3 chain


def test_chain_intact_after_reset(client, admin_headers):
    _completed_run(client)
    v1 = client.get("/audit_logs/verify").json()
    assert v1["intact"] is True

    client.post("/admin/demo/reset", headers=admin_headers)
    v2 = client.get("/audit_logs/verify").json()
    assert v2["intact"] is True
    assert v2["first_broken_seq"] is None
    assert v2["entries_checked"] == v1["entries_checked"] + 2
    assert v2["anchor"] is not None  # signed head anchor still present


# ---------------------------------------------------------------- T4 unchanged


def test_pre_reset_events_stay_verifiable(client, admin_headers):
    csid = _completed_run(client)
    before = {e["event_id"]: e for e in client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]}
    paid = next(e for e in before.values() if e["action"] == "payment_success")

    client.post("/admin/demo/reset", headers=admin_headers)

    after = {e["event_id"]: e for e in client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]}
    assert paid["event_id"] in after
    assert after[paid["event_id"]]["hash"] == paid["hash"]
    assert after[paid["event_id"]]["seq"] == paid["seq"]
    assert after[paid["event_id"]]["prev_hash"] == paid["prev_hash"]
    assert client.get("/audit_logs/verify").json()["intact"] is True


# ---------------------------------------------------------------- T5 new tx


def test_new_transaction_after_reset_keeps_chain_valid(client, admin_headers):
    client.post("/admin/demo/reset", headers=admin_headers)
    head = len(client.get("/audit_logs").json()["events"])  # the 2 reset events

    csid = _completed_run(client)
    events = client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]
    actions = {e["action"] for e in events}
    for required in ("checkout_create_requested", "gating_evaluated", "payment_success", "checkout_completed"):
        assert required in actions, f"missing audit event: {required}"

    v = client.get("/audit_logs/verify").json()
    assert v["intact"] is True
    assert v["entries_checked"] == head + len(events)


# ---------------------------------------------------------------- T6 no erase


def test_repeated_resets_never_silently_erase(client, admin_headers):
    csid = _completed_run(client)
    baseline_ids = [e["event_id"] for e in client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]]
    initial_total = len(client.get("/audit_logs").json()["events"])

    for _ in range(2):
        r = client.post("/admin/demo/reset", headers=admin_headers)
        assert r.status_code == 200, r.text

    ids = [e["event_id"] for e in client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]]
    assert ids == baseline_ids
    assert len(client.get("/audit_logs").json()["events"]) == initial_total + 4
    assert client.get("/audit_logs/verify").json()["intact"] is True


# ---------------------------------------------------------------- T7 auth


def test_reset_requires_admin_authorization(client):
    assert client.post("/admin/demo/reset").status_code == 401
    assert client.post("/admin/demo/reset", headers={"X-Admin-Token": "wrong"}).status_code == 401
    r = client.post("/admin/demo/reset", headers=ADMIN)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- T8 ap2


def test_ap2_regression_across_reset_and_provenance_limitation(client, admin_headers):
    csid_old = _completed_run(client)
    old_events = client.get(f"/audit_logs?checkout_session_id={csid_old}").json()["events"]
    assert client.get(f"/provenance/{csid_old}").json()["provenance_available"] is True

    r = client.post("/admin/demo/reset", headers=admin_headers)
    assert r.status_code == 200, r.text

    # Business rows are gone -> provenance honestly degrades to 404 (evidence stays).
    p404 = client.get(f"/provenance/{csid_old}")
    assert p404.status_code == 404
    assert p404.json()["error"]["code"] == "CHECKOUT_NOT_FOUND"
    c404 = client.get(f"/consumer/transactions/{csid_old}/provenance")
    assert c404.status_code == 404
    assert c404.json()["error"]["code"] == "CHECKOUT_NOT_FOUND"

    kept = client.get(f"/audit_logs?checkout_session_id={csid_old}").json()["events"]
    assert [e["event_id"] for e in kept] == [e["event_id"] for e in old_events]
    assert client.get("/audit_logs/verify").json()["intact"] is True

    # A fresh AP2 flow after the reset is fully valid.
    csid_new = _completed_run(client)
    new_events = client.get(f"/audit_logs?checkout_session_id={csid_new}").json()["events"]
    p = client.get(f"/provenance/{csid_new}")
    assert p.status_code == 200, p.text
    body = p.json()
    assert body["provenance_available"] is True
    assert all(c["ok"] for c in body["checks"])
    v = client.get("/audit_logs/verify").json()
    assert v["intact"] is True
    assert v["entries_checked"] == len(old_events) + 2 + len(new_events)