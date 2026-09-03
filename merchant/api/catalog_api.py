"""Public machine-readable catalog endpoints (GET /catalog/products/{id}, /catalog/packs/{id}).

Thin HTTP boundary over the authoritative catalog domain (merchant/domain/
public_catalog.py). Returns DTOs only — never ORM objects. Buyer-supplied price
is never accepted or trusted here.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from merchant.persistence.db import read_only
from merchant.persistence.models.dto import CatalogPackOut, CatalogProductOut
from merchant.domain import public_catalog

router = APIRouter(prefix="/catalog")


@router.get("/products/{product_id}", response_model=CatalogProductOut)
def get_product(product_id: str, db: Session = Depends(read_only)):
    return public_catalog.get_public_product(db, product_id)


@router.get("/packs/{pack_id}", response_model=CatalogPackOut)
def get_pack(pack_id: str, db: Session = Depends(read_only)):
    return public_catalog.get_public_pack(db, pack_id)
