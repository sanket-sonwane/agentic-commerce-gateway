"""Live Razorpay reachability probe (Workstream C).

Answers, from the REAL test-mode provider:
    1. can the API client initialize?
    2. do the configured credentials authenticate (order.list)?
    3. can a fresh test-mode order be created, and does the provider echo the
       requested amount?

Never prints secrets, never charges a card, never touches live mode. Writes a
categorized JSON summary to artifacts/.

Usage:
    .venv\\Scripts\\python.exe -m scripts._probe_razorpay
"""
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from merchant.config import settings
from merchant.payment import payment_service

OUT = Path(__file__).resolve().parents[1] / "artifacts" / "razorpay_probe.json"


def _result(category, detail, extra=None):
    return {
        "probed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "category": category,
        "detail": detail,
        "driver": settings.payment_driver,
        "credentials_present": payment_service.has_credentials(),
        "key_id_prefix": (settings.razorpay_key_id or "")[:9],
        **(extra or {}),
    }


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    result = None

    if not payment_service.has_credentials():
        result = _result("NO_CREDENTIALS", "RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET missing or empty")
        print(json.dumps(result, indent=2))
        OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 2

    # 1 + 2: initialize + authenticate (order.list is a cheap authenticated call).
    try:
        client = payment_service._get_client()
        listing = client.order.all({"count": 1})
        listing_ok = isinstance(listing, dict) and (listing.get("items") is not None)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        low = text.lower()
        if "authentication" in low or "unauthorized" in low or "401" in low or "not authorized" in low:
            cat = "AUTH_FAILED"
            detail = f"provider rejected credentials: {text[:200]}"
        elif "timeout" in low or "connect" in low or "name or service" in low or "network" in low:
            cat = "NETWORK_UNREACHABLE"
            detail = f"could not reach api.razorpay.com: {text[:200]}"
        else:
            cat = "PROVIDER_ERROR"
            detail = f"{type(exc).__name__}: {text[:300]}"
        result = _result(cat, detail, {"phase": "authenticate"})
        print(json.dumps(result, indent=2), file=sys.stderr)
        OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 1

    # 3: create one genuine low-value test-mode order (never captured/charged).
    try:
        order = client.order.create(
            {
                "amount": 100,  # ₹1 — probe only
                "currency": "INR",
                "receipt": f"acg-probe-{int(time.time())}",
                "payment_capture": 1,
                "notes": {"purpose": "acg-connectivity-probe"},
            }
        )
        echo_amount = int(order.get("amount", -1))
        amount_ok = echo_amount == 100
        result = _result(
            "REACHABLE" if amount_ok else "AMOUNT_MISMATCH",
            (
                f"authenticated OK; test-mode order {order.get('id')} created, "
                f"echo amount {echo_amount}/100 paise"
                if amount_ok
                else f"order created but echoed amount {echo_amount} != 100"
            ),
            {
                "phase": "order_create",
                "order_id": order.get("id"),
                "echo_amount_paise": echo_amount,
                "currency": order.get("currency"),
                "status": order.get("status"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        low = str(exc).lower()
        cat = "AUTH_FAILED" if ("auth" in low or "401" in low) else "ORDER_CREATE_FAILED"
        result = _result(cat, f"{type(exc).__name__}: {str(exc)[:300]}", {"phase": "order_create"})

    print(json.dumps(result, indent=2))
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["category"] == "REACHABLE" else 1


if __name__ == "__main__":
    sys.exit(main())