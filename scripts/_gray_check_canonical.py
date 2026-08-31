"""Workstream E — golden-vector parity check (Python half).

Recomputes the canonical string that the browser (tweetnacl) signed and asserts
byte-equality with the Python canon, plus cross-runtime signature verification.

Run:  node scripts/_js_canonical_check.mjs > artifacts/user_assertion_golden.json
      python -m scripts._gray_check_canonical
"""
import hashlib
import json
import sys

from merchant.ap2 import crypto_service
from merchant.ap2.user_intent import artifact_canonical_string, assertion_canonical_string

GOLDEN = "artifacts/user_assertion_golden.json"


def main() -> int:
    with open(GOLDEN, encoding="utf-8") as fh:
        go = json.load(fh)

    artifact = dict(go["artifact"])
    canonical_js = go["canonical"]

    canonical_py = assertion_canonical_string(
        assertion_id=artifact["assertion_id"],
        user_id=artifact["user_id"],
        user_key_id=artifact["user_key_id"],
        merchant_id=artifact["merchant_id"],
        delegate_id=artifact["delegate_id"],
        intent=artifact["intent"],
        constraint_ceiling=artifact["constraint_ceiling"],
        issued_at=artifact["issued_at"],
        expires_at=artifact["expires_at"],
        nonce=artifact["nonce"],
    )

    assert canonical_py == canonical_js, "BYTE MISMATCH: js != py canonical"
    assert artifact_canonical_string(artifact) == canonical_js, "BYTE MISMATCH: artifact rebuild"

    recorded_hash = go["canonical_payload_hash"]
    computed_hash = hashlib.sha256(canonical_js.encode("utf-8")).hexdigest()
    assert computed_hash == recorded_hash, "SHA-256 mismatch"

    assert crypto_service.verify_utf8(
        canonical_js, go["signature"], go["public_key_pem"]
    ), "JS signature does not verify under the JS-built SPKI PEM in Python"

    print("GOLDEN VECTOR OK (byte-identical JS<->Python canonical; signature verifies cross-runtime)")
    return 0


if __name__ == "__main__":
    sys.exit(main())