"""Probe: find a prompt that makes the buyer agent emit an order-ready bill."""
import json
import sys
import urllib.request

BASE = "http://localhost:8001"


def probe(prompt: str) -> None:
    body = json.dumps({"message": prompt}).encode()
    req = urllib.request.Request(
        BASE + "/chat", data=body, headers={"Content-Type": "application/json"}
    )
    events = []
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                events.append(ev)
                et = ev.get("type")
                if et in ("tool", "conversation_started", "user_message"):
                    print(f"  event {et}: {json.dumps(ev, default=str)[:160]}")
                elif et == "order-ready":
                    print(f"  event order-ready: session={ev.get('checkout_session_id')} total={ev.get('totals', {}).get('total')}")
                    print("  >>> ORDER-READY PRESENTED <<<")
                    print(json.dumps(ev, default=str)[:600])
                    return
                elif et == "bill":
                    print(f"  event bill: {json.dumps(ev, default=str)[:200]}")
                elif et == "assistant":
                    print(f"  event assistant: {str(ev.get('content'))[:200]}")
                elif et == "error":
                    print(f"  event ERROR: {json.dumps(ev, default=str)[:300]}")
                    return
                else:
                    print(f"  event {et}: {json.dumps(ev, default=str)[:200]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  probe failed: {exc}")
    print(f"  (tip: got {len(events)} events, no order-ready)")


if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Buy milk, bread and eggs for under 300"
    probe(prompt)