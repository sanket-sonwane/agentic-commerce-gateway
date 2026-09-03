"""Background reconciliation for payment_pending sessions.

Razorpay test-mode ``/payments/create/recurring`` debits are created at the bank
("Amount yet to be authenticated by the bank") and captured asynchronously
minutes later. The worker re-checks pending sessions on a cadence and finalizes
them through the same choke point a manual ``/reconcile`` uses, so the local
dashboard converges with Razorpay without operator action.

Safety: the worker never initiates a charge. It reads provider truth and, only
for an already-authorized payment, attempts capture of that same payment.
Network probes run OUTSIDE any write transaction; only the terminal transition
opens a serialized ``mutate()``. Sessions pinned to the sim driver are ignored.
"""
import asyncio
import logging
from typing import Awaitable, Callable

from merchant.config import settings
from merchant.persistence.db import SessionLocal, mutate
from merchant.persistence.models.entities import CheckoutSession

logger = logging.getLogger("merchant.reconcile")

RECONCILE_LOOP_SECONDS = 30.0
MAX_SESSIONS_PER_PASS = 50


def _pending_sessions() -> list[CheckoutSession]:
    session = SessionLocal()
    try:
        return (
            session.query(CheckoutSession)
            .filter(
                CheckoutSession.status == "payment_pending",
                CheckoutSession.razorpay_order_id.isnot(None),
            )
            .order_by(CheckoutSession.updated_at.asc())
            .limit(MAX_SESSIONS_PER_PASS)
            .all()
        )
    finally:
        session.close()


def reconcile_all_pending_once() -> int:
    """One non-blocking pass over pending sessions; returns count finalised."""
    if settings.payment_driver != "saved" or not settings.razorpay_key_id:
        return 0
    from merchant.payment import payment_service

    sessions = _pending_sessions()
    if not sessions:
        return 0
    outcomes = {}
    for cs in sessions:
        try:
            outcomes[cs.checkout_session_id] = payment_service.reconcile_pending(
                cs.razorpay_order_id or "",
                payment_id=cs.razorpay_payment_id,
            )
        except Exception:  # noqa: BLE001 — one session must never break a pass
            logger.warning("Reconcile check failed for %s", cs.checkout_session_id, exc_info=True)
    if not outcomes:
        return 0

    from merchant.domain.checkout.payment import _finalize_payment

    finished = 0
    with mutate() as db:
        for session_id, outcome in outcomes.items():
            cs = db.get(CheckoutSession, session_id)
            if cs is None or cs.status != "payment_pending":
                continue
            if outcome.status not in ("completed", "declined"):
                continue  # sticky pending: do not re-issue receipts on a no-op
            _finalize_payment(db, cs, outcome)
            finished += 1
            logger.info(
                "Auto-reconciled %s -> %s (payment %s)",
                session_id,
                outcome.status,
                outcome.payment_id,
            )
    return finished


async def _loop(stop: asyncio.Event, interval: float) -> None:
    while True:
        try:
            await asyncio.to_thread(reconcile_all_pending_once)
        except Exception:  # noqa: BLE001
            logger.exception("Reconciliation pass failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def start_reconcile_worker(
    interval: float = RECONCILE_LOOP_SECONDS,
) -> Callable[[], Awaitable[None]]:
    """Start the background worker; returns an async stop() callable.

    Runs one immediate pass (boot adoption of already-pending sessions) before
    starting the periodic loop.
    """
    stop = asyncio.Event()
    task = asyncio.create_task(_loop(stop, interval=interval))
    await asyncio.to_thread(reconcile_all_pending_once)

    async def stop_worker() -> None:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return stop_worker