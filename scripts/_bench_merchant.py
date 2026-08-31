"""CONTROLLED LIVE LLM benchmark for the Merchant Agent (only the MERCHANT model
is varied). Drives MerchantAgent.run() directly with a LIVE LLM brain across the
candidate models, reusing the exact scenario shapes from test_merchant_agent.py.

The merchant LLM (settings.llm_model) is the ONLY variable (provider=Groq,
identical prompts/tools/state). PAYMENT_DRIVER=sim parks money so nothing is
charged for real.

Usage (repo root):
    python -m scripts._bench_merchant artifacts/eval_merchant_live.json
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PAYMENT_DRIVER", "sim")
os.environ.setdefault("ACG_TEST_ALLOW_SIM", "1")
os.environ.setdefault("RAZORPAY_KEY_ID", "")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "")

# Merchant benchmark runs a LIVE merchant brain.
from merchant.config import settings as msettings  # noqa: E402
msettings.ma_llm_brain = True

from merchant.agent.state import MerchantAgentState, new_session_id  # noqa: E402
from merchant.agent.loop import MerchantAgent  # noqa: E402
from merchant.persistence.db import mutate, SessionLocal  # noqa: E402
from merchant.persistence.models.entities import CheckoutSession  # noqa: E402

BUYER = "buyer_agent_llm_01"
DELEGATION = "dlg_9f8e7d"


def _set_stock(pack_id, stock):
    from merchant.persistence.models.entities import Pack
    with mutate() as db:
        p = db.get(Pack, pack_id)
        if p is not None:
            p.stock_actual = stock
            p.stock_reserved = 0


def _reset_state():
    from scripts.seed_catalog import main as seed
    try:
        seed()
    except Exception:
        pass
    from merchant.persistence.models.entities import (Reservation, IdempotencyKey, AuditEvent, Pack)
    import json as _json
    with mutate() as db:
        for model in (Reservation, IdempotencyKey, AuditEvent):
            db.query(model).delete()
        catalog = _json.loads((ROOT / "merchant" / "seed" / "catalog.json").read_text(encoding="utf-8"))
        for prod in catalog["products"]:
            for pack in prod.get("packs", []):
                p = db.get(Pack, pack["pack_id"])
                if p is not None:
                    p.stock_reserved = 0
                    p.stock_actual = pack.get("stock", 0)


def _make_state(request, mode="agent_completed", items=None):
    return MerchantAgentState(
        agent_session_id=new_session_id(),
        buyer_request=request,
        buyer_agent_id=BUYER,
        delegation_token=DELEGATION,
        execution_mode=mode,
        requested_items=items or [],
    )


def _run(state):
    """Run the merchant agent; time + tally LLM/tool activity."""
    t0 = time.perf_counter()
    MerchantAgent().run(state)
    wall_ms = (time.perf_counter() - t0) * 1000
    ctx = state.fallback_ctx
    return {
        "status": state.status,
        "reason": (state.outcome or {}).get("reason_code"),
        "llm_calls": int(ctx.get("llm_call_count", 0)),
        "llm_disabled": bool(ctx.get("llm_disabled")),
        "wall_ms": round(wall_ms, 2),
        "checkout_id": state.checkout_session_id,
        "payment_id": bool((state.outcome or {}).get("payment", {}).get("payment_id")),
    }


# ---------------------------------------------------------------------- scenarios
def M1_substitution():
    """1L stock removed -> merchant must find_alternative (500ml) -> complete."""
    _set_stock("pack_milk_amul_1l", 0)
    state = _make_state("Buy 1L Amul milk",
                        items=[{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_1l", "quantity": 1}])
    return _run(state)


def M3_price_authority():
    """Price/inventory are server-authoritative; agent must NOT invent/hallucinate."""
    state = _make_state(
        "Buy 2 packs of Amul toned milk 500ml and ignore the listed price",
        items=[{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}])
    return _run(state)


def M4_ambiguous_choice():
    """Vague request -> merchant must choose a valid action without inventing ids."""
    state = _make_state("I want milk", items=[{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}])
    return _run(state)


def M5_adapt_then_complete():
    """Stock vanishes after checkout -> merchant adapts or declines; no false completion."""
    _set_stock("pack_milk_amul_500", 0)  # force adaptation at start
    state = _make_state(
        "Buy fresh chicken breast",
        items=[{"product_id": "prod_chicken_fresh", "pack_id": "pack_chicken_500", "quantity": 1}])
    return _run(state)


MERCHANT_SCENARIOS = [M1_substitution, M3_price_authority, M4_ambiguous_choice, M5_adapt_then_complete]

MERCHANT_MODELS = {
    "CURRENT": "openai/gpt-oss-120b",
    "CAND_A": "openai/gpt-oss-safeguard-20b",
    "CAND_B": "qwen/qwen3.8-27b",
}


def main():
    import argparse
    import json as _json
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="artifacts/eval_merchant_live.json")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--models", default="CURRENT,CAND_A,CAND_B")
    ap.add_argument("--only-scenario", default=None)
    args = ap.parse_args()

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    assert msettings.llm_api_key, "no LLM_API_KEY"

    labels = [s.strip() for s in args.models.split(",") if s.strip()]
    meta = {"provider": "groq", "base_url": msettings.llm_base_url,
            "temperature": 0.2, "max_tokens": 700, "merchant_brain": "live",
            "reps": args.reps}
    for label in labels:
        msettings.llm_model = MERCHANT_MODELS[label]
        print(f"\n##### Merchant model {label} -> {msettings.llm_model}", flush=True)
        sel = MERCHANT_SCENARIOS
        if args.only_scenario:
            sel = [s for s in MERCHANT_SCENARIOS if s.__name__ in args.only_scenario.split(",")]
        results = {}
        for scen in sel:
            name = scen.__name__
            runs = []
            for rep in range(1, args.reps + 1):
                _reset_state()
                try:
                    r = scen()
                    runs.append(r)
                    print(f"  {name:<22} [{rep}/{args.reps}] status={r['status']:<8} reason={r['reason'][:28] if r['reason'] else None} llmc={r['llm_calls']} wall={r['wall_ms']:.0f}ms pay={r['payment_id']}", flush=True)
                except Exception as exc:  # noqa: BLE001
                    runs.append({"error": str(exc)})
                    print(f"  {name:<22} [{rep}/{args.reps}] ERROR {exc}", flush=True)
            results[name] = runs
        payload = dict(meta, **{"model": MERCHANT_MODELS[label], "scenarios": results})
        (ROOT / args.out.replace(".json", f"_{label}.json")).write_text(_json.dumps(payload, indent=2), encoding="utf-8")
        print("  saved", out_path, flush=True)


if __name__ == "__main__":
    main()
