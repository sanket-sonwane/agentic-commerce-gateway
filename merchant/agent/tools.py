"""Merchant Agent tool registry (PRD §13–14, §51).

Every merchant capability reachable by the agent is a thin, validating wrapper
around an existing deterministic service. Tools are classified:

    read                   -> safe for reasoning
    write                  -> state-changing, validated
    financially_privileged -> run_gating / request_completion (never touches
                              Razorpay directly; calls the deterministic ladder)

All identifiers supplied by the LLM are validated server-side (PRD §32);
prices/inventory always come from the database (§33–34). Every tool returns
the structured contract of §51:

    {"success": true,  "tool": ..., "result": {...}}
    {"success": false, "tool": ..., "error": {"code", "message", "retryable"}}
"""
import json
from typing import Any, Callable

from sqlalchemy.orm import Session

from merchant.agent.state import MerchantAgentState
from merchant.config import settings
from merchant.persistence.db import SessionLocal, mutate
from merchant.persistence.models.dto import ErrorCode, ProtocolError
from merchant.control import delegation_service
from merchant.domain import checkout as checkout_service
from merchant.domain.catalog import (
    available,
    check_inventory,
    delegation_constraints,
    find_alternative,
    get_current_price,
    get_product,
    load_pack,
    search_catalog,
)

READ_TOOLS = {
    "search_catalog",
    "get_product",
    "check_inventory",
    "get_current_price",
    "find_alternative",
    "get_checkout",
}
WRITE_TOOLS = {"create_checkout", "update_checkout", "request_confirmation", "cancel_checkout"}
FINANCIAL_TOOLS = {"run_gating", "request_completion"}
ALL_TOOLS = READ_TOOLS | WRITE_TOOLS | FINANCIAL_TOOLS


class ToolError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, details: dict | None = None):
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        super().__init__(message)


# ---------------------------------------------------------------- helpers


def canonical_args(args: dict | None) -> str:
    """Deterministic, order-insensitive key for one tool call's arguments.

    Used to fingerprint duplicate observations (same tool, same normalized
    arguments) across decision cycles: equivalent JSON forms collapse to one
    key so an LLM cannot dodge the replay cache with cosmetic re-orders.
    """
    def _norm(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): _norm(v) for k, v in sorted(value.items(), key=lambda i: str(i[0]))}
        if isinstance(value, (list, tuple)):
            return [_norm(v) for v in value]
        return value

    return json.dumps(_norm(args or {}), sort_keys=True, default=str)


# ---------------------------------------------------------------- read tools


def _tool_search_catalog(db: Session, state: MerchantAgentState, query: str) -> dict:
    return search_catalog(db, query)


def _tool_get_product(db: Session, state: MerchantAgentState, product_id: str) -> dict:
    return get_product(db, product_id)


def _tool_check_inventory(db: Session, state: MerchantAgentState, pack_id: str) -> dict:
    return check_inventory(db, pack_id)


def _tool_get_current_price(db: Session, state: MerchantAgentState, pack_id: str) -> dict:
    return get_current_price(db, pack_id)


def _tool_find_alternative(
    db: Session, state: MerchantAgentState, product_id: str, pack_id: str, quantity: int | None = None
) -> dict:
    return find_alternative(
        db,
        product_id,
        pack_id,
        quantity,
        requested_items=state.requested_items,
        excluded_pack_ids=state.excluded_pack_ids,
        buyer_request=state.buyer_request,
        delegation_token=state.delegation_token,
    )


# ---------------------------------------------------------------- checkout tools


def _items_from_args(items: Any) -> list[dict]:
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            raise ToolError(ErrorCode.INVALID_REQUEST.value, "items must be valid JSON")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        raise ToolError(ErrorCode.INVALID_REQUEST.value, "items must be a non-empty list")
    if len(items) > 15:
        raise ToolError(ErrorCode.INVALID_REQUEST.value, "too many line items (max 15)")
    clean: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            raise ToolError(ErrorCode.INVALID_REQUEST.value, "each item must be an object")
        pid, kid, q = item.get("product_id"), item.get("pack_id"), item.get("quantity", 1)
        if not pid or not kid:
            raise ToolError(ErrorCode.INVALID_REQUEST.value, "each item needs product_id and pack_id")
        try:
            q = int(q)
        except (TypeError, ValueError):
            raise ToolError(ErrorCode.INVALID_REQUEST.value, f"invalid quantity {q!r}")
        if q < 1:
            raise ToolError(ErrorCode.INVALID_REQUEST.value, "quantity must be >= 1")
        clean.append({"product_id": str(pid), "pack_id": str(kid), "quantity": q})
    return clean


def _allowed_pack_ids(state: MerchantAgentState) -> set[str]:
    """Deterministic intent anchor: packs the buyer actually asked for, packs
    the agent itself verified as the target via read tools, and packs coming
    from a buyer-accepted proposal. Anything else in a basket write is a
    silent substitution and is blocked."""
    allowed = {str(i["pack_id"]) for i in state.requested_items if i.get("pack_id")}
    allowed |= set(state.fallback_ctx.get("observed_pack_ids", []))
    allowed |= set(state.fallback_ctx.get("accepted_proposal_packs", []))
    return allowed


def _guard_no_silent_substitution(state: MerchantAgentState, clean: list[dict]) -> None:
    if state.execution_mode != "agent_completed" and not state.requested_items and not state.fallback_ctx.get("observed_pack_ids"):
        return  # nothing known about intent yet (e.g. pure browse flows)
    if not clean:
        return
    allowed = _allowed_pack_ids(state)
    intruders = sorted({i["pack_id"] for i in clean} - allowed) if allowed else []
    if intruders:
        raise ToolError(
            ErrorCode.INVALID_REQUEST.value,
            "SILENT_SUBSTITUTION_BLOCKED: basket contains packs never requested by the buyer "
            f"and never accepted via a structured proposal: {intruders}. If the requested pack is "
            "unavailable, call find_alternative then request_confirmation and pause for the "
            "buyer's decision — never substitute unilaterally.",
            details={"blocked_packs": intruders, "allowed_packs": sorted(allowed)},
        )

    # Quantity-intent fidelity: an explicitly requested pack keeps exactly the
    # requested quantity unless a buyer-accepted proposal changed it.
    accepted_packs = set(state.fallback_ctx.get("accepted_proposal_packs", []))
    requested_qty = {
        str(i["pack_id"]): int(i.get("quantity", 1))
        for i in state.requested_items
        if i.get("pack_id")
    }
    for item in clean:
        pid = str(item["pack_id"])
        if pid in requested_qty and pid not in accepted_packs:
            want = requested_qty[pid]
            got = int(item["quantity"])
            if got != want:
                raise ToolError(
                    ErrorCode.INVALID_REQUEST.value,
                    f"SILENT_MODIFICATION_BLOCKED: buyer requested {pid} x{want} but the basket "
                    f"contains x{got}. Never change buyer quantities unilaterally — if that exact "
                    "pack/quantity is unavailable, call find_alternative then request_confirmation "
                    "so the buyer approves the change.",
                    details={"pack_id": pid, "requested_quantity": want, "basket_quantity": got},
                )


def _basket_digest(resolved: list[dict]) -> str:
    return "|".join(f"{i['product_id']}:{i['pack_id']}:{i['quantity']}" for i in resolved)


def _tool_create_checkout(db: Session, state: MerchantAgentState, items: Any) -> dict:
    clean = _items_from_args(items)
    _guard_no_silent_substitution(state, clean)
    key = (
        f"ma_{state.agent_session_id}_create_"
        + __import__("hashlib").sha1(_basket_digest(clean).encode()).hexdigest()[:10]
    )
    cs, created = checkout_service.create_checkout(
        db,
        buyer_agent_id=state.buyer_agent_id,
        delegation_token=state.delegation_token,
        line_items=clean,
        idempotency_key=key,
    )
    state.checkout_session_id = cs.checkout_session_id
    out = checkout_service.serialize_session(db, cs)
    if cs.status == "declined":
        failed = [c["detail"] for c in out.gating_result.get("checks", []) if c.get("result") == "fail"]
        code = ErrorCode.INVALID_REQUEST
        rules = {
            "spend_limit": ErrorCode.SPEND_LIMIT_EXCEEDED,
            "stock_check": ErrorCode.STOCK_UNAVAILABLE,
            "category_allowed": ErrorCode.CATEGORY_BLOCKED,
            "delegation_expiry": ErrorCode.DELEGATION_EXPIRED,
            "quantity_limit": ErrorCode.DELEGATION_SCOPE_VIOLATION,
        }
        for check in out.gating_result.get("checks", []):
            if check.get("result") == "fail":
                code = rules.get(check["rule"], ErrorCode.INVALID_REQUEST)
                break
        return {
            "success": False,
            "tool": "create_checkout",
            "error": {
                "code": code.value,
                "message": "; ".join(failed) or "policy rejected basket",
                "retryable": code == ErrorCode.STOCK_UNAVAILABLE,
                "details": {"gating": out.gating_result, "checkout_session_id": cs.checkout_session_id},
            },
        }
    state.current_items = out.line_items
    return {
        "checkout_session_id": out.checkout_session_id,
        "status": out.status,
        "version": out.version,
        "line_items": out.line_items,
        "totals": out.totals,
        "gating_passed": out.gating_result.get("passed"),
        "created": created,
    }


def _tool_update_checkout(db: Session, state: MerchantAgentState, items: Any, checkout_session_id: str | None = None) -> dict:
    clean = _items_from_args(items)
    _guard_no_silent_substitution(state, clean)
    csid = str(checkout_session_id or state.checkout_session_id or "")
    if not csid:
        raise ToolError(ErrorCode.INVALID_REQUEST.value, "no active checkout session to update")
    result = checkout_service.update_checkout(
        db,
        csid,
        buyer_agent_id=state.buyer_agent_id,
        delegation_token=state.delegation_token,
        line_items=clean,
    )
    if isinstance(result, ProtocolError):
        return {
            "success": False,
            "tool": "update_checkout",
            "error": {
                "code": result.code.value,
                "message": result.message,
                "retryable": result.retryable,
                "details": result.details,
            },
        }
    out = checkout_service.serialize_session(db, result)
    state.current_items = out.line_items
    return {
        "checkout_session_id": out.checkout_session_id,
        "status": out.status,
        "version": out.version,
        "line_items": out.line_items,
        "totals": out.totals,
        "gating_passed": out.gating_result.get("passed"),
    }


def _tool_get_checkout(db: Session, state: MerchantAgentState, checkout_session_id: str | None = None) -> dict:
    csid = str(checkout_session_id or state.checkout_session_id or "")
    if not csid:
        raise ToolError(ErrorCode.CHECKOUT_NOT_FOUND.value, "no checkout session referenced")
    cs = checkout_service.get_session(db, csid)
    out = checkout_service.serialize_session(db, cs)
    return {
        "checkout_session_id": out.checkout_session_id,
        "status": out.status,
        "version": out.version,
        "buyer_agent_id": out.buyer_agent_id,
        "line_items": out.line_items,
        "totals": out.totals,
        "gating_result": out.gating_result,
        "reservation": out.reservation.model_dump(mode="json"),
        "payment": out.payment,
    }


def _tool_cancel_checkout(db: Session, state: MerchantAgentState, checkout_session_id: str | None = None) -> dict:
    csid = str(checkout_session_id or state.checkout_session_id or "")
    if not csid:
        raise ToolError(ErrorCode.CHECKOUT_NOT_FOUND.value, "no checkout session referenced")
    result = checkout_service.cancel_checkout(
        db,
        csid,
        buyer_agent_id=state.buyer_agent_id,
        delegation_token=state.delegation_token,
    )
    if isinstance(result, ProtocolError):
        return {
            "success": False,
            "tool": "cancel_checkout",
            "error": {
                "code": result.code.value,
                "message": result.message,
                "retryable": result.retryable,
                "details": result.details,
            },
        }
    return {"cancelled": True, "checkout_session_id": result.checkout_session_id, "status": result.status}


# ---------------------------------------------------------------- policy preview


def _tool_run_gating(db: Session, state: MerchantAgentState, checkout_session_id: str | None = None) -> dict:
    """Dry-run of the deterministic engine. The agent cannot modify the result."""
    from merchant.control import gating_service

    csid = str(checkout_session_id or state.checkout_session_id or "")
    preview_of = "existing checkout"
    try:
        if csid:
            cs = checkout_service.get_session(db, csid)
            raw_items = [
                {"product_id": i["product_id"], "pack_id": i["pack_id"], "quantity": i["quantity"]}
                for i in json.loads(cs.line_items)
            ]
            resolved = checkout_service.resolve_line_items(db, raw_items)
        else:
            preview_of = "intended basket"
            intended = state.current_items or state.requested_items
            if not intended:
                raise ToolError(ErrorCode.INVALID_REQUEST.value, "no basket to evaluate")
            resolved = checkout_service.resolve_line_items(
                db, [{"product_id": i["product_id"], "pack_id": i["pack_id"], "quantity": i["quantity"]} for i in intended]
            )
    except ToolError:
        raise
    except ProtocolError as exc:
        # Surface session-level problems as tool errors, not policy results.
        raise ToolError(exc.code.value, exc.message, retryable=exc.retryable) from exc
    try:
        totals = checkout_service.compute_totals(resolved)
        delegation = delegation_service.validate_delegation(db, state.buyer_agent_id, state.delegation_token)
        gating = gating_service.evaluate(db, delegation, resolved, totals["total"])
        return {
            "preview": True,
            "preview_of": preview_of,
            "passed": gating.passed,
            "totals": totals,
            "checks": [c.model_dump() for c in gating.checks],
            "note": "authoritative policy evaluation; cannot be modified by the agent",
        }
    except ProtocolError as exc:
        return {
            "preview": True,
            "preview_of": preview_of,
            "passed": False,
            "checks": [{"rule": "delegation", "result": "fail", "detail": exc.message}],
            "note": "delegation-level failure",
        }


# ---------------------------------------------------------------- confirmation


def _tool_request_confirmation(
    db: Session,
    state: MerchantAgentState,
    reason_code: str,
    original: dict,
    proposal: dict,
    message: str = "",
) -> dict:
    if not isinstance(original, dict) or not isinstance(proposal, dict):
        raise ToolError(ErrorCode.INVALID_REQUEST.value, "original and proposal must be objects")
    orig_pid, orig_kid = original.get("product_id"), original.get("pack_id")
    prop_pid, prop_kid = proposal.get("product_id"), proposal.get("pack_id")
    if not orig_pid or not orig_kid:
        raise ToolError(ErrorCode.INVALID_REQUEST.value, "original needs product_id and pack_id")
    if not prop_pid or not prop_kid:
        raise ToolError(ErrorCode.INVALID_REQUEST.value, "proposal needs product_id and pack_id")
    try:
        qty = max(1, int(proposal.get("quantity", 1)))
    except (TypeError, ValueError):
        raise ToolError(ErrorCode.INVALID_REQUEST.value, "proposal quantity must be an integer")

    load_pack(db, str(orig_pid), str(orig_kid))  # must reference a real request
    prop_product, prop_pack = load_pack(db, str(prop_pid), str(prop_kid))

    # Doomed-proposal prevention: never pause the buyer for something the
    # deterministic control plane is guaranteed to reject.
    constraints = delegation_constraints(db, state.delegation_token)
    max_qty = constraints.get("max_quantity_per_item")
    allowed_cats = constraints.get("allowed_categories")
    if max_qty is not None and qty > int(max_qty):
        raise ToolError(
            ErrorCode.INVALID_REQUEST.value,
            f"cannot propose {prop_pack.pack_id} x{qty}: exceeds delegation max_quantity_per_item"
            f"={max_qty}. Propose a compliant quantity or finish declined.",
            retryable=True,
            details={"proposed_quantity": qty, "max_quantity_per_item": max_qty},
        )
    if allowed_cats is not None and prop_product.category not in allowed_cats:
        raise ToolError(
            ErrorCode.CATEGORY_BLOCKED.value,
            f"cannot propose {prop_pack.pack_id}: category '{prop_product.category}' is not in the "
            "buyer's delegation allow-list.",
            details={"category": prop_product.category},
        )

    available_stock = available(db, prop_pack)
    if available_stock < qty:
        raise ToolError(
            ErrorCode.STOCK_UNAVAILABLE.value,
            f"cannot propose {prop_pack.pack_id}: available={available_stock}, needed={qty}",
            retryable=True,
        )

    state.pending_proposal = {
        "kind": "substitution",
        "status": "requires_confirmation",
        "reason_code": str(reason_code or "alternative_proposed"),
        "original": {
            "product_id": str(orig_pid),
            "pack_id": str(orig_kid),
            "quantity": max(1, int(original.get("quantity", 1) or 1)),
        },
        "proposal": {
            "product_id": prop_product.product_id,
            "pack_id": prop_pack.pack_id,
            "name": prop_product.name,
            "size": prop_pack.size,
            "unit": prop_pack.unit,
            "quantity": qty,
            "price": prop_pack.price,
            "currency": prop_pack.currency,
            "line_total": prop_pack.price * qty,
        },
        "message": str(message or ""),
    }
    return {
        "confirmation_requested": True,
        "proposal": state.pending_proposal["proposal"],
        "note": "paused until the buyer accepts or rejects this structured proposal",
    }


# ---------------------------------------------------------------- completion


def _tool_request_completion(db: Session, state: MerchantAgentState) -> dict:
    if state.execution_mode != "agent_completed":
        raise ToolError(
            ErrorCode.INVALID_REQUEST.value,
            "execution_mode is 'buyer_completes': the buyer triggers payment via the protocol. "
            "Your correct next action is finish(outcome='ready_for_completion').",
        )
    if not state.checkout_session_id:
        raise ToolError(ErrorCode.INVALID_REQUEST.value, "no checkout session to complete")
    attempts = int(state.fallback_ctx.get("completion_attempts", 0)) + 1
    state.fallback_ctx["completion_attempts"] = attempts
    key = f"ma_{state.agent_session_id}_complete_{attempts}"
    resp, replayed = checkout_service.complete_checkout(
        db,
        state.checkout_session_id,
        buyer_agent_id=state.buyer_agent_id,
        delegation_token=state.delegation_token,
        idempotency_key=key,
    )
    session_out = resp.checkout_session
    status = session_out.status

    if status == "completed":
        return {
            "completed": True,
            "checkout_session_id": session_out.checkout_session_id,
            "total": session_out.totals.get("total"),
            "currency": session_out.totals.get("currency", "INR"),
            "payment": session_out.payment,
        }

    error = resp.error or {}
    code = error.get("code", ErrorCode.PAYMENT_FAILED.value)

    if code == ErrorCode.PRICE_CHANGED.value:
        change = error.get("details", {})
        state.pending_proposal = {
            "kind": "terms_change",
            "status": "requires_confirmation",
            "reason_code": "price_changed",
            "original": {"total": change.get("old_total")},
            "proposal": {"total": change.get("new_total"), "version": session_out.version},
            "message": f"Total changed from ₹{change.get('old_total')} to ₹{change.get('new_total')}; buyer confirmation required",
        }
        return {
            "completed": False,
            "awaiting_confirmation": True,
            "code": code,
            "message": error.get("message", "terms changed"),
            "change": change,
        }

    if status == "requires_payment" and session_out.payment.get("awaiting_authorization"):
        return {
            "completed": False,
            "awaiting_payment_authorization": True,
            "checkout_session_id": session_out.checkout_session_id,
            "razorpay_order_id": session_out.razorpay_order_id,
            "key_id": session_out.payment.get("key_id"),
            "amount": session_out.totals.get("total"),
            "note": "human must authorize via Razorpay Checkout",
        }

    retryable = bool(error.get("retryable"))
    return {
        "success": False,
        "tool": "request_completion",
        "error": {
            "code": code,
            "message": error.get("message", f"completion ended in {status}"),
            "retryable": retryable,
            "details": {"checkout_session_id": session_out.checkout_session_id, "final_status": status},
        },
    }


# ---------------------------------------------------------------- registry


_HANDLERS: dict[str, Callable[..., dict]] = {
    "search_catalog": _tool_search_catalog,
    "get_product": _tool_get_product,
    "check_inventory": _tool_check_inventory,
    "get_current_price": _tool_get_current_price,
    "find_alternative": _tool_find_alternative,
    "get_checkout": _tool_get_checkout,
    "create_checkout": _tool_create_checkout,
    "update_checkout": _tool_update_checkout,
    "cancel_checkout": _tool_cancel_checkout,
    "run_gating": _tool_run_gating,
    "request_confirmation": _tool_request_confirmation,
    "request_completion": _tool_request_completion,
}


def execute_tool(name: str, args: dict, state: MerchantAgentState) -> dict:
    """Run one allowlisted tool under the §51 contract with budget enforcement."""
    if name not in ALL_TOOLS:
        return {
            "success": False,
            "tool": name,
            "error": {"code": "UNKNOWN_TOOL", "message": f"tool '{name}' is not allowlisted", "retryable": False},
        }
    if state.tool_call_count >= settings.ma_max_tool_calls and name != "finish":
        return {
            "success": False,
            "tool": name,
            "error": {
                "code": "TOOL_BUDGET_EXHAUSTED",
                "message": f"tool-call budget ({settings.ma_max_tool_calls}) exhausted",
                "retryable": False,
            },
        }

    args = args or {}
    handler = _HANDLERS[name]
    needs_write = name in WRITE_TOOLS or name == "request_completion"

    def _run(db: Session) -> dict:
        kwargs: dict[str, Any] = {"db": db, "state": state}
        sig_params = _handler_params(handler)
        for arg_name in sig_params - {"db", "state"}:
            if arg_name in args:
                kwargs[arg_name] = args[arg_name]
        return handler(**kwargs)

    try:
        if needs_write:
            with mutate() as db:
                result = _run(db)
        else:
            db = SessionLocal()
            try:
                result = _run(db)
            finally:
                db.close()
    except ToolError as exc:
        result = {
            "success": False,
            "tool": name,
            "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable, "details": exc.details},
        }
    except ProtocolError as exc:
        result = {
            "success": False,
            "tool": name,
            "error": {"code": exc.code.value, "message": exc.message, "retryable": exc.retryable, "details": exc.details},
        }
    except Exception as exc:  # noqa: BLE001 — never leak raw exceptions into the loop
        result = {
            "success": False,
            "tool": name,
            "error": {"code": "TOOL_EXECUTION_ERROR", "message": str(exc)[:200], "retryable": True},
        }

    if "success" not in result:
        # Normalize to the §51 contract: {"success", "tool", "result"}.
        result = {"success": True, "tool": name, "result": result}
    else:
        result.setdefault("tool", name)
    state.tool_call_count += 1  # real budget: every executed call counts, success or not
    state.tool_results.append(result)

    # Observation bookkeeping (decision-cycle optimization): successful
    # deterministic reads are cached per session; a later decision cycle asking
    # for the SAME read in an UNCHANGED world replays the envelope instead of
    # re-executing. Any write bumps the world, invalidating stale observations
    # (a replay is then refused and the read re-runs — always the safe direction).
    ctx = state.fallback_ctx
    if name not in READ_TOOLS:
        ctx["world_version"] = int(ctx.get("world_version", 0)) + 1
    elif result.get("success"):
        ctx.setdefault("observe_map", {})[f"{name}|{canonical_args(args)}"] = {
            "envelope": result,
            "world": int(ctx.get("world_version", 0)),
        }

    # Intent anchor: remember packs the agent itself verified as targets via
    # read tools — these become the only packs a basket write may contain
    # without a buyer-accepted proposal (anti-silent-substitution).
    if result.get("success") and name in ("check_inventory", "get_current_price", "find_alternative"):
        observed_pack = args.get("pack_id")
        if observed_pack:
            observed = state.fallback_ctx.setdefault("observed_pack_ids", [])
            if str(observed_pack) not in observed:
                observed.append(str(observed_pack))
    return result


def _handler_params(handler: Callable[..., dict]) -> set[str]:
    import inspect

    return set(inspect.signature(handler).parameters.keys())
