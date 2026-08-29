"""AP2 cryptographic provenance layer (PRD §18–24).

Provides a reusable canonical-JSON utility plus Ed25519 signing/verification
for the merchant's cryptographic identity. The private key lives only in a
gitignored file and is never exposed to buyers, the dashboard, or logs.
"""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from merchant_agent.config import settings


# ---------------------------------------------------------------- canonical JSON


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization for signing/hashing.

    - object keys sorted (recursively),
    - separators are minimal (',', ':'),
    - datetimes serialized as ISO-8601 UTC strings,
    - ensure_ascii=False for stable byte representation of unicode.
    """

    def _norm(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): _norm(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_norm(v) for v in value]
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc)
                value = value.replace(tzinfo=None)
            return value.isoformat(timespec="seconds")
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) or value is None:
            return value
        return str(value)

    return json.dumps(
        _norm(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sha256_hex(payload: Any) -> str:
    """SHA-256 over the canonical JSON of an object (PRD §21)."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def object_hash(obj: dict) -> str:
    """Hash inputs must EXCLUDE signature + non-semantic DB metadata (PRD §21)."""
    copy = dict(obj)
    copy.pop("signature", None)
    return sha256_hex(copy)


# ---------------------------------------------------------------- key management


_KEY_DIR = Path(__file__).resolve().parents[2] / "secrets"


def _key_file() -> Path:
    env_path = settings.merchant_signing_key_path
    if env_path:
        p = Path(env_path)
        if p.parent != Path("."):
            p.parent.mkdir(parents=True, exist_ok=True)
        return p
    _KEY_DIR.mkdir(parents=True, exist_ok=True)
    return _KEY_DIR / "merchant_signing_key.pem"


def _load_or_create_private_key() -> Ed25519PrivateKey:
    key_file = _key_file()
    if key_file.exists():
        raw = key_file.read_bytes()
        try:
            return serialization.load_pem_private_key(raw, password=None)
        except Exception:  # noqa: BLE001 — corrupt key file: regenerate
            pass
    private_key = Ed25519PrivateKey.generate()
    key_file.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return private_key


_PRIVATE_KEY: Ed25519PrivateKey | None = None


def _private_key() -> Ed25519PrivateKey:
    global _PRIVATE_KEY
    if _PRIVATE_KEY is None:
        _PRIVATE_KEY = _load_or_create_private_key()
    return _PRIVATE_KEY


def public_key_pem() -> str:
    pub = _private_key().public_key()
    return pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8").strip()


def key_id() -> str:
    return settings.merchant_key_id


def public_key_info() -> dict:
    return {
        "key_id": key_id(),
        "algorithm": "Ed25519",
        "public_key": public_key_pem(),
    }


def sign(payload: Any) -> str:
    """Ed25519.sign(canonical_json(payload))."""
    data = canonical_json(payload).encode("utf-8")
    return _private_key().sign(data).hex()


def verify(payload: Any, signature: str, *, public_key_pem_str: str | None = None) -> bool:
    """Verify an Ed25519 signature over canonical JSON.

    Defaults to the merchant's own public key. A caller may pass a different
    PEM to verify artifacts signed by another party (trusted key check).
    """
    try:
        if public_key_pem_str:
            pub = serialization.load_pem_public_key(public_key_pem_str.encode("utf-8"))
        else:
            pub = _private_key().public_key()
        if not isinstance(pub, Ed25519PublicKey):
            return False
        data = canonical_json(payload).encode("utf-8")
        pub.verify(bytes.fromhex(signature), data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify_with_key(raw_signature: bytes, data: bytes, public_key_pem_str: str) -> bool:
    try:
        pub = serialization.load_pem_public_key(public_key_pem_str.encode("utf-8"))
        if not isinstance(pub, Ed25519PublicKey):
            return False
        pub.verify(raw_signature, data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# ---------------------------------------------------------------- helpers


def new_nonce() -> str:
    return uuid4().hex


def expires_in(minutes: int = 10) -> datetime:
    return now_utc() + timedelta(minutes=minutes)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
