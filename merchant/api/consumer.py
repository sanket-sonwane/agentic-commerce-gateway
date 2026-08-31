"""Consumer provenance projection endpoint.

A thin read-model for a normal end user. Composes existing authoritative
services (checkout state, signed provenance, signed audit chain, intent
authorization) into a presentation-friendly shape. It is NOT a second AP2
implementation and does no cryptographic verification on its own.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from merchant.persistence.db import read_only
from merchant.persistence.models.dto import ConsumerProvenanceResponse
from merchant.control import consumer_projection

router = APIRouter()


@router.get("/consumer/transactions/{checkout_session_id}/provenance",
            response_model=ConsumerProvenanceResponse)
def get_consumer_provenance(checkout_session_id: str, db: Session = Depends(read_only)):
    return consumer_projection.build_consumer_projection(db, checkout_session_id)
