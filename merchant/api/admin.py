"""Operator endpoints (admin-guarded): stock adjustment, demo reset, ops status.

Authorization gates:
- ``X-Admin-Token`` header matching ``settings.admin_token`` (CLI/tests; the
  secret lives server-side in .env, never in the shipped browser bundle).
- a valid, unexpired ``acg_admin_session`` HttpOnly cookie obtained from
  ``POST /admin/login`` (the browser path: the operator types the passphrase
  once, the backend signs a short-lived session cookie and the browser never
  holds the privileged secret).
"""
import base64
import hmac
import json
from datetime import timedelta

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
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
from merchant.ap2 import crypto_service

router = APIRouter(prefix="/admin")

_ADMIN_SESSION_COOKIE = "acg_admin_session"


class LoginRequest(BaseModel):
    password: str


def _issue_admin_session() -> str:
    """Stateless signed session token: b64url(canonical payload) . ed25519 sig.

    Signed with the merchant's own Ed25519 key (already present, gitignored,
    never exposed to clients). Stateless so restarts do not invalidate the
    operator session; expiry is inside the signed payload.
    """
    payload = {
        "sub": "admin",
        "exp": (crypto_service.now_utc() + timedelta(minutes=settings.admin_session_ttl_minutes)).isoformat(timespec="seconds"),
        "nonce": crypto_service.new_nonce(),
    }
    body = base64.urlsafe_b64encode(crypto_service.canonical_json(payload).encode("utf-8")).decode("ascii").rstrip("=")
    sig = crypto_service.sign(payload)
    return f"{body}.{sig}"


def _verify_admin_session(token: str) -> bool:
    try:
        body, sig = token.split(".", 1)
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, Exception):  # noqa: BLE001 — malformed token
        return False
    if not crypto_service.verify(payload, sig):
        return False
    if payload.get("sub") != "admin":
        return False
    exp = payload.get("exp")
    try:
        from datetime import datetime

        if datetime.fromisoformat(exp) <= crypto_service.now_utc():
            return False
    except (ValueError, TypeError):
        return False
    return True


def require_admin(
    x_admin_token: str = Header(default=""),
    acg_admin_session: str = Cookie(default=""),
):
    """Accept either the header secret (CLI) or a valid signed operator session."""
    if hmac.compare_digest(x_admin_token, settings.admin_token) or _verify_admin_session(acg_admin_session):
        return
    raise HTTPException(status_code=401, detail="Admin authentication required")


@router.post("/login")
def admin_login(body: LoginRequest, response: Response):
    """Exchange the operator passphrase for a short-lived HttpOnly session cookie."""
    if not hmac.compare_digest(body.password, settings.admin_token):
        raise HTTPException(status_code=401, detail="Invalid admin token")
    response.set_cookie(
        _ADMIN_SESSION_COOKIE,
        _issue_admin_session(),
        httponly=True,
        samesite="lax",
        max_age=settings.admin_session_ttl_minutes * 60,
        path="/",
    )
    return {"status": "ok", "session": "cookie"}


@router.post("/logout")
def admin_logout(response: Response):
    response.delete_cookie(_ADMIN_SESSION_COOKIE, path="/")
    return {"status": "logged_out"}


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
    """Reset demo transaction state, preserving the append-only audit trail.

    Transactions, idempotency keys and delegations are cleared and catalog
    stock is restored. AuditEvent rows are NEVER deleted here: the reset itself
    is recorded as ``demo_reset_requested`` + ``demo_reset_completed`` events
    appended inside the same serialized transaction, so the hash chain stays
    continuous and ``/audit_logs``, ``/explain`` and ``/audit_logs/verify``
    remain valid across resets. The developer-only hard wipe that deletes the
    whole DB file lives in ``scripts/reset_demo.py``, outside this API.
    """
    from scripts.seed_catalog import seed_delegations

    deleted = ["reservations", "idempotency_keys", "checkout_sessions", "delegations"]
    with mutate() as db:
        prior = db.query(AuditEvent).count()
        audit_service.append_event(
            db,
            actor="operator",
            action="demo_reset_requested",
            decision="info",
            reasoning_summary="Demo reset requested by operator",
            payload={
                "clears": deleted,
                "preserves": ["audit_events"],
                "audit_events_before": prior,
            },
        )
        for model in (Reservation, IdempotencyKey, CheckoutSession, Delegation):
            db.query(model).delete()
        data = json.loads((settings_catalog_path()).read_text(encoding="utf-8"))
        for prod in data["products"]:
            for pack in prod["packs"]:
                row = db.get(Pack, pack["pack_id"])
                if row is not None:
                    row.stock_actual = int(pack.get("stock", 0))
        seed_delegations(db)
        # This transaction appends exactly the requested + completed events, so
        # under BEGIN IMMEDIATE the final audit count is deterministically prior + 2.
        audit_service.append_event(
            db,
            actor="operator",
            action="demo_reset_completed",
            decision="info",
            reasoning_summary=(
                "Demo state reset; catalog stock restored; transactions cleared; "
                "audit history preserved"
            ),
            payload={
                "deleted": deleted,
                "reseeded_stock": True,
                "reseeded_delegations": True,
                "audit_events_before": prior,
                "audit_events_after": prior + 2,
                "audit_history": "preserved",
            },
        )
        return {
            "status": "reset_ok",
            "audit_history": "preserved",
            "deleted": deleted,
            "audit_events_preserved": True,
            "audit_events_before": prior,
            "audit_events_after": prior + 2,
        }


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
