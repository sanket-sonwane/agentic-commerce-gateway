"""Merchant Agent LLM brain — chat-completion decision loop (PRD §10, §30).

Builds the per-turn protocol context and asks the NIM model for exactly ONE
next tool call. Kept separate from orchestration so the model-facing
transcript/context logic is unit-visible and the deterministic fallback brain
shares none of it.

Every executed action still emits a hash-chained audit event in the
orchestration layer; money movement is ONLY ever attempted through
`request_completion` (FR-MA-13/14).
"""
import json
import re
from typing import Optional

from merchant.agent import prompts
from merchant.agent.state import MerchantAgentState
from merchant.config import settings

# Model-compat (F1): some models occasionally emit tool-call syntax or planning
# prose as plain text instead of a structured tool_calls response.
_LEAKED_TOOLCALL_RE = re.compile(r"<\|?(?:tool_call|eom_id)|call:\s*\w+\s*\{", re.IGNORECASE)


class _Finish(Exception):
    def __init__(self, outcome: str, reason_code: str, message: str):
        self.outcome = outcome
        self.reason_code = reason_code
        self.message = message
        super().__init__(message)


def _initial_context(state: MerchantAgentState) -> str:
    from merchant.persistence.db import SessionLocal
    from merchant.control import delegation_service

    parts = [
        f"BUYER REQUEST: {state.buyer_request}",
        "TARGET ITEMS: "
        + (
            json.dumps(state.requested_items)
            if state.requested_items
            else "(none pre-parsed - resolve them from the request yourself)"
        ),
    ]
    db = SessionLocal()
    try:
        try:
            d = delegation_service.get_delegation(db, state.delegation_token)
            if d is not None and d.status == "active":
                parts.append(
                    "DELEGATION CONTEXT: "
                    + json.dumps(
                        {
                            "max_order_value": d.max_order_value,
                            "max_quantity_per_item": d.max_quantity_per_item,
                            "allowed_categories": delegation_service.allowed_categories(d),
                        }
                    )
                )
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()
    if state.excluded_pack_ids:
        parts.append(f"PACKS TO AVOID (rejected/out-of-stock earlier): {state.excluded_pack_ids}")
    parts.append("Begin by observing merchant state with a tool. Exactly ONE tool call per turn.")
    return "\n".join(parts)


def _requested_quantity(state: MerchantAgentState) -> int:
    if state.requested_items:
        try:
            return max(1, int(state.requested_items[0].get("quantity", 1)))
        except (TypeError, ValueError):
            pass
    return 1


def _transaction_state_note(state: MerchantAgentState) -> str:
    """Deterministic per-turn protocol facts.

    Rebuilt fresh before every model call and spliced at a fixed slot so a
    small model never has to reconstruct identifiers, execution mode or
    quantity semantics from free-form history (audit failures F1–F3).
    """
    if state.execution_mode == "agent_completed":
        mode_line = (
            "execution_mode=agent_completed: when the checkout is live and policy-clean YOUR JOB is "
            "to call request_completion yourself. NEVER finish 'ready_for_completion' — that outcome "
            "belongs to buyer_completes mode only."
        )
    else:
        mode_line = (
            "execution_mode=buyer_completes: YOU CANNOT REQUEST PAYMENT. When create_checkout or "
            "update_checkout succeeds, your next action is finish(outcome='ready_for_completion')."
        )
    basket_digest = (
        "none yet"
        if not state.current_items
        else ", ".join(f"{i.get('pack_id')} x{i.get('quantity')}" for i in state.current_items[:5])
    )
    return (
        "TRANSACTION STATE (authoritative runtime facts, refreshed every turn):\n"
        f"- checkout_session_id: {state.checkout_session_id or 'none yet'}\n"
        f"- current basket: {basket_digest}\n"
        f"- {mode_line}\n"
        "- requested_quantity: "
        f"{_requested_quantity(state)} (set by the buyer request; NEVER copy stock/inventory "
        "numbers into quantity arguments)\n"
        "- Use only product_id/pack_id/checkout_session_id values exactly as returned by tools."
    )


def _exposed_tool_schemas(state: MerchantAgentState) -> list[dict]:
    """Mode-aware allowlist.

    request_completion remains implemented and server-guarded in both modes;
    it is only OFFERED to the model where completion is that agent's own
    responsibility (defense in depth: hiding temptation, keeping the guard).
    """
    if state.execution_mode == "agent_completed":
        return list(prompts.TOOL_SCHEMAS)
    return [s for s in prompts.TOOL_SCHEMAS if s["function"]["name"] != "request_completion"]


def _search_digest_scaffold(state: MerchantAgentState) -> Optional[str]:
    """F1: one-time scaffold after a planning-only turn — hand the model the
    exact candidate facts so its next action is a concrete tool call."""
    for envelope in reversed(state.tool_results):
        if envelope.get("tool") == "search_catalog" and envelope.get("success"):
            results = (envelope.get("result") or {}).get("results") or []
            if not results:
                return (
                    "Your last search returned NO matching products. Either call search_catalog "
                    "with different keywords or finish(outcome='declined', "
                    "reason_code='no_matching_product')."
                )
            lines = []
            for product in results[:3]:
                for pack in (product.get("packs") or [])[:2]:
                    lines.append(
                        f"{product['product_id']} | {pack['pack_id']} | {product['name']} "
                        f"{pack['size']}{pack['unit']} Rs.{pack['price']} available={pack.get('available', 0)}"
                    )
            return (
                "CANDIDATES from your last search (product_id | pack_id | name | price | stock):\n"
                + "\n".join(lines)
                + "\nYour next action MUST be create_checkout(items=[...]) using these exact ids, "
                "or find_alternative, or finish declined. Do NOT write plans as text."
            )
    return None


def _llm_decide(state: MerchantAgentState) -> tuple[str, dict, str, Optional[str]]:
    """Ask the NIM model for the next single action.

    Returns (tool_name, args, reasoning, tool_call_id) — the id is needed to
    append the matching role=tool result message afterwards. Raises on
    transport failure.
    """
    from openai import OpenAI

    # Decision-cycle evidence: one honest NIM round-trip per deliberation.
    state.fallback_ctx["llm_call_count"] = int(state.fallback_ctx.get("llm_call_count", 0)) + 1

    client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=45.0)
    messages = [
        {"role": "system", "content": prompts.SYSTEM_PROMPT},
        {"role": "user", "content": _transaction_state_note(state)},
    ]
    if not state.llm_history:
        state.llm_history.append({"role": "user", "content": _initial_context(state)})
    messages += state.llm_history

    resp = client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        tools=[*_exposed_tool_schemas(state), prompts.FINISH_TOOL],
        temperature=0.2,
        max_tokens=700,
    )
    msg = resp.choices[0].message

    # Model-compat: some NIM models (e.g. diffusiongemma) return content=""
    # on tool-call turns; their endpoint rejects EMPTY assistant content with
    # HTTP 400 on the next round. Normalize "" -> None everywhere below.
    if not msg.tool_calls:
        content = ((msg.content or "").strip() or None)
        state.llm_history.append({"role": "assistant", "content": content})
        nudges = int(state.fallback_ctx.get("llm_nudges", 0)) + 1
        state.fallback_ctx["llm_nudges"] = nudges
        if nudges >= 3:
            raise _Finish("declined", "agent_unable_to_progress", "Agent produced no actionable tool call.")
        leaked = bool(content and _LEAKED_TOOLCALL_RE.search(content))
        follow_up = (
            "Your last reply leaked tool-call syntax as plain text. Emit a proper structured "
            "tool_calls response using one of the provided tools."
            if leaked
            else "Respond ONLY by calling one of the provided tools. Continue fulfilling the request."
        )
        if not state.fallback_ctx.get("scaffold_sent"):
            scaffold = _search_digest_scaffold(state)
            if scaffold:
                state.fallback_ctx["scaffold_sent"] = True
                follow_up = scaffold
        state.llm_history.append({"role": "user", "content": follow_up})
        return "__nudge__", {}, content or "", None

    tc_payload = [
        {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
        for tc in msg.tool_calls
    ]
    state.llm_history.append({"role": "assistant", "content": (msg.content or "").strip() or None, "tool_calls": tc_payload})

    primary = msg.tool_calls[0]
    try:
        args = json.loads(primary.function.arguments or "{}")
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError:
        args = {}
    for extra in msg.tool_calls[1:]:
        state.llm_history.append(
            {
                "role": "tool",
                "tool_call_id": extra.id,
                "content": json.dumps({"skipped": "one action per turn"}),
            }
        )
    if primary.function.name != "finish":
        state.fallback_ctx["llm_nudges"] = 0
    return primary.function.name, args, (msg.content or "").strip(), primary.id