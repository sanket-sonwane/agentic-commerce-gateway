"""Workstream E — user-signed intent assertion tests.

The buying human signs a canonical string with a browser-held Ed25519 key
(tweetnacl). These tests replay real browser artifacts from a FIXED test key
(derived from a 32-byte seed) to prove the merchant verifies, binds and
single-use-consumes the assertion — and blocks every tamper path BEFORE any
Execution Mandate / Razorpay order / payment exists.

Nothing here needs the razorpay SDK: the sim payment driver is active (the
config gate is opened by conftest). A REAL user signature over the same
canonical bytes is exercised by scripts/_js_canonical_check.mjs (golden vector).
"""
import json

import pytest

from merchant.persistence.db import mutate
from merchant.persistence.models.entities import AuditEvent, ExecutionMandate, UserIntentAssertion
from merchant.tests.conftest import complete, make_checkout
from merchant.ap2 import authorization_service

SEED = bytes(range(32))
KEY_ID = "userkey_test_0000000000000001"


def make_keypair() -> tuple[bytes, str]:
    """(secret64, public32) from a fixed seed so signatures are deterministic."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.from_private_bytes(SEED)
    pub = priv.public_key().public_bytes_raw()
    sec = SEED + pub
    return sec, pub


def public_key_pem(pub: bytes) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    k = Ed25519PublicKey.from_public_bytes(pub)
    return k.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("utf-8")


def short_hash(canonical: str) -> str:
    import hashlib

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def register_key(client, user_id="user_alice_0001", key_id: str = KEY_ID):
    sec, pub = make_keypair()
    r = client.post(
        "/user_keys",
        json={
            "user_id": user_id,
            "key_id": key_id,
            "public_key_pem": public_key_pem(pub),
        },
    )
    assert r.status_code == 201, r.text
    return user_id


def build_assertion(
    user_id: str,
    csid: str | None = None,
    total_paise: int | None = None,
    *,
    override: dict | None = None,
    malicious_override: dict | None = None,
    user_key_id: str = KEY_ID,
    signing_seed: bytes = SEED,
) -> dict:
    """Build a canonical, signed UserIntentAssertion artifact.

    First we compute the canonical fields EXACTLY as the browser would; a
    malicious_override is applied to the produced artifact BEFORE signing to
    simulate an attacker changing a field on the wire (signature/hash then no
    longer match because signing used the pre-override canonical values).
    """
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    from merchant.ap2.user_intent import assertion_canonical_string
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    issued_at = (now - timedelta(seconds=30)).isoformat(timespec="seconds")
    expires_at = (now + timedelta(minutes=14)).isoformat(timespec="seconds")
    base = {
        "assertion_id": f"ua_unit_{uuid4().hex[:12]}",
        "user_id": user_id,
        "user_key_id": user_key_id,
        "merchant_id": "merchant_grocery_01",
        "delegate_id": "buyer_agent_llm_01",
        "intent": {
            "action": "checkout",
            "checkout_session_id": csid or "cs_fixture",
            "description": "unit test basket",
        },
        "constraint_ceiling": {"currency": "INR", "max_amount_paise": total_paise or 50000},
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": f"nonce_unit_{uuid4().hex[:12]}",
    }
    base.update(override or {})
    canonical = assertion_canonical_string(**base)
    artifact = dict(base)
    artifact["canonical_payload_hash"] = short_hash(canonical)
    if malicious_override:
        artifact.update(malicious_override)
    priv = Ed25519PrivateKey.from_private_bytes(signing_seed)
    artifact["signature"] = priv.sign(canonical.encode("utf-8")).hex()
    artifact["key_id"] = user_key_id
    return artifact


# ------------------------------------------------------------------ happy path


def test_user_signed_checkout_completes_and_consumes(client):
    user_id = register_key(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    sess = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    total_paise = int(round(sess["totals"]["total"] * 100))
    assertion = build_assertion(user_id, csid, total_paise)

    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={
            "buyer_agent_id": "buyer_agent_llm_01",
            "delegation_token": "dlg_9f8e7d",
            "user_assertion": assertion,
        },
    )
    body = r.json()
    assert r.status_code == 200, body
    assert body["checkout_session"]["status"] == "completed"
    assert body["checkout_session"]["payment"]["driver"] == "sim"

    with mutate() as db:
        ua = db.get(UserIntentAssertion, assertion["assertion_id"])
        assert ua is not None
        assert ua.consumed_at is not None  # single-use consumed
        assert ua.user_key_id == KEY_ID
        em = db.query(ExecutionMandate).filter(ExecutionMandate.checkout_session_id == csid).first()
        assert em is not None
        assert em.user_assertion_id == assertion["assertion_id"]
        assert em.user_assertion_hash == assertion["canonical_payload_hash"]

    actions = [e["action"] for e in client.get(f"/audit_logs?checkout_session_id={csid}").json()["events"]]
    assert "user_assertion_verified" in actions
    assert "user_assertion_rejected" not in actions


def test_mandate_verification_binds_consumed_user_assertion(client):
    user_id = register_key(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    sess = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    total_paise = int(round(sess["totals"]["total"] * 100))
    assertion = build_assertion(user_id, csid, total_paise)
    client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d", "user_assertion": assertion},
    )
    with mutate() as db:
        em = db.query(ExecutionMandate).filter(ExecutionMandate.checkout_session_id == csid).first()
        cs = __import__("merchant.persistence.models.entities", fromlist=["CheckoutSession"]).CheckoutSession
        row = db.get(cs, csid)
        checks = authorization_service.verify_execution_mandate(
            db, em, row, buyer_agent_id="buyer_agent_llm_01"
        )
        assert checks["user_assertion_bound"] is True
        # Signature/integrity/policy binding all hold even after consumption;
        # not_consumed/nonce_unused are expected False post-settlement (the
        # mandate was single-use consumed by the completed payment).
        assert checks["signature_valid"] is True
        assert checks["key_trusted"] is True
        assert checks["policy_approved"] is True
        assert checks["amount_matches"] is True


# ------------------------------------------------------------------ tamper paths


def test_replay_of_consumed_assertion_blocks(client):
    user_id = register_key(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    sess = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    total_paise = int(round(sess["totals"]["total"] * 100))
    assertion = build_assertion(user_id, csid, total_paise)
    r1 = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d", "user_assertion": assertion},
    )
    assert r1.status_code == 200, r1.text

    # Re-using the SAME (now consumed) assertion on a DIFFERENT checkout must
    # fail closed on single-use consumption: the assertion was already bound to
    # the first transaction and can never authorize another one.
    csid2 = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    r2 = client.post(
        f"/checkout_sessions/{csid2}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d", "user_assertion": assertion},
    )
    body = r2.json()
    assert r2.status_code == 409, body
    assert body["error"]["code"] == "PAYMENT_BLOCKED"
    assert "nonce_unused" in body["error"]["message"]
    assert client.get(f"/checkout_sessions/{csid2}").json()["checkout_session"]["razorpay_order_id"] is None


def test_unknown_key_blocked(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    sess = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    total_paise = int(round(sess["totals"]["total"] * 100))
    # No /user_keys registration for this key_id at all; signed by a DIFFERENT,
    # unregistered device key (subject/trust checks must both fail).
    assertion = build_assertion(
        "user_stranger_0001", csid, total_paise,
        user_key_id="userkey_unregistered", signing_seed=b"Z" * 32,
    )
    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d", "user_assertion": assertion},
    )
    body = r.json()
    assert r.status_code == 409, body
    assert body["error"]["code"] == "PAYMENT_BLOCKED"
    assert "key_trusted" in body["error"]["message"] or "signature" in body["error"]["message"]
    # Fail-closed: no mandate, no order, no payment.
    cs_check = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    assert cs_check["status"] != "completed"
    assert cs_check["razorpay_order_id"] is None


def test_recomputed_hash_integrity(client):
    user_id = register_key(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    sess = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    total_paise = int(round(sess["totals"]["total"] * 100))
    assertion = build_assertion(user_id, csid, total_paise)
    # Valid signature + valid fields, but a WRONG declared hash: the merchant
    # recomputes from fields and must catch it.
    assertion["canonical_payload_hash"] = "0" * 64

    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d", "user_assertion": assertion},
    )
    body = r.json()
    assert r.status_code == 409 and body["error"]["code"] == "PAYMENT_BLOCKED"
    assert "hash_integrity" in body["error"]["message"]


def test_spend_ceiling_enforced(client):
    user_id = register_key(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    sess = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    total_paise = int(round(sess["totals"]["total"] * 100))
    # User approves only HALF the basket's value: ceiling check must block.
    half = max(1, total_paise // 2)
    assertion = build_assertion(user_id, csid, half)
    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d", "user_assertion": assertion},
    )
    body = r.json()
    assert r.status_code == 409 and body["error"]["code"] == "PAYMENT_BLOCKED"
    assert "constraint_ceiling" in body["error"]["message"]
    assert client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]["status"] != "completed"


def test_subject_privacy_user_id_mismatch(client):
    user_id = register_key(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    sess = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    total_paise = int(round(sess["totals"]["total"] * 100))
    # Signature is valid for the registered key, but the assertion claims a
    # DIFFERENT user_id (key ownership mismatch).
    assertion = build_assertion(user_id, csid, total_paise)
    assertion["user_id"] = "user_alice_0002"
    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d", "user_assertion": assertion},
    )
    body = r.json()
    assert r.status_code == 409 and body["error"]["code"] == "PAYMENT_BLOCKED"
    assert "subject_match" in body["error"]["message"]


def test_merchant_and_delegate_binding(client):
    user_id = register_key(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    sess = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    total_paise = int(round(sess["totals"]["total"] * 100))
    assertion = build_assertion(user_id, csid, total_paise)
    assertion["delegate_id"] = "buyer_agent_evil_99"
    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d", "user_assertion": assertion},
    )
    body = r.json()
    assert r.status_code == 409 and body["error"]["code"] == "PAYMENT_BLOCKED"
    assert "delegate_match" in body["error"]["message"]


def test_session_binding(client):
    user_id = register_key(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    sess = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    total_paise = int(round(sess["totals"]["total"] * 100))
    # Pattern a "good" assertion for the session the browser actually saw, then
    # transpose it onto a DIFFERENT checkout via the wire fields: the intent
    # signed for csA cannot authorize csB.
    csid_b = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    assertion = build_assertion(user_id, csid_b, total_paise)

    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d", "user_assertion": assertion},
    )
    body = r.json()
    assert r.status_code == 409 and body["error"]["code"] == "PAYMENT_BLOCKED"
    assert "session_match" in body["error"]["message"]


def test_expired_assertion_blocked(client):
    from datetime import datetime, timedelta, timezone

    user_id = register_key(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    sess = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    total_paise = int(round(sess["totals"]["total"] * 100))
    old = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)).isoformat(timespec="seconds")
    assertion = build_assertion(user_id, csid, total_paise, override={"issued_at": old, "expires_at": old})
    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d", "user_assertion": assertion},
    )
    body = r.json()
    assert r.status_code == 409 and body["error"]["code"] == "PAYMENT_BLOCKED"
    assert "not_expired" in body["error"]["message"]


def test_signature_tamper_blocked(client):
    user_id = register_key(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    sess = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    total_paise = int(round(sess["totals"]["total"] * 100))
    assertion = build_assertion(user_id, csid, total_paise)
    # Same valid artifact, corrupted signature bytes.
    assertion["signature"] = ("ab" * 63) + "ff"
    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d", "user_assertion": assertion},
    )
    body = r.json()
    assert r.status_code == 409 and body["error"]["code"] == "PAYMENT_BLOCKED"
    assert "signature_valid" in body["error"]["message"]


# ------------------------------------------------------------------ user_keys api


def test_user_keys_registration_and_bad_pem(client):
    r = client.post(
        "/user_keys",
        json={"user_id": "user_bob_0001", "key_id": "userkey_bad", "public_key_pem": "not a key"},
    )
    assert r.status_code == 400, r.text
    r = client.post(
        "/user_keys",
        json={
            "user_id": "user_bob_0001",
            "key_id": "userkey_rotated_0001",
            "public_key_pem": public_key_pem(make_keypair()[1]),
        },
    )
    assert r.status_code == 201, r.text
    actions = [e["action"] for e in client.get("/audit_logs").json()["events"]]
    assert "user_key_registered" in actions


def test_provenance_reports_user_assertion_artifact(client):
    user_id = register_key(client)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    sess = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    total_paise = int(round(sess["totals"]["total"] * 100))
    assertion = build_assertion(user_id, csid, total_paise)
    client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d", "user_assertion": assertion},
    )
    prov = client.get(f"/provenance/{csid}").json()
    ua = prov["artifacts"]["user_assertion"]
    assert ua is not None
    assert ua["assertion_id"] == assertion["assertion_id"]
    assert ua["canonical_payload_hash"] == assertion["canonical_payload_hash"]
    ua_c = [c for c in prov["checks"] if c["name"] == "user_assertion"][0]
    assert ua_c["ok"] is True

    # Consumer projection must now legally claim the USER as the authorizer.
    projection = client.get(f"/consumer/transactions/{csid}/provenance").json()
    assert projection["authorization"]["authorization_source"] == "user_signed"
    assert "You" in projection["authorization"]["authorized_by"]


def test_legacy_session_never_claims_user_signed(client):
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    complete(client, csid)  # no user_assertion at all
    projection = client.get(f"/consumer/transactions/{csid}/provenance").json()
    assert projection["authorization"]["authorization_source"] == "delegation"
    assert "agent" in projection["authorization"]["authorized_by"].lower()