"""Buyer-service flow test with a scripted fake LLM (no NIM key required).

Validates: tool-loop -> bill pause -> approval -> payment -> completed,
and the cancel path. Merchant must be running on :8000.
"""
import json
import sys
from types import SimpleNamespace as F

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi.testclient import TestClient

import buyer_service.agent as agent_mod
from buyer_service.main import app


class FakeCompletions:
    def __init__(self, script):
        self.script = script  # shared list — pop() must exhaust across calls

    def create(self, **kwargs):
        if not self.script:
            raise RuntimeError("fake script exhausted")
        return self.script.pop(0)


class FakeChat:
    def __init__(self, script):
        self.completions = FakeCompletions(script)


class FakeOpenAI:
    def __init__(self, script):
        self.chat = FakeChat(script)


def msg_content(content):
    return F(choices=[F(message=F(content=content, tool_calls=None))])


def msg_toolcall(name, args, call_id="call_1"):
    tc = F(id=call_id, function=F(name=name, arguments=json.dumps(args)))
    return F(choices=[F(message=F(content=None, tool_calls=[tc]))])


def drain_sse(response):
    events = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            payload = line[len("data: ") :].strip()
            if payload and payload != "{}":
                events.append(json.loads(payload))
    return events


def main():
    import httpx as _hx

    # Clean merchant state (catalog stock restored, transactions cleared).
    try:
        _hx.post(
            "http://localhost:8000/admin/demo/reset",
            headers={"X-Admin-Token": "admin_demo_token"},
            timeout=30,
        )
    except Exception:
        pass

    script = [
        msg_toolcall("discover_products", {"query": "milk"}, "c1"),
        msg_toolcall(
            "create_checkout_session",
            {"items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}]},
            "c2",
        ),
    ]
    orig_client = agent_mod._client
    agent_mod._client = lambda: FakeOpenAI(script)

    try:
        with TestClient(app) as client:
            # ---- Turn 1: goal -> discovery -> checkout -> bill pause
            with client.stream("POST", "/chat", json={"message": "Buy 2 milk"}) as r:
                events = drain_sse(r)
            kinds = [e["type"] for e in events]
            print("turn1 events:", kinds)
            assert "bill" in kinds, events
            conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation_started")
            bill = next(e for e in events if e["type"] == "bill")
            print("bill total:", bill["totals"]["total"], "| session:", bill["checkout_session_id"])

            # chat while pending must 409
            r = client.post("/chat", json={"message": "hello", "conversation_id": conv_id})
            print("chat-while-pending:", r.status_code)

            # ---- Approve -> payment completes
            with client.stream("POST", f"/conversations/{conv_id}/decision", json={"approved": True}) as r:
                events2 = drain_sse(r)
            kinds2 = [e["type"] for e in events2]
            print("turn2 events:", kinds2)
            final = next(e for e in events2 if e["type"] == "status")
            print("final status:", final["status"])
            assert final["status"] == "completed"
            last = events2[-1]["content"]
            print("final message:", last[:80])

            # ---- Cancel path on a fresh conversation
            script.append(msg_toolcall(
                "create_checkout_session",
                {"items": [{"product_id": "prod_bread_britannia", "pack_id": "pack_bread_britannia_400", "quantity": 1}]},
                "d1",
            ))
            with client.stream("POST", "/chat", json={"message": "one bread please"}) as r:
                events3 = drain_sse(r)
            print("act2 events:", [(e.get("type"), str(e.get("content") or e.get("summary") or "")[:60]) for e in events3])
            assert any(e["type"] == "bill" for e in events3), events3
            conv3 = next(e["conversation_id"] for e in events3 if e["type"] == "conversation_started")
            with client.stream("POST", f"/conversations/{conv3}/decision", json={"approved": False}) as r:
                events4 = drain_sse(r)
            st = next(e for e in events4 if e["type"] == "status")
            print("cancel path status:", st["status"])
            assert st["status"] == "cancelled"

            # ---- Act 3: stock failure -> substitute proposal -> user chooses -> pays
            import httpx

            script.append(msg_toolcall("discover_products", {"query": "fresh chicken breast"}, "f1"))
            script.append(msg_toolcall(
                "create_checkout_session",
                {"items": [{"product_id": "prod_chicken_fresh", "pack_id": "pack_chicken_500", "quantity": 1}]},
                "f2",
            ))
            with client.stream("POST", "/chat", json={"message": "order fresh chicken breast"}) as r:
                events5 = drain_sse(r)
            assert any(e["type"] == "bill" for e in events5), events5
            conv5 = next(e["conversation_id"] for e in events5 if e["type"] == "conversation_started")
            bill5 = next(e for e in events5 if e["type"] == "bill")
            csid5 = bill5["checkout_session_id"]
            print("act3 bill:", csid5)

            # T2: reality changes behind the reservation
            pr = httpx.patch(
                "http://localhost:8000/admin/packs/pack_chicken_500/stock",
                json={"actual_stock": 0},
                headers={"X-Admin-Token": "admin_demo_token"},
                timeout=15,
            )
            assert pr.status_code == 200

            # T3/T4: approve -> decline -> deterministic substitute proposal
            with client.stream("POST", f"/conversations/{conv5}/decision", json={"approved": True}) as r:
                events6 = drain_sse(r)
            kinds6 = [e["type"] for e in events6]
            print("act3 decline events:", kinds6)
            assert "substitute_proposal" in kinds6, events6
            prop = next(e for e in events6 if e["type"] == "substitute_proposal")
            assert prop["options"], "no substitutes offered"
            print("act3 options:", [(o['name'], o['price']) for o in prop['options']])

            # chat must be blocked while substitution pending
            r = client.post("/chat", json={"message": "hello", "conversation_id": conv5})
            print("chat-while-choosing:", r.status_code)

            # T7: user picks option 0 -> new bill (kept items + substitute)
            with client.stream("POST", f"/conversations/{conv5}/choose", json={"choice": 0}) as r:
                events7 = drain_sse(r)
            kinds7 = [e["type"] for e in events7]
            assert "bill" in kinds7, events7
            bill7 = next(e for e in events7 if e["type"] == "bill")
            print("act3 new bill:", bill7["checkout_session_id"], [(li['name'], li['quantity']) for li in bill7['line_items']])
            swapped_names = [li["name"] for li in bill7["line_items"]]
            assert "Fresh Chicken Breast" not in swapped_names

            # T8: approve the rebuilt basket -> completed
            with client.stream("POST", f"/conversations/{conv5}/decision", json={"approved": True}) as r:
                events8 = drain_sse(r)
            final = next(e for e in events8 if e["type"] == "status")
            print("act3 final:", final["status"])
            assert final["status"] == "completed"

            print("BUYER SERVICE FLOW OK")
    finally:
        agent_mod.BuyerAgent._client = orig_client


if __name__ == "__main__":
    main()
