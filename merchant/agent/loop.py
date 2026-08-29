"""Merchant Agent orchestration loop (PRD §10, §30, §37–38, §50).

Observe -> Reason -> Act -> Observe Result -> Adapt, with two interchangeable
brains producing tool-call decisions:

    llm        — NIM chat-completion tool-loop (default)
    fallback   — deterministic decider used when the LLM is unavailable or
                 disabled (keeps demos/tests hermetic; mirrors the discovery
                 keyword-fallback philosophy)

Every executed action emits a hash-chained audit event (actor=merchant_agent).
Money movement is ONLY ever attempted through `request_completion`, which
delegates to the deterministic control plane (FR-MA-13/14).
"""
import json
import logging
import re
from typing import Any, Optional

from merchant.agent import prompts, tools
from merchant.agent.state import MerchantAgentState
from merchant.config import settings

logger = logging.getLogger("merchant.agent")

# Model-compat (F1): some models occasionally emit tool-call syntax or planning
# prose as plain text instead of a structured tool_calls response.
_LEAKED_TOOLCALL_RE = re.compile(r"<\|?(?:tool_call|eom_id)|call:\s*\w+\s*\{", re.IGNORECASE)


# ---------------------------------------------------------------- audit


def _args_digest(args: dict) -> dict:
    clean: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if isinstance(value, str):
            clean[key] = value[:120]
        elif isinstance(value, list):
            clean[key] = value[:5]
        elif isinstance(value, dict):
            clean[key] = {k: v for k, v in list(value.items())[:6]}
        else:
            clean[key] = value
    return clean


def _result_digest(result: dict, limit: int = 700) -> dict:
    text = json.dumps(result, default=str)
    return {"shape": sorted(result.keys()), "preview": text[:limit], "truncated": len(text) > limit}


def _audit(
    *,
    action: str,
    decision: str | None,
    summary: str,
    state: MerchantAgentState,
    payload: dict | None = None,
    rules_fired: list[str] | None = None,
) -> None:
    """Append one durable audit event (short-lived mutation session)."""
    from merchant.persistence.db import mutate
    from merchant.evidence import audit_service

    body = {
        "agent_session_id": state.agent_session_id,
        "iteration": state.iteration_count,
        **(payload or {}),
    }
    try:
        with mutate() as db:
            audit_service.append_event(
                db,
                actor="merchant_agent",
                action=action,
                checkout_session_id=state.checkout_session_id,
                decision=decision,
                rules_fired=rules_fired or [],
                reasoning_summary=(summary or "")[:400],
                payload=body,
            )
    except Exception:  # noqa: BLE001 — must never kill the loop mid-payment
        logger.exception("failed to persist agent audit event %s", action)


# ---------------------------------------------------------------- brains


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
    return (
        "TRANSACTION STATE (authoritative runtime facts, refreshed every turn):\n"
        f"- checkout_session_id: {state.checkout_session_id or 'none yet'}\n"
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


# ---- deterministic fallback brain ----


def _completion_or_ready(state: MerchantAgentState) -> tuple[str, dict, str]:
    if state.execution_mode == "agent_completed":
        return "request_completion", {}, "Checkout is live and policy-clean; requesting gated completion."
    return (
        "finish",
        {
            "outcome": "ready_for_completion",
            "reason_code": "basket_ready_for_buyer_payment",
            "message": "Basket is live under policy; the buyer completes payment via the protocol.",
        },
        "Handing the live basket to the buyer for payment.",
    )


def _declined(reason: str, message: str) -> tuple[str, dict, str]:
    return "finish", {"outcome": "declined", "reason_code": reason, "message": message}, f"Declining: {message}"


def _pick_candidate(results: list[dict], state: MerchantAgentState) -> Optional[dict]:
    """Deterministically select the best in-stock pack matching the request."""
    target = tools.parse_target_pack(state.buyer_request)
    excluded = set(state.excluded_pack_ids)
    for product in results:
        usable = [p for p in product["packs"] if p["available"] > 0 and p["pack_id"] not in excluded]
        if not usable:
            continue
        chosen = min(usable, key=lambda p: p["price"])
        same_unit = [p for p in usable if target and p["unit"] == target["unit"]]
        if same_unit:
            chosen = min(same_unit, key=lambda p: abs(p["size"] - target["size"]))
        return {
            "product_id": product["product_id"],
            "pack_id": chosen["pack_id"],
            "name": product["name"],
            "size": chosen["size"],
            "unit": chosen["unit"],
            "price": chosen["price"],
            "quantity": 1,
        }
    return None


def _fallback_decide(state: MerchantAgentState) -> tuple[str, dict, str]:
    ctx = state.fallback_ctx
    target = ctx.get("target")
    last = state.tool_results[-1] if state.tool_results else None

    def alt_args() -> dict:
        return {
            "product_id": target["product_id"],
            "pack_id": target["pack_id"],
            "quantity": target.get("quantity", 1),
        }

    if last is None:
        if state.requested_items:
            first = state.requested_items[0]
            ctx["target"] = {
                "product_id": first["product_id"],
                "pack_id": first["pack_id"],
                "quantity": int(first.get("quantity", 1)),
            }
            return (
                "check_inventory",
                {"pack_id": first["pack_id"]},
                f"Observing live inventory for requested pack {first['pack_id']} before acting.",
            )
        return "search_catalog", {"query": state.buyer_request}, "Observing the catalog for matching products."

    tool = last.get("tool")
    ok = bool(last.get("success"))
    error = last.get("error", {})

    if tool == "__nudge__":
        return _fallback_decide(state)

    if tool == "check_inventory":
        available = int((last.get("result") or {}).get("available", 0))
        if available >= target.get("quantity", 1):
            item = {
                "product_id": target["product_id"],
                "pack_id": target["pack_id"],
                "quantity": target.get("quantity", 1),
            }
            return (
                "create_checkout",
                {"items": [item]},
                f"{target['pack_id']} is in stock (available={available}); creating the checkout.",
            )
        return (
            "find_alternative",
            alt_args(),
            f"{target['pack_id']} unavailable (available={available}); searching a valid alternative.",
        )

    if tool == "search_catalog":
        results = (last.get("result") or {}).get("results", [])
        if not results:
            return _declined("no_matching_product", f"No catalog product matches '{state.buyer_request}'.")
        target_size = tools.parse_target_pack(state.buyer_request)
        # Prefer the exact requested pack; if it exists but is unavailable,
        # observe inventory and search alternatives (PRD §25) instead of
        # silently substituting a different size.
        if target_size:
            for product in results:
                same_unit = [p for p in product["packs"] if p["unit"] == target_size["unit"]]
                if not same_unit:
                    continue
                nearest = min(same_unit, key=lambda p: abs(p["size"] - target_size["size"]))
                if abs(nearest["size"] - target_size["size"]) < 1e-9:
                    ctx["target"] = {
                        "product_id": product["product_id"],
                        "pack_id": nearest["pack_id"],
                        "quantity": 1,
                    }
                    return (
                        "check_inventory",
                        {"pack_id": nearest["pack_id"]},
                        f"Verifying live stock for the exact requested pack {nearest['pack_id']}.",
                    )
        chosen = _pick_candidate(results, state)
        if chosen is None:
            top = results[0]["packs"][0]
            ctx["target"] = {
                "product_id": results[0]["product_id"],
                "pack_id": top["pack_id"],
                "quantity": 1,
            }
            return "check_inventory", {"pack_id": top["pack_id"]}, "Verifying inventory of closest match."
        ctx["target"] = chosen
        item = {"product_id": chosen["product_id"], "pack_id": chosen["pack_id"], "quantity": chosen["quantity"]}
        return (
            "create_checkout",
            {"items": [item]},
            f"Selected {chosen['name']} ({chosen['size']}{chosen['unit']}) at Rs.{chosen['price']}.",
        )

    if tool in ("create_checkout", "update_checkout"):
        if ok:
            ctx["checkout_live"] = True
            ctx.pop("checkout_dead", None)
            return _completion_or_ready(state)
        code = error.get("code", "")
        if code == "STOCK_UNAVAILABLE" and target:
            return "find_alternative", alt_args(), f"Basket rejected ({error.get('message')}); hunting an alternative."
        return _declined("policy_declined", error.get("message", "Deterministic policy rejected the basket."))

    if tool == "find_alternative":
        alts = (last.get("result") or {}).get("alternatives", [])
        offered = ctx.setdefault("offered_pack_ids", [])
        candidate = next((a for a in alts if a["pack_id"] not in offered), None)
        if candidate is None:
            return _declined(
                "no_valid_alternative",
                "Requested item unavailable and no valid merchant alternative found.",
            )
        offered.append(candidate["pack_id"])
        return (
            "request_confirmation",
            {
                "reason_code": "requested_pack_unavailable",
                "original": {
                    "product_id": target["product_id"],
                    "pack_id": target["pack_id"],
                    "quantity": target.get("quantity", 1),
                },
                "proposal": {
                    "product_id": candidate["product_id"],
                    "pack_id": candidate["pack_id"],
                    "quantity": candidate["quantity"],
                },
                "message": (
                    f"Requested pack unavailable; propose {candidate['name']} "
                    f"({candidate['size']}{candidate['unit']}) x{candidate['quantity']} at Rs.{candidate['price']}."
                ),
            },
            "Found a valid alternative; pausing for structured buyer confirmation.",
        )

    if tool == "request_confirmation":
        return "find_alternative", alt_args(), f"Proposal invalid ({error.get('message')}); trying the next alternative."

    if tool == "request_completion":
        code = error.get("code", "")
        if code == "PRICE_CHANGED":
            return (
                "finish",
                {
                    "outcome": "ready_for_completion",
                    "reason_code": "awaiting_terms_confirmation",
                    "message": "Terms changed; buyer confirmation requested.",
                },
                "Price changed at completion; confirmation proposed.",
            )
        if code == "STOCK_UNAVAILABLE" and target:
            ctx["checkout_dead"] = True
            ctx["checkout_live"] = False
            state.checkout_session_id = None
            return (
                "find_alternative",
                alt_args(),
                "Stock disappeared at final validation; recovering through an alternative.",
            )
        return _declined(
            (code or "completion_failed").lower(),
            error.get("message", "Completion declined by the deterministic control plane."),
        )

    if tool == "buyer_decision":
        if last.get("kind") == "terms_change" or last.get("accepted"):
            return _completion_or_ready(state)
        return "find_alternative", alt_args(), "Buyer rejected the proposal; looking for another option."

    if tool == "terms_confirmed":
        return _completion_or_ready(state)

    return _declined("unexpected_state", f"No fallback rule for last tool '{tool}'.")


def _next_action(state: MerchantAgentState) -> tuple[str, dict, str, Optional[str]]:
    override = state.fallback_ctx.get("brain_override")
    use_llm = (override == "llm") if override else settings.ma_llm_brain
    if use_llm and not state.fallback_ctx.get("llm_disabled"):
        try:
            return _llm_decide(state)
        except _Finish:
            raise
        except Exception as exc:  # noqa: BLE001 — degrade to the deterministic brain
            logger.warning("LLM brain unavailable (%s); falling back to deterministic decider", exc)
            state.fallback_ctx["llm_disabled"] = True
    name, args, reasoning = _fallback_decide(state)
    return name, args, reasoning, None


# ---------------------------------------------------------------- loop


class MerchantAgent:
    """Runs/resumes one transaction-scoped agent session."""

    # -- public API --------------------------------------------------

    def run(self, state: MerchantAgentState) -> MerchantAgentState:
        while state.status == "running":
            state.iteration_count += 1
            # Each buyer decision opens one fresh reasoning window: the pause is
            # a protocol boundary (human think-time), not agent dithering, and
            # must not starve the short apply/complete leg that follows it.
            # Tool-call budgets stay global and hard.
            budget = settings.ma_max_iterations * min(
                4, max(1, int(state.fallback_ctx.get("budget_windows", 1)))
            )
            if state.iteration_count > budget:
                self._terminate(
                    state,
                    "unable_to_fulfill",
                    "merchant_agent_unable_to_fulfill",
                    f"Iteration budget ({budget}) exhausted; no payment attempted.",
                )
                break
            try:
                name, args, reasoning, tool_call_id = _next_action(state)
            except _Finish as fin:
                self._apply_finish(state, fin.outcome, fin.reason_code, fin.message)
                break

            if name == "__nudge__":
                _audit(action="agent_reasoning_only", decision=None, summary=reasoning, state=state)
                continue
            if name == "finish":
                outcome = str(args.get("outcome", "declined"))
                hold = self._finish_hold_reason(state, outcome)
                if hold == "decline":
                    state.fallback_ctx["decline_confirmed"] = True
                    self._request_decline_confirmation(state, args)
                    continue
                if hold == "mode":
                    state.fallback_ctx["mode_confirmed"] = True
                    self._request_mode_correction(state, args)
                    continue
                if hold == "unpaid":
                    # Payment truth is non-negotiable: the first claim gets a
                    # corrective turn; an insisted 'completed' without money
                    # moving is downgraded to the honest handoff.
                    if state.fallback_ctx.get("completion_confirmed"):
                        self._apply_finish(
                            state,
                            "ready_for_completion",
                            "unverified_completion_downgraded",
                            "Model insisted on outcome 'completed' with no gateway-confirmed "
                            "payment; downgraded to the buyer handoff.",
                        )
                        break
                    state.fallback_ctx["completion_confirmed"] = True
                    self._request_completion_truth(state, args)
                    continue
                self._apply_finish(
                    state,
                    outcome,
                    str(args.get("reason_code", "finished")),
                    str(args.get("message", "")),
                )
                break

            state.record_action(name, _args_digest(args), reasoning)
            envelope = tools.execute_tool(name, args, state)
            if tool_call_id is not None:
                # Close the OpenAI tool-call round: the model must observe its
                # tool result before choosing the next action.
                state.llm_history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps(envelope, default=str)[:1800],
                    }
                )
            ok = bool(envelope.get("success"))
            _audit(
                action=f"agent_{name}",
                decision="approved" if ok else "declined",
                summary=reasoning or f"executed {name}",
                state=state,
                payload={"tool": name, "args": _args_digest(args), "result": _result_digest(envelope)},
                rules_fired=[] if ok else [envelope.get("error", {}).get("code", "UNKNOWN")],
            )
            self._guide_next_action(state, name, envelope)

            if self._post_action(state, name, envelope):
                break
        return state

    def resume(self, state: MerchantAgentState, accepted: bool, note: str = "") -> MerchantAgentState:
        """Apply a buyer decision on the pending proposal, then continue the loop."""
        proposal = state.pending_proposal
        if proposal is None:
            raise tools.ToolError("INVALID_REQUEST", "no pending proposal for this agent session")

        kind = proposal.get("kind", "substitution")
        state.pending_proposal = None
        state.status = "running"
        state.fallback_ctx["budget_windows"] = min(
            4, int(state.fallback_ctx.get("budget_windows", 1)) + 1
        )

        _audit(
            action="buyer_proposal_decision" if kind == "substitution" else "buyer_terms_decision",
            decision="approved" if accepted else "declined",
            summary=(note or ("buyer accepted" if accepted else "buyer rejected"))[:200],
            state=state,
            payload={"kind": kind, "accepted": accepted, "proposal": proposal.get("proposal")},
        )

        if kind == "terms_change":
            if not accepted:
                self._note_for_llm(state, {"event": "buyer_rejected_new_terms", "instruction": "terminate gracefully; nothing was charged"})
                self._reject_terms(state)
                return state
            self._confirm_terms(state, proposal)
            return self.run(state)

        if not accepted:
            rejected_pack = (proposal.get("proposal") or {}).get("pack_id")
            if rejected_pack and rejected_pack not in state.excluded_pack_ids:
                state.excluded_pack_ids.append(rejected_pack)
            state.rejected_proposals.append(proposal)
            state.tool_results.append({"tool": "buyer_decision", "success": True, "accepted": False, "kind": kind})
            self._note_for_llm(
                state,
                {
                    "event": "buyer_rejected_proposal",
                    "rejected_pack_id": rejected_pack,
                    "instruction": (
                        "The buyer REJECTED this substitution. Find a different valid alternative "
                        "(find_alternative) or finish declined — never re-propose the same pack."
                    ),
                },
            )
            return self.run(state)

        self._accept_substitution(state, proposal)
        return self.run(state)

    # -- internal ----------------------------------------------------

    def _note_for_llm(self, state: MerchantAgentState, content: dict) -> None:
        """Feed a system-applied outcome back into the LLM transcript so the
        model observes it before its next decision (keeps the OpenAI tool
        protocol consistent across pause/resume)."""
        if not settings.ma_llm_brain or not state.llm_history:
            return
        text = json.dumps(content, default=str)[:1500]
        last = state.llm_history[-1]
        if last.get("role") == "assistant" and last.get("tool_calls"):
            state.llm_history.append(
                {"role": "tool", "tool_call_id": last["tool_calls"][-1]["id"], "content": text}
            )
        else:
            state.llm_history.append({"role": "user", "content": text})

    def _guide_next_action(self, state: MerchantAgentState, name: str, envelope: dict) -> None:
        """Deterministic handoff notes injected after decisive tool results so
        the next decision copies protocol facts instead of inferring them."""
        ok = bool(envelope.get("success"))
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        code = str(error.get("code") or "")
        note: Optional[str] = None
        if name in ("create_checkout", "update_checkout") and ok:
            csid = state.checkout_session_id or "unknown"
            if state.execution_mode == "agent_completed":
                note = (
                    f"Checkout {csid} is live and passed all deterministic checks. Call "
                    "request_completion now to execute the gated payment."
                )
            else:
                note = (
                    f"Checkout {csid} is live and passed all deterministic checks. You cannot request "
                    "payment in buyer_completes mode — call finish(outcome='ready_for_completion', "
                    f"reason_code='basket_ready_for_buyer_payment') for {csid}."
                )
        elif code in ("CHECKOUT_NOT_FOUND", "PACK_NOT_FOUND", "PRODUCT_NOT_FOUND"):
            note = (
                f"{code}: that identifier does not exist on the merchant side. Use ONLY ids exactly "
                f"as returned by tool results. Known checkout_session_id: "
                f"{state.checkout_session_id or 'none yet'}."
            )
        if not note or state.fallback_ctx.get("last_guidance") == note:
            return
        state.fallback_ctx["last_guidance"] = note
        self._note_for_llm(state, {"event": "runtime_guidance", "instruction": note})

    def _warrants_decline_confirm(self, state: MerchantAgentState) -> bool:
        """F1 guardrail: one-time hold when the model tries to abandon a live,
        policy-approved basket in buyer_completes mode. The second decision is
        always honored; iteration budgets still apply."""
        if state.fallback_ctx.get("decline_confirmed"):
            return False
        if state.execution_mode != "buyer_completes":
            return False
        return self._live_checkout_status(state) == "requires_payment"

    def _finish_hold_reason(self, state: MerchantAgentState, outcome: str) -> Optional[str]:
        """One-time corrective holds on finish() calls that contradict the
        session's execution mode or payment truth while a policy-clean basket
        is live. Each hold fires at most once per session; the second decision
        is honored — except 'completed', which can never be accepted without a
        gateway-confirmed payment and is downgraded to the honest handoff."""
        ctx = state.fallback_ctx

        # Payment-truth guard (STEP 9/12): only a completed checkout in the DB
        # makes outcome='completed' truthful. The caller holds the first claim
        # and downgrades any insisted repeat.
        if outcome == "completed":
            cs_status = self._live_checkout_status(state)
            if cs_status != "completed":
                return "unpaid"

        live = self._live_checkout_status(state) == "requires_payment"
        if (
            outcome == "declined"
            and not ctx.get("decline_confirmed")
            and state.execution_mode == "buyer_completes"
            and live
        ):
            return "decline"
        if (
            outcome == "ready_for_completion"
            and not ctx.get("mode_confirmed")
            and state.execution_mode == "agent_completed"
            and live
        ):
            return "mode"
        return None

    def _request_completion_truth(self, state: MerchantAgentState, args: dict) -> None:
        csid = state.checkout_session_id or "none"
        _audit(
            action="agent_unpaid_completed_hold",
            decision=None,
            summary=(
                f"finish(outcome=completed) rejected: no gateway-confirmed payment exists for "
                f"{csid}; model must request_completion or hand off honestly"
            ),
            state=state,
            payload={"attempted_reason_code": args.get("reason_code")},
        )
        self._note_for_llm(
            state,
            {
                "event": "unverified_completion_rejected",
                "instruction": (
                    "finish(outcome='completed') was REJECTED: NO payment has occurred. Money may "
                    "only move through request_completion (deterministic ladder + Razorpay). Call "
                    "request_completion now, or finish with outcome 'ready_for_completion' / "
                    "'declined' to hand off honestly."
                ),
            },
        )

    def _request_mode_correction(self, state: MerchantAgentState, args: dict) -> None:
        csid = state.checkout_session_id or "none"
        _audit(
            action="agent_mode_hold",
            decision=None,
            summary=(
                f"finish(ready_for_completion) held once in agent_completed mode: checkout {csid} "
                "is live; model must call request_completion or confirm the handoff"
            ),
            state=state,
            payload={"attempted_reason_code": args.get("reason_code")},
        )
        self._note_for_llm(
            state,
            {
                "event": "mode_correction_required",
                "instruction": (
                    "Mode correction: this session runs execution_mode='agent_completed' — YOU are "
                    f"responsible for executing the gated payment. Checkout {csid} is live and "
                    "policy-clean: call request_completion. Do NOT use finish('ready_for_completion'); "
                    "that handoff exists only in buyer_completes mode."
                ),
            },
        )

    def _request_decline_confirmation(self, state: MerchantAgentState, args: dict) -> None:
        csid = state.checkout_session_id or "none"
        _audit(
            action="agent_decline_hold",
            decision=None,
            summary=(
                f"finish(declined) held once: checkout {csid} is live and policy-approved; "
                "model must confirm or hand off"
            ),
            state=state,
            payload={"attempted_reason_code": args.get("reason_code")},
        )
        self._note_for_llm(
            state,
            {
                "event": "decline_confirmation_required",
                "instruction": (
                    f"A valid checkout ({csid}) is live and policy-approved. If you truly intend to "
                    "abandon it, call finish again with outcome 'declined' and reason_code "
                    "'explicit_decline'. Otherwise call finish with outcome 'ready_for_completion'."
                ),
            },
        )

    def _live_checkout_status(self, state: MerchantAgentState) -> Optional[str]:
        if not state.checkout_session_id:
            return None
        from merchant.persistence.db import SessionLocal
        from merchant.persistence.models.entities import CheckoutSession

        db = SessionLocal()
        try:
            cs = db.get(CheckoutSession, state.checkout_session_id)
            return cs.status if cs is not None else None
        finally:
            db.close()

    def _accept_substitution(self, state: MerchantAgentState, proposal: dict) -> None:
        """Apply a buyer-approved alternative through the normal tool path."""
        prop = proposal["proposal"]
        items = [{"product_id": prop["product_id"], "pack_id": prop["pack_id"], "quantity": int(prop["quantity"])}]
        accepted = state.fallback_ctx.setdefault("accepted_proposal_packs", [])
        if prop["pack_id"] not in accepted:
            accepted.append(str(prop["pack_id"]))
        status = self._live_checkout_status(state)
        if status == "requires_payment":
            envelope = tools.execute_tool("update_checkout", {"items": items}, state)
        else:
            state.checkout_session_id = None  # previous session terminal/declined — start fresh
            envelope = tools.execute_tool(
                "create_checkout", {"items": items}, state
            )
        ok = bool(envelope.get("success"))
        self._note_for_llm(
            state,
            {
                "event": "buyer_accepted_proposal",
                "applied_tool": envelope.get("tool"),
                "result": envelope if not ok else envelope.get("result", envelope),
                "checkout_session_id": state.checkout_session_id,
                "instruction": (
                    "The buyer's accepted alternative was applied for you. Continue: verify state "
                    "if needed, then complete per execution mode."
                ),
            },
        )
        _audit(
            action="agent_apply_accepted_alternative",
            decision="approved" if ok else "declined",
            summary=f"Applied buyer-approved alternative {prop['pack_id']} x{prop['quantity']}",
            state=state,
            payload={"tool": envelope.get("tool"), "result": _result_digest(envelope)},
            rules_fired=[] if ok else [envelope.get("error", {}).get("code", "UNKNOWN")],
        )

    def _confirm_terms(self, state: MerchantAgentState, proposal: dict) -> None:
        """Buyer accepted changed terms (price change) -> confirm and continue."""
        from merchant.persistence.db import mutate
        from merchant.domain import checkout_service

        version = int((proposal.get("proposal") or {}).get("version", 0))
        with mutate() as db:
            result = checkout_service.confirm_checkout(
                db,
                state.checkout_session_id,
                buyer_agent_id=state.buyer_agent_id,
                delegation_token=state.delegation_token,
                accept_new_terms=True,
                version=version,
            )
            ok = not isinstance(result, tools.ProtocolError)
            detail = "terms confirmed" if ok else getattr(result, "message", "confirmation failed")
        state.tool_results.append({"tool": "terms_confirmed", "success": ok, "detail": detail})
        _audit(
            action="agent_terms_confirmed",
            decision="approved" if ok else "declined",
            summary=f"Buyer accepted new terms on {state.checkout_session_id}: {detail}",
            state=state,
        )

    def _reject_terms(self, state: MerchantAgentState) -> None:
        """Buyer declined new terms -> cancel session, graceful decline."""
        from merchant.persistence.db import mutate
        from merchant.domain import checkout_service

        with mutate() as db:
            checkout_service.cancel_checkout(
                db,
                state.checkout_session_id,
                buyer_agent_id=state.buyer_agent_id,
                delegation_token=state.delegation_token,
            )
        self._terminate(state, "declined", "buyer_rejected_new_terms", "Buyer declined the changed terms; nothing was charged.")

    def _post_action(self, state: MerchantAgentState, name: str, envelope: dict) -> bool:
        """Handle pause/terminal consequences of one executed tool. Returns True when loop must stop."""
        if name == "request_confirmation" and envelope.get("success"):
            state.status = "awaiting_buyer_confirmation"
            _audit(
                action="agent_awaiting_buyer_confirmation",
                decision=None,
                summary=(state.pending_proposal or {}).get("message", "structured proposal sent to buyer"),
                state=state,
                payload={"proposal": state.pending_proposal},
            )
            return True

        if name == "request_completion":
            result = envelope.get("result") or {}
            error = envelope.get("error")
            if envelope.get("success") and result.get("completed"):
                payment = result.get("payment") or {}
                self._terminate(
                    state,
                    "completed",
                    "fulfilled_after_gated_completion",
                    f"Paid Rs.{result.get('total')} after deterministic gating approved.",
                    payment={
                        "order_id": payment.get("order_id"),
                        "payment_id": payment.get("payment_id"),
                        "simulated": payment.get("simulated"),
                        "driver": payment.get("driver"),
                    },
                    total=result.get("total"),
                    checkout_session_id=result.get("checkout_session_id"),
                )
                return True
            if isinstance(error, dict):
                code = error.get("code", "")
                if code == "PRICE_CHANGED":
                    state.status = "awaiting_buyer_confirmation"
                    _audit(
                        action="agent_awaiting_terms_confirmation",
                        decision=None,
                        summary="Price changed at completion; structured confirmation requested.",
                        state=state,
                        payload={"proposal": state.pending_proposal},
                    )
                    return True
                details = error.get("details", {})
                if code == "STOCK_UNAVAILABLE":
                    failed_packs = [
                        part.split(":")[0].strip()
                        for part in str(error.get("message", "")).split(";")
                        if ":" in part and "requested=" in part
                    ]
                    for item in state.current_items or []:
                        if item.get("name") in failed_packs and item.get("pack_id"):
                            if item["pack_id"] not in state.excluded_pack_ids:
                                state.excluded_pack_ids.append(item["pack_id"])
                    state.tool_results.append(
                        {
                            "tool": "request_completion",
                            "success": False,
                            "error": error,
                            "details": details,
                        }
                    )
                    return False
                self._terminate(
                    state,
                    "declined",
                    code.lower() or "completion_failed",
                    error.get("message", "Completion declined by deterministic control plane."),
                )
                return True
            if result.get("awaiting_payment_authorization"):
                state.status = "awaiting_payment_authorization"
                state.outcome = {
                    "status": "awaiting_payment_authorization",
                    "reason_code": "awaiting_payment_authorization",
                    "message": "Deterministic gate passed; awaiting human Razorpay authorization.",
                    "checkout_session_id": result.get("checkout_session_id"),
                    "razorpay_order_id": result.get("razorpay_order_id"),
                    "key_id": result.get("key_id"),
                    "amount": result.get("amount"),
                }
                return True

        # Tool-budget exhaustion surfaces as a failure envelope from any tool.
        error = envelope.get("error")
        if isinstance(error, dict) and error.get("code") == "TOOL_BUDGET_EXHAUSTED":
            self._terminate(
                state,
                "unable_to_fulfill",
                "merchant_agent_unable_to_fulfill",
                f"Tool-call budget ({settings.ma_max_tool_calls}) exhausted; no payment attempted.",
            )
            return True
        return False

    def _apply_finish(self, state: MerchantAgentState, outcome: str, reason_code: str, message: str) -> None:
        outcome = outcome if outcome in ("completed", "ready_for_completion", "declined") else "declined"
        if outcome == "completed":
            self._terminate(state, "completed", reason_code, message)
        elif outcome == "ready_for_completion":
            state.status = "ready_for_completion"
            state.outcome = {
                "status": "ready_for_completion",
                "reason_code": reason_code,
                "message": message,
                "checkout_session_id": state.checkout_session_id,
            }
            _audit(action="agent_ready_for_completion", decision=None, summary=message, state=state)
        else:
            self._terminate(state, "declined", reason_code, message)

    def _terminate(
        self,
        state: MerchantAgentState,
        status: str,
        reason_code: str,
        message: str,
        **extra: Any,
    ) -> None:
        state.set_outcome(status, reason_code, message, **extra)
        _audit(
            action="agent_outcome",
            decision="approved" if status == "completed" else status,
            summary=message,
            state=state,
            payload={"reason_code": reason_code, **extra},
        )


# Backwards-friendly alias matching PRD §50 naming.
MerchantAgent.handle = MerchantAgent.run  # type: ignore[attr-defined]
