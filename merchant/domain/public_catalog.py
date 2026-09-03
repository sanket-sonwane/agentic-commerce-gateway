"""Public machine-readable catalog reads with explicit commerce semantics.

Reuses the authoritative catalog domain (merchant/domain/catalog.py) and
inventory logic — it does NOT duplicate business logic, only shapes the existing
authoritative reads into an explicit, machine-readable DTO boundary. The public
API never exposes ORM objects directly.

Only the merchant remains authoritative for price, inventory and availability;
none of these reads accept or trust any buyer-supplied price.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from merchant.domain import catalog, inventory_service
from merchant.persistence.models.dto import CatalogPackOut, CatalogProductOut, ErrorCode, ProtocolError
from merchant.persistence.models.entities import Pack, Product

_UNIT_TO_SALE = {"ml", "l", "g", "kg", "pcs"}


def _pack_out(db: Session, pack: Pack, product: Product) -> CatalogPackOut:
    reserved = inventory_service.reserved_quantity(db, pack.pack_id)
    available = max(0, pack.stock_actual - reserved)
    availability = "in_stock" if available > 0 else "out_of_stock"
    return CatalogPackOut(
        pack_id=pack.pack_id,
        product_id=pack.product_id,
        variant_id=pack.pack_id,
        name=product.name,
        size=pack.size,
        pack_size=pack.size,
        unit=pack.unit,
        quantity=1,  # per-pack sale basis: one pack at a time by default
        sale_basis="per_pack",
        unit_price=pack.price,
        price_basis="per_pack",
        currency=pack.currency,
        stock_actual=pack.stock_actual,
        reserved=reserved,
        available=available,
        availability=availability,
    )


def get_public_product(db: Session, product_id: str) -> CatalogProductOut:
    """Authoritative product read with its variant packs."""
    product = catalog.load_product(db, str(product_id or ""))
    packs = (
        db.query(Pack)
        .filter(Pack.product_id == product.product_id)
        .order_by(Pack.price)
        .all()
    )
    return CatalogProductOut(
        product_id=product.product_id,
        name=product.name,
        category=product.category,
        brand=product.brand,
        description=product.description,
        tags=json.loads(product.tags) if product.tags else [],
        sale_basis="per_pack",
        currency="INR",
        packs=[_pack_out(db, p, product) for p in packs],
    )


def get_public_pack(db: Session, pack_id: str) -> CatalogPackOut:
    """Authoritative pack read with explicit commerce semantics."""
    pack = db.get(Pack, str(pack_id or ""))
    if pack is None:
        raise ProtocolError(
            ErrorCode.PACK_NOT_FOUND,
            f"Unknown pack {pack_id}",
            http_status=404,
            details={"pack_id": pack_id},
        )
    product = catalog.load_product(db, pack.product_id)
    return _pack_out(db, pack, product)
