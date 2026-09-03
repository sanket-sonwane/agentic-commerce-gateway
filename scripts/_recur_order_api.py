"""Create + complete a REAL checkout deterministically (no buyer LLM), leaving
the session in requires_payment/awaiting_authorization so a HUMAN can pay in
the dashboard Ops console (real Razorpay modal -> mandate re-registration)."""
import json
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

sys.path.insert(0, r"D:\Hackathons\rz-agent")

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from merchant.ap2.user_intent import assertion_canonical_string, assertion_hash

BASE = "http://localhost:8000"
UA_KEY_ID = "live-final-userkey-01"
USER_ID = "live-user-final"
DELEGATION = "dlg_9f8e7d"

seed = b"M" * 32
priv = Ed25519PrivateKey.from_private_bytes(seed)
pem = priv.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()

# 1. register key (idempotent-ish; 409 if already present is fine)
r = requests.post(f"{BASE}/user_keys", json={
    "user_id": USER_ID, "key_id": UA_KEY_ID, "public_key_pem": pem,
}, timeout=10)
print("1 user_keys:", r.status_code)

out = None
for attempt in range(1, 4):
    # 2. create checkout (milk x1 -> subtotal 32 + shipping 40 = 72)
    r = requests.post(f"{BASE}/checkout_sessions", json={
        "buyer_agent_id": "buyer_agent_llm_01",
        "delegation_token": DELEGATION,
        "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}],
    }, timeout=15)
    body = r.json().get("checkout_session", {})
    csid = body.get("checkout_session_id")
    print(f"2 checkout (attempt {attempt}):", r.status_code, "csid:", csid, "| status:", body.get("status"))

    # 3. sign a fresh user intent assertion (single-use)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    base = {
        "assertion_id": f"ua_chk_{uuid4().hex[:12]}",
        "user_id": USER_ID,
        "user_key_id": UA_KEY_ID,
        "merchant_id": "merchant_grocery_01",
        "delegate_id": "buyer_agent_llm_01",
        "intent": {"action": "checkout", "checkout_session_id": csid, "description": "recurring flag check milk"},
        "constraint_ceiling": {"currency": "INR", "max_amount_paise": 7200},
        "issued_at": (now - timedelta(seconds=20)).isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(minutes=14)).isoformat(timespec="seconds"),
        "nonce": f"nonce_chk_{uuid4().hex[:12]}",
    }
    canonical = assertion_canonical_string(**base)
    assertion = dict(base)
    assertion["canonical_payload_hash"] = assertion_hash(**base)
    assertion["signature"] = priv.sign(canonical.encode("utf-8")).hex()

    # 4. complete -> should reach requires_payment + awaiting_authorization (modal path)
    r = requests.post(f"{BASE}/checkout_sessions/{csid}/complete", json={
        "buyer_agent_id": "buyer_agent_llm_01",
        "delegation_token": DELEGATION,
        "intent_mandate_id": None,
        "user_assertion": assertion,
    }, timeout=30)
    resp = r.json()
    cs = resp.get("checkout_session", {}) if r.status_code == 200 else {}
    pay = cs.get("payment", {})
    print("4 complete:", r.status_code)
    print("   status:", cs.get("status"), "| order_id:", pay.get("razorpay_order_id"))
    if cs.get("status") == "requires_payment" and pay.get("awaiting_authorization"):
        out = {
            "csid": csid, "status": cs.get("status"),
            "order_id": pay.get("razorpay_order_id"),
            "customer_id": pay.get("customer_id"),
            "key_id": pay.get("key_id"),
            "save_card": pay.get("save_card"),
            "amount": cs.get("totals", {}).get("total"),
            "amount_paise": pay.get("amount_paise"),
            "awaiting_authorization": pay.get("awaiting_authorization"),
        }
        print("   customer_id:", pay.get("customer_id"), "| save_card:", pay.get("save_card"), "| key_id:", pay.get("key_id"))
        break
    print("   not ready (transient?):", r.text[:200])
    if "PAYMENT_FAILED" not in resp.get("error", {}).get("code", ""):
        break

with open(r"D:\Hackathons\rz-agent\artifacts\recur_ready.json", "w") as fh:
    json.dump(out, fh, indent=2)
print("5 saved artifacts/recur_ready.json")
if not out:
    print("   !!! NOT READY FOR MODAL")
    sys.exit(2)