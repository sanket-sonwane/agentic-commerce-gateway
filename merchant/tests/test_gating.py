"""Deterministic gating engine tests (PRD §23) — rule matrix."""
from merchant_agent.tests.conftest import make_checkout

MILK = {"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500"}
SAFFRON = {"product_id": "prod_saffron_premium", "pack_id": "pack_saffron_5g"}
TOBACCO = {"product_id": "prod_paansupari_rajnigandha", "pack_id": "pack_supari_100"}
CHICKEN = {"product_id": "prod_chicken_fresh", "pack_id": "pack_chicken_500"}


def _checks(payload):
    return {c["rule"]: c["result"] for c in payload["checkout_session"]["gating_result"]["checks"]}


def _checks_for(client, decline_body):
    """Fetch the full declined session and return its rule->result map."""
    csid = decline_body["checkout_session"]["checkout_session_id"]
    session = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    return {c["rule"]: c["result"] for c in session["gating_result"]["checks"]}


def test_all_pass(client):
    r = make_checkout(client, items=[{**MILK, "quantity": 2}])
    assert r.status_code == 201
    checks = _checks(r.json())
    assert set(checks.values()) == {"pass"}
    assert set(checks.keys()) == {
        "spend_limit", "stock_check", "category_allowed", "delegation_expiry", "quantity_limit",
    }


def test_spend_limit_fail(client):
    # ₹2,600 saffron x1 under restricted ₹500 delegation; qty 1 <= 50 so only spend fires.
    r = make_checkout(client, items=[{**SAFFRON, "quantity": 1}], delegation="dlg_restricted_50")
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "SPEND_LIMIT_EXCEEDED"
    assert body["checkout_session"]["status"] == "declined"
    checks = _checks_for(client, body)
    assert checks["spend_limit"] == "fail"
    assert checks["delegation_expiry"] == "pass"


def test_category_blocked(client):
    r = make_checkout(client, items=[{**TOBACCO, "quantity": 1}])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CATEGORY_BLOCKED"


def test_quantity_limit_fail(client):
    r = make_checkout(client, items=[{**MILK, "quantity": 11}])  # max 10 on main delegation
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DELEGATION_SCOPE_VIOLATION"


def test_stock_unavailable_at_create(client, admin_headers):
    client.patch("/admin/packs/pack_chicken_500/stock", json={"actual_stock": 0}, headers=admin_headers)
    r = make_checkout(client, items=[{**CHICKEN, "quantity": 1}])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "STOCK_UNAVAILABLE"
    # retryable flag set for stock errors
    assert r.json()["error"]["retryable"] is True


def test_reserved_stock_reduces_availability(client):
    # chicken has stock_actual=1: first checkout reserves it...
    ok = make_checkout(client, items=[{**CHICKEN, "quantity": 1}])
    assert ok.status_code == 201
    # ...so a second buyer cannot even create a session for it.
    other = make_checkout(
        client,
        items=[{**CHICKEN, "quantity": 1}],
        delegation="dlg_buyerb_001",
        buyer="buyer_agent_b_deterministic",
    )
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "STOCK_UNAVAILABLE"


def test_delegation_expired(client):
    r = make_checkout(client, delegation="dlg_expired_001")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "DELEGATION_EXPIRED"


def test_forged_identity_rejected(client):
    # valid token but wrong buyer_agent_id -> unauthorized (threat: forged identity)
    r = make_checkout(client, buyer="buyer_agent_evil")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED_AGENT"


def test_unknown_product_and_pack(client):
    r = make_checkout(client, items=[{"product_id": "prod_nope", "pack_id": "pack_x", "quantity": 1}])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "PRODUCT_NOT_FOUND"
    r = make_checkout(client, items=[{"product_id": "prod_milk_amul", "pack_id": "pack_wrong", "quantity": 1}])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PACK_NOT_FOUND"
