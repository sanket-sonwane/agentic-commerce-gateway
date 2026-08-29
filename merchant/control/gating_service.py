"""Deterministic gating engine (PRD §23) — the core safety boundary.

Pure functions. No LLM. Identical inputs always produce identical outcomes
(NFR-03). Every rule result is exposed with pass/fail + human-readable detail.
"""
from sqlalchemy.orm import Session

from merchant.persistence.db import utcnow
from merchant.persistence.models.dto import ErrorCode, GatingCheck, GatingResult
from merchant.domain import inventory_service
from merchant.control.delegation_service import allowed_categories

RULE_SPEND_LIMIT = "spend_limit"
RULE_STOCK = "stock_check"
RULE_CATEGORY = "category_allowed"
RULE_EXPIRY = "delegation_expiry"
RULE_QUANTITY = "quantity_limit"

# Maps a failed rule to the protocol error code it produces.
FAILURE_CODES: dict[str, ErrorCode] = {
    RULE_SPEND_LIMIT: ErrorCode.SPEND_LIMIT_EXCEEDED,
    RULE_STOCK: ErrorCode.STOCK_UNAVAILABLE,
    RULE_CATEGORY: ErrorCode.CATEGORY_BLOCKED,
    RULE_EXPIRY: ErrorCode.DELEGATION_EXPIRED,
    RULE_QUANTITY: ErrorCode.DELEGATION_SCOPE_VIOLATION,
}


def evaluate(
    db: Session,
    delegation,  # Delegation entity
    resolved_items: list[dict],  # [{product_id, pack_id, name, category, quantity, unit_price}]
    total: int,
) -> GatingResult:
    """Run all five deterministic rules and return a structured result."""
    checks: list[GatingCheck] = []
    now = utcnow()

    # Rule 4 — authorization expiry
    if delegation.expires_at > now:
        checks.append(
            GatingCheck(
                rule=RULE_EXPIRY,
                result="pass",
                detail=f"valid until {delegation.expires_at.isoformat()}Z",
            )
        )
    else:
        checks.append(
            GatingCheck(rule=RULE_EXPIRY, result="fail", detail=f"expired {delegation.expires_at.isoformat()}Z")
        )

    # Rule 3 — category allow-list
    allowed = set(allowed_categories(delegation))
    blocked = sorted({item["category"] for item in resolved_items} - allowed)
    if not blocked:
        cats = ", ".join(sorted({item["category"] for item in resolved_items}))
        checks.append(GatingCheck(rule=RULE_CATEGORY, result="pass", detail=f"categories allowed: {cats}"))
    else:
        checks.append(
            GatingCheck(rule=RULE_CATEGORY, result="fail", detail=f"blocked categories: {', '.join(blocked)}")
        )

    # Rule 5 — per-item quantity limit
    over_qty = [i for i in resolved_items if i["quantity"] > delegation.max_quantity_per_item]
    if not over_qty:
        checks.append(
            GatingCheck(
                rule=RULE_QUANTITY,
                result="pass",
                detail=f"all quantities <= {delegation.max_quantity_per_item}",
            )
        )
    else:
        worst = max(over_qty, key=lambda i: i["quantity"])
        checks.append(
            GatingCheck(
                rule=RULE_QUANTITY,
                result="fail",
                detail=(
                    f"{worst['name']}: requested={worst['quantity']}, "
                    f"max={delegation.max_quantity_per_item}"
                ),
            )
        )

    # Rule 1 — spend limit
    if total <= delegation.max_order_value:
        checks.append(
            GatingCheck(rule=RULE_SPEND_LIMIT, result="pass", detail=f"₹{total} <= ₹{delegation.max_order_value}")
        )
    else:
        checks.append(
            GatingCheck(
                rule=RULE_SPEND_LIMIT,
                result="fail",
                detail=f"₹{total} > ₹{delegation.max_order_value}",
            )
        )

    # Rule 2 — stock availability (available = actual - reserved)
    stock_failures: list[str] = []
    for item in resolved_items:
        available = inventory_service.available_stock(db, item["pack_id"], item["stock_actual"])
        if available >= item["quantity"]:
            continue
        stock_failures.append(f"{item['name']}: requested={item['quantity']}, available={available}")
    if not stock_failures:
        checks.append(GatingCheck(rule=RULE_STOCK, result="pass", detail="all items in stock"))
    else:
        checks.append(GatingCheck(rule=RULE_STOCK, result="fail", detail="; ".join(stock_failures)))

    return GatingResult(passed=all(c.result == "pass" for c in checks), checks=checks)


def first_failure_code(result: GatingResult) -> ErrorCode:
    for check in result.checks:
        if check.result == "fail":
            return FAILURE_CODES[check.rule]
    return ErrorCode.INVALID_REQUEST
