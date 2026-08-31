"""Probe: how long does /discover really take, and does the LLM parse fire?"""
import os, time
os.environ.setdefault("PAYMENT_DRIVER", "sim")
os.environ.setdefault("ACG_TEST_ALLOW_SIM", "1")
os.environ.setdefault("MA_LLM_BRAIN", "false")
from fastapi.testclient import TestClient
from merchant.main import app

c = TestClient(app)
for query in ["milk", "milk bread eggs", "fresh chicken breast"]:
    t0 = time.perf_counter()
    r = c.post("/discover", json={"query": query, "buyer_agent_id": "buyer_agent_llm_01"})
    dt = (time.perf_counter() - t0) * 1000
    j = r.json()
    meta = j.get("_meta", {}) or {}
    print(f"/discover status={r.status_code} elapsed={dt:.0f}ms parser={meta.get('parsed_by')} items={len(j.get('basket_proposal', []))}")

# Skip_llm_parse comparison
for query in ["milk", "milk bread eggs"]:
    t0 = time.perf_counter()
    r = c.post("/discover", json={"query": query, "buyer_agent_id": "buyer_agent_llm_01", "skip_llm_parse": True})
    dt = (time.perf_counter() - t0) * 1000
    j = r.json()
    meta = j.get("_meta", {}) or {}
    print(f"/discover[skip] status={r.status_code} elapsed={dt:.0f}ms parser={meta.get('parsed_by')} items={len(j.get('basket_proposal', []))}")
