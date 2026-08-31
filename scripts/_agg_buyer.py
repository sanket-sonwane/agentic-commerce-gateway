"""Merge per-model buyer benchmark files + print a human-review dump of messages.

Builds a single master JSON and prints per-model/per-scenario message texts so a
human reviewer can score conversational quality. Read-only.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"

MODELS = ["CURRENT", "CAND_A", "CAND_B"]
LABELS = {
    "CURRENT": "gpt-oss-20b",
    "CAND_A": "gpt-oss-safeguard-20b",
    "CAND_B": "qwen/qwen3.8-27b",
}


def load():
    master = {}
    for m in MODELS:
        f = ART / f"eval_buyer_live_{m}.json"
        if not f.exists():
            print(f"  (missing {f.name})")
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        master[m] = data[m]["scenarios"]
    (ART / "eval_buyer_master.json").write_text(json.dumps(master, indent=2), encoding="utf-8")
    return master


def dump(master):
    for m in MODELS:
        if m not in master:
            continue
        print(f"\n{'='*70}\n  {m}  ({LABELS[m]})\n{'='*70}")
        for scn, runs in master[m].items():
            print(f"\n--- {scn} ({len(runs)} runs) ---")
            for i, r in enumerate(runs):
                if "error" in r:
                    print(f"  #run{i}: ERROR {r['error']}")
                    continue
                msg = (r.get("msg") or "").strip() or "(no message — flow ended on event)"
                ev = ",".join(r.get("event_types", []))
                print(f"  #run{i} wall={r['wall_ms']:.0f}ms llmc={r['llm_calls']} tools={r['tool_calls']} disc={r['discover_calls']} notes={r.get('notes')}")
                print(f"      events: {ev}")
                print(f"      msg: {msg[:300]}")


if __name__ == "__main__":
    m = load()
    dump(m)
