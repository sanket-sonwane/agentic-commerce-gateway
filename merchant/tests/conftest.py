"""Shared fixtures: isolated temp SQLite DB, seeded catalog, TestClient."""
import json
import os
import tempfile
from pathlib import Path

import pytest

TEST_DB = os.path.join(tempfile.gettempdir(), "acg_test_merchant.db")
os.environ["MERCHANT_DB_PATH"] = TEST_DB
os.environ.setdefault("PAYMENT_DRIVER", "sim")
# Simulated payments are a pytest-only fixture: this flag unlocks the
# credential-less hermetic paths that production refuses (config validator +
# payment_service gates). Never set this in a real deployment.
os.environ["ACG_TEST_ALLOW_SIM"] = "1"
# Keep the suite hermetic: never call the real Razorpay API from tests.
os.environ["RAZORPAY_KEY_ID"] = ""
os.environ["RAZORPAY_KEY_SECRET"] = ""
# Merchant Agent tests use the deterministic fallback brain (no live NIM calls).
os.environ["MA_LLM_BRAIN"] = "false"
# Pin the operator secret so tests are independent of any live .env value.
os.environ["ADMIN_TOKEN"] = "admin_demo_token"

ROOT = Path(__file__).resolve().parents[2]

for _suffix in ("", "-wal", "-shm"):
    try:
        os.remove(TEST_DB + _suffix)
    except FileNotFoundError:
        pass


from fastapi.testclient import TestClient  # noqa: E402

from merchant.persistence.db import mutate  # noqa: E402
from merchant.main import app  # noqa: E402
from merchant.persistence.models.entities import (  # noqa: E402
    AuditEvent,
    CheckoutSession,
    Delegation,
    IdempotencyKey,
    Pack,
    Reservation,
)
import scripts.seed_catalog as seeder  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def seeded_db():
    seeder.main()
    yield


@pytest.fixture(autouse=True)
def clean_state(seeded_db):
    """Fresh transactions + restored catalog stock + fresh delegations per test."""
    catalog = json.loads((ROOT / "merchant" / "seed" / "catalog.json").read_text(encoding="utf-8"))
    with mutate() as db:
        for model in (Reservation, IdempotencyKey, AuditEvent, CheckoutSession, Delegation):
            db.query(model).delete()
        for prod in catalog["products"]:
            for pack in prod["packs"]:
                row = db.get(Pack, pack["pack_id"])
                if row is not None:
                    row.stock_actual = int(pack.get("stock", 0))
                    row.price = int(pack["price"])
        seeder.seed_delegations(db)
    yield


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def admin_headers():
    return {"X-Admin-Token": "admin_demo_token"}


def make_checkout(client, items=None, delegation="dlg_9f8e7d", buyer="buyer_agent_llm_01"):
    payload = {
        "buyer_agent_id": buyer,
        "delegation_token": delegation,
        "line_items": items
        or [{"product_id": "prod_milk_amul", "pack_id": "pack_milk_amul_500", "quantity": 2}],
    }
    return client.post("/checkout_sessions", json=payload)


def complete(client, csid, delegation="dlg_9f8e7d", buyer="buyer_agent_llm_01", key=None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        f"/checkout_sessions/{csid}/complete",
        json={"buyer_agent_id": buyer, "delegation_token": delegation},
        headers=headers,
    )
