"""Append-only audit trail with SHA-256 hash chaining (PRD §31–32).

    entry_n.hash = SHA256(canonical_content + entry_(n-1).hash)

No application workflow ever updates or deletes rows; the API only appends.
"""
import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from merchant.persistence.db import utcnow
from merchant.persistence.models.entities import AuditEvent


def _canonical(event: AuditEvent) -> str:
    content = {
        "event_id": event.event_id,
        "checkout_session_id": event.checkout_session_id,
        "actor": event.actor,
        "action": event.action,
        "decision": event.decision,
        "rules_fired": json.loads(event.rules_fired),
        "reasoning_summary": event.reasoning_summary,
        "payload": json.loads(event.payload),
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
    }
    return json.dumps(content, sort_keys=True, separators=(",", ":"))


def append_event(
    db: Session,
    *,
    actor: str,
    action: str,
    checkout_session_id: str | None = None,
    decision: str | None = None,
    rules_fired: list[str] | None = None,
    reasoning_summary: str = "",
    payload: dict | None = None,
) -> AuditEvent:
    last = db.execute(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)).scalar_one_or_none()
    prev_hash = last.hash if last else ""

    event = AuditEvent(
        event_id=f"evt_{uuid.uuid4().hex[:12]}",
        checkout_session_id=checkout_session_id,
        actor=actor,
        action=action,
        decision=decision,
        rules_fired=json.dumps(rules_fired or []),
        reasoning_summary=reasoning_summary,
        payload=json.dumps(payload or {}, default=str),
        prev_hash=prev_hash,
        timestamp=utcnow(),
    )
    event.hash = hashlib.sha256((_canonical(event) + prev_hash).encode("utf-8")).hexdigest()
    db.add(event)
    db.flush()
    return event


def events_for_session(db: Session, checkout_session_id: str) -> list[AuditEvent]:
    return list(
        db.execute(
            select(AuditEvent)
            .where(AuditEvent.checkout_session_id == checkout_session_id)
            .order_by(AuditEvent.seq.asc())
        ).scalars().all()
    )


def all_events(db: Session, limit: int = 500) -> list[AuditEvent]:
    return list(
        db.execute(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(limit)).scalars().all()
    )


def verify_chain(db: Session) -> tuple[bool, int, int | None]:
    """Recompute the full chain. Returns (intact, entries_checked, first_broken_seq)."""
    events = list(db.execute(select(AuditEvent).order_by(AuditEvent.seq.asc())).scalars().all())
    prev_hash = ""
    for event in events:
        expected = hashlib.sha256((_canonical(event) + prev_hash).encode("utf-8")).hexdigest()
        if event.prev_hash != prev_hash or event.hash != expected:
            return False, len(events), event.seq
        prev_hash = event.hash
    return True, len(events), None


def signed_head_anchor(db: Session) -> dict | None:
    """AP2 P2: sign the current audit-chain head so suffix-rewrites are provable.

    The signature is over the head hash only — it cannot prove the whole chain,
    but it anchors the moment in time a given head was witnessed. Returns None
    when there are no events yet.
    """
    head = db.execute(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)).scalar_one_or_none()
    if head is None:
        return None
    from merchant.ap2 import crypto_service

    payload = {"head_hash": head.hash, "seq": head.seq}
    return {
        "seq": head.seq,
        "head_hash": head.hash,
        "signature": crypto_service.sign(payload),
        "key_id": crypto_service.key_id(),
    }
