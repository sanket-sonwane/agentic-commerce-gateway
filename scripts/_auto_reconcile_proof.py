"""Live proof that the background reconcile worker auto-finalises a fresh
headless token charge (no operator /reconcile). Prints the session state
transitions as it settles. Exits 0 when the session reaches a terminal state
on its own.
"""
import json
import sys
import time
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

requests.post(f"{BASE}/user_keys", json={
    "user_id": USER_ID, "key_id": UA_KEY_ID, "public_key_pem": pem,
}, timeout=10)

r = requests.post(f"{BASE}/checkout_sessions", json={
    "buyer_agent_id": "buyer_agent_llm_01",
    "delegation_token": DELEGATION,
    "line_items": [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 1}],
}, timeout=15)
csid = r.json()["checkout_session"]["checkout_session_id"]
print(f"[1] created {csid}", flush=True)

now = datetime.now(timezone.utc).replace(tzinfo=None)
base = {
    "assertion_id": f"ua_apr_{uuid4().hex[:12]}",
    "user_id": USER_ID,
    "user_key_id": UA_KEY_ID,
    "merchant_id": "merchant_grocery_01",
    "delegate_id": "buyer_agent_llm_01",
    "intent": {"action": "checkout", "checkout_session_id": csid, "description": "auto-reconcile proof"},
    "constraint_ceiling": {"currency": "INR", "max_amount_paise": 7200},
    "issued_at": (now - timedelta(seconds=20)).isoformat(timespec="seconds"),
    "expires_at": (now + timedelta(minutes=14)).isoformat(timespec="seconds"),
    "nonce": f"nonce_apr_{uuid4().hex[:12]}",
}
canonical = assertion_canonical_string(**base)
assertion = dict(base)
assertion["canonical_payload_hash"] = assertion_hash(**base)
assertion["signature"] = priv.sign(canonical.encode("utf-8")).hex()

r = requests.post(f"{BASE}/checkout_sessions/{csid}/complete", json={
    "buyer_agent_id": "buyer_agent_llm_01",
    "delegation_token": DELEGATION,
    "intent_mandate_id": None,
    "user_assertion": assertion,
}, timeout=30)
resp = r.json()
cs = resp.get("checkout_session", {})
print(f"[2] complete -> {r.status_code} status={cs.get('status')} order={cs.get('razorpay_order_id')} payment={cs.get('razorpay_payment_id')}", flush=True)

if cs.get("status") != "payment_pending":
    print("   unexpected; dumping:", json.dumps(resp)[:600], flush=True)
    sys.exit(3)

print("[3] poller must now auto-finalise this session (NO manual /reconcile) ...", flush=True)
for i in range(40):  # up to ~6-7 min (settle observed ~3-5 min; poller every 30s)
    time.sleep(15)
    st = requests.get(f"{BASE}/checkout_sessions/{csid}", timeout=10).json()["checkout_session"]
    print(f"    t+{15*(i+1)}s status={st.get('status')} payment={st.get('razorpay_payment_id')}", flush=True)
    if st.get("status") in ("completed", "declined"):
        print(f"[DONE] status={st.get('status')}", flush=True)
        sys.exit(0 if st.get("status") == "completed" else 4)

print("[FAIL] still not terminal after the wait", flush=True)
sys.exit(5)