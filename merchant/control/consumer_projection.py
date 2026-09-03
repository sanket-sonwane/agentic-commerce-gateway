"""Consumer provenance projection — a thin read model over authoritative AP2.

This module does NOT re-implement any AP2 logic. It composes the existing
authoritative services (checkout state, signed provenance verification, signed
audit chain, mercy explain) into a presentation-friendly shape for a normal
end user. The backend remains authoritative; this is only a serializer.

Design rules:
- Never expose secrets (no private keys, no Razorpay secrets, no PAN/CVV).
- Never claim the user "signed" anything UNLESS a user-signed intent assertion
  was cryptographically verified for this transaction (Workstream E) — i.e. we
  may claim "Authorized by you" only when an executed, single-use, hash-matched
  UserIntentAssertion is linked to the Execution Mandate. Legacy/delegation-only
  transactions stay labeled as such.
- Never infer "Razorpay not called" merely from a missing payment id — it uses
  the authoritative order id and razorpay audit events.
- All stage/status mapping is deterministic and driven by authoritative evidence.
"""
from __future__ import annotations

import json
from typing import Any

from merchant.config import settings
from merchant.persistence.models.dto import (
    ConsumerAuthorization,
    ConsumerCheck,
    ConsumerJourneyStage,
    ConsumerPayment,
    ConsumerProof,
    ConsumerProvenanceResponse,
    ConsumerTransaction,
    ErrorCode,
    ProtocolError,
)
from merchant.domain import checkout as checkout_service
from merchant.evidence import audit_service

# Display-only mapping. The data model has no merchant display name; we map the
# authoritative merchant_id to a friendly label, falling back to the raw id.
_MERCHANT_DISPLAY = {
    "merchant_grocery_01": "GreenMart",
}


def _merchant_display(merchant_id: str | None) -> str:
    return _MERCHANT_DISPLAY.get(merchant_id or "", merchant_id or "Merchant")


def _agent_display(buyer_agent_id: str | None) -> str:
    if not buyer_agent_id:
        return "Shopping agent"
    return buyer_agent_id.replace("buyer_agent_", "").replace("_", " ")


RULE_LABELS = {
    "spend_limit": "Spend limit",
    "stock_check": "Stock",
    "category_allowed": "Category",
    "delegation_expiry": "Delegation",
    "quantity_limit": "Quantity",
}

# Deterministic stage order — only stages that can be evidenced by audit events.
STAGE_ORDER = [
    "intent",
    "authorization",
    "merchant_decision",
    "execution_authorization",
    "safety_checks",
    "razorpay",
    "payment_verification",
    "completed",
]

STAGE_TITLES = {
    "intent": "User intent",
    "authorization": "Authorization",
    "merchant_decision": "Merchant decision",
    "execution_authorization": "Execution authorization",
    "safety_checks": "Safety checks",
    "razorpay": "Razorpay",
    "payment_verification": "Payment verification",
    "completed": "Completed",
}

# Audit actions that mark a stage as reached (authoritative evidence).
_STAGE_REACHED_ACTIONS: dict[str, tuple[str, ...]] = {
    "intent": ("intent_mandate_created", "intent_scope_verified"),
    "authorization": ("intent_mandate_created", "intent_scope_verified"),
    "merchant_decision": ("gating_evaluated", "final_validation"),
    "execution_authorization": ("execution_mandate_created",),
    "safety_checks": ("gating_evaluated", "final_validation"),
    "razorpay": (
        "razorpay_order_created",
        "razorpay_token_charge",
        "awaiting_authorization",
        "payment_success",
        "payment_failure",
        "payment_pending",
    ),
    "payment_verification": ("payment_success",),
    "completed": ("checkout_completed",),
}

# Actions that indicate an explicit gateway (Razorpay) interaction.
_RAZORPAY_ACTIONS = (
    "razorpay_order_created",
    "razorpay_token_charge",
    "awaiting_authorization",
    "payment_success",
    "payment_failure",
    "payment_pending",
)


def _events_for(db, csid: str) -> list:
    return audit_service.events_for_session(db, csid)


def _razorpay_called(cs, events: list) -> bool:
    if cs.razorpay_order_id:
        return True
    return any(e.action in _RAZORPAY_ACTIONS for e in events)


def _latest_gating_checks(events: list) -> list[dict]:
    for e in reversed(events):
        if e.action in ("gating_evaluated", "final_validation"):
            payload = _loads(e.payload)
            if isinstance(payload, dict):
                checks = payload.get("checks")
                if checks:
                    return checks
    return []


def _loads(raw: Any) -> Any:
    if isinstance(raw, dict) or raw is None:
        return raw or {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def _resolve_intent(db, cs) -> Any | None:
    """Return the linked IntentMandate (or None) via the Execution Mandate."""
    if not cs.execution_mandate_id:
        return None
    from merchant.persistence.models.entities import ExecutionMandate, IntentMandate

    em = db.get(ExecutionMandate, cs.execution_mandate_id)
    if em is None or not em.intent_mandate_id:
        return None
    return db.get(IntentMandate, em.intent_mandate_id)


def _resolve_user_assertion(db, cs) -> Any | None:
    """Return the executed, single-use, hash-matched UserIntentAssertion (or None).

    A user assertion is deemed AUTHORITATIVE evidence for this transaction only
    when it is linked by the Execution Mandate, has been single-use consumed, and
    its canonical hash matches the mandate's reference. Absent any of those, the
    projection stays on the legacy "delegation" label — we never guess.
    """
    if not cs.execution_mandate_id:
        return None
    from merchant.persistence.models.entities import ExecutionMandate, UserIntentAssertion

    em = db.get(ExecutionMandate, cs.execution_mandate_id)
    if em is None or not em.user_assertion_id:
        return None
    ua = db.get(UserIntentAssertion, em.user_assertion_id)
    if ua is None or ua.consumed_at is None:
        return None
    if ua.canonical_payload_hash != em.user_assertion_hash:
        return None
    return ua


def _build_authorization(db, cs, intent, events: list) -> ConsumerAuthorization:
    """Consumer-friendly authorization from authoritative evidence.

    When a user-signed intent assertion is linked and verified (Workstream E),
    the root of authority is the USER, so we claim "Authorized by you (signed)".
    Otherwise authority derives from the buyer's delegation and we never claim a
    user signature.
    """
    agent = _agent_display(cs.buyer_agent_id)
    user_assertion = _resolve_user_assertion(db, cs)

    if user_assertion is not None:
        ceiling = {}
        try:
            ceiling = json.loads(user_assertion.constraint_ceiling) or {}
        except (ValueError, TypeError):
            ceiling = {}
        intent_txt = ""
        try:
            intent_txt = json.loads(user_assertion.intent).get("description", "")
        except (ValueError, TypeError):
            intent_txt = ""
        return ConsumerAuthorization(
            authorized_by="You (signed with your device key)",
            agent=agent,
            scope=intent_txt or "Purchase you approved in chat",
            max_amount_paise=ceiling.get("max_amount_paise"),
            merchant_scope=[],
            authorization_source="user_signed",
            authority_note=(
                "Your user-signed intent assertion was cryptographically verified "
                "and consumed for this exact transaction (single-use)."
            ),
        )

    if intent is None:
        return ConsumerAuthorization(
            authorized_by="Your shopping agent (delegation)",
            agent=agent,
            scope="Purchase authorized by the account's delegation",
            merchant_scope=[],
            verified=True,
            authorization_source="delegation",
            authority_note=(
                "No signed Intent Mandate and no user-signed assertion are "
                "linked to this transaction; authority derives from the buyer's "
                "delegation."
            ),
        )

    constraints = {}
    try:
        constraints = json.loads(intent.constraints) or {}
    except (ValueError, TypeError):
        constraints = {}

    scope = ""
    try:
        scope = json.loads(intent.intent).get("description", "") if intent.intent else ""
    except (ValueError, TypeError):
        scope = ""

    return ConsumerAuthorization(
        authorized_by="Your shopping agent (delegation)",
        agent=agent,
        scope=scope or "Purchase authorized by the account's delegation",
        max_amount_paise=constraints.get("max_order_value_paise"),
        merchant_scope=list(constraints.get("allowed_categories", []) or []),
        expires_at=intent.expires_at,
        verified=True,
        authorization_source="delegation",
        authority_note=(
            "Authorization recognized from your agent delegation. "
            "The intent artifact is signed by the merchant key; no user-signed "
            "assertion is linked to this transaction."
        ),
    )


def _build_checks(events: list) -> list[ConsumerCheck]:
    raw = _latest_gating_checks(events)
    out: list[ConsumerCheck] = []
    for c in raw:
        rule = c.get("rule", "check")
        out.append(
            ConsumerCheck(
                name=RULE_LABELS.get(rule, rule),
                status=c.get("result", "skipped"),
                summary=c.get("detail", ""),
            )
        )
    return out


def _build_payment(cs, events: list) -> ConsumerPayment:
    called = _razorpay_called(cs, events)
    totals = {}
    try:
        totals = json.loads(cs.totals) or {}
    except (ValueError, TypeError):
        pass
    amount = totals.get("total")

    # Deterministic payment status from authoritative evidence.
    #   verified  — gateway confirmed capture; money moved.
    #   authorized — gateway created a payment object (real payment_id exists)
    #                but capture is unresolved (test-mode bank settlement or
    #                manual reconciliation pending).  Distinct from "pending"
    #                which means only an order was created and no payment object
    #                exists yet.
    #   pending   — order created at gateway; no payment object yet.
    #   declined  — gateway rejected the payment.
    #   order_created — order exists but no payment action was taken.
    #   not_called — no Razorpay interaction at all.
    if not called:
        status = "not_called"
    elif cs.status == "completed" and any(e.action == "payment_success" for e in events):
        status = "verified"
    elif any(e.action == "payment_failure" for e in events) or cs.decline_code == "PAYMENT_FAILED":
        status = "declined"
    elif cs.status == "payment_pending":
        # payment_pending means Razorpay was called; distinguish:
        #   - payment_id exists → a real payment was initiated/authorized
        #   - no payment_id → only an order was created
        status = "authorized" if cs.razorpay_payment_id else "pending"
    elif any(e.action == "payment_pending" for e in events):
        status = "authorized" if cs.razorpay_payment_id else "pending"
    elif cs.razorpay_order_id:
        status = "order_created"
    else:
        status = "not_called"

    return ConsumerPayment(
        status=status,
        razorpay_called=called,
        order_id=cs.razorpay_order_id,
        payment_id=cs.razorpay_payment_id,
        amount=amount,
        amount_paise=amount * 100 if amount is not None else None,
    )


def _build_proof(cs, provenance: dict, chain: dict) -> ConsumerProof:
    ok_map: dict[str, bool] = {}
    if provenance and provenance.get("provenance_available"):
        ok_map = {c["name"]: bool(c["ok"]) for c in provenance.get("checks", [])}

    def status(check_name: str) -> str:
        if check_name in ok_map:
            return "verified" if ok_map[check_name] else "failed"
        return "unavailable"

    mandate_id = None
    if provenance and provenance.get("artifacts", {}).get("execution_mandate"):
        mandate_id = provenance["artifacts"]["execution_mandate"].get("mandate_id")

    return ConsumerProof(
        execution_mandate=status("execution_mandate_signature"),
        policy_decision=status("policy_approved"),
        decision_receipt=status("decision_receipt"),
        provenance="verified" if (provenance and provenance.get("provenance_available")) else "unavailable",
        audit_chain="verified" if chain else "unavailable",
        key_id=(provenance or {}).get("artifacts", {}).get("merchant_key_id"),
        mandate_id=mandate_id,
        verifier_status=(
            "VALID"
            if provenance and provenance.get("provenance_available")
            else "NOT_AVAILABLE"
        ),
    )


def _build_journey(events: list, cs, payment: ConsumerPayment, failure_stage: str | None) -> list[ConsumerJourneyStage]:
    actions = {e.action for e in events}
    reached = {stage for stage, acts in _STAGE_REACHED_ACTIONS.items() if actions & set(acts)}

    if failure_stage and failure_stage in STAGE_ORDER:
        fail_idx = STAGE_ORDER.index(failure_stage)
    else:
        fail_idx = None

    stages: list[ConsumerJourneyStage] = []
    for i, stage in enumerate(STAGE_ORDER):
        if stage in reached:
            status = "completed"
        elif stage == failure_stage:
            status = "failed"
        elif fail_idx is not None and i < fail_idx:
            status = "reached"
        else:
            status = "not_reached"
        stages.append(
            ConsumerJourneyStage(
                stage=stage,
                status=status,
                title=STAGE_TITLES[stage],
                summary=_stage_summary(stage, cs, payment),
            )
        )
    return stages


def _stage_summary(stage: str, cs, payment: ConsumerPayment) -> str:
    if stage == "intent":
        return "Shopping agent requested a purchase on your behalf"
    if stage == "authorization":
        return "Authorization recognized from your agent delegation"
    if stage == "merchant_decision":
        return "Merchant evaluated the basket against policy"
    if stage == "execution_authorization":
        return "Exact transaction frozen in a signed Execution Mandate"
    if stage == "safety_checks":
        return "Amount, category, stock and merchant scope verified"
    if stage == "razorpay":
        if not payment.razorpay_called:
            return "Razorpay was not called"
        return "Payment was processed at the Razorpay gateway"
    if stage == "payment_verification":
        return "Payment verified at the gateway"
    if stage == "completed":
        return "Transaction completed"
    return ""


def _stopped_at(cs, events: list, payment: ConsumerPayment) -> tuple[str | None, str | None, str | None]:
    """Return (stopped_at, failure_stage, failure_reason) from authoritative state.

    Deterministic precedence:
      1. completed/cancelled -> no stop.
      2. declined after a gateway interaction -> payment/razorpay stage.
      3. declined before any gateway interaction -> safety/authorization stage.
      4. payment_pending/requires_payment -> razorpay stage, unresolved.
    """
    if cs.status == "completed":
        return None, None, None
    if cs.status == "cancelled":
        return "cancelled", "cancelled", "Transaction cancelled before payment"

    failed_rule = ""
    for e in reversed(events):
        if e.action in ("gating_evaluated", "final_validation") and e.decision == "declined":
            rules = _loads(e.rules_fired)
            failed_rule = (
                "; ".join(r.split(":")[0] for r in rules if r.endswith(":fail"))
                or e.reasoning_summary
                or ""
            )
            break

    if cs.status == "payment_pending":
        return "razorpay", "razorpay", "Payment is pending gateway reconciliation"

    if payment.razorpay_called:
        # A gateway interaction happened; if it did not complete it stopped in payment.
        if cs.status == "declined":
            return "razorpay", "razorpay", failed_rule or "Gateway declined the payment"
        return "razorpay", "razorpay", "Payment not completed"

    # Never reached the gateway.
    if cs.status == "declined":
        reason = failed_rule or (cs.decline_code or "declined")
        stage = "safety_checks" if failed_rule else "authorization"
        return stage, stage, reason
    if cs.status == "requires_payment" or cs.status == "requires_confirmation":
        return "authorization", None, "Awaiting buyer confirmation/authorization"

    return None, None, None


def build_consumer_projection(db, checkout_session_id: str) -> dict:
    cs = checkout_service.get_session(db, checkout_session_id)
    if cs is None:
        raise ProtocolError(
            ErrorCode.CHECKOUT_NOT_FOUND,
            f"Checkout session {checkout_session_id} not found",
            http_status=404,
        )

    events = _events_for(db, checkout_session_id)
    intent = _resolve_intent(db, cs)
    payment = _build_payment(cs, events)
    checks = _build_checks(events)
    authorization = _build_authorization(db, cs, intent, events)

    stopped_at, failure_stage, failure_reason = _stopped_at(cs, events, payment)

    # Authoritative signed proof + audit chain.
    provenance = None
    try:
        from merchant.ap2.provenance_service import verify_provenance

        provenance = verify_provenance(db, checkout_session_id)
    except Exception:  # noqa: BLE001 — projection must degrade gracefully
        provenance = None

    chain = None
    try:
        intact, _, _ = audit_service.verify_chain(db)
        chain = intact
    except Exception:  # noqa: BLE001
        chain = None

    proof = _build_proof(cs, provenance, chain)

    # Stage ordering + last successful stage.
    stages = _build_journey(events, cs, payment, failure_stage)
    reached_stages = [s for s in STAGE_ORDER if any(
        st.stage == s and st.status in ("completed", "reached") for st in stages
    )]
    last_successful = reached_stages[-1] if reached_stages else None
    current_stage = failure_stage or (cs.status if cs.status in STAGE_ORDER else last_successful or "intent")

    resp = ConsumerProvenanceResponse(
        checkout_session_id=checkout_session_id,
        transaction=ConsumerTransaction(
            id=checkout_session_id,
            status=cs.status,
            amount=int((json.loads(cs.totals) or {}).get("total", 0)) if cs.totals else 0,
            currency="INR",
            merchant=_merchant_display(settings.merchant_id),
            agent=_agent_display(cs.buyer_agent_id),
            created_at=cs.created_at,
            updated_at=cs.updated_at,
        ),
        journey=stages,
        authorization=authorization,
        checks=checks,
        payment=payment,
        proof=proof,
        current_stage=current_stage,
        last_successful_stage=last_successful,
        failure_stage=failure_stage,
        failure_reason=failure_reason,
        stopped_at=stopped_at,
        razorpay_called=payment.razorpay_called,
    )
    return resp.model_dump(mode="json")
