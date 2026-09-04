"""Consumer provenance projection endpoint.

A thin read-model for a normal end user. Composes existing authoritative
services (checkout state, signed provenance, signed audit chain, intent
authorization) into a presentation-friendly shape. It is NOT a second AP2
implementation and does no cryptographic verification on its own.

Also hosts the server-side consumer order proxy: ``GET /consumer/orders`` and
``GET /consumer/orders/{order_id}`` mirror the authoritative Order domain
(``/orders``) for the acting demo buyer, with the buyer's ownership delegation
resolved server-side. The browser dashboard calls these routes and never holds
the buyer's delegation token.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from merchant.config import settings
from merchant.persistence.db import read_only
from merchant.persistence.models.dto import ConsumerProvenanceResponse, OrderListResponse, OrderOut
from merchant.control import consumer_projection
from merchant.domain import order as order_service

router = APIRouter()


@router.get("/consumer/transactions/{checkout_session_id}/provenance",
            response_model=ConsumerProvenanceResponse)
def get_consumer_provenance(checkout_session_id: str, db: Session = Depends(read_only)):
    return consumer_projection.build_consumer_projection(db, checkout_session_id)


@router.get("/consumer/orders", response_model=OrderListResponse)
def list_consumer_orders(db: Session = Depends(read_only)):
    """The acting demo buyer's authoritative orders (server-side context)."""
    rows = order_service.list_orders_for_buyer(
        db, settings.demo_buyer_agent_id, settings.demo_delegation_token
    )
    items = [OrderOut(**order_service.serialize_order(o)) for o in rows]
    return OrderListResponse(orders=items, count=len(items))


@router.get("/consumer/orders/{order_id}", response_model=OrderOut)
def get_consumer_order(order_id: str, db: Session = Depends(read_only)):
    """A single authoritative order owned by the acting demo buyer."""
    order = order_service.get_order(db, order_id)
    order_service._check_order_ownership(
        order, settings.demo_buyer_agent_id, settings.demo_delegation_token
    )
    return OrderOut(**order_service.serialize_order(order))
