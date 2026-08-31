"""Export AP2 evidence for one checkout session into a self-contained bundle.

This is a DEMO EXPORT utility — it intentionally touches the merchant database
to read persisted artifacts, then writes a deterministic JSON bundle that the
DB-free standalone verifier (``scripts.verify_ap2.py``) can consume offline.

It is NOT the verifier. The verifier never touches a database.

Usage:
    python -m scripts.export_ap2_evidence <checkout_session_id> \
        [--out artifacts/evidence_bundle.json]

The exported bundle contains NO private keys, secrets or admin credentials.
"""
from __future__ import annotations

import argparse
import json
import sys

from merchant.ap2 import crypto_service
from merchant.ap2.signed_payloads import _iso
from merchant.persistence.db import SessionLocal
from merchant.persistence.models.entities import (
    CheckoutSession,
    DecisionReceipt,
    ExecutionMandate,
    IntentMandate,
    PolicyDecision,
)


def _execution_mandate_out(m: ExecutionMandate) -> dict:
    return {
        "mandate_id": m.mandate_id,
        "type": "execution",
        "intent_mandate_id": m.intent_mandate_id,
        "merchant_id": m.merchant_id,
        "buyer_agent_id": m.buyer_agent_id,
        "checkout_session_id": m.checkout_session_id,
        "items": m.items,
        "total_paise": m.total_paise,
        "currency": m.currency,
        "cart_hash": m.cart_hash,
        "issued_at": _iso(m.issued_at),
        "expires_at": _iso(m.expires_at),
        "nonce": m.nonce,
        "canonical_payload_hash": m.canonical_payload_hash,
        "key_id": m.key_id,
        "signature": m.signature,
    }


def _policy_decision_out(d: PolicyDecision) -> dict:
    return {
        "decision_id": d.decision_id,
        "type": "policy_decision",
        "execution_mandate_id": d.execution_mandate_id,
        "checkout_session_id": d.checkout_session_id,
        "decision": d.decision,
        "checks": d.checks,
        "authorized_amount_paise": d.authorized_amount_paise,
        "timestamp": _iso(d.timestamp),
        "canonical_payload_hash": d.canonical_payload_hash,
        "key_id": d.key_id,
        "signature": d.signature,
    }


def _decision_receipt_out(r: DecisionReceipt) -> dict:
    return {
        "receipt_id": r.receipt_id,
        "type": "decision_receipt",
        "intent_mandate_id": r.intent_mandate_id,
        "execution_mandate_id": r.execution_mandate_id,
        "decision_id": r.decision_id,
        "checkout_session_id": r.checkout_session_id,
        "authorized_amount_paise": r.authorized_amount_paise,
        "razorpay_order_id": r.razorpay_order_id,
        "razorpay_payment_id": r.razorpay_payment_id,
        "payment_status": r.payment_status,
        "outcome": r.outcome,
        "created_at": _iso(r.created_at),
        "canonical_payload_hash": r.canonical_payload_hash,
        "key_id": r.key_id,
        "signature": r.signature,
    }


def _intent_mandate_out(m: IntentMandate) -> dict:
    return {
        "mandate_id": m.mandate_id,
        "type": "intent",
        "merchant_id": m.merchant_id,
        "buyer_agent_id": m.buyer_agent_id,
        "intent": m.intent,
        "constraints": m.constraints,
        "issued_at": _iso(m.issued_at),
        "expires_at": _iso(m.expires_at),
        "nonce": m.nonce,
        "version": m.version,
        "canonical_payload_hash": m.canonical_payload_hash,
        "key_id": m.key_id,
        "signature": m.signature,
    }


def export_bundle(checkout_session_id: str) -> dict:
    with SessionLocal() as db:
        cs = db.get(CheckoutSession, checkout_session_id)
        if cs is None:
            raise SystemExit(f"ERROR: no checkout session {checkout_session_id}")
        if not cs.execution_mandate_id:
            raise SystemExit(
                f"ERROR: session {checkout_session_id} has no Execution Mandate "
                "(predates the AP2 layer); nothing to export."
            )

        em = db.get(ExecutionMandate, cs.execution_mandate_id)
        if em is None:
            raise SystemExit("ERROR: Execution Mandate record missing")

        decision = (
            db.query(PolicyDecision)
            .filter(PolicyDecision.execution_mandate_id == em.mandate_id)
            .order_by(PolicyDecision.created_at.desc())
            .first()
        )
        receipt = (
            db.query(DecisionReceipt)
            .filter(DecisionReceipt.execution_mandate_id == em.mandate_id)
            .order_by(DecisionReceipt.created_at.desc())
            .first()
        )
        intent = db.get(IntentMandate, em.intent_mandate_id) if em.intent_mandate_id else None

        if receipt is None:
            print("WARNING: no Decision Receipt yet (transaction not settled).", file=sys.stderr)

        artifacts = {
            "execution_mandate": _execution_mandate_out(em),
            "policy_decision": _policy_decision_out(decision) if decision else None,
            "decision_receipt": _decision_receipt_out(receipt) if receipt else None,
        }
        if decision is None:
            print("WARNING: no Policy Decision artifact found.", file=sys.stderr)
        if intent is not None:
            artifacts["intent_mandate"] = _intent_mandate_out(intent)

        return {
            "version": "1.0",
            "description": "AP2-inspired signed transaction artifacts (no secrets)",
            "checkout_session_id": checkout_session_id,
            "key_id": crypto_service.key_id(),
            "public_key": {
                "key_id": crypto_service.key_id(),
                "algorithm": "Ed25519",
                "public_key_pem": crypto_service.public_key_pem(),
            },
            "artifacts": artifacts,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export an AP2 evidence bundle for a checkout session")
    parser.add_argument("checkout_session_id", help="ID of the completed/declined checkout session")
    parser.add_argument("--out", default="artifacts/evidence_bundle.json", help="Output bundle path")
    args = parser.parse_args(argv)

    bundle = export_bundle(args.checkout_session_id)
    import os

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2, sort_keys=True, default=str)
    print(f"Exported AP2 evidence bundle -> {args.out}")
    print(f"  checkout_session_id : {bundle['checkout_session_id']}")
    print(f"  execution_mandate   : {bundle['artifacts']['execution_mandate']['mandate_id']}")
    print(f"  key_id              : {bundle['key_id']}")
    if bundle["artifacts"].get("decision_receipt"):
        print(f"  decision_receipt    : {bundle['artifacts']['decision_receipt']['receipt_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
