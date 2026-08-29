"""Checkout session lifecycle — the transactional core (PRD §13–20, §26).

Money-critical paths run inside db.mutate() (serialized). The complete()
ladder revalidates EVERYTHING immediately before payment; earlier successful
checks are never trusted (G4/FR-10).
"""
import json
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from merchant_agent.config import settings
from merchant_agent.db import mutate, read_only, utcnow
from merchant_agent.models.dto import (
    CheckoutSessionOut,
    CompleteResponse,
    ErrorCode,
    GatingResult,
    ProtocolError,
    ReservationInfo,
)
from merchant_agent.models.entities import (
    AuditEvent,
    CheckoutSession,
    IdempotencyKey,
    Pack,
    PaymentMethod,
    Product,
    Reservation,
)
from merchant_agent.services import audit_service, gating_service, inventory_service
from merchant_agent.services.delegation_service import validate_delegation
from merchant_agent.services.payment_service import PaymentOutcome

VALID_TRANSITIONS: dict[str, set[str]] = {
    "created": {"requires_payment", "declined", "cancelled"},
    "requires_payment": {"requires_confirmation", "payment_pending", "completed", "declined", "cancelled"},
    "requires_confirmation": {"requires_payment", "declined", "cancelled"},
    "payment_pending": {"completed", "declined"},
    "completed": set(),
    "declined": set(),
    "cancelled": set(),
}

TERMINAL_STATES = {"completed", "declined", "cancelled"}


# ---------------------------------------------------------------- helpers


def _session_ref(cs: CheckoutSession) -> dict:
    return {"checkout_session_id": cs.checkout_session_id, "status": cs.status}


def _load_pack(db: Session, product_id: str, pack_id: str) -> tuple[Product, Pack]:
    product = db.get(Product, product_id)
    if product is None:
        raise ProtocolError(
            ErrorCode.PRODUCT_NOT_FOUND,
            f"Unknown product {product_id}",
            details={"product_id": product_id},
        )
    pack = db.get(Pack, pack_id)
    if pack is None or pack.product_id != product_id:
        raise ProtocolError(
            ErrorCode.PACK_NOT_FOUND,
            f"Unknown pack {pack_id} for product {product_id}",
            http_status=404,
            details={"product_id": product_id, "pack_id": pack_id},
        )
    return product, pack


def _resolve_items(db: Session, raw_items: list[dict]) -> list[dict]:
    """Resolve buyer line items against the authoritative catalog."""
    resolved = []
    for item in raw_items:
        product, pack = _load_pack(db, item["product_id"], item["pack_id"])
        resolved.append(
            {
                "product_id": product.product_id,
                "pack_id": pack.pack_id,
                "name": product.name,
                "category": product.category,
                "tags": json.loads(product.tags),
                "size": pack.size,
                "unit": pack.unit,
                "quantity": int(item["quantity"]),
                "unit_price": pack.price,  # authoritative price — never client-supplied
                "stock_actual": pack.stock_actual,
            }
        )
    return resolved


def _compute_totals(resolved_items: list[dict]) -> dict:
    subtotal = sum(i["unit_price"] * i["quantity"] for i in resolved_items)
    shipping = 0 if subtotal >= settings.free_shipping_threshold else settings.shipping_fee
    tax = 0
    return {
        "subtotal": subtotal,
        "tax": tax,
        "shipping": shipping,
        "total": subtotal + tax + shipping,
        "currency": "INR",
    }


def computes_cart_hash(items: list[dict]) -> str:
    """Stable SHA-256 over the resolved line items (AP2 §21 cart_hash).

    Canonical form keys on product_id/pack_id/quantity/unit_price_paise so it
    is stable regardless of metadata ordering. Accepts both the resolved session
    item shape (unit_price in rupees) and the Execution Mandate shape
    (unit_price_paise), normalizing to paise so both sides hash identically.
    """
    from merchant_agent.services import crypto_service

    normalized = []
    for i in items:
        up = i.get("unit_price_paise")
        if up is None:
            up = int(i["unit_price"]) * 100
        normalized.append(
            {
                "product_id": i["product_id"],
                "pack_id": i["pack_id"],
                "quantity": int(i["quantity"]),
                "unit_price_paise": int(up),
            }
        )
    normalized.sort(key=lambda i: (i["product_id"], i["pack_id"]))
    return crypto_service.sha256_hex(normalized)


def _gate(db: Session, delegation, resolved_items: list[dict], totals: dict) -> GatingResult:
    return gating_service.evaluate(db, delegation, resolved_items, totals["total"])


def _decline_error(result: GatingResult) -> ProtocolError:
    code = gating_service.first_failure_code(result)
    failed = [c.detail for c in result.checks if c.result == "fail"]
    return ProtocolError(
        code,
        "; ".join(failed) or "Policy check failed",
        http_status=409,
        retryable=code in (ErrorCode.STOCK_UNAVAILABLE,),
    )


def serialize_session(db: Session, cs: CheckoutSession) -> CheckoutSessionOut:
    active_res = (
        db.execute(
            select(Reservation).where(
                Reservation.checkout_session_id == cs.checkout_session_id,
                Reservation.status == "active",
            )
        )
        .scalars()
        .first()
    )

    reservation = (
        ReservationInfo(status="active", expires_at=active_res.expires_at)
        if active_res
        else ReservationInfo(status="none")
    )

    payment: dict = {}
    if cs.razorpay_order_id:
        payment = {
            "driver": cs.payment_driver,
            "order_id": cs.razorpay_order_id,
            "payment_id": cs.razorpay_payment_id,
            "amount": json.loads(cs.totals).get("total"),
            "currency": "INR",
        }
        if cs.authorized_amount_paise is not None:
            payment["authorized_amount_paise"] = cs.authorized_amount_paise
            payment["execution_mandate_id"] = cs.execution_mandate_id
        # A live order on a non-terminal session means we're waiting for the card UI.
        if cs.status == "requires_payment" and settings.payment_driver in ("checkout", "saved"):
            payment["awaiting_authorization"] = True
            payment["key_id"] = settings.razorpay_key_id
            if settings.payment_driver == "saved":
                method = (
                    db.execute(
                        select(PaymentMethod).where(PaymentMethod.buyer_agent_id == cs.buyer_agent_id)
                    )
                    .scalars()
                    .first()
                )
                if method and method.razorpay_customer_id:
                    payment["customer_id"] = method.razorpay_customer_id
                    payment["save_card"] = True

    return CheckoutSessionOut(
        checkout_session_id=cs.checkout_session_id,
        version=cs.version,
        status=cs.status,
        buyer_agent_id=cs.buyer_agent_id,
        delegation_id=cs.delegation_id,
        line_items=json.loads(cs.line_items),
        totals=json.loads(cs.totals),
        gating_result=json.loads(cs.gating_result),
        reservation=reservation,
        payment=payment,
        razorpay_order_id=cs.razorpay_order_id,
        created_at=cs.created_at,
        updated_at=cs.updated_at,
    )


def get_session(db: Session, checkout_session_id: str) -> CheckoutSession:
    cs = db.get(CheckoutSession, checkout_session_id)
    if cs is None:
        raise ProtocolError(
            ErrorCode.CHECKOUT_NOT_FOUND,
            f"Checkout session {checkout_session_id} not found",
            http_status=404,
        )
    return cs


def _check_ownership(cs: CheckoutSession, buyer_agent_id: str, delegation_token: str) -> None:
    if cs.buyer_agent_id != buyer_agent_id or cs.delegation_id != delegation_token:
        raise ProtocolError(
            ErrorCode.UNAUTHORIZED_AGENT,
            "Session belongs to a different buyer agent/delegation",
            http_status=403,
        )


def _assert_transition(current: str, target: str) -> None:
    if target not in VALID_TRANSITIONS.get(current, set()):
        raise ProtocolError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Illegal transition {current} -> {target}",
            http_status=409,
            details={"current_status": current},
        )


# ---------------------------------------------------------------- idempotency


def _idempotent_replay(db: Session, key: str | None):
    if not key:
        return None
    record = db.get(IdempotencyKey, key)
    if record is None:
        return None
    return json.loads(record.response_snapshot)


def _store_idempotent(db: Session, key: str | None, endpoint: str, cs: CheckoutSession, snapshot: dict) -> None:
    if not key:
        return
    db.merge(
        IdempotencyKey(
            key=key,
            endpoint=endpoint,
            session_id=cs.checkout_session_id,
            response_snapshot=json.dumps(snapshot, default=str),
        )
    )
    db.flush()


def store_complete_idempotency(db: Session, key: str | None, session_id: str, result: CompleteResponse) -> None:
    """Persist the full completion response so replays reproduce it byte-for-byte."""
    if not key:
        return
    db.merge(
        IdempotencyKey(
            key=key,
            endpoint="complete",
            session_id=session_id,
            response_snapshot=json.dumps(result.model_dump(mode="json")),
        )
    )
    db.flush()


# ---------------------------------------------------------------- create


def create_checkout(
    db: Session,
    *,
    buyer_agent_id: str,
    delegation_token: str,
    line_items: list[dict],
    idempotency_key: str | None = None,
) -> tuple[CheckoutSession, bool]:
    """Returns (session, created_bool). Replays return the existing session."""
    replay = _idempotent_replay(db, idempotency_key)
    if replay:
        existing = db.get(CheckoutSession, replay["checkout_session_id"])
        if existing is not None:
            return existing, False

    delegation = validate_delegation(db, buyer_agent_id, delegation_token)

    # Merge duplicate lines targeting the same pack.
    merged: dict[tuple[str, str], int] = {}
    for item in line_items:
        key = (item["product_id"], item["pack_id"])
        merged[key] = merged.get(key, 0) + int(item["quantity"])
    raw_items = [
        {"product_id": k[0], "pack_id": k[1], "quantity": v} for k, v in merged.items()
    ]

    resolved = _resolve_items(db, raw_items)
    totals = _compute_totals(resolved)
    gating = _gate(db, delegation, resolved, totals)

    cs = CheckoutSession(
        checkout_session_id=f"cs_{uuid.uuid4().hex[:10]}",
        version=1,
        status="created",
        buyer_agent_id=buyer_agent_id,
        delegation_id=delegation_token,
        line_items=json.dumps(resolved),
        totals=json.dumps(totals),
        gating_result=gating.model_dump_json(),
        idempotency_key=idempotency_key,
    )
    db.add(cs)
    db.flush()

    audit_service.append_event(
        db,
        actor="buyer_agent",
        action="checkout_create_requested",
        checkout_session_id=cs.checkout_session_id,
        payload={"line_items": resolved, "totals": totals},
    )

    if gating.passed:
        expires_at = inventory_service.create_reservations(db, cs.checkout_session_id, resolved)
        cs.status = "requires_payment"
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="gating_evaluated",
            checkout_session_id=cs.checkout_session_id,
            decision="approved",
            rules_fired=[f"{c.rule}:{c.result}" for c in gating.checks],
            reasoning_summary="All deterministic policy checks passed at creation",
            payload={"checks": [c.model_dump() for c in gating.checks]},
        )
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="reservation_created",
            checkout_session_id=cs.checkout_session_id,
            decision="info",
            reasoning_summary=f"Reserved {len(resolved)} line item(s)",
            payload={"expires_at": expires_at},
        )
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="checkout_created",
            checkout_session_id=cs.checkout_session_id,
            decision="approved",
            reasoning_summary=f"Session requires_payment, total ₹{totals['total']}",
        )
    else:
        cs.status = "declined"
        cs.decline_code = gating_service.first_failure_code(gating).value
        err = _decline_error(gating)
        err.session_ref = _session_ref(cs)
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="gating_evaluated",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=[f"{c.rule}:{c.result}" for c in gating.checks],
            reasoning_summary="Deterministic policy rejected basket at creation",
            payload={"checks": [c.model_dump() for c in gating.checks]},
        )
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="checkout_declined",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=[cs.decline_code],
            reasoning_summary=f"Create declined: {cs.decline_code}; Razorpay NOT called",
        )

    db.flush()
    _store_idempotent(db, idempotency_key, "create", cs,
                      {"checkout_session_id": cs.checkout_session_id})
    return cs, True


# ---------------------------------------------------------------- update


def update_checkout(
    db: Session,
    checkout_session_id: str,
    *,
    buyer_agent_id: str,
    delegation_token: str,
    line_items: list[dict],
) -> "CheckoutSession | ProtocolError":
    """Apply a basket update. Returns the updated session, or a ProtocolError
    (already recorded in audit) that the caller must raise AFTER commit."""
    cs = get_session(db, checkout_session_id)
    _check_ownership(cs, buyer_agent_id, delegation_token)
    if cs.status != "requires_payment":
        raise ProtocolError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Only live sessions can be edited; session is {cs.status}",
            http_status=409,
            details={"current_status": cs.status},
        )

    delegation = validate_delegation(db, buyer_agent_id, delegation_token)

    merged: dict[tuple[str, str], int] = {}
    for item in line_items:
        key = (item["product_id"], item["pack_id"])
        merged[key] = merged.get(key, 0) + int(item["quantity"])
    raw_items = [{"product_id": k[0], "pack_id": k[1], "quantity": v} for k, v in merged.items()]

    resolved = _resolve_items(db, raw_items)
    totals = _compute_totals(resolved)
    gating = _gate(db, delegation, resolved, totals)

    audit_service.append_event(
        db,
        actor="buyer_agent",
        action="checkout_update_requested",
        checkout_session_id=cs.checkout_session_id,
        payload={"new_line_items": resolved, "new_totals": totals},
    )

    if not gating.passed:
        # Session stays intact with its original items/reservation; buyer adapts again.
        err = _decline_error(gating)
        err.session_ref = _session_ref(cs)
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="gating_evaluated",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=[f"{c.rule}:{c.result}" for c in gating.checks],
            reasoning_summary="Update rejected by policy; previous basket preserved",
            payload={"checks": [c.model_dump() for c in gating.checks]},
        )
        db.flush()
        return err

    inventory_service.release_session_reservations(db, cs.checkout_session_id)
    expires_at = inventory_service.create_reservations(db, cs.checkout_session_id, resolved)
    cs.line_items = json.dumps(resolved)
    cs.totals = json.dumps(totals)
    cs.gating_result = gating.model_dump_json()
    cs.version += 1
    cs.status = "requires_payment"

    audit_service.append_event(
        db,
        actor="merchant_agent",
        action="gating_evaluated",
        checkout_session_id=cs.checkout_session_id,
        decision="approved",
        rules_fired=[f"{c.rule}:{c.result}" for c in gating.checks],
        reasoning_summary="Updated basket passed all policy checks",
        payload={"checks": [c.model_dump() for c in gating.checks]},
    )
    audit_service.append_event(
        db,
        actor="merchant_agent",
        action="checkout_updated",
        checkout_session_id=cs.checkout_session_id,
        decision="approved",
        reasoning_summary=f"Basket updated to version {cs.version}, total ₹{totals['total']}",
        payload={"expires_at": expires_at},
    )
    db.flush()
    return cs


# ---------------------------------------------------------------- cancel


def cancel_checkout(
    db: Session,
    checkout_session_id: str,
    *,
    buyer_agent_id: str,
    delegation_token: str,
) -> "CheckoutSession | ProtocolError":
    cs = get_session(db, checkout_session_id)
    _check_ownership(cs, buyer_agent_id, delegation_token)

    if cs.status == "cancelled":
        return cs  # idempotent
    if cs.status in TERMINAL_STATES:
        db.flush()
        return ProtocolError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Cannot cancel a {cs.status} session",
            http_status=409,
            details={"current_status": cs.status},
        )

    inventory_service.release_session_reservations(db, cs.checkout_session_id)
    cs.status = "cancelled"
    audit_service.append_event(
        db,
        actor="buyer_agent",
        action="checkout_cancelled",
        checkout_session_id=cs.checkout_session_id,
        decision="info",
        reasoning_summary="Buyer cancelled; reservations released; no payment attempted",
    )
    db.flush()
    return cs


# ---------------------------------------------------------------- confirm


def confirm_checkout(
    db: Session,
    checkout_session_id: str,
    *,
    buyer_agent_id: str,
    delegation_token: str,
    accept_new_terms: bool,
    version: int,
) -> "CheckoutSession | ProtocolError":
    """Apply buyer confirmation. Returns the updated session or a recorded
    ProtocolError that the caller must raise AFTER commit."""
    cs = get_session(db, checkout_session_id)
    _check_ownership(cs, buyer_agent_id, delegation_token)

    audit_service.append_event(
        db,
        actor="buyer_agent",
        action="confirmation_requested",
        checkout_session_id=cs.checkout_session_id,
        payload={"accept_new_terms": accept_new_terms, "version": version, "session_version": cs.version},
    )

    if version != cs.version:
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="confirmation_rejected",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=["confirmation_stale"],
            reasoning_summary=f"Confirmation referenced version {version}; current is {cs.version}",
        )
        db.flush()
        return ProtocolError(
            ErrorCode.CONFIRMATION_STALE,
            f"Confirmation references version {version} but session is version {cs.version}",
            http_status=409,
            details={"expected_version": cs.version, "provided_version": version},
            session_ref=_session_ref(cs),
        )

    if cs.status != "requires_confirmation":
        db.flush()
        return ProtocolError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Session {cs.status} does not require confirmation",
            http_status=409,
            details={"current_status": cs.status},
        )

    if not accept_new_terms:
        inventory_service.release_session_reservations(db, cs.checkout_session_id)
        cs.status = "cancelled"
        audit_service.append_event(
            db,
            actor="buyer_agent",
            action="confirmation_rejected",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            reasoning_summary="Buyer declined new terms; session cancelled",
        )
        db.flush()
        return cs

    # Acceptance persists the NEW terms: re-resolve authoritative prices/totals
    # so the next completion validates against what the buyer just approved.
    resolved = _resolve_items(db, [{"product_id": i["product_id"], "pack_id": i["pack_id"],
                                    "quantity": i["quantity"]} for i in json.loads(cs.line_items)])
    new_totals = _compute_totals(resolved)
    cs.line_items = json.dumps(resolved)
    cs.totals = json.dumps(new_totals)
    cs.version += 1
    cs.status = "requires_payment"
    audit_service.append_event(
        db,
        actor="merchant_agent",
        action="confirmation_accepted",
        checkout_session_id=cs.checkout_session_id,
        decision="approved",
        reasoning_summary=(
            f"Buyer accepted new terms; session updated to ₹{new_totals['total']} "
            f"(v{cs.version}) and returned to requires_payment"
        ),
    )
    db.flush()
    return cs


# ---------------------------------------------------------------- complete


def complete_checkout(
    db: Session,
    checkout_session_id: str,
    *,
    buyer_agent_id: str,
    delegation_token: str,
    idempotency_key: str | None = None,
    intent_mandate_id: str | None = None,
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
        inventory_service.release_session_reservations(db, cs.checkout_session_id)
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

    # --- 5. AP2: build + sign the Execution Mandate (PRD §14–15) ---
    # Freeze the exact basket about to be charged, then verify it before any
    # money movement. Fail closed: any verification failure -> NO Razorpay call.
    from merchant_agent.services import authorization_service, crypto_service

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
            "checks": verify_checks,
        },
    )

    # --- 6. Money movement ---
    from merchant_agent.services import payment_service

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

        cs.razorpay_order_id = order["id"]
        cs.razorpay_order_amount = int(order.get("amount") or amount_paise)
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


def get_or_create_payment_method(db: Session, buyer_agent_id: str) -> PaymentMethod:
    row = (
        db.execute(select(PaymentMethod).where(PaymentMethod.buyer_agent_id == buyer_agent_id))
        .scalars()
        .first()
    )
    if row is None:
        row = PaymentMethod(buyer_agent_id=buyer_agent_id, status="pending_save")
        db.add(row)
        db.flush()
    return row


def finalize_with_outcome(db: Session, cs: CheckoutSession, outcome: PaymentOutcome) -> CompleteResponse:
    return _finalize_payment(db, cs, outcome)


def settle_probe(cs: CheckoutSession, payment_id: str) -> int | None:
    """Best-effort settled payment amount (paise) for amount-binding checks.

    Returns None when the amount cannot be determined (no credentials / sim),
    in which case the callback falls back to the persisted order amount. Never
    raises: a probe failure must not block an already-verified payment.
    """
    from merchant_agent.services import payment_service

    try:
        pay = payment_service.fetch_payment(payment_id)
    except Exception:  # noqa: BLE001
        return None
    raw_amount = pay.get("amount")
    if isinstance(raw_amount, int) and raw_amount > 0:
        return raw_amount
    return cs.razorpay_order_amount


def _finalize_payment(db: Session, cs: CheckoutSession, outcome: PaymentOutcome) -> CompleteResponse:
    cs.payment_driver = cs.payment_driver or settings.payment_driver
    cs.razorpay_order_id = outcome.order_id or cs.razorpay_order_id
    cs.razorpay_payment_id = outcome.payment_id

    if outcome.status == "completed":
        # Consume reservations and decrement actual stock.
        items = json.loads(cs.line_items)
        for item in items:
            pack = db.get(Pack, item["pack_id"])
            pack.stock_actual = max(0, pack.stock_actual - item["quantity"])
        inventory_service.consume_session_reservations(db, cs.checkout_session_id)
        cs.status = "completed"
        audit_service.append_event(
            db,
            actor="razorpay",
            action="payment_success",
            checkout_session_id=cs.checkout_session_id,
            decision="approved",
            reasoning_summary=(
                f"Paid ₹{json.loads(cs.totals)['total']} "
                f"(order={outcome.order_id}, payment={outcome.payment_id}"
                f"{', simulated' if outcome.simulated else ''})"
            ),
            payload={"simulated": outcome.simulated, **outcome.raw},
        )
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="checkout_completed",
            checkout_session_id=cs.checkout_session_id,
            decision="approved",
            reasoning_summary="Transaction completed; stock consumed",
        )
    elif outcome.status == "pending":
        cs.status = "payment_pending"
        audit_service.append_event(
            db,
            actor="razorpay",
            action="payment_pending",
            checkout_session_id=cs.checkout_session_id,
            decision="pending",
            reasoning_summary=outcome.message or "Payment unresolved; reconciliation required",
        )
    else:
        inventory_service.release_session_reservations(db, cs.checkout_session_id)
        cs.status = "declined"
        cs.decline_code = ErrorCode.PAYMENT_FAILED.value
        audit_service.append_event(
            db,
            actor="razorpay",
            action="payment_failure",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=["PAYMENT_FAILED"],
            reasoning_summary=outcome.message or "Gateway rejected the payment",
        )
        audit_service.append_event(
            db,
            actor="merchant_agent",
            action="checkout_declined",
            checkout_session_id=cs.checkout_session_id,
            decision="declined",
            rules_fired=["PAYMENT_FAILED"],
            reasoning_summary="Gateway decline recorded",
        )
    db.flush()

    # AP2: issue a signed Decision Receipt binding mandate + policy + payment.
    _issue_receipt_for_outcome(db, cs, outcome)

    resp = CompleteResponse(checkout_session=serialize_session(db, cs))
    if outcome.status == "pending":
        resp.error = {
            "code": ErrorCode.PAYMENT_PENDING.value,
            "message": outcome.message or "Payment unresolved",
            "retryable": False,
            "details": {},
        }
    elif outcome.status == "declined":
        resp.error = {
            "code": ErrorCode.PAYMENT_FAILED.value,
            "message": outcome.message or "Payment failed",
            "retryable": False,
            "details": {},
        }
    return resp


def _issue_receipt_for_outcome(db: Session, cs: CheckoutSession, outcome: PaymentOutcome) -> None:
    """Create + sign a Decision Receipt only when an Execution Mandate exists.

    Old/legacy transactions without a mandate (created before the AP2 layer)
    simply have no receipt — provenance reports NOT_AVAILABLE for them.
    """
    if not cs.execution_mandate_id:
        return
    from merchant_agent.models.entities import ExecutionMandate, PolicyDecision
    from merchant_agent.services import authorization_service

    mandate = db.get(ExecutionMandate, cs.execution_mandate_id)
    if mandate is None:
        return
    decision = (
        db.query(PolicyDecision)
        .filter(PolicyDecision.execution_mandate_id == mandate.mandate_id)
        .order_by(PolicyDecision.created_at.desc())
        .first()
    )
    receipt = authorization_service.issue_decision_receipt(
        db,
        cs,
        mandate=mandate,
        decision=decision,
        outcome=outcome.status,
        razorpay_order_id=cs.razorpay_order_id,
        razorpay_payment_id=cs.razorpay_payment_id,
    )
    # AP2 single-use: once settled/declined, the mandate can never authorize a
    # second payment. Verify-and-complete on a consumed mandate now fails closed.
    authorization_service.mark_mandate_consumed(db, mandate)
    audit_service.append_event(
        db,
        actor="merchant_agent",
        action="decision_receipt_issued",
        checkout_session_id=cs.checkout_session_id,
        decision="info",
        reasoning_summary=f"Signed Decision Receipt {receipt.receipt_id} recorded",
        payload={
            "receipt_id": receipt.receipt_id,
            "execution_mandate_id": mandate.mandate_id,
            "authorized_amount_paise": receipt.authorized_amount_paise,
            "outcome": outcome.status,
        },
    )
    db.flush()


def reconcile_session(db: Session, checkout_session_id: str) -> CompleteResponse:
    """Operator/buyer-safe reconciliation for payment_pending sessions."""
    cs = get_session(db, checkout_session_id)
    if cs.status != "payment_pending":
        raise ProtocolError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Session {cs.status} is not payment_pending",
            http_status=409,
        )
    from merchant_agent.services import payment_service

    outcome = payment_service.reconcile_pending(cs.razorpay_order_id or "")
    return _finalize_payment(db, cs, outcome)
