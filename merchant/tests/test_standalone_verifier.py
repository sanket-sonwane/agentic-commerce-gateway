"""Standalone AP2 verifier tests.

Proofs required by the AP2-completion task:

- valid artifact + public key -> VALID
- modified artifact + same key -> INVALID
- different public key -> INVALID
- malformed artifact -> structured INVALID (never crashes)
- decision receipt verification
- complete evidence-bundle verification (and broken relationships)
- OFFLINE verification (no merchant DB / app runtime / private key)
- the verifier NEVER loads the private key

IMPORTANT: tests use ``crypto_service.sign`` / ``public_key_pem`` ONLY to build
valid fixtures (which legitimately requires the merchant key). The standalone
verifier under test must never rely on them.
"""
import importlib
import json
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from merchant.ap2 import crypto_service
from merchant.ap2.signed_payloads import (
    decision_receipt_payload,
    execution_mandate_payload,
    intent_mandate_payload,
    policy_decision_payload,
)
from merchant.ap2.standalone_verifier import (
    verify_artifact,
    verify_transaction_evidence,
)

KEY_ID = "merchant-key-01"
MERCHANT_ID = "merchant_grocery_01"
PUB = crypto_service.public_key_pem()

_ITEMS = '[{"product_id":"p1","pack_id":"k1","quantity":2,"unit_price_paise":100,"category":"bakery"}]'
_CHECKS = '[{"rule":"spend_limit","result":"pass"}]'


def _sign_payload(payload: dict) -> tuple[str, str]:
    sig = crypto_service.sign(payload)
    hsh = crypto_service.object_hash(payload)
    return sig, hsh


def build_execution_mandate(**overrides) -> dict:
    art = {
        "mandate_id": "em_test_1",
        "type": "execution",
        "intent_mandate_id": None,
        "merchant_id": MERCHANT_ID,
        "buyer_agent_id": "buyer_1",
        "checkout_session_id": "cs_1",
        "items": _ITEMS,
        "total_paise": 200,
        "currency": "INR",
        "cart_hash": "cart_hash_abc",
        "issued_at": "2026-01-01T00:00:00",
        "expires_at": "2026-01-01T00:10:00",
        "nonce": "nonce_abc_1",
        "canonical_payload_hash": "",
        "key_id": KEY_ID,
        "signature": "",
    }
    art.update(overrides)
    payload = execution_mandate_payload(art)
    sig, hsh = _sign_payload(payload)
    art["signature"] = overrides.get("signature", sig)
    art["canonical_payload_hash"] = overrides.get("canonical_payload_hash", hsh)
    return art


def build_policy_decision(**overrides) -> dict:
    art = {
        "decision_id": "dec_1",
        "type": "policy_decision",
        "execution_mandate_id": "em_test_1",
        "checkout_session_id": "cs_1",
        "decision": "approved",
        "checks": _CHECKS,
        "authorized_amount_paise": 200,
        "timestamp": "2026-01-01T00:09:00",
        "canonical_payload_hash": "",
        "key_id": KEY_ID,
        "signature": "",
    }
    art.update(overrides)
    payload = policy_decision_payload(art)
    sig, hsh = _sign_payload(payload)
    art["signature"] = overrides.get("signature", sig)
    art["canonical_payload_hash"] = overrides.get("canonical_payload_hash", hsh)
    return art


def build_decision_receipt(**overrides) -> dict:
    art = {
        "receipt_id": "rcpt_1",
        "type": "decision_receipt",
        "intent_mandate_id": None,
        "execution_mandate_id": "em_test_1",
        "decision_id": "dec_1",
        "checkout_session_id": "cs_1",
        "authorized_amount_paise": 200,
        "razorpay_order_id": "order_1",
        "razorpay_payment_id": "pay_1",
        "payment_status": "verified",
        "outcome": "completed",
        "created_at": "2026-01-01T00:10:00",
        "canonical_payload_hash": "",
        "key_id": KEY_ID,
        "signature": "",
    }
    art.update(overrides)
    payload = decision_receipt_payload(art)
    sig, hsh = _sign_payload(payload)
    art["signature"] = overrides.get("signature", sig)
    art["canonical_payload_hash"] = overrides.get("canonical_payload_hash", hsh)
    return art


def build_intent_mandate(**overrides) -> dict:
    art = {
        "mandate_id": "im_1",
        "type": "intent",
        "merchant_id": MERCHANT_ID,
        "buyer_agent_id": "buyer_1",
        "intent": '{"description":"Buy groceries"}',
        "constraints": '{"max_order_value_paise":10000,"allowed_categories":["bakery"]}',
        "issued_at": "2026-01-01T00:00:00",
        "expires_at": "2026-01-31T00:00:00",
        "nonce": "nonce_im_1",
        "version": 1,
        "canonical_payload_hash": "",
        "key_id": KEY_ID,
        "signature": "",
    }
    art.update(overrides)
    payload = intent_mandate_payload(art)
    sig, hsh = _sign_payload(payload)
    art["signature"] = overrides.get("signature", sig)
    art["canonical_payload_hash"] = overrides.get("canonical_payload_hash", hsh)
    return art


# ---------------------------------------------------------------------------
# 1-9: mutated-field / key / signature checks
# ---------------------------------------------------------------------------


def test_1_valid_execution_mandate():
    res = verify_artifact(build_execution_mandate(), PUB, expected_key_id=KEY_ID)
    assert res.valid is True
    assert res.reason == "OK"
    assert res.checks["signature"] is True
    assert res.checks["payload_hash"] is True
    assert res.checks["key_id"] is True


def test_2_modified_amount():
    art = build_execution_mandate()
    art["total_paise"] = 999  # tamper AFTER signing -> signature/hash now stale
    res = verify_artifact(art, PUB)
    assert res.valid is False


def test_3_modified_merchant():
    art = build_execution_mandate()
    art["merchant_id"] = "evil_merchant"
    res = verify_artifact(art, PUB)
    assert res.valid is False


def test_4_modified_cart():
    art = build_execution_mandate()
    art["items"] = json.dumps([{"product_id": "pX"}])
    art["cart_hash"] = "different"
    res = verify_artifact(art, PUB)
    assert res.valid is False


def test_5_modified_nonce():
    art = build_execution_mandate()
    art["nonce"] = "tampered_nonce"
    res = verify_artifact(art, PUB)
    assert res.valid is False


def test_6_modified_expiry():
    art = build_execution_mandate()
    art["expires_at"] = "2026-01-01T00:30:00"
    res = verify_artifact(art, PUB)
    assert res.valid is False


def test_7_modified_signature():
    art = build_execution_mandate(signature="0" * 128)
    res = verify_artifact(art, PUB)
    assert res.valid is False
    assert res.reason in ("SIGNATURE_INVALID", "PAYLOAD_HASH_MISMATCH")


def test_8_wrong_public_key():
    other = Ed25519PrivateKey.generate()
    other_pem = (
        other.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    res = verify_artifact(build_execution_mandate(), other_pem)
    assert res.valid is False
    assert res.reason == "SIGNATURE_INVALID"


def test_9_payload_hash_mismatch():
    art = build_execution_mandate(canonical_payload_hash="0" * 64)
    res = verify_artifact(art, PUB)
    assert res.valid is False
    assert res.reason == "PAYLOAD_HASH_MISMATCH"


def test_9b_key_id_mismatch():
    art = build_execution_mandate(key_id="wrong-key")
    res = verify_artifact(art, PUB, expected_key_id=KEY_ID)
    assert res.valid is False
    assert res.reason == "KEY_ID_MISMATCH"


# ---------------------------------------------------------------------------
# 10: malformed
# ---------------------------------------------------------------------------


def test_10_malformed_artifact_no_crash():
    assert verify_artifact(None, PUB).valid is False
    assert verify_artifact("garbage", PUB).valid is False
    assert verify_artifact([1, 2, 3], PUB).valid is False
    res = verify_artifact({"type": "execution", "mandate_id": "x"}, PUB)
    assert res.valid is False
    assert res.reason == "MISSING_FIELD"
    # invalid artifact type
    res2 = verify_artifact({"type": "not_real", "foo": 1}, PUB)
    assert res2.valid is False
    assert res2.reason == "INVALID_ARTIFACT_TYPE"
    # invalid public key
    res3 = verify_artifact(build_execution_mandate(), "not-a-pem")
    assert res3.valid is False
    assert res3.reason == "INVALID_PUBLIC_KEY"


# ---------------------------------------------------------------------------
# 11: offline — no merchant DB / app runtime / private key
# ---------------------------------------------------------------------------


class _ForbidPersistence:
    """Import hook that raises if merchant.persistence (DB/app layer) is imported."""

    def __init__(self) -> None:
        self._raised = False

    def find_module(self, fullname: str, path=None):  # noqa: ANN001
        if fullname == "merchant.persistence" or fullname.startswith("merchant.persistence."):
            self._raised = True
            raise ImportError(f"DB/app module {fullname} must not be imported by the standalone verifier")
        return None


def test_11_offline_no_db_no_private_key(monkeypatch, tmp_path):
    # Prove the verifier chain never imports merchant.persistence (the DB/app
    # layer) and never loads the private key.
    art = build_execution_mandate()  # build fixture BEFORE blocking the key

    hook = _ForbidPersistence()
    sys.meta_path.insert(0, hook)  # type: ignore[arg-type]
    try:
        # fresh-import the verifier so no stale persistence import hides it
        for mod in list(sys.modules):
            if mod == "merchant.ap2.standalone_verifier" or mod.startswith("merchant.ap2.standalone_verifier"):
                del sys.modules[mod]

        # private key must never be loaded
        monkeypatch.setattr(crypto_service, "_private_key", lambda: (_ for _ in ()).throw(AssertionError("PRIVATE KEY TOUCHED")))
        monkeypatch.setattr(crypto_service, "_PRIVATE_KEY", None)

        verifier = importlib.import_module("merchant.ap2.standalone_verifier")
        res = verifier.verify_artifact(art, PUB, expected_key_id=KEY_ID)
        assert res.valid is True
        assert crypto_service._PRIVATE_KEY is None
        assert hook._raised is False, "verifier imported merchant.persistence (offline violation)"
    finally:
        sys.meta_path.remove(hook)


def test_11b_import_never_touches_db_dir(tmp_path, monkeypatch):
    # Run the verifier in a clean dir (no data/ created) via subprocess.
    import os
    import subprocess
    import textwrap

    art = build_execution_mandate()
    root = os.path.abspath(".")
    code = textwrap.dedent(
        f"""
        import sys, json
        from merchant.ap2.standalone_verifier import verify_artifact
        pub = {json.dumps(PUB)}
        art = json.loads({json.dumps(json.dumps(art))})
        res = verify_artifact(art, pub, expected_key_id={json.dumps(KEY_ID)})
        sys.exit(0 if res.valid else 1)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        env={**dict(os.environ), "PYTHONPATH": root},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "data").exists(), "verifier must not create merchant DB dir"


# ---------------------------------------------------------------------------
# 12-13: decision receipt
# ---------------------------------------------------------------------------


def test_12_valid_decision_receipt():
    res = verify_artifact(build_decision_receipt(), PUB, expected_key_id=KEY_ID)
    assert res.valid is True


def test_13_modified_decision_receipt():
    art = build_decision_receipt()
    art["razorpay_payment_id"] = "pay_tampered"
    res = verify_artifact(art, PUB)
    assert res.valid is False


def test_13b_modified_receipt_amount():
    art = build_decision_receipt()
    art["authorized_amount_paise"] = 1
    res = verify_artifact(art, PUB)
    assert res.valid is False


# ---------------------------------------------------------------------------
# 14-15: evidence bundle
# ---------------------------------------------------------------------------


def _bundle():
    return {
        "execution_mandate": build_execution_mandate(),
        "policy_decision": build_policy_decision(),
        "decision_receipt": build_decision_receipt(),
    }


def test_14_valid_evidence_bundle():
    im = build_intent_mandate()
    em = build_execution_mandate(intent_mandate_id=im["mandate_id"])
    pd = build_policy_decision()
    rcpt = build_decision_receipt()
    res = verify_transaction_evidence(em, pd, rcpt, PUB, expected_key_id=KEY_ID, intent_mandate=im)
    assert res.valid is True
    assert res.reason == "OK"
    assert res.checks["execution_refs_intent"] is True


def test_15_broken_bundle_relationship():
    # receipt references a different execution mandate than the one supplied
    b = _bundle()
    b["decision_receipt"]["execution_mandate_id"] = "em_OTHER"
    b["decision_receipt"] = build_decision_receipt(execution_mandate_id="em_OTHER")
    res = verify_transaction_evidence(b["execution_mandate"], b["policy_decision"], b["decision_receipt"], PUB)
    assert res.valid is False
    assert res.reason == "BUNDLE_RELATIONSHIP_FAILURE"


def test_15b_broken_bundle_amount():
    b = _bundle()
    b["decision_receipt"] = build_decision_receipt(authorized_amount_paise=999999)
    res = verify_transaction_evidence(b["execution_mandate"], b["policy_decision"], b["decision_receipt"], PUB)
    assert res.valid is False


def test_15c_bundle_rejects_tampered_mandate():
    # Even if relationships hold, a tampered artifact inside the bundle fails.
    b = _bundle()
    b["execution_mandate"] = build_execution_mandate(total_paise=100)
    res = verify_transaction_evidence(b["execution_mandate"], b["policy_decision"], b["decision_receipt"], PUB)
    assert res.valid is False


# ---------------------------------------------------------------------------
# single-byte/value modification proof
# ---------------------------------------------------------------------------


def test_single_byte_flip_invalidates_signature():
    art = build_execution_mandate()
    payload = execution_mandate_payload(art)
    # mutate one signed character in the merchant id
    art["merchant_id"] = art["merchant_id"][:-1] + ("0" if art["merchant_id"][-1] != "0" else "1")
    res = verify_artifact(art, PUB)
    assert res.valid is False
    assert res.checks["signature"] is False or res.checks["payload_hash"] is False


def test_never_loads_private_key():
    # If the verifier tried to load the private key, verification would raise.
    import merchant.ap2.standalone_verifier as sv_mod

    art = build_execution_mandate()  # build fixture BEFORE blocking the key

    original = crypto_service._private_key

    def _explode():
        raise AssertionError("verifier attempted to access the private key")

    crypto_service._private_key = _explode  # type: ignore[assignment]
    try:
        res = sv_mod.verify_artifact(art, PUB, expected_key_id=KEY_ID)
        assert res.valid is True
    finally:
        crypto_service._private_key = original


def test_result_is_structured_not_boolean():
    res = verify_artifact(build_execution_mandate(), PUB, expected_key_id=KEY_ID)
    d = res.to_dict()
    assert isinstance(d["valid"], bool)
    assert isinstance(d["checks"], dict)
    assert isinstance(d["reason"], str)
    assert "artifact_type" in d
    assert "key_id" in d
    assert "verified_at_utc" in d
