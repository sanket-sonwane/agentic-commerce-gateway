"""GET /audit_logs + chain verification."""
import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from merchant_agent.db import read_only
from merchant_agent.models.dto import AuditEventOut, AuditVerifyResponse
from merchant_agent.services import audit_service

router = APIRouter()


def _to_out(e) -> AuditEventOut:
    return AuditEventOut(
        seq=e.seq,
        event_id=e.event_id,
        checkout_session_id=e.checkout_session_id,
        actor=e.actor,
        action=e.action,
        decision=e.decision,
        rules_fired=json.loads(e.rules_fired),
        reasoning_summary=e.reasoning_summary,
        payload=json.loads(e.payload),
        prev_hash=e.prev_hash,
        hash=e.hash,
        timestamp=e.timestamp,
    )


@router.get("/audit_logs")
def list_audit_logs(
    checkout_session_id: str | None = Query(default=None),
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(read_only),
):
    if checkout_session_id:
        events = audit_service.events_for_session(db, checkout_session_id)
    else:
        events = list(reversed(audit_service.all_events(db, limit=limit)))
    return {"events": [_to_out(e).model_dump() for e in events]}


@router.get("/audit_logs/verify")
def verify_audit_logs(db: Session = Depends(read_only)):
    intact, checked, broken = audit_service.verify_chain(db)
    return AuditVerifyResponse(intact=intact, entries_checked=checked, first_broken_seq=broken)


@router.get("/explain/{checkout_session_id}")
def explain(checkout_session_id: str, db: Session = Depends(read_only)):
    from merchant_agent.services.explain_service import explain_session

    return explain_session(db, checkout_session_id)
