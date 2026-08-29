"""Delegated-authority registry (prototype model per PRD §22)."""
import json

from sqlalchemy.orm import Session

from merchant_agent.db import utcnow
from merchant_agent.models.dto import ErrorCode, ProtocolError
from merchant_agent.models.entities import Delegation


def get_delegation(db: Session, delegation_token: str) -> Delegation | None:
    return db.get(Delegation, delegation_token)


def validate_delegation(db: Session, buyer_agent_id: str, delegation_token: str) -> Delegation:
    """Resolve and validate a delegation token. Raises ProtocolError on failure."""
    delegation = get_delegation(db, delegation_token)
    if delegation is None or delegation.buyer_agent_id != buyer_agent_id or delegation.status != "active":
        raise ProtocolError(
            ErrorCode.UNAUTHORIZED_AGENT,
            "Unknown delegation token for this buyer agent",
            http_status=401,
        )
    if delegation.expires_at <= utcnow():
        raise ProtocolError(
            ErrorCode.DELEGATION_EXPIRED,
            f"Delegation {delegation.delegation_id} expired at "
            f"{delegation.expires_at.isoformat()}Z",
            http_status=401,
        )
    return delegation


def allowed_categories(delegation: Delegation) -> list[str]:
    return json.loads(delegation.allowed_categories)
