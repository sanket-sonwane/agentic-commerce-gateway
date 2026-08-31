"""Dump the Buyer safeguard probe for human review + tabulate key metrics.

Reads artifacts/eval_buyer_probe_{GPT20,SAFEGUARD}.json and prints, per model and
per probe scenario, the reported wall/latency/tool info and the full user-facing
message text (the LLM-authored conversational surface), so a human reviewer can
judge: warmth/naturalness (A1), over-refusal / invented facts / forward recovery
(A2). Also prints a compact latency summary. Read-only.
"""
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"

MODELS = ["GPT20", "SAFEGUARD"]
LABELS = {"GPT20": "gpt-oss-20b", "SAFEGUARD": "gpt-oss-safeguard-20b"}


def load():
    data = {}
    for m in MODELS:
        f = ART / f"eval_buyer_probe_{m}.json"
        if f.exists():
            data[m] = json.loads(f.read_text(encoding="utf-8"))["models"][m]["scenarios"]
    return data


def dump(data):
    for m in MODELS:
        if m not in data:
            continue
        print(f"\n{'='*72}\n  {m}  ({LABELS[m]})\n{'='*72}")
        for scn, runs in data[m].items():
            print(f"\n--- {scn} ({len(runs)} runs) ---")
            for i, r in enumerate(runs):
                if "error" in r:
                    print(f"  #run{i}: ERROR {r['error']}")
                    continue
                msg = (r.get("msg") or "").strip() or "(no message — flow ended on event)"
                ev = ",".join(r.get("event_types", []))
                print(f"  #run{i} wall={r['wall_ms']:.0f}ms llmc={r['llm_calls']} tools={r['tool_calls']} disc={r['discover_calls']} notes={r.get('notes')}")
                print(f"      msg: {msg[:400]}")


def latency_summary(data):
    print(f"\n{'='*72}\n  LATENCY SUMMARY (wall_ms, p50/p90/mean)\n{'='*72}")
    for scn in ("warm_browse", "warm_ambig", "warm_health", "warm_compare"):
        print(f"\n{scn}:")
        for m in MODELS:
            if m not in data or scn not in data[m]:
                continue
            walls = sorted(r.get("wall_ms", 0) for r in data[m][scn] if "error" not in r)
            if not walls:
                continue
            p50 = walls[len(walls)//2]
            p90 = walls[int(len(walls)*0.9)-1]
            mean = statistics.mean(walls)
            print(f"   {LABELS[m]:<22} n={len(walls):2d} p50={p50:7.0f}  p90={p90:7.0f}  mean={mean:7.0f}")
        # llm-call count
        print("   llm_calls mean:")
        for m in MODELS:
            if m not in data or scn not in data[m]:
                continue
            lc = [r.get("llm_calls", 0) for r in data[m][scn] if "error" not in r]
            print(f"      {LABELS[m]:<22} {statistics.mean(lc):.2f}")


if __name__ == "__main__":
    d = load()
    dump(d)
    latency_summary(d)
