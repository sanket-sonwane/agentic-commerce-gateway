"""Consumer order API (M5) — public machine-readable order reads.

The list is strictly scoped to the requesting buyer/delegation context: the
caller must present their `buyer_agent_id` + `delegation_token` (the repo's
ownership model) and cannot read another buyer's orders by passing an arbitrary
buyer id. The buyer never needs dashboard-only APIs to learn fulfillment state.
"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from merchant.domain import order as order_service
from merchant.persistence.db import read_only
from merchant.persistence.models.dto import OrderListResponse, OrderOut

router = APIRouter(prefix="/orders")


class OrderScope(BaseModel):
    buyer_agent_id: str
    delegation_token: str


@router.get("", response_model=OrderListResponse)
def list_orders(
    buyer_agent_id: str = Query(..., description="The requesting buyer agent id"),
    delegation_token: str = Query(..., description="The buyer's delegation token (ownership scope)"),
    db: Session = Depends(read_only),
):
    """List the requesting buyer's orders. Scoped to buyer+delegation."""
    rows = order_service.list_orders_for_buyer(db, buyer_agent_id, delegation_token)
    items = [OrderOut(**order_service.serialize_order(o)) for o in rows]
    return OrderListResponse(orders=items, count=len(items))


@router.get("/{order_id}", response_model=OrderOut)
def get_order(
    order_id: str,
    buyer_agent_id: str = Query(..., description="The requesting buyer agent id"),
    delegation_token: str = Query(..., description="The buyer's delegation token (ownership scope)"),
    db: Session = Depends(read_only),
):
    order = order_service.get_order(db, order_id)
    order_service._check_order_ownership(order, buyer_agent_id, delegation_token)
    return OrderOut(**order_service.serialize_order(order))
