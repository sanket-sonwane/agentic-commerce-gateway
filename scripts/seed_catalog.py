"""Seed the merchant database: catalog + delegations.

Usage (from repo root):
    python -m scripts.seed_catalog
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from merchant_agent.db import SessionLocal, init_db, utcnow  # noqa: E402
from merchant_agent.models.entities import Delegation, Pack, Product  # noqa: E402

CATALOG_PATH = ROOT / "merchant_agent" / "seed" / "catalog.json"

ALLOWED_CATEGORIES = [
    "dairy",
    "produce",
    "staples",
    "bakery",
    "meat_eggs",
    "beverages",
    "snacks",
    "household",
]


def seed_delegations(session) -> None:
    now = utcnow()
    delegations = [
        # Primary demo delegation — generous limit, long expiry for reliable demos.
        Delegation(
            delegation_id="dlg_9f8e7d",
            buyer_agent_id="buyer_agent_llm_01",
            max_order_value=5000,
            allowed_categories=json.dumps(ALLOWED_CATEGORIES),
            max_quantity_per_item=10,
            expires_at=now + timedelta(days=365),
            status="active",
        ),
        # Restricted delegation for the adversarial over-budget demo (PRD §36).
        # High quantity ceiling on purpose: the SPEND LIMIT must be the rule that fires.
        Delegation(
            delegation_id="dlg_restricted_50",
            buyer_agent_id="buyer_agent_llm_01",
            max_order_value=500,
            allowed_categories=json.dumps(ALLOWED_CATEGORIES),
            max_quantity_per_item=50,
            expires_at=now + timedelta(days=365),
            status="active",
        ),
        # Delegation for Buyer B (independent deterministic interop client).
        Delegation(
            delegation_id="dlg_buyerb_001",
            buyer_agent_id="buyer_agent_b_deterministic",
            max_order_value=2000,
            allowed_categories=json.dumps(ALLOWED_CATEGORIES),
            max_quantity_per_item=10,
            expires_at=now + timedelta(days=365),
            status="active",
        ),
        # Expired delegation to demonstrate DELEGATION_EXPIRED.
        Delegation(
            delegation_id="dlg_expired_001",
            buyer_agent_id="buyer_agent_llm_01",
            max_order_value=5000,
            allowed_categories=json.dumps(ALLOWED_CATEGORIES),
            max_quantity_per_item=10,
            expires_at=now - timedelta(minutes=1),
            status="active",
        ),
    ]
    session.add_all(delegations)


def seed_catalog(session) -> None:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    for prod in data["products"]:
        session.add(
            Product(
                product_id=prod["product_id"],
                name=prod["name"],
                category=prod["category"],
                brand=prod.get("brand", ""),
                description=prod.get("description", ""),
                tags=json.dumps(prod.get("tags", [])),
            )
        )
        for pack in prod["packs"]:
            session.add(
                Pack(
                    pack_id=pack["pack_id"],
                    product_id=prod["product_id"],
                    size=float(pack["size"]),
                    unit=pack["unit"],
                    price=int(pack["price"]),
                    currency="INR",
                    stock_actual=int(pack.get("stock", 0)),
                )
            )


def main() -> None:
    init_db()
    with SessionLocal() as session:
        if session.query(Product).count() > 0:
            print("Database already seeded — nothing to do.")
            return
        seed_catalog(session)
        seed_delegations(session)
        session.commit()
        products = session.query(Product).count()
        packs = session.query(Pack).count()
        delegations = session.query(Delegation).count()
        print(f"Seeded {products} products / {packs} packs / {delegations} delegations.")


if __name__ == "__main__":
    main()
