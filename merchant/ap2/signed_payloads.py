"""AP2 signed-payload builders — DB-free and ORM-agnostic.

These pure functions reconstruct the canonical JSON payload that is signed for
each AP2 artifact (PRD §18–24). They are the SINGLE source of truth for ``what
is signed`` and are shared by:

- the merchant runtime (``merchant.ap2.authorization_service``) that creates
  and signs artifacts, and
- the standalone independent verifier
  (``merchant.ap2.standalone_verifier``) that re-verifies exported artifacts.

Because both sides use the exact same builders and the exact same
``crypto_service.canonical_json`` serialization, a signature produced by the
runtime verifies identically in the standalone verifier.

This module must stay free of any merchant database / application / LLM
import so the verifier can run fully offline with only supplied artifacts and
a public key.
"""
import json
from typing import Any

from merchant.ap2.iso import _iso  # noqa: F401 — re-export for backward compat


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute access that also works for plain dict artifacts.

    For plain dicts the real key is read first so that artifact fields such as
    ``items`` do not collide with dict built-in methods (``dict.items``). For
    ORM entity objects attribute access is used.
    """
    if isinstance(obj, dict):
        return obj[name] if name in obj else default
    if hasattr(obj, name):
        return getattr(obj, name)
    return default


def execution_mandate_payload(m: Any) -> dict:
    m_items = _get(m, "items", "[]")
    return {
        "mandate_id": _get(m, "mandate_id"),
        "type": "execution",
        "intent_mandate_id": _get(m, "intent_mandate_id"),
        "merchant_id": _get(m, "merchant_id"),
        "buyer_agent_id": _get(m, "buyer_agent_id"),
        "checkout_session_id": _get(m, "checkout_session_id"),
        "items": json.loads(m_items) if isinstance(m_items, str) else m_items,
        "total_paise": _get(m, "total_paise"),
        "currency": _get(m, "currency"),
        "user_assertion_id": _get(m, "user_assertion_id"),
        "user_assertion_hash": _get(m, "user_assertion_hash"),
        "iss_ts": _iso(_get(m, "issued_at")),
        "exp": _iso(_get(m, "expires_at")),
        "nonce": _get(m, "nonce"),
    }


def policy_decision_payload(d: Any) -> dict:
    d_checks = _get(d, "checks", "[]")
    return {
        "decision_id": _get(d, "decision_id"),
        "execution_mandate_id": _get(d, "execution_mandate_id"),
        "checkout_session_id": _get(d, "checkout_session_id"),
        "decision": _get(d, "decision"),
        "checks": json.loads(d_checks) if isinstance(d_checks, str) else d_checks,
        "authorized_amount_paise": _get(d, "authorized_amount_paise"),
        "ts": _iso(_get(d, "timestamp")),
    }


def decision_receipt_payload(r: Any) -> dict:
    return {
        "receipt_id": _get(r, "receipt_id"),
        "intent_mandate_id": _get(r, "intent_mandate_id"),
        "execution_mandate_id": _get(r, "execution_mandate_id"),
        "decision_id": _get(r, "decision_id"),
        "checkout_session_id": _get(r, "checkout_session_id"),
        "authorized_amount_paise": _get(r, "authorized_amount_paise"),
        "razorpay_order_id": _get(r, "razorpay_order_id"),
        "razorpay_payment_id": _get(r, "razorpay_payment_id"),
        "payment_status": _get(r, "payment_status"),
        "outcome": _get(r, "outcome"),
        "ts": _iso(_get(r, "created_at")),
    }


def intent_mandate_payload(m: Any) -> dict:
    m_intent = _get(m, "intent", "{}")
    m_constraints = _get(m, "constraints", "{}")
    return {
        "mandate_id": _get(m, "mandate_id"),
        "type": "intent",
        "merchant_id": _get(m, "merchant_id"),
        "buyer_agent_id": _get(m, "buyer_agent_id"),
        "intent": json.loads(m_intent) if isinstance(m_intent, str) else m_intent,
        "constraints": json.loads(m_constraints) if isinstance(m_constraints, str) else m_constraints,
        "iss_ts": _iso(_get(m, "issued_at")),
        "exp": _iso(_get(m, "expires_at")),
        "nonce": _get(m, "nonce"),
        "version": _get(m, "version"),
    }
