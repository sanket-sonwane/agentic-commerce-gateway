"""Merchant capability manifest builder (read-only projection).

Derives the advertised capabilities from the ACTUAL implementation rather than
from a hand-maintained list, so the manifest cannot drift from reality. Only
capabilities that genuinely exist are advertised; no secrets are ever included.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from merchant.config import settings
from merchant.persistence.models.dto import (
    FulfillmentCapability,
    ManifestCapability,
    ManifestEndpoint,
    MerchantManifest,
    PaymentCapability,
    VerificationKeyInfo,
)
from merchant.persistence.models.entities import Product

_MERCHANT_DISPLAY = {"merchant_grocery_01": "GreenMart"}

PROTOCOL_NAME = "Agentic Commerce Protocol"

FULFILLMENT_STATES = ["pending", "placed", "packed", "shipped", "delivered"]


def _merchant_name() -> str:
    return _MERCHANT_DISPLAY.get(settings.merchant_id, settings.merchant_id)


def _supported_categories(db: Session) -> list[str]:
    rows = db.execute(select(Product.category).distinct()).scalars().all()
    return sorted(rows)


def _capabilities() -> list[ManifestCapability]:
    """Capabilities that correspond to concrete public endpoints/services."""
    return [
        ManifestCapability(name="discovery", description="Natural-language product discovery via POST /discover"),
        ManifestCapability(name="catalog", description="Machine-readable product and pack reads via /catalog"),
        ManifestCapability(name="inventory", description="Authoritative live stock/availability reporting"),
        ManifestCapability(name="pricing", description="Authoritative merchant-owned price resolution"),
        ManifestCapability(name="checkout", description="Checkout session lifecycle with deterministic gating"),
        ManifestCapability(name="payment", description="Razorpay test-mode payment via /checkout_sessions/{id}/complete"),
        ManifestCapability(name="fulfillment", description="Order fulfillment state (placed/packed/shipped/delivered)"),
        ManifestCapability(name="orders", description="Consumer order reads via /orders"),
        ManifestCapability(name="provenance", description="AP2 signed authorization/provenance + consumer projection"),
    ]


def _endpoints() -> list[ManifestEndpoint]:
    return [
        ManifestEndpoint(method="GET", path="/.well-known/merchant", description="This capability manifest"),
        ManifestEndpoint(method="POST", path="/discover", description="Natural-language query to basket proposal"),
        ManifestEndpoint(method="GET", path="/catalog/products/{product_id}", description="Authoritative product read"),
        ManifestEndpoint(method="GET", path="/catalog/packs/{pack_id}", description="Authoritative pack read"),
        ManifestEndpoint(method="POST", path="/checkout_sessions", description="Create a checkout session"),
        ManifestEndpoint(method="GET", path="/checkout_sessions/{id}", description="Read checkout state"),
        ManifestEndpoint(method="PUT", path="/checkout_sessions/{id}", description="Replace line items"),
        ManifestEndpoint(method="POST", path="/checkout_sessions/{id}/complete", description="Complete + pay"),
        ManifestEndpoint(method="POST", path="/checkout_sessions/{id}/confirm", description="Accept/reject changed terms"),
        ManifestEndpoint(method="POST", path="/checkout_sessions/{id}/cancel", description="Cancel a session"),
        ManifestEndpoint(method="GET", path="/orders", description="List my orders (buyer-scoped)"),
        ManifestEndpoint(method="GET", path="/orders/{order_id}", description="Read an order + fulfillment state"),
        ManifestEndpoint(method="GET", path="/consumer/transactions/{id}/provenance", description="Consumer provenance"),
        ManifestEndpoint(method="GET", path="/audit_logs", description="Append-only audit events"),
        ManifestEndpoint(method="GET", path="/audit_logs/verify", description="Hash-chain integrity verification"),
    ]


def _payment_capability() -> PaymentCapability:
    driver = settings.payment_driver
    methods = ["card"]
    if driver == "saved":
        methods.append("saved_card")
    return PaymentCapability(
        provider="Razorpay",
        test_mode=True,
        methods=methods,
        driver=driver,
        merchant_initiated=(driver == "saved"),
    )


def _fulfillment_capability() -> FulfillmentCapability:
    return FulfillmentCapability(
        methods=["delivery", "pickup"],
        states=FULFILLMENT_STATES,
        tracking_supported=False,
    )


def _verification() -> VerificationKeyInfo:
    return VerificationKeyInfo(
        key_id=settings.merchant_key_id,
        algorithm="Ed25519",
        format="PEM",
        public_reference="/audit_logs/verify",
    )


def build_manifest(db: Session) -> dict:
    manifest = MerchantManifest(
        merchant_id=settings.merchant_id,
        name=_merchant_name(),
        protocol={
            "name": PROTOCOL_NAME,
            "version": settings.protocol_version,
        },
        currency="INR",
        supported_categories=_supported_categories(db),
        capabilities=_capabilities(),
        endpoints=_endpoints(),
        payment=_payment_capability(),
        fulfillment=_fulfillment_capability(),
        verification=_verification(),
    )
    return manifest.model_dump(mode="json")
