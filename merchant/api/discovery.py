"""POST /discover"""
from fastapi import APIRouter
from sqlalchemy.orm import Session

from merchant_agent.db import mutate
from merchant_agent.models.dto import DiscoverRequest, DiscoveryResponse
from merchant_agent.services import discovery_service

router = APIRouter()


@router.post("/discover", response_model=DiscoveryResponse)
def discover(body: DiscoverRequest):
    # Uses a mutating (committing) transaction: discovery itself only reads the
    # catalog but MUST persist its audit events.
    with mutate() as db:
        result = discovery_service.discover(
            db,
            query=body.query,
            buyer_agent_id=body.buyer_agent_id,
            exclude_pack_ids=body.exclude_pack_ids,
            deprioritize_product_ids=body.deprioritize_product_ids,
            exclude_product_ids=body.exclude_product_ids,
            skip_llm_parse=body.skip_llm_parse,
        )
    meta = result.pop("_meta", {})
    result["parser"] = meta.get("parsed_by", "keyword-fallback")
    return DiscoveryResponse(**result)
