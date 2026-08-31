"""Checkout session lifecycle domain package (PRD §13–20, §26).

Split from merchant/domain/checkout_service.py into single-concern modules:

    pricing.py      line-item resolution, totals, canonical cart hash
    session.py      lifecycle constants, ownership/transition guards, serialization
    gating.py       deterministic policy-gate helpers
    idempotency.py  replay/store for idempotency keys
    create.py / update.py / cancel.py / confirm.py   session mutations
    complete.py     completion ladder (highest-risk operation)
    payment.py      finalizer, payment-method records, reconciliation

The public API is re-exported here so consumers import from
``merchant.domain.checkout``.
"""
from merchant.domain.checkout.cancel import cancel_checkout
from merchant.domain.checkout.complete import complete_checkout
from merchant.domain.checkout.confirm import confirm_checkout
from merchant.domain.checkout.create import create_checkout
from merchant.domain.checkout.idempotency import store_complete_idempotency
from merchant.domain.checkout.payment import (
    finalize_with_outcome,
    get_or_create_payment_method,
    reconcile_session,
    settle_probe,
)
from merchant.domain.checkout.pricing import compute_totals, computes_cart_hash, resolve_line_items
from merchant.domain.checkout.session import get_session, get_session_status, serialize_session
from merchant.domain.checkout.update import update_checkout

__all__ = [
    "cancel_checkout",
    "complete_checkout",
    "compute_totals",
    "computes_cart_hash",
    "confirm_checkout",
    "create_checkout",
    "finalize_with_outcome",
    "get_or_create_payment_method",
    "get_session",
    "get_session_status",
    "reconcile_session",
    "resolve_line_items",
    "serialize_session",
    "settle_probe",
    "store_complete_idempotency",
    "update_checkout",
]