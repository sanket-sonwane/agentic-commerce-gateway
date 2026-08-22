"""Simulate the PRD §34 T2 step: an external event zeroes out stock.

Zeroes the given pack (default: the low-stock chicken used in the demo) while a
checkout session is holding a reservation — completion must then decline with
STOCK_UNAVAILABLE and Razorpay must never be called.

Usage:
    python -m scripts.simulate_stock_failure [pack_id] [new_stock]
"""
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "http://localhost:8000"
ADMIN_TOKEN = "admin_demo_token"

DEFAULT_PACK = "pack_chicken_500"  # Fresh Chicken Breast 500g — seeded with stock=1


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pack_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PACK
    new_stock = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    r = httpx.patch(
        f"{BASE}/admin/packs/{pack_id}/stock",
        json={"actual_stock": new_stock},
        headers={"X-Admin-Token": ADMIN_TOKEN},
        timeout=15,
    )
    if r.status_code == 200:
        print(f"[demo] operator action: {pack_id} actual stock -> {new_stock}")
        print("[demo] any active reservation for it is now invalid under final validation.")
    else:
        print(f"[demo] FAILED ({r.status_code}): {r.text}")
        sys.exit(1)


if __name__ == "__main__":
    main()
