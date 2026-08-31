"""Standalone verifier (offline) + user-assertion artifact coverage (Workstream E).

The standalone verifier must be able to independently verify exported AP2
artifacts from PLAIN DATA — including the user-signed intent assertion, which
signs a canonical STRING (not JSON). These tests prove:
  - a valid user_assertion artifact verifies under the user's public key,
  - every tamper of the canonical fields/signature/hash fails,
  - the bundle binding the Execution Mandate -> user_assertion holds,
  - a mandate claiming user authority WITHOUT supplied user artifacts FAILS
    closed (never guessed),
  - legacy bundles (no user scope at all) verify as before.
"""
import json

import pytest

from merchant.tests.conftest import make_checkout
from merchant.tests.test_user_assertion import (
    build_assertion,
    public_key_pem,
    make_keypair,
    register_key,
)
from merchant.ap2.crypto_service import public_key_pem as merchant_public_key_pem
from merchant.ap2.standalone_verifier import (
    REASON_BUNDLE_RELATIONSHIP,
    REASON_CANONICAL_STRING,
    REASON_INVALID_ARTIFACT_TYPE,
    REASON_KEY_ID_MISMATCH,
    REASON_OK,
    REASON_PAYLOAD_HASH_MISMATCH,
    REASON_SIGNATURE_INVALID,
    verify_artifact,
    verify_transaction_evidence,
)


KEY_VERIFY_01 = "userkey_verify_0001"
KEY_VERIFY_02 = "userkey_verify_0002"


@pytest.fixture()
def completed_bundle(client):
    """One completed user-signed checkout; returns its exported provenance."""
    user_id = register_key(client, user_id="user_verify_01", key_id=KEY_VERIFY_01)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    sess = client.get(f"/checkout_sessions/{csid}").json()["checkout_session"]
    total_paise = int(round(sess["totals"]["total"] * 100))
    assertion = build_assertion(user_id, csid, total_paise, user_key_id=KEY_VERIFY_01)
    r = client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d", "user_assertion": assertion},
    )
    assert r.status_code == 200, r.text
    prov = client.get(f"/provenance/{csid}").json()
    return {
        "csid": csid,
        "provenance": prov,
        "execution_mandate": prov["artifacts"]["execution_mandate"],
        "policy_decision": prov["artifacts"]["policy_decision"],
        "decision_receipt": prov["artifacts"]["decision_receipt"],
        "user_assertion": prov["artifacts"]["user_assertion"],
        "user_pem": prov["artifacts"]["user_assertion"]["user_public_key_pem"],
    }


# ----------------------------------------------------------- individual artifact


def test_user_assertion_artifact_verifies(completed_bundle):
    res = verify_artifact(
        completed_bundle["user_assertion"],
        completed_bundle["user_pem"],
        artifact_type="user_assertion",
    )
    assert res.valid is True, res.reason
    assert res.reason == REASON_OK
    assert res.checks["signature"] is True
    assert res.checks["payload_hash"] is True
    assert res.checks["canonical_string"] is True


def test_user_assertion_wrong_public_key_fails(completed_bundle):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    other_pub = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    res = verify_artifact(
        completed_bundle["user_assertion"],
        other_pub,
        artifact_type="user_assertion",
    )
    assert res.valid is False
    assert res.reason == REASON_SIGNATURE_INVALID


def test_user_assertion_tampered_signature_fails(completed_bundle):
    art = dict(completed_bundle["user_assertion"])
    art["signature"] = "aa" * 64
    res = verify_artifact(art, completed_bundle["user_pem"], artifact_type="user_assertion")
    assert res.valid is False
    assert res.reason in (REASON_SIGNATURE_INVALID, REASON_PAYLOAD_HASH_MISMATCH)


def test_user_assertion_tampered_hash_fails(completed_bundle):
    art = dict(completed_bundle["user_assertion"])
    art["canonical_payload_hash"] = "0" * 64
    res = verify_artifact(art, completed_bundle["user_pem"], artifact_type="user_assertion")
    assert res.valid is False
    assert "hash" in res.reason or REASON_PAYLOAD_HASH_MISMATCH in res.reason


def test_user_assertion_tampered_canonical_field_fails(completed_bundle):
    # Change a field INSIDE the canonical string without re-signing: the
    # canonical-string reconstruction now differs from the signed bytes.
    art = dict(completed_bundle["user_assertion"])
    intent = dict(art["intent"])
    intent["description"] = "hacked"
    art["intent"] = intent
    res = verify_artifact(art, completed_bundle["user_pem"], artifact_type="user_assertion")
    assert res.valid is False


def test_user_assertion_expected_key_id_mismatch(completed_bundle):
    art = dict(completed_bundle["user_assertion"])
    res = verify_artifact(
        art, completed_bundle["user_pem"], artifact_type="user_assertion", expected_key_id="userkey_other"
    )
    assert res.valid is False
    assert res.reason == REASON_KEY_ID_MISMATCH


def test_user_assertion_unknown_type_fails(completed_bundle):
    art = dict(completed_bundle["user_assertion"])
    art["type"] = "bogus"
    res = verify_artifact(art, completed_bundle["user_pem"])
    assert res.valid is False
    assert res.reason == REASON_INVALID_ARTIFACT_TYPE


def test_user_assertion_malformed_missing_field_fails(completed_bundle):
    art = dict(completed_bundle["user_assertion"])
    del art["assertion_id"]
    res = verify_artifact(art, completed_bundle["user_pem"], artifact_type="user_assertion")
    assert res.valid is False
    assert res.reason in (REASON_CANONICAL_STRING, REASON_INVALID_ARTIFACT_TYPE)


# ------------------------------------------------------------- bundle binding


def test_full_bundle_with_user_assertion_verifies(completed_bundle):
    b = completed_bundle
    res = verify_transaction_evidence(
        b["execution_mandate"],
        b["policy_decision"],
        b["decision_receipt"],
        merchant_public_key_pem(),
        expected_key_id="merchant-key-01",
        user_assertion=b["user_assertion"],
        user_public_key_pem=b["user_pem"],
    )
    assert res.valid is True, res.checks
    assert res.checks["execution_refs_user"] is True
    assert res.checks["amounts_agree"] is True


def test_mandate_claiming_user_assertion_missing_artifacts_fails_closed(completed_bundle):
    # The Execution Mandate references a user assertion, but the caller supplies
    # no user artifacts: MUST fail closed, never guess.
    b = completed_bundle
    res = verify_transaction_evidence(
        b["execution_mandate"],
        b["policy_decision"],
        b["decision_receipt"],
        merchant_public_key_pem(),
        expected_key_id="merchant-key-01",
    )
    assert res.valid is False
    assert res.reason == REASON_BUNDLE_RELATIONSHIP
    assert res.checks["execution_refs_user"] is False
    assert res.checks.get("missing_user_artifacts") is True


def test_bundle_assertion_hash_mismatch_fails(completed_bundle):
    b = completed_bundle
    tampered = dict(b["user_assertion"])
    tampered["canonical_payload_hash"] = "f" * 64  # breaks individual + binding
    res = verify_transaction_evidence(
        b["execution_mandate"],
        b["policy_decision"],
        b["decision_receipt"],
        merchant_public_key_pem(),
        expected_key_id="merchant-key-01",
        user_assertion=tampered,
        user_public_key_pem=b["user_pem"],
    )
    assert res.valid is False


def test_bundle_amount_tamper_fails(completed_bundle):
    # total_paise is inside the signed canonical payload: tampering it breaks
    # the mandate's signature/hash BEFORE any relationship level is reached.
    b = completed_bundle
    em = dict(b["execution_mandate"])
    em["total_paise"] = em["total_paise"] + 10000
    res = verify_transaction_evidence(
        em,
        b["policy_decision"],
        b["decision_receipt"],
        merchant_public_key_pem(),
        expected_key_id="merchant-key-01",
        user_assertion=b["user_assertion"],
        user_public_key_pem=b["user_pem"],
    )
    assert res.valid is False
    assert res.checks["execution_mandate_valid"] is False


# ------------------------------------------------------------------ legacy path


def test_legacy_bundle_without_user_assertion_still_verifies(client):
    # A legacy (pre-E) completed transaction has NO user_assertion anywhere:
    # the bundle must verify with execution_refs_user True (consistent absence).
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"},
    )
    prov = client.get(f"/provenance/{csid}").json()
    res = verify_transaction_evidence(
        prov["artifacts"]["execution_mandate"],
        prov["artifacts"]["policy_decision"],
        prov["artifacts"]["decision_receipt"],
        merchant_public_key_pem(),
        expected_key_id="merchant-key-01",
    )
    assert res.valid is True, res.checks
    assert res.checks["execution_refs_user"] is True


def test_user_assertion_for_legacy_mandate_is_inconsistent(client):
    # Supplying a user assertion for a mandate that does NOT reference one is a
    # bundle inconsistency and must be reported as such.
    user_id = register_key(client, user_id="user_verify_02", key_id=KEY_VERIFY_02)
    csid = make_checkout(client).json()["checkout_session"]["checkout_session_id"]
    client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"},
    )
    prov = client.get(f"/provenance/{csid}").json()
    em = prov["artifacts"]["execution_mandate"]
    assertion = build_assertion(user_id, csid, em["total_paise"], user_key_id=KEY_VERIFY_02)
    _, pub = make_keypair()
    res = verify_transaction_evidence(
        em,
        prov["artifacts"]["policy_decision"],
        prov["artifacts"]["decision_receipt"],
        merchant_public_key_pem(),
        expected_key_id="merchant-key-01",
        user_assertion=assertion,
        user_public_key_pem=public_key_pem(pub),
    )
    assert res.valid is False
    assert res.checks["execution_refs_user"] is False