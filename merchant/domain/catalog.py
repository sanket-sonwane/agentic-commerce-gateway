"""Merchant catalog domain capability (PRD §25, §33–34).

Deterministic read capabilities that back the Merchant Agent tool layer:
authoritative product/pack views, live inventory checks, price lookups,
alternative ranking (PRD §25) and target-size extraction from free text.

All functions are pure reads over the given `Session`; agent-facing result
dicts and protocol codes are owned here so the agent tool never touches how
SQLite stores Product/Pack. Failures raise `ProtocolError` (protocol codes)
and are translated to the §51 tool envelope at the agent boundary.
"""
import json
import math
import re

from sqlalchemy.orm import Session

from merchant.control import delegation_service
from merchant.domain import discovery_service, inventory_service
from merchant.persistence.models.dto import ErrorCode, ProtocolError
from merchant.persistence.models.entities import Pack, Product

SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|l|g|kg|pcs|piece|pieces)\b", re.IGNORECASE)
UNIT_ALIASES = {"piece": "pcs", "pieces": "pcs"}


def parse_target_pack(query: str) -> dict:
    """Extract a desired size/unit from free text, e.g. '1L', '500 ml', '2kg'."""
    m = SIZE_RE.search(query or "")
    if not m:
        return {}
    unit = UNIT_ALIASES.get(m.group(2).lower(), m.group(2).lower())
    try:
        return {"size": float(m.group(1)), "unit": unit}
    except ValueError:
        return {}


def load_product(db: Session, product_id: str) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise ProtocolError(
            ErrorCode.PRODUCT_NOT_FOUND,
            f"Unknown product {product_id}",
            details={"product_id": product_id},
        )
    return product


def load_pack(db: Session, product_id: str, pack_id: str) -> tuple[Product, Pack]:
    product = load_product(db, product_id)
    pack = db.get(Pack, pack_id)
    if pack is None or pack.product_id != product_id:
        raise ProtocolError(
            ErrorCode.PACK_NOT_FOUND,
            f"Unknown pack {pack_id} for product {product_id}",
            details={"product_id": product_id, "pack_id": pack_id},
        )
    return product, pack


def available(db: Session, pack: Pack) -> int:
    return inventory_service.available_stock(db, pack.pack_id, pack.stock_actual)


def suggest_quantity(original: Pack, candidate: Pack, quantity: int) -> int:
    """How many of `candidate` cover the volume of `quantity` x `original`."""
    if original.unit != candidate.unit or not candidate.size:
        return quantity
    needed = original.size * quantity
    return max(1, math.ceil(needed / candidate.size - 1e-9))


def pack_view(db: Session, pack: Pack) -> dict:
    return {
        "pack_id": pack.pack_id,
        "size": pack.size,
        "unit": pack.unit,
        "price": pack.price,
        "currency": pack.currency,
        "available": available(db, pack),
        "stock_actual": pack.stock_actual,
    }


def delegation_constraints(db: Session, delegation_token: str) -> dict:
    """Best-effort delegation constraints used to avoid proposing doomed baskets."""
    try:
        delegation = delegation_service.get_delegation(db, delegation_token)
        if delegation is None or delegation.status != "active":
            return {}
        return {
            "allowed_categories": set(delegation_service.allowed_categories(delegation)),
            "max_quantity_per_item": delegation.max_quantity_per_item,
            "max_order_value": delegation.max_order_value,
        }
    except Exception:  # noqa: BLE001 — constraints are advisory for proposals
        return {}


def search_catalog(db: Session, query: str) -> dict:
    """Ranked free-text catalog search (agent-facing result dict)."""
    query = str(query or "").strip()
    if not query:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "query must be non-empty")
    parsed = discovery_service._fallback_parse(query)
    keywords = list(dict.fromkeys(parsed.keywords))[:4]
    tokens = [
        t
        for t in re.sub(r"[^a-z0-9\s\-]", " ", query.lower()).split()
        if len(t) > 2 and not t.isdigit() and t not in keywords
    ]
    keywords += tokens[:3]
    if not keywords:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"could not derive search keywords from '{query}'")

    scored: list[tuple[int, Product, str]] = []
    for product in db.query(Product).all():
        tags = json.loads(product.tags)
        best_score, best_kw = 0, ""
        for kw in keywords:
            s, _ = discovery_service._score_product(product, tags, kw)
            if s > best_score:
                best_score, best_kw = s, kw
        if best_score > 0:
            scored.append((best_score, product, best_kw))
    scored.sort(key=lambda t: (-t[0], t[1].name))

    results = []
    for _, product, matched_for in scored[:8]:
        packs = db.query(Pack).filter(Pack.product_id == product.product_id).all()
        results.append(
            {
                "product_id": product.product_id,
                "name": product.name,
                "brand": product.brand,
                "category": product.category,
                "tags": json.loads(product.tags),
                "matched_for": matched_for,
                "packs": sorted((pack_view(db, p) for p in packs), key=lambda v: v["price"]),
            }
        )
    if not results:
        return {"results": [], "note": f"no catalog products matched '{query}'"}
    return {"results": results, "count": len(results)}


def get_product(db: Session, product_id: str) -> dict:
    product = load_product(db, str(product_id or ""))
    packs = db.query(Pack).filter(Pack.product_id == product.product_id).all()
    return {
        "product_id": product.product_id,
        "name": product.name,
        "brand": product.brand,
        "category": product.category,
        "tags": json.loads(product.tags),
        "packs": sorted((pack_view(db, p) for p in packs), key=lambda v: v["price"]),
    }


def check_inventory(db: Session, pack_id: str) -> dict:
    pack = db.get(Pack, str(pack_id or ""))
    if pack is None:
        raise ProtocolError(
            ErrorCode.PACK_NOT_FOUND,
            f"Unknown pack {pack_id}",
            details={"pack_id": pack_id},
        )
    reserved = inventory_service.reserved_quantity(db, pack.pack_id)
    available_stock = max(0, pack.stock_actual - reserved)
    return {
        "product_id": pack.product_id,
        "pack_id": pack.pack_id,
        "stock_actual": pack.stock_actual,
        "reserved": reserved,
        "available": available_stock,
        "available_quantity": available_stock,
        "availability": "in_stock" if available_stock > 0 else "out_of_stock",
        "note": (
            "stock fields describe CURRENT MERCHANT INVENTORY for this pack — they are NOT the "
            "buyer's requested quantity"
        ),
    }


def get_current_price(db: Session, pack_id: str) -> dict:
    pack = db.get(Pack, str(pack_id or ""))
    if pack is None:
        raise ProtocolError(
            ErrorCode.PACK_NOT_FOUND,
            f"Unknown pack {pack_id}",
            details={"pack_id": pack_id},
        )
    return {
        "product_id": pack.product_id,
        "pack_id": pack.pack_id,
        "price": pack.price,
        "currency": pack.currency,
        "note": "authoritative merchant price",
    }


def find_alternative(
    db: Session,
    product_id: str,
    pack_id: str,
    quantity: int | None = None,
    *,
    requested_items: list,
    excluded_pack_ids: list,
    buyer_request: str,
    delegation_token: str | None,
) -> dict:
    """PRD §25 alternative ranking for an unavailable requested pack.

    Deterministic tiers: same product other packs (closest volume), then same
    category with strongly shared tags (brand swap). Candidate filtering drops
    what the deterministic control plane would reject (delegation ceiling,
    category allow-list, stock). The result includes a drop_summary and a
    human-only note so the agent understands WHY options were excluded.
    """
    product, requested_pack = load_pack(db, str(product_id or ""), str(pack_id or ""))
    qty = max(1, int(quantity or 1))
    constraints = delegation_constraints(db, delegation_token)
    allowed_cats = constraints.get("allowed_categories")
    max_qty = constraints.get("max_quantity_per_item")

    # Intent anchor: without explicit requested_items, a huge search quantity is
    # usually the small model copying inventory numbers. The control plane can
    # never approve more than max_quantity_per_item anyway, so clamp the SEARCH
    # scope to the proposeable ceiling and say so.
    quantity_note = ""
    if (
        max_qty is not None
        and qty > int(max_qty)
        and not requested_items
        and str(qty) not in re.findall(r"\d+", buyer_request or "")
    ):
        qty = int(max_qty)
        quantity_note = (
            f"requested quantity was not stated by the buyer; search clamped to the delegation "
            f"ceiling (max_quantity_per_item={max_qty})"
        )

    excluded = set(excluded_pack_ids)

    alternatives: list[dict] = []
    drops = {"excluded": 0, "category_blocked": 0, "quantity_over_limit": 0, "insufficient_stock": 0}

    GENERIC_TAGS = {"dairy", "daily-essential", "grocery", "essentials"}

    def add_candidate(cand_product: Product, pack: Pack, reason: str, suggested: int, extra_reasons: list[str] | None = None) -> None:
        if pack.pack_id in excluded:
            drops["excluded"] += 1
            return
        if allowed_cats is not None and cand_product.category not in allowed_cats:
            drops["category_blocked"] += 1
            return
        if max_qty is not None and suggested > max_qty:
            drops["quantity_over_limit"] += 1
            return
        available_stock = available(db, pack)
        if available_stock < suggested:
            drops["insufficient_stock"] += 1
            return
        alternatives.append(
            {
                "product_id": cand_product.product_id,
                "pack_id": pack.pack_id,
                "name": cand_product.name,
                "brand": cand_product.brand,
                "category": cand_product.category,
                "size": pack.size,
                "unit": pack.unit,
                "quantity": suggested,
                "price": pack.price,
                "currency": pack.currency,
                "line_total": pack.price * suggested,
                "reason": reason,
                "reason_detail": extra_reasons or [],
            }
        )

    # Tier 1 — same product, different pack (closest volume first).
    siblings = [p for p in db.query(Pack).filter(Pack.product_id == product.product_id).all() if p.pack_id != pack_id]
    for pack in siblings:
        suggested = suggest_quantity(requested_pack, pack, qty)
        if pack.size < requested_pack.size and pack.unit == requested_pack.unit:
            reason = "same product, smaller pack"
        elif pack.size > requested_pack.size and pack.unit == requested_pack.unit:
            reason = "same product, larger pack"
        else:
            reason = "same product, different pack size"
        add_candidate(
            product,
            pack,
            reason,
            suggested,
            [f"{requested_pack.size}{requested_pack.unit} x{qty} -> {pack.size}{pack.unit} x{suggested}"],
        )

    # Tier 2 — different product, same category (brand swap / close analogue).
    if product.category in (allowed_cats or {product.category}):
        for other in db.query(Product).filter(Product.category == product.category).all():
            if other.product_id == product.product_id:
                continue
            shared_tags = sorted(set(json.loads(other.tags)) & set(json.loads(product.tags)))
            strong_tags = [t for t in shared_tags if t not in GENERIC_TAGS]
            if not strong_tags:
                continue  # avoid proposing loosely-related items (butter for milk)
            for pack in db.query(Pack).filter(Pack.product_id == other.product_id).all():
                suggested = suggest_quantity(requested_pack, pack, qty)
                detail = ([f"shared_tags={','.join(strong_tags)}"] if strong_tags else []) + [f"brand={other.brand}"]
                add_candidate(other, pack, "same category alternative", suggested, detail)

    # Deterministic preference: same-product first, nearest volume, then price.
    alternatives.sort(key=lambda a: (0 if a["reason"].startswith("same product") else 1, abs(a["size"] - requested_pack.size), a["price"]))
    trimmed = alternatives[:5]

    out: dict = {
        "requested": {"product_id": product_id, "pack_id": pack_id, "quantity": qty, "requested_quantity": qty},
        "alternatives": trimmed,
        "count": len(trimmed),
        "drop_summary": drops,
    }
    if quantity_note:
        out["quantity_note"] = quantity_note
    if trimmed:
        out["note"] = ""
    elif drops["quantity_over_limit"] and not drops["insufficient_stock"]:
        out["note"] = (
            f"every candidate was dropped because the suggested quantity exceeds the delegation "
            f"limit (max_quantity_per_item={max_qty}); retry with quantity <= {max_qty}"
        )
    elif drops["insufficient_stock"]:
        out["note"] = "matching packs exist but lack sufficient stock for the requested quantity"
    elif drops["category_blocked"]:
        out["note"] = "candidates exist but are blocked by the delegation category restrictions"
    elif drops["excluded"]:
        out["note"] = "all matching packs were already proposed and rejected"
    else:
        out["note"] = "no candidate packs found for this product/category"
    return out