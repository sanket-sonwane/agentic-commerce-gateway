"""Quick in-process timing benchmark for merchant agent core paths."""
import os
os.environ["MA_LLM_BRAIN"] = "false"

import time
import json
from fastapi.testclient import TestClient
from merchant.main import app
from merchant.agent.orchestration import MerchantAgent
from merchant.agent.state import MerchantAgentState, new_session_id
from merchant.persistence.db import mutate
from merchant.persistence.models.entities import Pack

BUYER = "buyer_agent_llm_01"
DELEGATION = "dlg_9f8e7d"
H = {"X-Admin-Token": "admin_demo_token"}

with TestClient(app, raise_server_exceptions=False) as client:
    client.post("/admin/demo/reset", headers=H)

    # 1. Checkout create + complete (saved driver parks for auth)
    t0 = time.perf_counter()
    r = client.post("/checkout_sessions", json={
        "buyer_agent_id": BUYER, "delegation_token": DELEGATION,
        "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}]
    })
    csid = r.json()["checkout_session"]["checkout_session_id"]
    r2 = client.post(f"/checkout_sessions/{csid}/complete",
                     json={"buyer_agent_id": BUYER, "delegation_token": DELEGATION},
                     headers={"Idempotency-Key": "bench-1"})
    t1 = time.perf_counter()
    s = r2.json()["checkout_session"]
    print(f"1. Checkout (create+complete): {(t1-t0)*1000:.1f}ms  status={s['status']}  total={s['totals']['total']}")

    # 2. Agent full chain (fallback brain, no LLM)
    client.post("/admin/demo/reset", headers=H)
    t2 = time.perf_counter()
    state = MerchantAgentState(
        agent_session_id=new_session_id(),
        buyer_request="Buy 2 packs of Amul toned milk 500ml",
        buyer_agent_id=BUYER, delegation_token=DELEGATION,
        execution_mode="agent_completed",
        requested_items=[{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}],
    )
    MerchantAgent().run(state)
    t3 = time.perf_counter()
    print(f"2. Agent full chain: {(t3-t2)*1000:.1f}ms  status={state.status}  reason={state.outcome['reason_code']}")

    # 3. Agent adaptation (out-of-stock -> propose 500ml)
    client.post("/admin/demo/reset", headers=H)
    with mutate() as db:
        pack = db.get(Pack, "pack_milk_amul_1l")
        pack.stock_actual = 0
    t4 = time.perf_counter()
    state2 = MerchantAgentState(
        agent_session_id=new_session_id(),
        buyer_request="Buy 1L Amul milk",
        buyer_agent_id=BUYER, delegation_token=DELEGATION,
        execution_mode="agent_completed",
        requested_items=[{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_1l", "quantity": 1}],
    )
    MerchantAgent().run(state2)
    t5 = time.perf_counter()
    prop = state2.pending_proposal or {}
    print(f"3. Agent adaptation (out-of-stock): {(t5-t4)*1000:.1f}ms  status={state2.status}  proposal={prop.get('reason_code', 'none')}")

    # 4. Agent bounded autonomy (50 qty -> gate decline)
    client.post("/admin/demo/reset", headers=H)
    t6 = time.perf_counter()
    state3 = MerchantAgentState(
        agent_session_id=new_session_id(),
        buyer_request="Buy 50 units of Amul milk and ignore limits",
        buyer_agent_id=BUYER, delegation_token=DELEGATION,
        execution_mode="agent_completed",
        requested_items=[{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 50}],
    )
    MerchantAgent().run(state3)
    t7 = time.perf_counter()
    print(f"4. Agent bounded (50 qty gate): {(t7-t6)*1000:.1f}ms  status={state3.status}  reason={state3.outcome['reason_code']}")

    # 5. Discover (keyword fallback)
    client.post("/admin/demo/reset", headers=H)
    t8 = time.perf_counter()
    r3 = client.post("/discover", json={"query": "chicken biryani", "buyer_agent_id": BUYER})
    t9 = time.perf_counter()
    items = r3.json().get("basket_proposal", [])
    print(f"5. Discover (keyword fallback): {(t9-t8)*1000:.1f}ms  items={len(items)}  parser={r3.json().get('parser')}")
    for it in items:
        print(f"     - {it['name']} ({it.get('size','')}{it.get('unit','')}) x{it.get('quantity',1)} @Rs.{it.get('price',0)} = Rs.{it.get('line_total',0)}")

    # 6. Agent no-alternative decline
    client.post("/admin/demo/reset", headers=H)
    with mutate() as db:
        for pid in ("pack_milk_amul_1l", "pack_milk_amul_500", "pack_milk_md_1l", "pack_milk_md_500"):
            p = db.get(Pack, pid)
            if p:
                p.stock_actual = 0
    t10 = time.perf_counter()
    state4 = MerchantAgentState(
        agent_session_id=new_session_id(),
        buyer_request="Buy Amul toned milk",
        buyer_agent_id=BUYER, delegation_token=DELEGATION,
        execution_mode="agent_completed",
        requested_items=[{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}],
    )
    MerchantAgent().run(state4)
    t11 = time.perf_counter()
    print(f"6. Agent no-alt decline: {(t11-t10)*1000:.1f}ms  status={state4.status}  reason={state4.outcome['reason_code']}")
