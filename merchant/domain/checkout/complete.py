"""Checkout completion ladder — the highest-risk operation (PRD §19–20).

The ladder revalidates EVERYTHING immediately before payment (G4/FR-10):
session -> ownership -> state -> price re-check -> delegation -> stock
(strict actual+available) -> policy -> signed user intent (Workstream E) ->
AP2 Execution Mandate build + verify -> ONLY THEN Razorpay. The AP2/mandate
and Razorpay calls are imported lazily so this module never creates import
cycles with the ap2/payment layers.
"""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from merchant.config import settings
from merchant.control import gating_service
from merchant.control.delegation_service import validate_delegation
from merchant.domain import inventory_service
from merchant.domain.checkout.gating import _gate
from merchant.domain.checkout.idempotency import _idempotent_replay
from merchant.domain.checkout.payment import _finalize_payment, get_or_create_payment_method
from merchant.domain.checkout.pricing import _compute_totals, _resolve_items
from merchant.domain.checkout.session import (
    _assert_transition,
    _check_ownership,
    get_session,
    serialize_session,
)
from merchant.evidence import audit_service
from merchant.payment.payment_service import PaymentOutcome
from merchant.persistence.models.dto import CompleteResponse, ErrorCode, GatingResult, ProtocolError
from merchant.persistence.models.entities import CheckoutSession, Pack, Reservation


def complete_checkout(
    db: Session,
    checkout_session_id: str,
    *,
    buyer_agent_id: str,
    delegation_token: str,
    idempotency_key: str | None = None,
    intent_mandate_id: str | None = None,
    user_assertion: dict | None = None,
) -> tuple[CompleteResponse, bool]:
    """The highest-risk operation. Returns (response, replayed_flag).

    Ladder: session -> ownership -> state -> price re-check -> delegation ->
    stock (strict actual+available) -> policy -> ONLY THEN Razorpay.
    """
    replay = _idempotent_replay(db, idempotency_key)
    if replay:
        return CompleteResponse(**replay), True

    cs = get_session(db, checkout_session_id)
    _check_ownership(cs, buyer_agent_id, delegation_token)

    audit_service.append_event(
        db,
        actor="buyer_agent",
        action="complete_requested",
        checkout_session_id=cs.checkout_session_id,
        payload={"idempotency_key": idempotency_key},
    )

    # Already-completed sessions return their existing result without a new charge.
    if cs.status == "completed":
        out = CompleteResponse(checkout_session=serialize_session(db, cs))
        return out, True

    _assert_transition(cs.status, "completed")

    resolved = json.loads(cs.line_items)
    old_totals = json.loads(cs.totals)

    def _fail(code: ErrorCode, message: str, rules: list[str], summary: str) -> CompleteResponse:
        inventory_service.release_session_reservations(
            db,
            cs.checkout_session_id,
            reason="Final validation failed; reservation was released",
        )
        cs.status = "declined"
        cs.decline_code = code.value
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="final_validation",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=rules,
            reasoning_summary=summary,
        )
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="checkout_declined",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=[code.value],
            reasoning_summary=f"{code.value}: Razorpay NOT called",
        )
        db.flush()
        error = {
            "code": code.value,
            "message": message,
            "retryable": code == ErrorCode.STOCK_UNAVAILABLE,
            "details": {},
        }
        return CompleteResponse(
            checkout_session=serialize_session(db, cs),
            error=error,
        )

    # --- 1. Price/terms re-check against the authoritative catalog ---
    fresh = _resolve_items(db, [i for i in resolved])
    new_totals = _compute_totals(fresh)
    if new_totals["total"] != old_totals["total"]:
        cs.status = "requires_confirmation"
        cs.gating_result = GatingResult(passed=True, checks=[]).model_dump_json()
        change = {"type": "price_changed", "old_total": old_totals["total"], "new_total": new_totals["total"]}
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="final_validation",
            checkout_session_id=cs.checkout_session_id,
            decision="hold",
            rules_fired=["price_changed"],
            reasoning_summary=f"Total changed ₹{old_totals['total']} -> ₹{new_totals['total']}; confirmation required",
        )
        db.flush()
        return CompleteResponse(
            checkout_session=serialize_session(db, cs),
            change=change,
            error={
                "code": ErrorCode.PRICE_CHANGED.value,
                "message": "Transaction terms changed; buyer confirmation required",
                "retryable": True,
                "details": change,
            },
        ), False

    # --- 2. Delegation revalidation ---
    try:
        delegation = validate_delegation(db, buyer_agent_id, delegation_token)
    except ProtocolError as exc:
        return _fail(exc.code, exc.message, [exc.code.value], f"Delegation invalid at completion: {exc.message}"), False

    # --- 3. Stock revalidation (strict: actual AND available) ---
    stock_failures: list[str] = []
    for item in fresh:
        pack = db.get(Pack, item["pack_id"])
        available = inventory_service.available_stock(db, pack.pack_id, pack.stock_actual)
        own_reservation = db.execute(
            select(Reservation).where(
                Reservation.checkout_session_id == cs.checkout_session_id,
                Reservation.pack_id == pack.pack_id,
                Reservation.status == "active",
            )
        ).scalars().first()
        reserved_for_us = own_reservation.quantity if own_reservation else 0
        effective_available = available + reserved_for_us
        # A reservation does not override reality: actual stock must still cover it.
        if pack.stock_actual < item["quantity"] or effective_available < item["quantity"]:
            stock_failures.append(
                f"{item['name']}: requested={item['quantity']}, available={min(available, effective_available)}"
            )
    if stock_failures:
        return _fail(
            ErrorCode.STOCK_UNAVAILABLE,
            "; ".join(stock_failures),
            ["stock_check:fail"],
            "Stock disappeared between checkout and completion; reservation invalidated by final validation",
        ), False

    # --- 4. Policy re-run ---
    gating = _gate(db, delegation, fresh, new_totals)
    if not gating.passed:
        code = gating_service.first_failure_code(gating)
        failed = [c.detail for c in gating.checks if c.result == "fail"]
        return _fail(
            code,
            "; ".join(failed),
            [f"{c.rule}:{c.result}" for c in gating.checks],
            "Policy re-evaluation failed at completion",
        ), False

    audit_service.append_event(
        db,
        actor="merchant_agent",
        action="final_validation",
        checkout_session_id=cs.checkout_session_id,
        decision="approved",
        rules_fired=[f"{c.rule}:{c.result}" for c in gating.checks],
        reasoning_summary="Price, delegation, stock and policy all valid at completion",
        payload={"checks": [c.model_dump() for c in gating.checks]},
    )

    # --- 4.5 Workstream E: user-signed intent assertion (root of authority) ---
    # If the buying human's consent was cryptographically signed (browser holds
    # the private key), EVERY check is enforced BEFORE any execution mandate,
    # Razorpay order, or payment exists. A single failed check blocks the
    # transaction entirely (PAYMENT_BLOCKED). Absent == legacy authorization
    # path, explicitly labeled as such in provenance.
    user_assertion_id: str | None = None
    user_assertion_hash: str | None = None
    if user_assertion:
        from merchant.ap2.authorization_service import (
            store_user_assertion,
            verify_user_assertion,
        )

        # The ceiling check compares against the exact execution total; derive
        # paise the same way the Execution Mandate does a few steps below.
        ua_total_paise = int(round(new_totals["total"] * 100))
        ua_checks, ua_detail, ua_assertion_id = verify_user_assertion(
            db,
            user_assertion,
            checkout_session_id=checkout_session_id,
            buyer_agent_id=buyer_agent_id,
            total_paise=ua_total_paise,
        )
        if not all(ua_checks.values()):
            failed = ", ".join(name for name, ok in ua_checks.items() if not ok)
            audit_service.append_event(
                db,
                actor="user",
                action="user_assertion_rejected",
                checkout_session_id=cs.checkout_session_id,
                decision="declined",
                rules_fired=["authorization_verify:user_assertion"],
                reasoning_summary=(
                    f"User intent assertion rejected ({failed}); "
                    "no mandate/order/payment created"
                ),
                payload={"checks": ua_checks},
            )
            return _fail(
                ErrorCode.PAYMENT_BLOCKED,
                f"User authorization could not be verified: {failed}",
                ["authorization_verify:user_assertion"],
                f"User intent assertion failed ({failed}); Razorpay NOT called",
            ), False
        stored = store_user_assertion(
            db,
            user_assertion,
            key_id=str(user_assertion["user_key_id"]),
            signature=str(user_assertion["signature"]),
        )
        user_assertion_id = stored.assertion_id
        user_assertion_hash = stored.canonical_payload_hash
        audit_service.append_event(
            db,
            actor="user",
            action="user_assertion_verified",
            checkout_session_id=cs.checkout_session_id,
            decision="approved",
            rules_fired=["authorization_verify:user_assertion"],
            reasoning_summary=(
                f"User {stored.user_id} signed consent verified "
                f"(key {stored.user_key_id}, ceiling ₹{ua_total_paise / 100}); "
                "single-use consent consumed for this execution"
            ),
            payload={
                "assertion_id": stored.assertion_id,
                "user_key_id": stored.user_key_id,
                "delegate_id": stored.delegate_id,
                "canonical_payload_hash": stored.canonical_payload_hash,
                "max_amount_paise": ua_total_paise,
                "checks": ua_checks,
            },
        )

    # --- 5. AP2: build + sign the Execution Mandate (PRD §14–15) ---
    # Freeze the exact basket about to be charged, then verify it before any
    # money movement. Fail closed: any verification failure -> NO Razorpay call.
    from merchant.ap2 import authorization_service, crypto_service

    amount_paise = new_totals["total"] * 100
    mandate_items = [
        {
            "product_id": i["product_id"],
            "pack_id": i["pack_id"],
            "quantity": int(i["quantity"]),
            "unit_price_paise": int(i["unit_price"]) * 100,
            "category": i.get("category", ""),
        }
        for i in fresh
    ]

    # AP2 intent binding (RE-3): if an Intent Mandate is supplied, the execution
    # MUST fall within its signed constraints. Verified against the intent BEFORE
    # any money movement — out-of-scope transactions are blocked fail-closed.
    if intent_mandate_id:
        scope_ok, scope_detail = authorization_service.verify_intent_scope(
            db,
            intent_mandate_id,
            buyer_agent_id=buyer_agent_id,
            items=mandate_items,
            total_paise=amount_paise,
        )
        if not scope_ok:
            return _fail(
                ErrorCode.PAYMENT_BLOCKED,
                f"Execution outside signed intent scope: {scope_detail}",
                ["authorization_verify:intent_scope"],
                f"Intent Mandate {intent_mandate_id} does not authorize this execution; Razorpay NOT called",
            ), False
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="intent_scope_verified",
            checkout_session_id=cs.checkout_session_id,
            decision="approved",
            reasoning_summary=scope_detail,
            payload={"intent_mandate_id": intent_mandate_id},
        )

    exec_mandate = authorization_service.build_execution_mandate(
        db,
        cs,
        buyer_agent_id=buyer_agent_id,
        items=mandate_items,
        total_paise=amount_paise,
        intent_mandate_id=intent_mandate_id,
        user_assertion_id=user_assertion_id,
        user_assertion_hash=user_assertion_hash,
    )

    policy_decision = authorization_service.sign_policy_decision(
        db,
        cs,
        exec_mandate.mandate_id,
        decision="approved" if gating.passed else "rejected",
        checks=[c.model_dump() for c in gating.checks],
        authorized_amount_paise=amount_paise,
    )

    verify_checks = authorization_service.verify_execution_mandate(
        db, exec_mandate, cs, buyer_agent_id=buyer_agent_id
    )
    failed_checks = [name for name, ok in verify_checks.items() if not ok]
    if failed_checks:
        # Fail closed: unsigned/unbound transaction must never reach Razorpay.
        return _fail(
            ErrorCode.PAYMENT_BLOCKED,
            f"Authorization verification failed: {', '.join(failed_checks)}",
            ["authorization_verify:fail"],
            "Execution Mandate failed verification; Razorpay NOT called",
        ), False
    audit_service.append_event(
        db,
        actor="merchant_agent",
        action="execution_mandate_created",
        checkout_session_id=cs.checkout_session_id,
        decision="approved",
        reasoning_summary=(
            f"Execution Mandate {exec_mandate.mandate_id} signed + verified "
            f"(authorized ₹{new_totals['total']})"
        ),
        payload={
            "mandate_id": exec_mandate.mandate_id,
            "decision_id": policy_decision.decision_id,
            "authorized_amount_paise": amount_paise,
            "canonical_payload_hash": exec_mandate.canonical_payload_hash,
            "user_assertion_id": exec_mandate.user_assertion_id,
            "user_assertion_hash": exec_mandate.user_assertion_hash,
            "checks": verify_checks,
        },
    )

    # --- 6. Money movement ---
    from merchant.payment import payment_service

    driver = settings.payment_driver
    total_rupees = new_totals["total"]

    # driver=saved: resolve the Razorpay customer BEFORE order creation —
    # mandate-authorization orders must carry customer_id + the token block.
    mandate: dict | None = None
    if driver == "saved":
        try:
            customer = payment_service.ensure_customer(cs.buyer_agent_id)
        except Exception as exc:  # noqa: BLE001 — credentials/config problems
            return _fail(
                ErrorCode.PAYMENT_FAILED,
                f"Razorpay customer setup failed ({exc}); payment not initiated",
                ["customer_setup_failed"],
                f"Customer setup errored: {exc}; Razorpay NOT called",
            ), False
        # Hard guarantee: a mandate order without a real customer id is invalid
        # and would silently produce non-recurring tokens downstream.
        if not customer.get("id"):
            return _fail(
                ErrorCode.PAYMENT_FAILED,
                "Razorpay customer setup returned no id; payment not initiated",
                ["customer_setup_failed"],
                "ensure_customer returned no id; Razorpay NOT called",
            ), False
        mandate = {"customer_id": customer["id"]}

    # Retry guard: if a previous attempt already created an open order for this
    # live session (modal dismissed, agent retried), reuse it — never duplicate.
    if cs.status == "requires_payment" and cs.razorpay_order_id:
        # C-1: never reuse an order whose persisted amount disagrees with the
        # authorized amount — fail closed rather than charge a diverged order.
        if cs.razorpay_order_amount is not None and cs.razorpay_order_amount != amount_paise:
            return _fail(
                ErrorCode.PAYMENT_BLOCKED,
                "Existing Razorpay order amount does not match the authorized amount",
                ["authorization_verify:order_amount"],
                "Order reuse attempt with mismatched amount; Razorpay payment blocked",
            ), False
        order = {"id": cs.razorpay_order_id}
        cs.payment_driver = driver
    else:
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="razorpay_order_creation",
            checkout_session_id=cs.checkout_session_id,
            decision="info",
            reasoning_summary=f"Creating Razorpay order for ₹{total_rupees}",
        )
        try:
            order = payment_service.create_order(
                cs.checkout_session_id, total_rupees, mandate=mandate
            )
        except Exception as exc:  # noqa: BLE001
            cs.status = "payment_pending"
            audit_service.append_event(
                db,
                actor="razorpay",
                action="payment_pending",
                checkout_session_id=cs.checkout_session_id,
                decision="pending",
                reasoning_summary=f"Order creation errored ({exc}); reconciliation required; no blind retry",
            )
            db.flush()
            return CompleteResponse(
                checkout_session=serialize_session(db, cs),
                error={
                    "code": ErrorCode.PAYMENT_PENDING.value,
                    "message": "Payment could not be initiated; awaiting reconciliation",
                    "retryable": False,
                    "details": {},
                },
            ), False

        echo_amount = int(order.get("amount") or amount_paise)
        # C-1: the amount actually recorded at Razorpay must equal the authorized
        # amount. Reject any divergence fail-closed (never persist a stray order).
        if echo_amount != amount_paise:
            return _fail(
                ErrorCode.PAYMENT_BLOCKED,
                "Razorpay order amount does not match the authorized amount",
                ["authorization_verify:order_amount"],
                "Gateway order amount diverged from authorized amount; payment blocked",
            ), False

        cs.razorpay_order_id = order["id"]
        cs.razorpay_order_amount = echo_amount
        cs.payment_driver = driver
        audit_service.append_event(
            db,
            actor="razorpay",
            action="razorpay_order_created",
            checkout_session_id=cs.checkout_session_id,
            decision="info",
            reasoning_summary=(
                f"Order {order['id']} created for ₹{total_rupees}"
                + (" (authorization/mandate order)" if mandate else "")
            ),
            payload={
                "order_id": order["id"],
                "driver": driver,
                "mandate_order": bool(mandate),
                # Diagnostic pair: what we sent vs what Razorpay stored.
                "expected_customer_id": mandate["customer_id"] if mandate else None,
                "echoed_customer_id": order.get("customer_id"),
            },
        )

    if driver == "sim":
        outcome: PaymentOutcome = payment_service.execute_sim_capture(order["id"], total_rupees)
        return _finalize_payment(db, cs, outcome), False

    # checkout | saved — human authorization via Razorpay Checkout (token when available)
    method = get_or_create_payment_method(db, cs.buyer_agent_id)

    if driver == "saved" and method.razorpay_token_id and method.status == "active":
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="razorpay_token_charge",
            checkout_session_id=cs.checkout_session_id,
            decision="info",
            reasoning_summary=(
                f"Charging saved {method.card_network or 'card'} ••••{method.card_last4 or '????'} "
                f"server-side via token"
            ),
        )
        outcome = payment_service.charge_with_token(
            order["id"],
            amount_paise,
            method.razorpay_customer_id or "",
            method.razorpay_token_id,
            buyer_agent_id=cs.buyer_agent_id,
        )
        if outcome.status != "declined":
            return _finalize_payment(db, cs, outcome), False
        if getattr(outcome, "rejection_class", None) == "auth_required":
            # Token cannot be charged merchant-initiated (e.g. saved without a
            # recurring mandate). Park it and re-register via the next modal.
            method.status = "needs_reauth"
            audit_service.append_event(
                db,
                actor="merchant_agent",
                action="token_needs_reauth",
                checkout_session_id=cs.checkout_session_id,
                decision="info",
                reasoning_summary=(
                    "Saved-card token not merchant-chargeable "
                    f"({outcome.message}); one-time re-authorization required; "
                    "falling back to Razorpay Checkout modal on the same order "
                    "(likely cause: E-Mandate disabled on the Razorpay account, "
                    "or the card does not support mandates)"
                ),
                payload={"gateway_error": outcome.raw},
            )
            db.flush()
        else:
            # Gateway rejected the saved-card token (e.g. network unsupported in test
            # mode). Do NOT terminally decline the order — fall back to human
            # authorization via the Razorpay Checkout modal on the SAME open order.
            audit_service.append_event(
                db,
                actor="merchant_agent",
                action="token_charge_fallback",
                checkout_session_id=cs.checkout_session_id,
                decision="info",
                reasoning_summary=(
                    f"Saved-card charge rejected ({outcome.message}); falling back to "
                    "Razorpay Checkout modal authorization on the same order"
                ),
                payload={"gateway_error": outcome.raw},
            )

    # Park the session for modal authorization.
    customer_id: str | None = None
    save_card = False
    if driver == "saved":
        # Customer was resolved before order creation (mandate requirement);
        # persist it on the method row for the frontend + future token charges.
        method.razorpay_customer_id = mandate["customer_id"]
        if method.status != "needs_reauth" and not method.razorpay_token_id:
            # Only brand-new methods drop to pending_save. An existing token
            # stays active so later completions keep attempting headless
            # charges (transient SERVER_ERRORs must never disable them).
            method.status = "pending_save"
        customer_id = mandate["customer_id"]
        save_card = True

    audit_service.append_event(
        db,
        actor="merchant_agent",
        action="awaiting_authorization",
        checkout_session_id=cs.checkout_session_id,
        decision="pending",
        reasoning_summary=(
            "Awaiting card authorization via Razorpay Checkout"
            + (" (save-card enabled)" if save_card else "")
        ),
    )
    db.flush()
    out = CompleteResponse(checkout_session=serialize_session(db, cs))
    pay = out.checkout_session.payment
    pay["awaiting_authorization"] = True
    pay["key_id"] = payment_service.get_key_id()
    if customer_id:
        pay["customer_id"] = customer_id
        pay["save_card"] = True
    return out, False