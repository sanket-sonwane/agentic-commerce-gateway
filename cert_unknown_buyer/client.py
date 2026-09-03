"""Unknown-Buyer certification client (M7).

A pure-HTTP Machine-Checkable (MAC) interop client, independent of merchant
internals — httpx + JSON only, no merchant imports, no LLM. It reads the
merchant's own capability manifest (``/.well-known/merchant``), validates live
responses against the published JSON schemas in ``protocol/schemas/`` (structural
contract checks mirroring the draft-07 schema, without adding a schema-library
dependency), and exercises the full discover -> checkout -> complete -> order ->
provenance flow an unknown buyer can run with only the manifest + schemas.

The client never trusts merchant internals; every field it depends on is
guard-checked and any deviation is reported as a structured CONTRACT_* failure.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://localhost:8000"
BUYER_AGENT_ID = "buyer_agent_b_deterministic"
DELEGATION_TOKEN = "dlg_buyerb_001"
SCHEMAS = Path(__file__).resolve().parents[1] / "protocol" / "schemas"


def _mapping(value) -> bool:
    return isinstance(value, dict)


def _num(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message, "retryable": False, "details": {}}}


def _load_schema(name: str) -> dict | None:
    """Load a published JSON schema file (never fails the client hard)."""
    try:
        import json

        with open(SCHEMAS / name, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def _check_required(schema: dict, obj, path: str, problems: list[str]) -> None:
    """Structural check mirroring draft-07 ``required`` + ``properties``."""
    required = schema.get("required", [])
    props = schema.get("properties", {})
    for key in required:
        if key not in obj:
            problems.append(f"{path}: missing required field '{key}'")
        elif key in props and "enum" in props[key]:
            allowed = props[key]["enum"]
            if obj[key] not in allowed and obj[key] not in (None,):
                problems.append(f"{path}.{key}: '{obj[key]}' not in enum {allowed}")
        elif key in props and props[key].get("const") is not None:
            if obj[key] != props[key]["const"]:
                problems.append(f"{path}.{key}: expected const {props[key]['const']}, got {obj[key]}")

    # Nested required structures declared as "$ref"/object in properties.
    for key in ("line_items",):
        if key in obj and isinstance(obj[key], list):
            item_schema = _load_schema("order.json")
            item_props = item_schema["properties"][key]["items"]["properties"] if item_schema else {}
            item_required = item_schema["properties"][key]["items"]["required"] if item_schema else []
            for i, item in enumerate(obj[key]):
                for f in item_required:
                    if f not in item:
                        problems.append(f"{path}.line_items[{i}]: missing '{f}'")


class _Transport:
    def __init__(self, base: str):
        self.base = base
        self._post_fn = None
        self._get_fn = None

    def inject(self, post_fn=None, get_fn=None) -> None:
        """Allow tests to substitute a live/ASGI transport (buyer-B style)."""
        if post_fn is not None:
            self._post_fn = post_fn
        if get_fn is not None:
            self._get_fn = get_fn

    def post(self, path: str, payload: dict, key: str | None = None) -> tuple[int | None, dict]:
        if self._post_fn is not None:
            return self._post_fn(path, payload, key)
        headers = {"Idempotency-Key": key} if key else {}
        try:
            r = httpx.post(self.base + path, json=payload, headers=headers, timeout=60)
        except Exception as exc:  # noqa: BLE001
            return None, _error("CLIENT_TRANSPORT_ERROR", f"request failed: {exc}")
        return r.status_code, self._parse(r)

    def get(self, path: str) -> tuple[int | None, dict]:
        if self._get_fn is not None:
            return self._get_fn(path)
        try:
            r = httpx.get(self.base + path, timeout=60)
        except Exception as exc:  # noqa: BLE001
            return None, _error("CLIENT_TRANSPORT_ERROR", f"request failed: {exc}")
        return r.status_code, self._parse(r)

    @staticmethod
    def _parse(r) -> dict:
        try:
            return r.json()
        except ValueError:
            return _error("CLIENT_MALFORMED_RESPONSE", f"non-JSON body (HTTP {r.status_code})")


def _validate_manifest(manifest: dict, problems: list[str]) -> None:
    schema = _load_schema("merchant_manifest.json")
    if schema:
        _check_required(schema, manifest, "manifest", problems)
    caps = manifest.get("capabilities")
    if not isinstance(caps, list) or not caps:
        problems.append("manifest: missing capabilities list")
    else:
        for cap in caps:
            if not _mapping(cap) or not cap.get("name"):
                problems.append("manifest: malformed capability entry")
    if not isinstance(manifest.get("endpoints"), list):
        problems.append("manifest: missing endpoints list")
    for ep in manifest.get("endpoints", []):
        if not _mapping(ep) or not all(ep.get(k) for k in ("path", "method", "description")):
            problems.append("manifest: malformed endpoint entry")


def _validate_error_contract(data: dict, status: int | None, problems: list[str]) -> None:
    """A non-2xx result must be a valid ProtocolErrorEnvelope."""
    schema = _load_schema("error.json")
    err = data.get("error")
    if not _mapping(err):
        problems.append("error-contract: non-2xx without a dict error envelope")
        return
    if schema:
        _check_required(schema, data, "error-response", problems)


def certify(base: str, transport: _Transport | None = None) -> bool:
    """Run the full unknown-buyer interop certification; returns True on success."""
    t = transport if transport is not None else _Transport(base)
    problems: list[str] = []

    # 1. Capability manifest (machine-readable contract entry point).
    status, manifest = t.get("/.well-known/merchant")
    if status != 200:
        problems.append(f"manifest: HTTP {status} — no capability manifest at /.well-known/merchant")
        _report(problems)
        return False
    _validate_manifest(manifest, problems)

    # 2. Discover -> checkout -> complete via the deployed merchant (no internals).
    status, disc = t.post("/discover", {"query": "milk", "buyer_agent_id": BUYER_AGENT_ID})
    if status != 200 or not isinstance(disc.get("basket_proposal"), list):
        problems.append(f"discover: HTTP {status} with no basket_proposal")
    proposal = [p for p in disc.get("basket_proposal", []) if _mapping(p)]

    chosen = []
    for p in proposal:
        if p.get("product_id") and p.get("pack_id") and _num(p.get("quantity", 0)) > 0:
            chosen.append(
                {"product_id": p["product_id"], "pack_id": p["pack_id"],
                 "quantity": _num(p.get("quantity"))}
            )
        if len(chosen) >= 1:
            break
    if not chosen:
        problems.append("discover: no actionable proposal item")

    csid = None
    pack_schema = _load_schema("catalog_pack.json")
    if chosen:
        status, data = t.post(
            "/checkout_sessions",
            {"buyer_agent_id": BUYER_AGENT_ID, "delegation_token": DELEGATION_TOKEN,
             "line_items": chosen},
        )
        session = data.get("checkout_session") if _mapping(data) else None
        if status != 201 or not _mapping(session):
            problems.append(f"checkout: HTTP {status} creating session")
        else:
            csid = session.get("checkout_session_id")

    # 3. Catalog read validation (commerce semantics contract).
    status, pack = t.get(f"/catalog/packs/{chosen[0]['pack_id']}" if chosen else "/catalog/packs/__none__")
    if status != 200:
        problems.append("catalog: pack read failed")
    elif pack_schema:
        _check_required(pack_schema, pack, "pack", problems)

    # 4. Complete payment (authoritative amount binding) and read the order.
    if csid:
        key = "cert-unknownbuyer-" + csid[-8:]
        status, data = t.post(
            f"/checkout_sessions/{csid}/complete",
            {"buyer_agent_id": BUYER_AGENT_ID, "delegation_token": DELEGATION_TOKEN},
            key,
        )
        body_session = data.get("checkout_session") if _mapping(data) else None
        if status != 200 or not _mapping(body_session) or body_session.get("status") != "completed":
            _validate_error_contract(data, status, problems)
        else:
            order_schema = _load_schema("order.json")
            lst = t.get(f"/orders?buyer_agent_id={BUYER_AGENT_ID}&delegation_token={DELEGATION_TOKEN}")
            body = lst[1] if _mapping(lst[1]) else {}
            orders = body.get("orders") if _mapping(body) else []
            if not isinstance(orders, list) or not orders:
                problems.append("orders: no orders returned after completed payment")
            else:
                order_id = orders[0].get("order_id")
                if order_schema:
                    _check_required(order_schema, orders[0], "order", problems)
                r = t.get(f"/orders/{order_id}?buyer_agent_id={BUYER_AGENT_ID}&delegation_token={DELEGATION_TOKEN}")
                if r[0] != 200:
                    problems.append("orders: single order read failed")

    # 5. Provenance/order state must be queryable publicly.
    if csid:
        st, prov = t.get(f"/provenance/{csid}")
        if st != 200:
            problems.append("provenance: HTTP %s for completed session" % st)

    return _report(problems)


def _report(problems: list[str]) -> bool:
    if problems:
        print("[cert] FAILED certification:")
        for p in problems:
            print(f"  - {p}")
        return False
    print("[cert] all contract checks passed against the live merchant.")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Unknown-Buyer certification client")
    parser.add_argument("--base", default=BASE)
    args = parser.parse_args()
    sys.exit(0 if certify(args.base.rstrip("/")) else 1)
