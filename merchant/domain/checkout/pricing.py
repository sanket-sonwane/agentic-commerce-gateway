"""Checkout pricing primitives: authoritative line-item resolution + totals.

"Authoritative" means the unit price always comes from the catalog Pack row —
never from client-supplied values (C-1/C-2). Also hosts the canonical cart hash
used by the AP2 layer so the session side and the Execution-Mandate side hash
identically regardless of metadata ordering.
"""
import json

from sqlalchemy.orm import Session

from merchant.config import settings
from merchant.persistence.models.dto import ErrorCode, ProtocolError
from merchant.persistence.models.entities import Pack, Product


def _load_pack(db: Session, product_id: str, pack_id: str) -> tuple[Product, Pack]:
    product = db.get(Product, product_id)
    if product is None:
        raise ProtocolError(
            ErrorCode.PRODUCT_NOT_FOUND,
            f"Unknown product {product_id}",
            details={"product_id": product_id},
        )
    pack = db.get(Pack, pack_id)
    if pack is None or pack.product_id != product_id:
        raise ProtocolError(
            ErrorCode.PACK_NOT_FOUND,
            f"Unknown pack {pack_id} for product {product_id}",
            http_status=404,
            details={"product_id": product_id, "pack_id": pack_id},
        )
    return product, pack


def _resolve_items(db: Session, raw_items: list[dict]) -> list[dict]:
    """Resolve buyer line items against the authoritative catalog."""
    resolved = []
    for item in raw_items:
        product, pack = _load_pack(db, item["product_id"], item["pack_id"])
        resolved.append(
            {
                "product_id": product.product_id,
                "pack_id": pack.pack_id,
                "name": product.name,
                "category": product.category,
                "tags": json.loads(product.tags),
                "size": pack.size,
                "unit": pack.unit,
                "quantity": int(item["quantity"]),
                "unit_price": pack.price,  # authoritative price — never client-supplied
                "stock_actual": pack.stock_actual,
            }
        )
    return resolved


def _compute_totals(resolved_items: list[dict]) -> dict:
    subtotal = sum(i["unit_price"] * i["quantity"] for i in resolved_items)
    shipping = 0 if subtotal >= settings.free_shipping_threshold else settings.shipping_fee
    tax = 0
    return {
        "subtotal": subtotal,
        "tax": tax,
        "shipping": shipping,
        "total": subtotal + tax + shipping,
        "currency": "INR",
    }


def resolve_line_items(db: Session, raw_items: list[dict]) -> list[dict]:
    """Public seam over the authoritative line-item resolution (agent gating preview)."""
    return _resolve_items(db, raw_items)


def compute_totals(resolved_items: list[dict]) -> dict:
    """Public seam over the authoritative totals computation (agent gating preview)."""
    return _compute_totals(resolved_items)


def computes_cart_hash(items: list[dict]) -> str:
    """Stable SHA-256 over the resolved line items (AP2 §21 cart_hash).

    Canonical form keys on product_id/pack_id/quantity/unit_price_paise so it
    is stable regardless of metadata ordering. Accepts both the resolved session
    item shape (unit_price in rupees) and the Execution Mandate shape
    (unit_price_paise), normalizing to paise so both sides hash identically.
    """
    from merchant.ap2 import crypto_service

    normalized = []
    for i in items:
        up = i.get("unit_price_paise")
        if up is None:
            up = int(i["unit_price"]) * 100
        normalized.append(
            {
                "product_id": i["product_id"],
                "pack_id": i["pack_id"],
                "quantity": int(i["quantity"]),
                "unit_price_paise": int(up),
            }
        )
    normalized.sort(key=lambda i: (i["product_id"], i["pack_id"]))
    return crypto_service.sha256_hex(normalized)