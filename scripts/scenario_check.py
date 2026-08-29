"""Quick end-to-end scenario smoke test (dev helper, not pytest)."""
from fastapi.testclient import TestClient

from merchant.main import app

H = {"X-Admin-Token": "admin_demo_token"}

with TestClient(app, raise_server_exceptions=False) as client:
    # Clean slate: restore catalog stock, clear transactions.
    r = client.post("/admin/demo/reset", headers=H)
    assert r.status_code == 200, r.text

    # 1. Adversarial spend-limit: 50 units of mutton under restricted delegation
    r = client.post(
        "/checkout_sessions",
        json={
            "buyer_agent_id": "buyer_agent_llm_01",
            "delegation_token": "dlg_restricted_50",
            "line_items": [{"product_id": "prod_mutton_fresh", "pack_id": "pack_mutton_500", "quantity": 50}],
        },
    )
    e = r.json()
    print("1. spend-limit:", r.status_code, e["error"]["code"], "| session:", e.get("checkout_session", {}).get("status"))

    # 2. Category blocked (tobacco)
    r = client.post(
        "/checkout_sessions",
        json={
            "buyer_agent_id": "buyer_agent_llm_01",
            "delegation_token": "dlg_9f8e7d",
            "line_items": [{"product_id": "prod_paansupari_rajnigandha", "pack_id": "pack_supari_100", "quantity": 1}],
        },
    )
    print("2. category-block:", r.status_code, r.json()["error"]["code"])

    # 3. Expired delegation
    r = client.post(
        "/checkout_sessions",
        json={
            "buyer_agent_id": "buyer_agent_llm_01",
            "delegation_token": "dlg_expired_001",
            "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}],
        },
    )
    print("3. expired-delegation:", r.status_code, r.json()["error"]["code"])

    # 4. TOCTOU stock failure
    r = client.post(
        "/checkout_sessions",
        json={
            "buyer_agent_id": "buyer_agent_llm_01",
            "delegation_token": "dlg_9f8e7d",
            "line_items": [{"product_id": "prod_chicken_fresh", "pack_id": "pack_chicken_500", "quantity": 1}],
        },
    )
    cs = r.json()["checkout_session"]
    cs_id = cs["checkout_session_id"]
    print("4a. checkout:", cs["status"], cs["totals"]["total"])
    r = client.patch("/admin/packs/pack_chicken_500/stock", json={"actual_stock": 0}, headers=H)
    print("4b. operator zeroes stock:", r.status_code, r.json())
    r = client.post(f"/checkout_sessions/{cs_id}/complete",
                    json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"})
    e = r.json()
    print("4c. complete:", r.status_code, e["error"]["code"], "| session:", e["checkout_session"]["status"])
    r = client.get(f"/audit_logs?checkout_session_id={cs_id}")
    actions = [ev["action"] for ev in r.json()["events"]]
    print("4d. razorpay called?", any("razorpay" in a for a in actions), "| actions:", actions)

    # 5. Buyer adapts: declined is terminal (PRD §14), so it creates a NEW
    #    checkout with the alternative protein (chicken -> paneer) -> completes.
    r = client.post(
        "/checkout_sessions",
        json={
            "buyer_agent_id": "buyer_agent_llm_01",
            "delegation_token": "dlg_9f8e7d",
            "line_items": [{"product_id": "prod_paneer_amul", "pack_id": "pack_paneer_200", "quantity": 2}],
        },
    )
    cs2 = r.json()["checkout_session"]
    print("5a. adapt-new-session:", r.status_code, cs2["status"], cs2["totals"]["total"])
    r = client.post(f"/checkout_sessions/{cs2['checkout_session_id']}/complete",
                    json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"})
    print("5b. complete-after-adapt:", r.status_code, r.json()["checkout_session"]["status"])

    # 6. Terminal state protection: complete again without key
    r = client.post(f"/checkout_sessions/{cs_id}/complete",
                    json={"buyer_agent_id": "buyer_agent_llm_01", "delegation_token": "dlg_9f8e7d"})
    print("6. terminal-protect:", r.status_code,
          r.json()["error"]["code"] if r.status_code != 200 else r.json()["checkout_session"]["status"])
