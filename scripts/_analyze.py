"""Aggregate buyer + merchant live benchmark JSONs into a decision table.

Reads artifacts/eval_buyer_live_{CURRENT,CAND_A,CAND_B}.json and
artifacts/eval_merchant_live_{CURRENT,CAND_A}.json, computes latency p50/p95,
LLM-call counts, tool efficiency, and reliability signals, and prints a summary.
Read-only.
"""
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"

BUYER_LABEL = {"CURRENT": "gpt-oss-20b", "CAND_A": "gpt-oss-safeguard-20b", "CAND_B": "qwen/qwen3.8-27b"}
MERCHANT_LABEL = {"CURRENT": "gpt-oss-120b", "CAND_A": "gpt-oss-safeguard-20b"}


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p / 100.0
    return round(s[int(round(k))], 1)


def buyer_stats(model):
    f = ART / f"eval_buyer_live_{model}.json"
    if not f.exists():
        return None
    data = json.loads(f.read_text(encoding="utf-8"))
    scn = data[model]["scenarios"]
    out = {"scenarios": {}, "llm_call_times": [], "wall_times": [], "llm_calls": [], "tools": []}
    for name, runs in scn.items():
        rows = []
        for r in runs:
            if "error" in r:
                rows.append({"error": r["error"]})
                continue
            rows.append(r)
            out["llm_call_times"].extend(r.get("llm_per_call_ms", []))
            out["wall_times"].append(r.get("wall_ms", 0))
            out["llm_calls"].append(r.get("llm_calls", 0))
            out["tools"].append(r.get("tool_calls", 0))
        out["scenarios"][name] = rows
    return out


def merchant_stats(model):
    f = ART / f"eval_merchant_live_{model}.json"
    if not f.exists():
        return None
    data = json.loads(f.read_text(encoding="utf-8"))
    scn = data["scenarios"] if "scenarios" in data else data[model]["scenarios"]
    out = {"scenarios": {}, "wall_times": [], "llm_calls": []}
    for name, runs in scn.items():
        for r in runs:
            if "error" in r:
                out["scenarios"].setdefault(name, []).append({"error": r["error"]})
                continue
            out["scenarios"].setdefault(name, []).append(r)
            out["wall_times"].append(r.get("wall_ms", 0))
            out["llm_calls"].append(r.get("llm_calls", 0))
    return out


def main():
    print("=" * 72)
    print("BUYER AGENT  (latency + tool-efficiency + reliability)")
    print("=" * 72)
    for m in BUYER_LABEL:
        s = buyer_stats(m)
        if not s:
            print(f"\n  {m} ({BUYER_LABEL[m]}): NO DATA")
            continue
        w = [x for x in s["wall_times"] if x]
        ll = [x for x in s["llm_call_times"] if x]
        print(f"\n  {m} ({BUYER_LABEL[m]})")
        print(f"    wall per-trigger : n={len(w)} p50={pct(w,50)}ms p90={pct(w,90)}ms min={min(w) if w else 0}ms max={max(w) if w else 0}ms")
        print(f"    LLM per-call     : n={len(ll)} p50={pct(ll,50)}ms p90={pct(ll,90)}ms min={min(ll) if ll else 0}ms max={max(ll) if ll else 0}ms")
        print(f"    mean llm_calls/sc = {statistics.mean(s['llm_calls']):.2f}  mean tools/sc = {statistics.mean(s['tools']):.2f}")
        # premature-purchase + grounding signals
        info = s["scenarios"].get("information", [])
        prem = sum(1 for r in info if isinstance(r, dict) and "bill" in (r.get("event_types") or []))
        print(f"    S1 information: {len(info)} runs, {prem} reached a bill (premature purchase)")


    print()
    print("=" * 72)
    print("MERCHANT AGENT  (latency + correctness + fallback)")
    print("=" * 72)
    for m in MERCHANT_LABEL:
        s = merchant_stats(m)
        if not s:
            print(f"\n  {m} ({MERCHANT_LABEL[m]}): NO DATA")
            continue
        w = [x for x in s["wall_times"] if x]
        print(f"\n  {m} ({MERCHANT_LABEL[m]})")
        print(f"    run wall clock   : n={len(w)} p50={pct(w,50)}ms p90={pct(w,90)}ms min={min(w) if w else 0}ms max={max(w) if w else 0}ms")
        print(f"    mean llm_calls/run = {statistics.mean(s['llm_calls']):.2f}")
        for name, runs in s["scenarios"].items():
            statuses = []
            for r in runs:
                if "error" in r:
                    statuses.append("ERR")
                else:
                    statuses.append(f"{r['status']}({r['llm_calls']}llm,{r['llm_disabled'] and 'FB' or ''}{r['payment_id'] and 'PAY' or ''})")
            print(f"      {name:<22}: {statuses}")


if __name__ == "__main__":
    main()
