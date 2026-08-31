"""Idempotency record persistence for checkout ops.

Stores op snapshots keyed by the caller-supplied idempotency key so replays
return the original response instead of side-effecting twice. Threshold-bounded
behavior is unchanged from the original checkout_service module.
"""
import json

from sqlalchemy.orm import Session

from merchant.persistence.models.dto import CompleteResponse
from merchant.persistence.models.entities import CheckoutSession, IdempotencyKey


def _idempotent_replay(db: Session, key: str | None):
    if not key:
        return None
    record = db.get(IdempotencyKey, key)
    if record is None:
        return None
    return json.loads(record.response_snapshot)


def _store_idempotent(db: Session, key: str | None, endpoint: str, cs: CheckoutSession, snapshot: dict) -> None:
    if not key:
        return
    db.merge(
        IdempotencyKey(
            key=key,
            endpoint=endpoint,
            session_id=cs.checkout_session_id,
            response_snapshot=json.dumps(snapshot, default=str),
        )
    )
    db.flush()


def store_complete_idempotency(db: Session, key: str | None, session_id: str, result: CompleteResponse) -> None:
    """Persist the full completion response so replays reproduce it byte-for-byte."""
    if not key:
        return
    db.merge(
        IdempotencyKey(
            key=key,
            endpoint="complete",
            session_id=session_id,
            response_snapshot=json.dumps(result.model_dump(mode="json")),
        )
    )
    db.flush()