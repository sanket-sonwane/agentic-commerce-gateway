"""AP2 provenance + intent-mandate + user-key API (PRD §10–12, §30; Workstream E)."""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from merchant.config import settings
from merchant.persistence.db import mutate, read_only
from merchant.persistence.models.dto import IntentMandateCreate, UserKeyRegister
from merchant.ap2 import authorization_service, crypto_service
from merchant.evidence import audit_service
from merchant.control.delegation_service import validate_delegation
from merchant.ap2.provenance_service import verify_provenance

router = APIRouter()


@router.get("/provenance/{checkout_session_id}")
def get_provenance(checkout_session_id: str, db: Session = Depends(read_only)):
    return verify_provenance(db, checkout_session_id)


@router.post("/mandates/intents", status_code=201)
def create_intent_mandate(body: IntentMandateCreate):
    """Create + sign a buyer Intent Mandate (authorized scope, not a purchase).

    Constraints are derived from the buyer's delegation by default; a caller may
    tighten them via constraints_override (never loosen beyond delegation).
    """
    with mutate() as db:
        delegation = validate_delegation(db, body.buyer_agent_id, body.delegation_token)
        constraints = {
            "max_order_value_paise": delegation.max_order_value * 100,
            "allowed_categories": json.loads(delegation.allowed_categories),
            "max_quantity_per_item": delegation.max_quantity_per_item,
        }
        over = body.constraints_override or {}
        if "max_order_value_paise" in over:
            constraints["max_order_value_paise"] = min(
                constraints["max_order_value_paise"], int(over["max_order_value_paise"])
            )
        if "allowed_categories" in over:
            allowed = set(constraints["allowed_categories"])
            constraints["allowed_categories"] = sorted(
                c for c in over["allowed_categories"] if c in allowed
            )
        if over.get("max_quantity_per_item") is not None:
            constraints["max_quantity_per_item"] = min(
                constraints["max_quantity_per_item"], int(over["max_quantity_per_item"])
            )

        m = authorization_service.build_intent_mandate(
            db,
            buyer_agent_id=body.buyer_agent_id,
            merchant_id=settings.merchant_id,
            intent={"description": body.intent},
            constraints=constraints,
        )
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="intent_mandate_created",
            checkout_session_id=None,
            decision="info",
            reasoning_summary=f"Signed Intent Mandate {m.mandate_id} for {body.buyer_agent_id}",
            payload={
                "mandate_id": m.mandate_id,
                "canonical_payload_hash": m.canonical_payload_hash,
                "expires_at": authorization_service._iso(m.expires_at),
            },
        )
        mandate_out = {
            "mandate_id": m.mandate_id,
            "type": "intent",
            "merchant_id": m.merchant_id,
            "buyer_agent_id": m.buyer_agent_id,
            "intent": json.loads(m.intent),
            "constraints": json.loads(m.constraints),
            "issued_at": authorization_service._iso(m.issued_at),
            "expires_at": authorization_service._iso(m.expires_at),
            "nonce": m.nonce,
            "version": m.version,
            "canonical_payload_hash": m.canonical_payload_hash,
            "key_id": m.key_id,
            "signature": m.signature,
            "verifiable_public_key": crypto_service.public_key_pem(),
        }
    return mandate_out


@router.post("/user_keys", status_code=201)
def register_user_key(body: UserKeyRegister):
    """Register a buying human's Ed25519 PUBLIC key (Workstream E).

    Only the public key is registered; the private key stays exclusively in
    the user's browser localStorage and is never transmitted. The merchant can
    cryptographically VERIFY user signatures but can never FORGE one. A
    re-registration for the same user rotates the key (previous key replaced).
    """
    with mutate() as db:
        try:
            row = authorization_service.register_user_key(
                db,
                user_id=body.user_id,
                key_id=body.key_id,
                public_key_pem=body.public_key_pem,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid public key: {exc}") from exc
        audit_service.append_event(
            db,
            actor="user",
            action="user_key_registered",
            checkout_session_id=None,
            decision="info",
            reasoning_summary=(
                f"User {row.user_id} registered public key {row.key_id} "
                "(private key stays in the user's browser; never transmitted)"
            ),
            payload={"user_id": row.user_id, "key_id": row.key_id, "status": row.status},
        )
    return {"user_id": row.user_id, "key_id": row.key_id, "status": row.status, "registered": True}
