"""Audit script that exercises the merchant protocol end-to-end."""
import json
import httpx

BASE = "http://localhost:8000"
BUYER = "buyer_agent_b_deterministic"
DLG = "dlg_buyerb_001"


def step(title):
    print(f"\n===== {title} =====")


def main():
    r = httpx.post(f"{BASE}/admin/demo/reset", headers={"X-Admin-Token": "admin_demo_token"}, timeout=15)
    print("reset:", r.json())

    step("DISCOVERY")
    r = httpx.post(f"{BASE}/discover",
                   json={"query": "chicken biryani", "buyer_agent_id": BUYER},
                   timeout=15)
    d = r.json()
    print("items:", len(d.get("basket_proposal", [])))
    print("total:", d.get("estimated_total"))
    print("parser:", d.get("parser"))

    step("CREATE CHECKOUT (milk 2x)")
    r = httpx.post(f"{BASE}/checkout_sessions",
                   json={"buyer_agent_id": BUYER, "delegation_token": DLG,
                         "line_items": [{"product_id": "prod_milk_amul",
                                         "pack_id": "pack_milk_amul_500", "quantity": 2}]},
                   timeout=15)
    d = r.json()
    cs = d["checkout_session"]
    print("id:", cs["checkout_session_id"])
    print("status:", cs["status"])
    print("total:", cs["totals"]["total"])
    print("checks:", [c["rule"] + ":" + c["result"] for c in cs["gating_result"]["checks"]])
    csid = cs["checkout_session_id"]

    step("COMPLETE")
    r = httpx.post(f"{BASE}/checkout_sessions/{csid}/complete",
                   json={"buyer_agent_id": BUYER, "delegation_token": DLG},
                   headers={"Idempotency-Key": "audit-key-1"}, timeout=30)
    d = r.json()
    if "checkout_session" in d:
        cs = d["checkout_session"]
        print("status:", cs["status"])
        print("payment:", cs.get("payment"))
    else:
        print("ERROR:", d)

    step("IDEMPOTENT REPLAY")
    r = httpx.post(f"{BASE}/checkout_sessions/{csid}/complete",
                   json={"buyer_agent_id": BUYER, "delegation_token": DLG},
                   headers={"Idempotency-Key": "audit-key-1"}, timeout=30)
    cs2 = r.json()["checkout_session"]
    print("status:", cs2["status"])
    print("same_payment_id:", cs2["payment"].get("payment_id") == cs["payment"].get("payment_id"))

    step("AUDIT EVENTS")
    r = httpx.get(f"{BASE}/audit_logs?checkout_session_id={csid}", timeout=15)
    evs = r.json()["events"]
    print("actions:", [e["action"] for e in evs])

    step("CHAIN VERIFY")
    r = httpx.get(f"{BASE}/audit_logs/verify", timeout=15)
    print(r.json())

    step("EXPLAIN")
    r = httpx.get(f"{BASE}/explain/{csid}", timeout=15)
    print(r.json())

    step("ADVERSARIAL: BUYER TRIES TO INJECT PRICE")
    r = httpx.post(f"{BASE}/checkout_sessions",
                   json={"buyer_agent_id": BUYER, "delegation_token": DLG,
                         "line_items": [{"product_id": "prod_milk_amul",
                                         "pack_id": "pack_milk_amul_500",
                                         "quantity": 1, "unit_price": 1}]},
                   timeout=15)
    d = r.json()
    if "checkout_session" in d:
        cs = d["checkout_session"]
        print("server-chosen unit_price:", cs["line_items"][0]["unit_price"])
        print("total:", cs["totals"]["total"])
    else:
        print("response:", d)

    step("ADVERSARIAL: WRONG BUYER / FORGED DELEGATION")
    r = httpx.post(f"{BASE}/checkout_sessions",
                   json={"buyer_agent_id": "evil_agent", "delegation_token": DLG,
                         "line_items": [{"product_id": "prod_milk_amul",
                                         "pack_id": "pack_milk_amul_500", "quantity": 1}]},
                   timeout=15)
    print(r.json()["error"])

    step("ADVERSARIAL: SPEND LIMIT (50 mutton under restricted 500)")
    r = httpx.post(f"{BASE}/checkout_sessions",
                   json={"buyer_agent_id": "buyer_agent_llm_01",
                         "delegation_token": "dlg_restricted_50",
                         "line_items": [{"product_id": "prod_mutton_fresh",
                                         "pack_id": "pack_mutton_500", "quantity": 50}]},
                   timeout=15)
    d = r.json()
    print("error code:", d.get("error", {}).get("code"))
    print("session status:", d.get("checkout_session", {}).get("status"))

    step("ADVERSARIAL: CATEGORY BLOCK (tobacco)")
    r = httpx.post(f"{BASE}/checkout_sessions",
                   json={"buyer_agent_id": BUYER, "delegation_token": DLG,
                         "line_items": [{"product_id": "prod_paansupari_rajnigandha",
                                         "pack_id": "pack_supari_100", "quantity": 1}]},
                   timeout=15)
    print(r.json().get("error"))

    step("ADVERSARIAL: EXPIRED DELEGATION")
    r = httpx.post(f"{BASE}/checkout_sessions",
                   json={"buyer_agent_id": "buyer_agent_llm_01",
                         "delegation_token": "dlg_expired_001",
                         "line_items": [{"product_id": "prod_milk_amul",
                                         "pack_id": "pack_milk_amul_500", "quantity": 1}]},
                   timeout=15)
    print(r.json().get("error"))

    step("ADVERSARIAL: TOCTOU STOCK DISAPPEARS")
    r = httpx.post(f"{BASE}/checkout_sessions",
                   json={"buyer_agent_id": BUYER, "delegation_token": DLG,
                         "line_items": [{"product_id": "prod_chicken_fresh",
                                         "pack_id": "pack_chicken_500", "quantity": 1}]},
                   timeout=15)
    csid2 = r.json()["checkout_session"]["checkout_session_id"]
    httpx.patch(f"{BASE}/admin/packs/pack_chicken_500/stock",
                json={"actual_stock": 0}, headers={"X-Admin-Token": "admin_demo_token"}, timeout=15)
    r = httpx.post(f"{BASE}/checkout_sessions/{csid2}/complete",
                   json={"buyer_agent_id": BUYER, "delegation_token": DLG}, timeout=30)
    d = r.json()
    if "checkout_session" in d:
        print("session status:", d["checkout_session"]["status"])
    print("error:", d.get("error"))
    evs = httpx.get(f"{BASE}/audit_logs?checkout_session_id={csid2}").json()["events"]
    print("audit actions:", [e["action"] for e in evs])
    print("Razorpay NOT called?", "razorpay_order_created" not in [e["action"] for e in evs])


if __name__ == "__main__":
    main()
