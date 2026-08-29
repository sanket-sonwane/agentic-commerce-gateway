"""Explainability layer (PRD §33): structured audit facts -> human sentences.

Deterministic templating only — it never invents reasons.
"""
from sqlalchemy.orm import Session

from merchant.evidence import audit_service

RULE_PHRASES = {
    "spend_limit": "the basket total exceeded the authorized spending limit",
    "stock_check": "an item was no longer available in stock",
    "category_allowed": "the basket contained a category this account is not allowed to buy",
    "delegation_expiry": "the shopping authorization had expired",
    "quantity_limit": "a quantity limit was exceeded",
}

ACTION_SENTENCES = {
    "discovery_requested": "The buyer agent asked for product suggestions.",
    "discovery_result_generated": "The merchant returned matching candidates.",
    "checkout_created": "A checkout session was created and stock reserved.",
    "checkout_updated": "The basket was updated and revalidated.",
    "confirmation_accepted": "New terms were confirmed by the buyer.",
    "confirmation_rejected": "New terms were rejected.",
    "razorpay_order_created": "A Razorpay order was created.",
    "payment_success": "The payment succeeded.",
    "payment_failure": "The payment failed at the gateway.",
    "payment_pending": "The payment is unresolved and awaiting reconciliation.",
    "checkout_completed": "The order was completed.",
    "checkout_cancelled": "The order was cancelled; reserved stock was released.",
    "inventory_adjusted": "An operator adjusted inventory mid-transaction.",
    "execution_mandate_created": "The exact transaction was frozen in a signed Execution Mandate before payment.",
    "decision_receipt_issued": "A signed Decision Receipt bound the mandate, policy and payment.",
    "payment_amount_mismatch": "A payment was BLOCKED because the settled amount did not match the authorized amount.",
    "intent_mandate_created": "A signed Intent Mandate defined the buyer's authorized scope.",
    "intent_scope_verified": "The transaction was confirmed to stay within the buyer's signed Intent Mandate scope before payment.",
}


def explain_session(db: Session, checkout_session_id: str) -> dict:
    events = audit_service.events_for_session(db, checkout_session_id)
    if not events:
        return {"explanation": "No recorded activity for this session.", "facts": []}

    facts: list[str] = []
    decline_reason = None
    approved = False

    for ev in events:
        if ev.action == "gating_evaluated":
            failed_rules = [r.split(":")[0] for r in ev.rules_fired if r.endswith(":fail")]
            if failed_rules:
                phrases = [RULE_PHRASES.get(r, r) for r in failed_rules]
                decline_reason = "; ".join(phrases)
                facts.append(f"Policy check FAILED — {decline_reason}.")
            else:
                facts.append("All deterministic policy checks passed.")
        elif ev.action == "final_validation":
            if ev.decision == "declined":
                failed_rules = [r.split(":")[0] for r in ev.rules_fired if r.endswith(":fail")]
                phrases = [RULE_PHRASES.get(r, r) for r in failed_rules]
                decline_reason = "; ".join(phrases) or ev.reasoning_summary
                facts.append(f"Final re-validation before payment FAILED — {decline_reason}.")
            elif ev.decision == "hold":
                facts.append(f"Payment paused: {ev.reasoning_summary.lower()}.")
            else:
                facts.append("Final re-validation passed immediately before payment.")
        elif ev.action == "checkout_declined":
            facts.append(
                f"The transaction was declined ({ev.rules_fired[0] if ev.rules_fired else 'policy'}). "
                "Razorpay was NOT called."
            )
        elif ev.action == "razorpay_order_creation":
            continue  # covered by order_created
        elif ev.decision == "approved" and ev.action == "gating_evaluated":
            approved = True
        elif ev.action in ACTION_SENTENCES:
            facts.append(ACTION_SENTENCES[ev.action])

    if any("completed" in f for f in facts):
        headline = "This order was completed successfully."
    elif decline_reason:
        headline = f"This order was declined because {decline_reason}."
    elif any("cancelled" in f for f in facts):
        headline = "This order was cancelled before any payment."
    else:
        headline = "This transaction is still in progress."

    return {"explanation": headline, "timeline_facts": facts}
