"""Buyer B — independent deterministic protocol client (PRD §39 / M7).

Proves the merchant is implemented against the published protocol, not
hardwired to Buyer Agent A: no LLM, no merchant internals — HTTP + JSON only.

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


def post(path: str, payload: dict) -> tuple[int, dict]:
    r = httpx.post(f"{BASE}{path}", json=payload, timeout=30)
    return r.status_code, r.json()


def get(path: str) -> tuple[int, dict]:
    r = httpx.get(f"{BASE}{path}", timeout=30)
    return r.status_code, r.json()


def run(query: str, budget: int | None, delegation_token: str) -> bool:
    print(f"[buyer-b] goal: {query!r} budget={budget}")

    # 1. Discovery via the public protocol.
    status, data = post("/discover", {"query": query, "buyer_agent_id": BUYER_AGENT_ID})
    if status != 200:
        print(f"[buyer-b] discovery failed: {data}")
        return False
    proposal = data["basket_proposal"]
    print(f"[buyer-b] proposal ({len(proposal)} items):")
    for p in proposal:
        print(f"    {p['name']:<38} ₹{p['price']:>5}  matched_for={p['matched_for']}")

    # 2. Deterministic selection: keep cheapest items until budget would break.
    chosen, spent = [], 0
    for p in sorted(proposal, key=lambda x: x["price"] * x["quantity"]):
        cost = p["price"] * p["quantity"]
        if budget is None or spent + cost <= budget:
            chosen.append({"product_id": p["product_id"], "pack_id": p["pack_id"], "quantity": p["quantity"]})
            spent += cost
        else:
            print(f"[buyer-b] skipping {p['name']} (₹{cost}) — would exceed budget")
    dropped = [p["matched_for"] for p in proposal if p["product_id"] not in {c["product_id"] for c in chosen}]
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
    session = data.get("checkout_session", {})
    if status != 201:
        err = data.get("error", {})
        print(f"[buyer-b] checkout declined: {err.get('code')} — {err.get('message')}")
        return False
    csid = session["checkout_session_id"]
    total = session["totals"]["total"]
    print(f"[buyer-b] checkout {csid} created: requires_payment, merchant total=₹{total} (v{session['version']})")

    # 4. Complete with an idempotency key; retry-safe by contract.
    import uuid

    key = f"buyerb-{uuid.uuid4().hex[:10]}"
    status, data = post_with_key(
        f"/checkout_sessions/{csid}/complete",
        {"buyer_agent_id": BUYER_AGENT_ID, "delegation_token": delegation_token},
        key,
    )
    body_session = data.get("checkout_session", {})
    final_status = body_session.get("status")

    if status == 200 and final_status == "completed":
        payment = body_session.get("payment", {})
        print(f"[buyer-b] ✅ completed: paid ₹{body_session['totals']['total']} "
              f"(order={payment.get('order_id')}, payment={payment.get('payment_id')})")

        # Idempotency proof: replay must return the SAME result, no double charge.
        status2, data2 = post_with_key(
            f"/checkout_sessions/{csid}/complete",
            {"buyer_agent_id": BUYER_AGENT_ID, "delegation_token": delegation_token},
            key,
        )
        pay2 = data2.get("checkout_session", {}).get("payment", {})
        same = pay2.get("payment_id") == payment.get("payment_id")
        print(f"[buyer-b] replay check: status={status2}, same_payment={same} (no double charge)")
        return same

    err = data.get("error", {})
    print(f"[buyer-b] outcome: {final_status} | error={err.get('code')} — {err.get('message')}")
    return False


def post_with_key(path: str, payload: dict, key: str) -> tuple[int, dict]:
    r = httpx.post(
        f"{BASE}{path}",
        json=payload,
        headers={"Idempotency-Key": key},
        timeout=60,
    )
    return r.status_code, r.json()


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
