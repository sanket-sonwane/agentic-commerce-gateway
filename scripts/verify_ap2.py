"""Standalone AP2 artifact / evidence-bundle verifier CLI.

Verifies signed AP2-inspired transaction artifacts purely from supplied JSON
and a public key — no merchant DB, no application runtime, no private key.

Usage:
    python -m scripts.verify_ap2 --artifact artifacts/execution_mandate.json \
        --public-key artifacts/merchant_public_key.pem

    python -m scripts.verify_ap2 --bundle artifacts/evidence_bundle.json \
        --public-key artifacts/merchant_public_key.pem --json
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from merchant.ap2 import standalone_verifier as sv

# The CLI prints human-readable status glyphs; always emit UTF-8 regardless of
# the console's default codepage so it never crashes on Windows cp1252 etc.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _load_public_key(arg: str | None, pem: str | None) -> str:
    if pem:
        return pem
    if not arg:
        raise SystemExit("ERROR: --public-key (path) or --public-key-pem (inline) is required")
    with open(arg, "r", encoding="utf-8") as fh:
        return fh.read()


def _result_to_block(label: str, res: sv.VerificationResult) -> str:
    mapping = {
        "public_key": "Public key",
        "required_fields": "Required fields",
        "canonical_payload": "Canonical form",
        "payload_hash": "Payload hash",
        "signature": "Signature",
        "key_id": "Key ID",
        "expiry_relationship": "Expiry relationship",
    }
    lines = [f"Artifact: {label}"]
    for check, human in mapping.items():
        if check in res.checks:
            value = res.checks[check]
            rendered = (
                "VALID"
                if value is True
                else ("INVALID" if value is False else ("N/A" if value is None else str(value)))
            )
            lines.append(f"{human}:{' ' * (16 - len(human))}{rendered}")
    return "\n".join(lines)


def _run_artifact(args: argparse.Namespace) -> sv.VerificationResult:
    artifact = _load_json(args.artifact)
    pub = _load_public_key(args.public_key, args.public_key_pem)
    return sv.verify_artifact(
        artifact,
        pub,
        expected_key_id=args.expected_key_id,
        artifact_type=args.type,
    )


def _run_bundle(args: argparse.Namespace) -> sv.VerificationResult:
    bundle = _load_json(args.bundle)
    pub = _load_public_key(args.public_key, args.public_key_pem)
    artifacts = bundle.get("artifacts") or {}
    missing = [k for k in ("execution_mandate", "policy_decision", "decision_receipt") if k not in artifacts]
    if missing:
        raise SystemExit(f"ERROR: bundle missing artifacts: {', '.join(missing)}")
    return sv.verify_transaction_evidence(
        artifacts["execution_mandate"],
        artifacts["policy_decision"],
        artifacts["decision_receipt"],
        pub,
        expected_key_id=args.expected_key_id or bundle.get("key_id"),
        intent_mandate=artifacts.get("intent_mandate"),
    )


def _print_human(label: str, res: sv.VerificationResult) -> None:
    print("AP2 VERIFICATION")
    print("-" * 56)
    print(_result_to_block(label, res))
    print()
    if res.valid:
        print("RESULT: ✅ VALID")
    else:
        print("RESULT: ❌ INVALID")
        print(f"Reason: {res.reason}")
        failed = [k for k, v in (res.checks or {}).items() if v is False and k != "detail"]
        if failed:
            print(f"Failed checks: {', '.join(failed)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone AP2 artifact verifier")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--artifact", metavar="PATH", help="Signed artifact JSON to verify")
    group.add_argument("--bundle", metavar="PATH", help="Exported evidence bundle JSON to verify")

    parser.add_argument("--type", choices=["execution", "intent", "policy_decision", "decision_receipt"],
                        help="Artifact type (defaults to the artifact's 'type' field)")
    parser.add_argument("--public-key", metavar="PATH", help="Path to the merchant Ed25519 public key PEM")
    parser.add_argument("--public-key-pem", metavar="PEM", help="Inline merchant Ed25519 public key PEM")
    parser.add_argument("--expected-key-id", default=None, help="Expected key_id (recommended)")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON result")
    args = parser.parse_args(argv)

    try:
        if args.artifact:
            label = args.type or "artifact"
            res = _run_artifact(args)
        else:
            label = "Evidence bundle"
            res = _run_bundle(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — CLI must not crash on bad input
        print(f"AP2 VERIFICATION\nERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(res.to_dict(), indent=2))
    else:
        _print_human(label, res)

    return 0 if res.valid else 1


if __name__ == "__main__":
    sys.exit(main())
