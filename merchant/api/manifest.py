"""Merchant capability manifest endpoint (GET /.well-known/merchant).

A read-only machine-readable projection of the merchant's public capabilities.
Derived from actual implementation (merchant/domain/manifest.py) — never a
hand-maintained list that can drift from reality. No secrets exposed.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from merchant.persistence.db import read_only
from merchant.domain import manifest as manifest_service

router = APIRouter()


@router.get("/.well-known/merchant")
def get_merchant_manifest(db: Session = Depends(read_only)):
    return manifest_service.build_manifest(db)
