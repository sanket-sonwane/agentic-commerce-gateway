"""CONTROLLED LIVE LLM benchmark for the Buyer Agent (only the BUYER model is
varied). The merchant runs with a DETERMINISTIC brain (MA_LLM_BRAIN=false) and
the buyer's HTTP traffic is routed to an in-process merchant over ASGI, with
PAYMENT_DRIVER=sim so money is parked (never charged). This isolates the BUYER
model as the single variable (provider=Groq, identical prompts/tools/state).

Usage (repo root):
    python -m scripts._bench_live results.json

Writes per-run timing/calls/events to the given JSON for later analysis.
"""
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PAYMENT_DRIVER", "sim")
os.environ.setdefault("ACG_TEST_ALLOW_SIM", "1")
os.environ.setdefault("RAZORPAY_KEY_ID", "")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "")
os.environ.setdefault("MA_LLM_BRAIN", "false")

from fastapi.testclient import TestClient  # noqa: E402

from buyer_service.config import settings as buyer_settings  # noqa: E402
from buyer_service.conversations import get_conversation, new_conversation  # noqa: E402
from merchant.main import app as merchant_app  # noqa: E402
import buyer_service.main as buyer_main  # noqa: E402

from merchant.tests._buyer_harness import install, uninstall, drain_sse  # noqa: E402


# --------------------------------------------------------------------------- harness
def _reset_state():
    """Restore the merchant to a fresh seeded state between reps."""
    from scripts.seed_catalog import main as seed
    try:
        seed()
    except Exception:
        pass
    from merchant.persistence.db import mutate
    from merchant.persistence.models.entities import (Reservation, IdempotencyKey, CheckoutSession, AuditEvent, Delegation)
    with mutate() as db:
        for model in (Reservation, IdempotencyKey, CheckoutSession, AuditEvent):
            db.query(model).delete()
        from merchant.persistence.models.entities import Pack
        import json as _json
        catalog = _json.loads((ROOT / "merchant" / "seed" / "catalog.json").read_text(encoding="utf-8"))
        for prod in catalog["products"]:
            for pack in prod.get("packs", []):
                p = db.get(Pack, pack["pack_id"])
                if p is not None:
                    p.stock_reserved = 0
                    p.stock_actual = pack.get("stock", 0)


def _set_stock(pack_id, stock):
    from merchant.persistence.db import mutate
    from merchant.persistence.models.entities import Pack
    with mutate() as db:
        p = db.get(Pack, pack_id)
        if p is not None:
            p.stock_actual = stock
            p.stock_reserved = 0


_TOOLS_SEEN = []


def _run_chat(buy_client, message, conversation_id=None):
    """POST /chat, drain SSE, return (events, wall_ms)."""
    t0 = time.perf_counter()
    with buy_client.stream("POST", "/chat", json={"message": message, "conversation_id": conversation_id}) as r:
        events = drain_sse(r)
    wall_ms = (time.perf_counter() - t0) * 1000
    _TOOLS_SEEN.clear()
    for e in events:
        if e.get("type") == "tool":
            _TOOLS_SEEN.append(e.get("tool"))
    return events, wall_ms


def _conv_timings(conversation_id):
    conv = get_conversation(conversation_id)
    return (conv.timings if conv else []) or []


def _timing_stats(timings):
    llm_calls = [t for t in timings if t.get("stage") == "buyer_llm"]
    tool_entries = [t for t in timings if t.get("stage") == "buyer_tool"]
    disc = [t for t in timings if t.get("stage") == "discover_http"]
    return {
        "llm_calls": len(llm_calls),
        "llm_total_ms": round(sum(t["elapsed_ms"] for t in llm_calls), 2),
        "llm_per_call_ms": [round(t["elapsed_ms"], 2) for t in llm_calls],
        "tool_calls": len([e for e in _TOOLS_SEEN]),
        "tool_total_ms": round(sum(t["elapsed_ms"] for t in tool_entries), 2),
        "discover_calls": len(disc),
        "discover_total_ms": round(sum(t["elapsed_ms"] for t in disc), 2),
        "stages": [t.get("stage") for t in timings],
    }


def _event_types(events):
    return [e.get("type") for e in events if isinstance(e, dict)]


def _msg_text(events):
    return " | ".join((e.get("content") or "") for e in events if e.get("type") == "message")


def _ids(events):
    cid = None
    for e in events:
        if e.get("type") == "conversation_started":
            cid = e["conversation_id"]
    return cid


# ----------------------------------------------------------------------- scenarios
# Each scenario returns a summary dict: {ok, llm_calls, llm_ms, per, tools, wall_ms,
# events, msg, note}. Receives (buyer_app, merchant_testclient, new_conversation_id_on_first=True)

def _scenario_information(bc, mc, first=True):
    """S1: What milk do you have?"""
    ev, wall = _run_chat(bc, "What milk do you have?")
    ts = _timing_stats(_conv_timings(_ids(ev)))
    return _done("information", ev, wall, ts, _msg_text(ev))


def _scenario_buy_amul(bc, mc, first=True):
    """S2: Buy 1 litre Amul milk."""
    ev, wall = _run_chat(bc, "Buy 1 litre Amul milk.")
    cid = _ids(ev)
    ts = _timing_stats(_conv_timings(cid))
    notes = []
    if "bill" in _event_types(ev):
        notes.append("bill_reached")
    elif "substitute_proposal" in _event_types(ev):
        notes.append("substitute_at_buy")
    return _done("buy_amul", ev, wall, ts, _msg_text(ev), cid=cid, notes=notes)


def _scenario_multi(bc, mc, first=True):
    """S3: Buy milk, bread and eggs."""
    ev, wall = _run_chat(bc, "Buy milk, bread and eggs.")
    ts = _timing_stats(_conv_timings(_ids(ev)))
    return _done("multi", ev, wall, ts, _msg_text(ev))


def _scenario_budget(bc, mc, first=True):
    """S4: Buy groceries under 500 rupees."""
    ev, wall = _run_chat(bc, "Buy groceries under 500 rupees.")
    ts = _timing_stats(_conv_timings(_ids(ev)))
    return _done("budget", ev, wall, ts, _msg_text(ev))


def _scenario_stock_fail(bc, mc, first=True):
    """S5: Buy milk when stock unavailable (adaptation)."""
    _set_stock("pack_milk_amul_500", 0)
    _set_stock("pack_milk_amul_1l", 0)
    ev, wall = _run_chat(bc, "I'd like to buy Amul milk please.")
    ts = _timing_stats(_conv_timings(_ids(ev)))
    return _done("stock_fail", ev, wall, ts, _msg_text(ev))


def _scenario_reject_alternative(bc, mc, first=True):
    """S6: In a substitution flow, user rejects the alternative and picks Mother Dairy."""
    # First create a bill for Amul milk.
    ev, wall = _run_chat(bc, "Buy 1 litre Amul milk.")
    cid = _ids(ev)
    ts = _timing_stats(_conv_timings(cid))
    notes = []
    if "bill" in _event_types(ev):
        notes.append("bill_reached")
        # Create a stock race so completion offers a substitute.
        from merchant.persistence.db import mutate
        from merchant.persistence.models.entities import Pack
        with mutate() as db:
            p = db.get(Pack, "pack_milk_amul_500")
            if p is not None:
                p.stock_actual = 0
        with bc.stream("POST", f"/conversations/{cid}/decision", json={"approved": True}) as r:
            e2 = drain_sse(r)
        wall += 0
        if "substitute_proposal" in _event_types(e2):
            notes.append("substitute_proposed")
            # User rejects -> choose Mother Dairy
            with bc.stream("POST", f"/conversations/{cid}/choose", json={"choice": "other", "item": "Mother Dairy milk"}) as r:
                e3 = drain_sse(r)
            notes.append("rejected_choose_other")
        else:
            notes.append("no_substitute")
    return _done("reject_alternative", ev, wall, ts, _msg_text(ev) + " ||LATER|| " + _msg_text(e2 if 'e2' in dir() else []), cid=cid, notes=notes)


def _scenario_followup_cost(bc, mc, first=True):
    """S7: Follow-up cost asked after an order is placed."""
    ev, wall = _run_chat(bc, "Buy 2 Amul milk 500ml.")
    cid = _ids(ev)
    ts = _timing_stats(_conv_timings(cid))
    notes = []
    if "bill" in _event_types(ev):
        notes.append("bill_reached")
        with bc.stream("POST", f"/conversations/{cid}/decision", json={"approved": True}) as r:
            e2 = drain_sse(r)
        if "substitute_proposal" in _event_types(e2):
            with bc.stream("POST", f"/conversations/{cid}/choose", json={"choice": 0}) as r:
                e3 = drain_sse(r)
        ev3, wall3 = _run_chat(bc, "How much did that cost?", conversation_id=cid)
        ts = _timing_stats(_conv_timings(cid))
        return _done("followup_cost", ev3, wall3, ts, _msg_text(ev3), cid=cid, notes=["post_order"] + notes)
    return _done("followup_cost", ev, wall, ts, _msg_text(ev), cid=cid, notes=notes)


def _scenario_cancel(bc, mc, first=True):
    """S8: Cancel a pending order."""
    ev, wall = _run_chat(bc, "Buy 1 Amul milk 500ml.")
    cid = _ids(ev)
    ts = _timing_stats(_conv_timings(cid))
    notes = []
    if "bill" in _event_types(ev):
        notes.append("bill_reached")
        ev2, wall2 = _run_chat(bc, "Actually forget it, cancel my order.", conversation_id=cid)
        ts = _timing_stats(_conv_timings(cid))
        return _done("cancel", ev2, wall2, ts, _msg_text(ev2), cid=cid, notes=notes)
    return _done("cancel", ev, wall, ts, _msg_text(ev), cid=cid, notes=notes)


def _scenario_payment_status(bc, mc, first=True):
    """S9: Ask payment status after checkout created (no money moved)."""
    ev, wall = _run_chat(bc, "Buy 1 Amul milk 500ml.")
    cid = _ids(ev)
    ts = _timing_stats(_conv_timings(cid))
    notes = []
    if "bill" in _event_types(ev):
        notes.append("bill_reached")
        ev2, wall2 = _run_chat(bc, "Did my payment go through?", conversation_id=cid)
        ts = _timing_stats(_conv_timings(cid))
        return _done("payment_status", ev2, wall2, ts, _msg_text(ev2), cid=cid, notes=notes)
    return _done("payment_status", ev, wall, ts, _msg_text(ev), cid=cid, notes=notes)


def _scenario_completed_followup(bc, mc, first=True):
    """S10: 'How much did I pay' after completion (must ground in completed state)."""
    ev, wall = _run_chat(bc, "Buy 1 Amul milk 500ml.")
    cid = _ids(ev)
    ts = _timing_stats(_conv_timings(cid))
    notes = []
    if "bill" in _event_types(ev):
        notes.append("bill_reached")
        with bc.stream("POST", f"/conversations/{cid}/decision", json={"approved": True}) as r:
            e2 = drain_sse(r)
        if "substitute_proposal" in _event_types(e2):
            with bc.stream("POST", f"/conversations/{cid}/choose", json={"choice": 0}) as r:
                e3 = drain_sse(r)
        ev3, wall3 = _run_chat(bc, "How much did I pay?", conversation_id=cid)
        ts = _timing_stats(_conv_timings(cid))
        return _done("completed_followup", ev3, wall3, ts, _msg_text(ev3), cid=cid, notes=["post_order"] + notes)
    return _done("completed_followup", ev, wall, ts, _msg_text(ev), cid=cid, notes=notes)


def _scenario_unrelated(bc, mc, first=True):
    """S11: Unrelated question during an active checkout."""
    ev, wall = _run_chat(bc, "Buy 1 Amul milk.")
    cid = _ids(ev)
    ts = _timing_stats(_conv_timings(cid))
    notes = []
    if "bill" in _event_types(ev):
        notes.append("bill_reached")
        with bc.stream("POST", f"/conversations/{cid}/decision", json={"approved": False}) as r:
            e2 = drain_sse(r)
        ev2, wall2 = _run_chat(bc, "What's the weather like today?", conversation_id=cid)
        ts = _timing_stats(_conv_timings(cid))
        tools2 = [e for e in ev2 if e.get("type") == "tool"]
        notes.append("tools_called=" + str(len(tools2)))
        return _done("unrelated", ev2, wall2, ts, _msg_text(ev2), cid=cid, notes=notes)
    return _done("unrelated", ev, wall, ts, _msg_text(ev), cid=cid, notes=notes)


def _scenario_ambiguous(bc, mc, first=True):
    """S12: Ambiguous shopping request -> clarify, don't buy prematurely."""
    ev, wall = _run_chat(bc, "I want something for dinner tonight")
    ts = _timing_stats(_conv_timings(_ids(ev)))
    return _done("ambiguous", ev, wall, ts, _msg_text(ev))


def _done(name, events, wall_ms, ts, msg, cid=None, notes=None):
    return {
        "scenario": name,
        "wall_ms": round(wall_ms, 2),
        "event_types": _event_types(events),
        "msg": msg,
        "notes": notes or [],
        **ts,
    }


SCENARIOS = [
    _scenario_information,       # S1
    _scenario_buy_amul,          # S2
    _scenario_multi,             # S3
    _scenario_budget,            # S4
    _scenario_stock_fail,        # S5
    _scenario_reject_alternative,  # S6
    _scenario_followup_cost,     # S7
    _scenario_cancel,            # S8
    _scenario_payment_status,    # S9
    _scenario_completed_followup,  # S10
    _scenario_unrelated,         # S11
    _scenario_ambiguous,         # S12
]

BUYER_MODELS = {
    "CURRENT": "openai/gpt-oss-20b",
    "CAND_A": "openai/gpt-oss-safeguard-20b",
    "CAND_B": "qwen/qwen3.8-27b",
}

# Token-heavy multi-turn scenarios get fewer reps to cap spend; others get reps.
CREDIT_SCENARIOS = ["budget", "followup_cost", "completed_followup", "unrelated", "ambiguous"]


def _reps_for(scenario, reps):
    return min(reps, 2) if scenario in CREDIT_SCENARIOS else reps


def run_candidate(model_label, model, reps, out_by_candidate, buy_client, mc, only=None, per_model_path=None):
    print(f"\n##### Model {model_label} -> {model}", flush=True)
    buyer_settings.buyer_llm_model = model
    results = {}
    sel = SCENARIOS if not only else [s for s in SCENARIOS if s.__name__.replace("_scenario_", "") in only]
    for scn in sel:
        name = scn.__name__.replace("_scenario_", "")
        n = _reps_for(name, reps)
        runs = []
        for rep in range(1, n + 1):
            _reset_state()
            try:
                res = scn(buy_client, mc)
                runs.append(res)
                print(f"  {name:<20} [{rep}/{n}] wall={res['wall_ms']:7.1f}ms llmc={res['llm_calls']} tools={res['tool_calls']} disc={res['discover_calls']} note={res['notes']}", flush=True)
            except Exception as exc:  # noqa: BLE001
                runs.append({"scenario": name, "error": str(exc), "wall_ms": 0})
                print(f"  {name:<20} [{rep}/{n}] ERROR {exc}", flush=True)
        results[name] = runs
        if per_model_path:  # persist partial as scenarios complete (abort-safe)
            try:
                per_model_path.write_text(json.dumps({model_label: {"model": model, "scenarios": results}}, indent=2), encoding="utf-8")
            except Exception:
                pass
    out_by_candidate[model_label] = {"model": model, "scenarios": results}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="artifacts/eval_buyer_live.json")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--models", default="CURRENT,CAND_A,CAND_B")
    ap.add_argument("--only-scenario", default=None, help="run one scenario name (comma list) for a quick smoke")
    args = ap.parse_args()

    from pathlib import Path as P
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    mc = TestClient(merchant_app)
    install(mc)
    with TestClient(buyer_main.app) as buy_client:
        assert buyer_settings.llm_api_key, "no LLM_API_KEY in .env"
        labels = [s.strip() for s in args.models.split(",") if s.strip()]
        meta = {"provider": "groq", "base_url": buyer_settings.llm_base_url,
                "temperature": 0.2, "max_tokens": 700, "timeout": 60,
                "merchant_brain": "deterministic(fixed)",
                "credit_scenarios": CREDIT_SCENARIOS, "reps": args.reps}
        for label in labels:
            out = dict(meta, **{"candidates": {}})
            only = [s.strip() for s in args.only_scenario.split(",")] if args.only_scenario else None
            per_model = ROOT / args.out.replace(".json", f"_{label}.json")
            run_candidate(label, BUYER_MODELS[label], args.reps, out["candidates"], buy_client, mc, only=only, per_model_path=per_model)
            # Save partial output after each candidate so an abort keeps prior work.
            out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
            print("  saved", out_path, "and", per_model, flush=True)
    print("\nWrote", out_path)


if __name__ == "__main__":
    main()
