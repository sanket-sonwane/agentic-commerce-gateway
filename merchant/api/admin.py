"""Operator endpoints (ADMIN_TOKEN-guarded): stock adjustment, demo reset, ops status."""
import json

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from merchant.config import settings
from merchant.persistence.db import mutate, read_only
from merchant.persistence.models.entities import (
    AuditEvent,
    CheckoutSession,
    Delegation,
    IdempotencyKey,
    Pack,
    Reservation,
)
from merchant.evidence import audit_service
from merchant.payment import payment_service

router = APIRouter(prefix="/admin")


def require_admin(x_admin_token: str = Header(default="")):
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid admin token")


class StockAdjust(BaseModel):
    actual_stock: int


@router.get("/packs", dependencies=[Depends(require_admin)])
def list_packs(db: Session = Depends(read_only)):
    """Pack list for the operator console stock editor."""
    packs = db.query(Pack).all()
    out = []
    for p in packs:
        product = p.product
        out.append(
            {
                "pack_id": p.pack_id,
                "product_id": p.product_id,
                "name": f"{product.name} ({p.size:g}{p.unit})",
                "category": product.category,
                "price": p.price,
                "stock_actual": p.stock_actual,
            }
        )
    return {"packs": sorted(out, key=lambda x: x["name"])}


@router.patch("/packs/{pack_id}/stock", dependencies=[Depends(require_admin)])
def set_stock(pack_id: str, body: StockAdjust):
    with mutate() as db:
        pack = db.get(Pack, pack_id)
        if pack is None:
            raise HTTPException(status_code=404, detail=f"Unknown pack {pack_id}")
        old = pack.stock_actual
        pack.stock_actual = max(0, body.actual_stock)
        product = pack.product
        audit_service.append_event(
            db,
            actor="operator",
            action="inventory_adjusted",
            decision="info",
            reasoning_summary=(
                f"{product.name} ({pack.pack_id}) stock {old} -> {pack.stock_actual}"
            ),
            payload={"pack_id": pack_id, "old": old, "new": pack.stock_actual},
        )
        return {"pack_id": pack_id, "stock_actual": pack.stock_actual}


@router.post("/demo/reset", dependencies=[Depends(require_admin)])
def reset_demo():
    """Wipe transactions + reseed catalog stock & delegations for a clean demo run."""
    from scripts.seed_catalog import seed_delegations

    with mutate() as db:
        for model in (Reservation, IdempotencyKey, AuditEvent, CheckoutSession, Delegation):
            db.query(model).delete()
        data = json.loads((settings_catalog_path()).read_text(encoding="utf-8"))
        for prod in data["products"]:
            for pack in prod["packs"]:
                row = db.get(Pack, pack["pack_id"])
                if row is not None:
                    row.stock_actual = int(pack.get("stock", 0))
        seed_delegations(db)
        audit_service.append_event(
            db,
            actor="operator",
            action="demo_reset",
            decision="info",
            reasoning_summary="Demo state reset; catalog stock restored; transactions cleared",
        )
        return {"status": "reset_ok"}


def settings_catalog_path():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "seed" / "catalog.json"


@router.get("/ops/status", dependencies=[Depends(require_admin)])
def ops_status(db: Session = Depends(read_only)):
    sessions = db.query(CheckoutSession).order_by(CheckoutSession.updated_at.desc()).limit(50).all()
    return {
        "payment_driver": settings.payment_driver,
        "razorpay_credentials_present": payment_service.has_credentials(),
        "razorpay_api_reachable": payment_service.ping_api(),
        "recent_sessions": [
            {
                "checkout_session_id": s.checkout_session_id,
                "status": s.status,
                "buyer_agent_id": s.buyer_agent_id,
                "total": json.loads(s.totals).get("total"),
                "updated_at": s.updated_at.isoformat() + "Z",
            }
            for s in sessions
        ],
    }
