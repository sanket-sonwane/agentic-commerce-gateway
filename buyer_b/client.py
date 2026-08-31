"""Buyer B — independent deterministic protocol client (PRD §39 / M7).

Proves the merchant is implemented against the published protocol, not
hardwired to Buyer Agent A: no LLM, no merchant internals — HTTP + JSON only.

Success, structured errors and malformed responses are handled explicitly.
A successful merchant response is treated as SUCCESS even when the body
contains ``"error": null`` (the merchant serializes the optional error field
as JSON null on non-error completions). An error object is only ever read
after proving it is a dict.

Usage:
    python -m buyer_b.client --query "milk bread eggs" --budget 300 [--delegation dlg_9f8e7d]
"""
import argparse
import sys

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://localhost:8000"
BUYER_AGENT_ID = "buyer_agent_b_deterministic"
DEFAULT_DELEGATION = "dlg_buyerb_001"


def _error(code: str, message: str, retryable: bool) -> dict:
    """Structured client-side error mirroring the merchant error envelope."""
    return {"error": {"code": code, "message": message, "retryable": retryable, "details": {}}}


def _body(status: int | None, response: httpx.Response | None) -> tuple[int | None, dict]:
    """Transport + parse guard: never raise on an unreachable/non-JSON merchant."""
    if response is None:
        return status, _error(
            "CLIENT_TRANSPORT_ERROR",
            f"Merchant unreachable (HTTP request failed with status {status})",
            True,
        )
    try:
        return status, response.json()
    except ValueError:
        return status, _error(
            "CLIENT_MALFORMED_RESPONSE",
            f"Merchant returned a non-JSON body (HTTP {status})",
            False,
        )


def post(path: str, payload: dict) -> tuple[int | None, dict]:
    try:
        r = httpx.post(f"{BASE}{path}", json=payload, timeout=30)
    except httpx.RequestError:
        return _body(None, None)
    except Exception as exc:  # noqa: BLE001 — any failure is reported, never raised
        return None, _error("CLIENT_TRANSPORT_ERROR", f"Merchant request failed: {exc}", True)
    return _body(r.status_code, r)


def get(path: str) -> tuple[int | None, dict]:
    try:
        r = httpx.get(f"{BASE}{path}", timeout=30)
    except httpx.RequestError:
        return _body(None, None)
    except Exception as exc:  # noqa: BLE001
        return None, _error("CLIENT_TRANSPORT_ERROR", f"Merchant request failed: {exc}", True)
    return _body(r.status_code, r)


def post_with_key(path: str, payload: dict, key: str) -> tuple[int | None, dict]:
    try:
        r = httpx.post(
            f"{BASE}{path}",
            json=payload,
            headers={"Idempotency-Key": key},
            timeout=60,
        )
    except httpx.RequestError:
        return _body(None, None)
    except Exception as exc:  # noqa: BLE001
        return None, _error("CLIENT_TRANSPORT_ERROR", f"Merchant request failed: {exc}", True)
    return _body(r.status_code, r)


def _is_mapping(value) -> bool:
    return isinstance(value, dict)


def _num(value) -> int:
    """Coerce a possibly-malformed numeric field; never raise."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _report_error(status, data: dict, prefix: str) -> None:
    """Print a structured protocol error (decline / merchant error) safely."""
    envelope = data.get("error")
    if _is_mapping(envelope) and envelope.get("code"):
        print(f"[buyer-b] {prefix} | reason={envelope.get('code')}: {envelope.get('message')}")
    else:
        print(f"[buyer-b] {prefix} | HTTP {status}, no merchant error envelope")


def run(query: str, budget: int | None, delegation_token: str) -> bool:
    print(f"[buyer-b] goal: {query!r} budget={budget}")

    # 1. Discovery via the public protocol.
    status, data = post("/discover", {"query": query, "buyer_agent_id": BUYER_AGENT_ID})
    if not _is_mapping(data):
        print("[buyer-b] discovery MALFORMED: merchant returned a non-object response")
        return False
    if status != 200:
        _report_error(status, data, "discovery FAILED")
        return False
    proposal = data.get("basket_proposal")
    if not isinstance(proposal, list):
        print("[buyer-b] discovery MALFORMED: no basket_proposal list in response")
        return False
    print(f"[buyer-b] proposal ({len(proposal)} items):")
    for p in proposal:
        if not _is_mapping(p):
            print(f"    {p!r}  matched_for=<malformed item>")
            continue
        print(f"    {p.get('name', '<unknown>'):<38} ₹{p.get('price', 0):>5}  matched_for={p.get('matched_for', '?')}")

    # 2. Deterministic selection: keep cheapest items until budget would break.
    chosen, spent = [], 0
    valid_items = [p for p in proposal if _is_mapping(p)]
    for p in sorted(valid_items, key=lambda x: _num(x.get("price")) * _num(x.get("quantity"))):
        cost = _num(p.get("price")) * _num(p.get("quantity"))
        if budget is None or spent + cost <= budget:
            item_id = (p.get("product_id"), p.get("pack_id"))
            if not all(item_id):
                print(f"[buyer-b] skipping {p.get('name')} (missing product_id/pack_id)")
                continue
            quantity = _num(p.get("quantity", 1))
            if quantity <= 0:
                print(f"[buyer-b] skipping {p.get('name')} (malformed quantity)")
                continue
            chosen.append({"product_id": item_id[0], "pack_id": item_id[1], "quantity": quantity})
            spent += cost
        else:
            print(f"[buyer-b] skipping {p.get('name')} (₹{cost}) — would exceed budget")
    if not chosen:
        print("[buyer-b] nothing affordable within budget; aborting.")
        return False

    # 3. Create checkout (merchant prices authoritatively).
    status, data = post(
        "/checkout_sessions",
        {
            "buyer_agent_id": BUYER_AGENT_ID,
            "delegation_token": delegation_token,
            "line_items": [
                {"product_id": c["product_id"], "pack_id": c["pack_id"], "quantity": c["quantity"]}
                for c in chosen
            ],
        },
    )
    if not _is_mapping(data):
        print("[buyer-b] checkout MALFORMED: merchant returned a non-object response")
        return False
    session = data.get("checkout_session")
    if status != 201:
        _report_error(status, data, "checkout declined")
        return False
    if not _is_mapping(session):
        print("[buyer-b] checkout MALFORMED: no checkout_session object in response")
        return False
    csid = session.get("checkout_session_id")
    totals = session.get("totals")
    total = totals.get("total") if _is_mapping(totals) else None
    print(f"[buyer-b] checkout {csid} created: requires_payment, merchant total=₹{total} "
          f"(v{session.get('version')})")

    # 4. Complete with an idempotency key; retry-safe by contract.
    import uuid

    key = f"buyerb-{uuid.uuid4().hex[:10]}"
    status, data = post_with_key(
        f"/checkout_sessions/{csid}/complete",
        {"buyer_agent_id": BUYER_AGENT_ID, "delegation_token": delegation_token},
        key,
    )
    if not _is_mapping(data):
        print("[buyer-b] outcome: MALFORMED | merchant returned a non-object response")
        return False

    body_session = data.get("checkout_session")
    final_status = body_session.get("status") if _is_mapping(body_session) else None

    # --- SUCCESS (first-class). The merchant body may still contain
    #     "error": null — that is not an error object and is never read as one.
    if status == 200 and final_status == "completed":
        payment = body_session.get("payment") if _is_mapping(body_session.get("payment")) else {}
        totals = body_session.get("totals") if _is_mapping(body_session.get("totals")) else {}
        if totals.get("total") is None:
            # Malformed: a completed session must carry its authoritative totals.
            print("[buyer-b] outcome: MALFORMED | session claims completed but totals are missing")
            return False
        print(f"[buyer-b] outcome: completed | paid ₹{totals.get('total')} "
              f"(order={payment.get('order_id')}, payment={payment.get('payment_id')})")

        # Idempotency proof: replay must return the SAME result, no double charge.
        status2, data2 = post_with_key(
            f"/checkout_sessions/{csid}/complete",
            {"buyer_agent_id": BUYER_AGENT_ID, "delegation_token": delegation_token},
            key,
        )
        pay2 = data2.get("checkout_session", {}).get("payment", {})
        if not _is_mapping(pay2):
            pay2 = {}
        same = pay2.get("payment_id") == payment.get("payment_id")
        print(f"[buyer-b] replay check: status={status2}, same_payment={same} (no double charge)")
        return same

    # --- DECLINE / structured protocol error (non-2xx envelope or 200-with-error).
    if _is_mapping(data.get("error")) and data.get("error").get("code"):
        envelope = data["error"]
        print(f"[buyer-b] outcome: {final_status or 'ERROR'} | "
              f"reason={envelope.get('code')}: {envelope.get('message')}")
        return False

    # --- Everything else: malformed / non-terminal without an error envelope.
    # The original bug seam (200 + requires_payment + "error": null) lands here:
    # it is NOT success and NOT a structured error, so we report it honestly.
    print(f"[buyer-b] outcome: {final_status or 'ERROR'} | HTTP {status}, no merchant error envelope")
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deterministic Buyer B protocol client")
    parser.add_argument("--query", default="milk bread and eggs")
    parser.add_argument("--budget", type=int, default=None)
    parser.add_argument("--delegation", default=DEFAULT_DELEGATION)
    parser.add_argument("--base", default=BASE)
    args = parser.parse_args()
    BASE = args.base.rstrip("/")
    ok = run(args.query, args.budget, args.delegation)
    sys.exit(0 if ok else 1)