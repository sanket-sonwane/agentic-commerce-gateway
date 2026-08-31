"""Admin authorization security tests (Workstream A).

The privileged admin secret must be:
- absent from any shipped client (verified separately by the bundle scan),
- required server-side on every admin operation,
- replaceable in the browser by a server-signed HttpOnly session cookie from
  POST /admin/login, while X-Admin-Token stays valid for CLI/scripts/tests.

Coverage:
1. unauthenticated admin request         -> 401
2. wrong credential (header)             -> 401
3. correct credential (header, CLI path) -> 200
4. login with wrong passphrase           -> 401 + no session cookie
5. login with correct passphrase         -> 200 + HttpOnly session cookie
6. authenticated via cookie              -> 200
7. forged session cookie                 -> 401
8. expired session cookie                -> 401
9. tampered session cookie               -> 401
10. consumer (no admin) cannot reset     -> 401
11. logout invalidates the session       -> 401 afterwards
"""
import base64
from datetime import timedelta

from merchant.ap2 import crypto_service


def _session_cookie(exp_delta_minutes: float):
    payload = {
        "sub": "admin",
        "exp": (crypto_service.now_utc() + timedelta(minutes=exp_delta_minutes)).isoformat(timespec="seconds"),
        "nonce": "test-nonce",
    }
    body = base64.urlsafe_b64encode(crypto_service.canonical_json(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{body}.{crypto_service.sign(payload)}"


def test_1_unauthenticated_admin_is_rejected(client):
    r = client.get("/admin/ops/status")
    assert r.status_code == 401
    r2 = client.post("/admin/demo/reset")
    assert r2.status_code == 401
    r3 = client.get("/admin/packs")
    assert r3.status_code == 401


def test_2_wrong_header_credential_rejected(client):
    r = client.get("/admin/ops/status", headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401


def test_3_correct_header_credential_accepted(client):
    r = client.get("/admin/ops/status", headers={"X-Admin-Token": "admin_demo_token"})
    assert r.status_code == 200


def test_4_login_wrong_password_no_cookie(client):
    r = client.post("/admin/login", json={"password": "nope"})
    assert r.status_code == 401
    set_cookie = r.headers.get("set-cookie", "")
    assert "acg_admin_session" not in set_cookie


def test_5_login_correct_password_sets_httponly_cookie(client):
    r = client.post("/admin/login", json={"password": "admin_demo_token"})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "acg_admin_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie


def test_6_session_cookie_authorizes_admin(client):
    client.post("/admin/login", json={"password": "admin_demo_token"})
    assert client.get("/admin/ops/status").status_code == 200
    assert client.get("/admin/packs").status_code == 200
    assert client.post("/admin/demo/reset").status_code == 200


def test_7_forged_session_cookie_rejected(client):
    r = client.get("/admin/ops/status", cookies={"acg_admin_session": "forged.payload"})
    assert r.status_code == 401


def test_8_expired_session_cookie_rejected(client):
    expired = _session_cookie(-5)  # signed payload already past expiry
    r = client.get("/admin/ops/status", cookies={"acg_admin_session": expired})
    assert r.status_code == 401


def test_9_tampered_session_cookie_rejected(client):
    good = _session_cookie(30)
    unit = _session_cookie(30)
    body, sig = good.rsplit(".", 1)
    tampered = f"{body}.{'0' if sig[0] != '0' else '1'}{sig[1:]}" if len(sig) > 1 else f"{unit[:-1]}{'A'}"
    if tampered == good:  # guard against an unlikely no-op flip
        tampered = good[:-1] + ("0" if good[-1] != "0" else "1")
    r = client.get("/admin/ops/status", cookies={"acg_admin_session": tampered})
    assert r.status_code == 401


def test_10_consumer_cannot_invoke_admin(client):
    # A normal (unauthenticated) consumer hitting the admin surface.
    assert client.get("/admin/ops/status").status_code == 401
    assert client.patch("/admin/packs/pack_milk_amul_500/stock", json={"actual_stock": 0}).status_code == 401
    admin_headers = {"X-Admin-Token": "WRONG"}
    assert client.post("/admin/demo/reset", headers=admin_headers).status_code == 401


def test_11_logout_revokes_session(client):
    client.post("/admin/login", json={"password": "admin_demo_token"})
    assert client.get("/admin/ops/status").status_code == 200
    client.post("/admin/logout")
    assert client.get("/admin/ops/status").status_code == 401


def test_12_ops_status_reports_driver_and_razorpay_credentials(client):
    """B coverage: ops/status surface works for an authorized operator."""
    r = client.get("/admin/ops/status", headers={"X-Admin-Token": "admin_demo_token"})
    assert r.status_code == 200
    body = r.json()
    assert "payment_driver" in body
    assert "razorpay_credentials_present" in body
    assert "razorpay_api_reachable" in body
    assert isinstance(body.get("recent_sessions"), list)