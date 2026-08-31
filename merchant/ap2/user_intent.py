"""User-signed intent assertion canon (Workstream E).

The buying human's browser signs a DETERMINISTIC pipe-separated canonical
string (fixed field order, no JSON framing) using the user's own Ed25519
keypair held in localStorage. Python (runtime verifier + standalone verifier)
and the browser (tweetnacl) must produce BYTE-IDENTICAL strings so signatures
cross-verify.

The canonical string shape:

    s1|type=UserIntentAssertion|version=1.0|assertion_id=..|user_id=..|user_key_id=..|merchant_id=..|delegate_id=..|intent=<compact json>|constraint_ceiling=<compact json>|issued_at=<iso-utc-seconds>|expires_at=<iso-utc-seconds>|nonce=..

Values are escaped deterministically (``\\`` then ``|``) so any user-supplied
text inside ``intent`` can never break field boundaries. The assertion hash is
SHA-256 over this exact canonical string.

This module is DB-free and app-free so it can be imported by the offline
standalone verifier exactly like ``signed_payloads``.
"""
import hashlib
import json
from datetime import datetime
from typing import Any

VERSION = "1.0"
TYPE = "UserIntentAssertion"
SCHEMA_PREFIX = "s1"


from merchant.ap2.iso import _iso  # noqa: F401


def _compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _esc(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|")


def assertion_canonical_string(
    *,
    assertion_id: str,
    user_id: str,
    user_key_id: str,
    merchant_id: str,
    delegate_id: str,
    intent: dict,
    constraint_ceiling: dict,
    issued_at: datetime,
    expires_at: datetime,
    nonce: str,
) -> str:
    """The exact byte string the user's key signs."""
    fields = [
        f"{SCHEMA_PREFIX}|type={TYPE}",
        f"version={_esc(VERSION)}",
        f"assertion_id={_esc(assertion_id)}",
        f"user_id={_esc(user_id)}",
        f"user_key_id={_esc(user_key_id)}",
        f"merchant_id={_esc(merchant_id)}",
        f"delegate_id={_esc(delegate_id)}",
        f"intent={_esc(_compact_json(intent))}",
        f"constraint_ceiling={_esc(_compact_json(constraint_ceiling))}",
        f"issued_at={_esc(_iso(issued_at))}",
        f"expires_at={_esc(_iso(expires_at))}",
        f"nonce={_esc(nonce)}",
    ]
    return "|".join(fields)


def assertion_hash(**kwargs: Any) -> str:
    """SHA-256 over the canonical string; stored on the assertion + execution mandate."""
    return hashlib.sha256(assertion_canonical_string(**kwargs).encode("utf-8")).hexdigest()


def _coerce_json_field(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return {}
    return {}


def artifact_canonical_string(artifact: dict) -> str | None:
    """Rebuild the canonical string from a serialized assertion artifact (dict).

    Mirrors exactly what the browser signed: malformed artifacts return None so
    the standalone verifier reports MALFORMED rather than mis-verifying. The
    ``intent``/``constraint_ceiling`` fields may be dicts or compact-JSON
    strings exactly as they were serialized at signing time.
    """
    try:
        return assertion_canonical_string(
            assertion_id=str(artifact["assertion_id"]),
            user_id=str(artifact["user_id"]),
            user_key_id=str(artifact["user_key_id"]),
            merchant_id=str(artifact["merchant_id"]),
            delegate_id=str(artifact["delegate_id"]),
            intent=_coerce_json_field(artifact.get("intent", {})),
            constraint_ceiling=_coerce_json_field(artifact.get("constraint_ceiling", {})),
            issued_at=artifact["issued_at"],
            expires_at=artifact["expires_at"],
            nonce=str(artifact["nonce"]),
        )
    except (KeyError, TypeError):
        return None