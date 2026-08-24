"""Adversarial tests focused on bypass attempts."""
import httpx

BASE = "http://localhost:8000"
BUYER = "buyer_agent_llm_01"
DLG = "dlg_9f8e7d"

# Reset state
httpx.post(f"{BASE}/admin/demo/reset", headers={"X-Admin-Token": "admin_demo_token"}, timeout=15)


def step(title):
    print(f"\n===== {title} =====")


step("BYPASS ATTEMPT 1: Client tries to set status=completed via PUT update")
# Create a session
r = httpx.post(f"{BASE}/checkout_sessions",
               json={"buyer_agent_id": BUYER, "delegation_token": DLG,
                     "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
               timeout=15)
csid = r.json()["checkout_session"]["checkout_session_id"]
print("session created:", csid)
# Try to update with malicious status
r = httpx.put(f"{BASE}/checkout_sessions/{csid}",
              json={"buyer_agent_id": BUYER, "delegation_token": DLG,
                    "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}],
                    "status": "completed"},  # Try to bypass
              timeout=15)
print("status after PUT:", r.json()["checkout_session"]["status"])

step("BYPASS ATTEMPT 2: Update to exceed spend limit (rejected)")
# Create cheap session
r = httpx.post(f"{BASE}/checkout_sessions",
               json={"buyer_agent_id": BUYER, "delegation_token": "dlg_restricted_50",
                     "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
               timeout=15)
print("initial total:", r.json()["checkout_session"]["totals"]["total"])
csid = r.json()["checkout_session"]["checkout_session_id"]
# Try to update with saffron (over budget)
r = httpx.put(f"{BASE}/checkout_sessions/{csid}",
              json={"buyer_agent_id": BUYER, "delegation_token": "dlg_restricted_50",
                    "line_items": [{"product_id": "prod_saffron_premium", "pack_id": "pack_saffron_5g", "quantity": 1}]},
              timeout=15)
print("update rejected:", r.status_code, r.json().get("error", {}).get("code"))
# Original basket preserved
r = httpx.get(f"{BASE}/checkout_sessions/{csid}")
print("original basket preserved:", r.json()["checkout_session"]["line_items"][0]["pack_id"])

step("BYPASS ATTEMPT 3: Complete after cancelling")
r = httpx.post(f"{BASE}/checkout_sessions",
               json={"buyer_agent_id": BUYER, "delegation_token": DLG,
                     "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
               timeout=15)
csid = r.json()["checkout_session"]["checkout_session_id"]
r = httpx.post(f"{BASE}/checkout_sessions/{csid}/cancel",
               json={"buyer_agent_id": BUYER, "delegation_token": DLG}, timeout=15)
print("cancel status:", r.json()["checkout_session"]["status"])
r = httpx.post(f"{BASE}/checkout_sessions/{csid}/complete",
               json={"buyer_agent_id": BUYER, "delegation_token": DLG}, timeout=15)
print("complete after cancel:", r.status_code, r.json().get("error", {}).get("code"))

step("BYPASS ATTEMPT 4: Unauthenticated discovery")
r = httpx.post(f"{BASE}/discover",
               json={"query": "milk", "buyer_agent_id": ""}, timeout=15)
print("anonymous discovery:", r.status_code, "items:", len(r.json().get("basket_proposal", [])))
# Note: discovery does NOT require delegation - intentional design
print("discovery is intentionally public (delegation enforced at checkout)")

step("BYPASS ATTEMPT 5: Stock check bypassed via delete from cart")
# Create session, stock is reserved
r = httpx.post(f"{BASE}/checkout_sessions",
               json={"buyer_agent_id": BUYER, "delegation_token": DLG,
                     "line_items": [{"product_id": "prod_chicken_fresh", "pack_id": "pack_chicken_500", "quantity": 1}]},
               timeout=15)
csid = r.json()["checkout_session"]["checkout_session_id"]
# Cancel and try fresh: does the reservation release?
httpx.post(f"{BASE}/checkout_sessions/{csid}/cancel",
           json={"buyer_agent_id": BUYER, "delegation_token": DLG}, timeout=15)
# Now stock should be free again
r = httpx.post(f"{BASE}/checkout_sessions",
               json={"buyer_agent_id": "buyer_agent_b_deterministic", "delegation_token": "dlg_buyerb_001",
                     "line_items": [{"product_id": "prod_chicken_fresh", "pack_id": "pack_chicken_500", "quantity": 1}]},
               timeout=15)
print("stock released after cancel:", r.status_code, r.json().get("checkout_session", {}).get("status"))

step("BYPASS ATTEMPT 6: Unknown pack_id")
r = httpx.post(f"{BASE}/checkout_sessions",
               json={"buyer_agent_id": BUYER, "delegation_token": DLG,
                     "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_unknown", "quantity": 1}]},
               timeout=15)
print(r.json().get("error"))

step("BYPASS ATTEMPT 7: Unknown product_id")
r = httpx.post(f"{BASE}/checkout_sessions",
               json={"buyer_agent_id": BUYER, "delegation_token": DLG,
                     "line_items": [{"product_id": "prod_unknown", "pack_id": "pack_unknown", "quantity": 1}]},
               timeout=15)
print(r.json().get("error"))

step("BYPASS ATTEMPT 8: Tamper with already-stored payment status")
# Create + complete a session
r = httpx.post(f"{BASE}/checkout_sessions",
               json={"buyer_agent_id": BUYER, "delegation_token": DLG,
                     "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
               timeout=15)
csid = r.json()["checkout_session"]["checkout_session_id"]
r = httpx.post(f"{BASE}/checkout_sessions/{csid}/complete",
               json={"buyer_agent_id": BUYER, "delegation_token": DLG}, timeout=15)
print("payment_id:", r.json()["checkout_session"]["payment"].get("payment_id"))
# Can buyer submit a payment_callback with their own payment_id?
r = httpx.post(f"{BASE}/checkout_sessions/{csid}/payment_callback",
               json={"razorpay_order_id": "fake", "razorpay_payment_id": "fake", "razorpay_signature": "fake"},
               timeout=15)
print("callback with fake signature:", r.status_code, r.json().get("error", {}).get("code"))
# Verify chain still intact
r = httpx.get(f"{BASE}/audit_logs/verify", timeout=15)
print("chain after fake callback attempt:", r.json())

step("BYPASS ATTEMPT 9: Confirm with stale version")
r = httpx.post(f"{BASE}/checkout_sessions",
               json={"buyer_agent_id": BUYER, "delegation_token": DLG,
                     "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}]},
               timeout=15)
csid = r.json()["checkout_session"]["checkout_session_id"]
# Try to confirm version=999 of a non-confirmation session
r = httpx.post(f"{BASE}/checkout_sessions/{csid}/confirm",
               json={"buyer_agent_id": BUYER, "delegation_token": DLG, "accept_new_terms": True, "version": 999},
               timeout=15)
print("confirm on non-confirmation session:", r.status_code, r.json().get("error", {}).get("code"))
