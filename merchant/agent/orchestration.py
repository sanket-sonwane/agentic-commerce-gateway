"""Merchant Agent orchestration loop (PRD §10, §30, §37–38, §50).

Observe -> Reason -> Act -> Observe Result -> Adapt, driving three focused
modules:

    llm_brain      — NIM chat-completion tool-loop (default)
    fallback_brain — deterministic decider used when the LLM is unavailable or
                     disabled (keeps demos/tests hermetic)
    tools          — allowlisted capability registry (executes a decision)

Every executed action emits a hash-chained audit event (actor=merchant_agent).
Money movement is ONLY ever attempted through `request_completion`, which
delegates to the deterministic control plane (FR-MA-13/14).
"""
import json
import logging
import time
from typing import Any, Optional

from merchant.agent import timing as agent_timing
from merchant.agent import tools
from merchant.agent.fallback_brain import _fallback_decide
from merchant.agent.llm_brain import _Finish, _llm_decide
from merchant.agent.state import MerchantAgentState
from merchant.config import settings

logger = logging.getLogger("merchant.agent")


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


# ---------------------------------------------------------------- brain routing


def _next_action(state: MerchantAgentState) -> tuple[str, dict, str, Optional[str]]:
    override = state.fallback_ctx.get("brain_override")
    use_llm = (override == "llm") if override else settings.ma_llm_brain
    if use_llm and not state.fallback_ctx.get("llm_disabled"):
        start = time.perf_counter()
        try:
            return _llm_decide(state)
        except _Finish:
            raise
        except Exception as exc:  # noqa: BLE001 — degrade to the deterministic brain
            logger.warning("LLM brain unavailable (%s); falling back to deterministic decider", exc)
            state.fallback_ctx["llm_disabled"] = True
            agent_timing.record(state.agent_session_id, "llm_decide", start,
                                time.perf_counter(), outcome="fallback_error", llm_model=settings.llm_model)
        else:
            agent_timing.record(state.agent_session_id, "llm_decide", start,
                                time.perf_counter(), outcome="ok", llm_model=settings.llm_model)
    start = time.perf_counter()
    name, args, reasoning = _fallback_decide(state)
    agent_timing.record(state.agent_session_id, "fallback_decide", start,
                        time.perf_counter(), outcome="ok")
    return name, args, reasoning, None


def _maybe_replay_observation(state: MerchantAgentState, name: str, args: dict) -> Optional[dict]:
    """Replay a deterministic read whose world has not moved since observation.

    Only successful READ_TOOLS are cached (by tools.execute_tool). A replay is
    exact when no write intervened: any write bumps world_version and forces a
    fresh read (over-invalidation is always the SAFE direction). A repeated
    identical read is escalated to a BLOCKED_REPEAT directive so the brain
    cannot spin on duplicate observations.

    Returns the §51 envelope to use INSTEAD of executing (replayed or a
    blocked-repeat refusal), or None when the call must execute.
    """
    if name not in tools.READ_TOOLS:
        return None
    ctx = state.fallback_ctx
    entry = (ctx.get("observe_map") or {}).get(f"{name}|{tools.canonical_args(args)}")
    if entry is None:
        return None
    if int(entry.get("world", -1)) != int(ctx.get("world_version", 0)):
        return None  # the world advanced since the observation — the read must re-run

    repeats = int(entry.get("repeat_count", 0)) + 1
    entry["repeat_count"] = repeats
    if repeats > 1:
        entry["blocked_repeat"] = True
        return {
            "success": False,
            "tool": name,
            "error": {
                "code": "BLOCKED_REPEAT",
                "message": (
                    f"Repeating {name} with identical arguments returns the same observation in an "
                    "unchanged world. Act on the prior result — continue toward checkout/finish or "
                    "call find_alternative — do NOT re-read."
                ),
                "retryable": False,
            },
        }

    envelope = dict(entry["envelope"])
    envelope["replayed_from_observation"] = True
    return envelope


# ---------------------------------------------------------------- loop


class MerchantAgent:
    """Runs/resumes one transaction-scoped agent session."""

    # -- public API --------------------------------------------------

    def run(self, state: MerchantAgentState) -> MerchantAgentState:
        agent_timing.begin_session(state.agent_session_id)
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
            # Decision seam: a policy-clean basket that just went live ends the
            # loop deterministically instead of burning a (frequently wrong)
            # extra reasoning round. Completion failures keep the loop alive so
            # the next decision cycle can adapt.
            if self._decision_point(state):
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
            envelope = self._observe_or_execute(state, name, args, reasoning, tool_call_id)
            if name == "create_checkout" and envelope.get("success"):
                # A loop-executed, policy-clean checkout leaves only one correct
                # follow-up (mode-appropriate completion/handoff); the next turn
                # takes it deterministically via the decision seam.
                state.fallback_ctx["pending_continuation"] = True

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
        # A buyer decision is a world change: observations from before the pause
        # may not be replayed against it.
        state.fallback_ctx["world_version"] = int(state.fallback_ctx.get("world_version", 0)) + 1

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
            # Decision seam: the next-ranked candidate from the ALREADY-OBSERVED
            # list is proposed deterministically; the model only re-decides when
            # that list is exhausted.
            if self._continue_after_rejection(state, proposal):
                return state
            return self.run(state)

        self._accept_substitution(state, proposal)
        return self.run(state)

    # -- internal ----------------------------------------------------

    def _decision_point(self, state: MerchantAgentState) -> bool:
        """Deterministic loop short-circuit (decision-cycle optimization).

        A policy-clean basket that went LIVE through a loop-executed
        create_checkout (or a buyer-accepted alternative / confirmed terms)
        never needs another reasoning round to terminate: the mode-appropriate
        handoff is fully determined. Running the model again here is wasted work
        AND the most common source of mode/payment-truth mistakes, which is why
        this seam exists instead of prompting.

        Returns True when the loop must stop; False keeps the adaptive cycle
        alive (e.g. gated completion failed and the agent must adapt).
        """
        ctx = state.fallback_ctx
        if not ctx.get("pending_continuation"):
            return False
        ctx["pending_continuation"] = None

        last = state.tool_results[-1] if state.tool_results else None
        if not (
            last
            and last.get("success")
            and last.get("tool") in ("create_checkout", "update_checkout", "terms_confirmed")
        ):
            return False

        if state.execution_mode == "buyer_completes":
            self._apply_finish(
                state,
                "ready_for_completion",
                "basket_ready_for_buyer_payment",
                "Basket is live under policy; the buyer completes payment via the protocol.",
            )
            return True

        # agent_completed: run the SAME gated money ladder deterministically.
        # Every consequence (completed / awaiting_authorization / PRICE_CHANGED /
        # STOCK_UNAVAILABLE / hard failure) flows through _post_action unchanged,
        # so the seam can never weaken payment truth.
        state.record_action(
            "request_completion",
            {},
            "Auto-executing gated completion for the live, policy-clean basket (decision seam).",
        )
        _rc_start = time.perf_counter()
        envelope = tools.execute_tool("request_completion", {}, state)
        agent_timing.record(state.agent_session_id, "request_completion", _rc_start,
                            time.perf_counter(), outcome="ok" if envelope.get("success") else "error")
        _audit(
            action="agent_request_completion",
            decision="approved" if envelope.get("success") else "declined",
            summary="Gated completion executed deterministically for the live policy-clean checkout.",
            state=state,
            payload={"tool": "request_completion", "result": _result_digest(envelope)},
            rules_fired=[] if envelope.get("success") else [envelope.get("error", {}).get("code", "UNKNOWN")],
        )
        self._note_for_llm(state, {"event": "gated_auto_completion", "result": envelope})
        return self._post_action(state, "request_completion", envelope)

    def _observe_or_execute(
        self,
        state: MerchantAgentState,
        name: str,
        args: dict,
        reasoning: str,
        tool_call_id: Optional[str],
    ) -> dict:
        """Run one decision, replaying unchanged deterministic reads.

        Returns the §51 envelope. A replayed envelope carries the
        `replayed_from_observation` marker and still consumes a tool-call
        budget slot (budgets remain honest ceilings, never soft).
        """
        replay = _maybe_replay_observation(state, name, args)
        if replay is None:
            _start = time.perf_counter()
            envelope = tools.execute_tool(name, args, state)
            agent_timing.record(state.agent_session_id, "merchant_tool", _start,
                                time.perf_counter(), tool=name,
                                outcome="ok" if envelope.get("success") else "error")
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
            _audit(
                action=f"agent_{name}",
                decision="approved" if envelope.get("success") else "declined",
                summary=reasoning or f"executed {name}",
                state=state,
                payload={"tool": name, "args": _args_digest(args), "result": _result_digest(envelope)},
                rules_fired=[] if envelope.get("success") else [envelope.get("error", {}).get("code", "UNKNOWN")],
            )
            self._guide_next_action(state, name, envelope)
            return envelope

        state.tool_call_count += 1
        state.tool_results.append(replay)
        if tool_call_id is not None:
            state.llm_history.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps(replay, default=str)[:1800],
                }
            )
        ok = bool(replay.get("success"))
        _audit(
            action=f"agent_{name}_replayed",
            decision="approved" if ok else "blocked",
            summary=(
                f"replayed prior {name} observation (world unchanged)"
                if ok
                else f"blocked duplicate {name} read (world unchanged); act on the prior observation"
            ),
            state=state,
            payload={"tool": name, "args": _args_digest(args), "result": _result_digest(replay)},
            rules_fired=[] if ok else [replay.get("error", {}).get("code", "UNKNOWN")],
        )
        return replay

    def _last_find_alternative(self, state: MerchantAgentState) -> Optional[dict]:
        for envelope in reversed(state.tool_results):
            if envelope.get("tool") == "find_alternative" and envelope.get("success"):
                return envelope
        return None

    def _continue_after_rejection(self, state: MerchantAgentState, rejected_proposal: dict) -> bool:
        """Deterministic next-candidate seam (decision-cycle optimization).

        A REJECTED substitution does not require a model to pick the next
        option: the ranked candidate list was already observed. Propose the next
        eligible candidate from that same list and pause for the buyer.

        Returns True when a follow-up proposal was issued (session paused for
        the buyer's decision), False to fall back to the adaptive loop (list
        exhausted / no usable observation).
        """
        envelope = self._last_find_alternative(state)
        if envelope is None:
            return False
        result = envelope.get("result") or {}
        requested = result.get("requested") or {}
        excluded = set(state.excluded_pack_ids)
        offered = state.fallback_ctx.setdefault("offered_pack_ids", [])
        candidate = next(
            (a for a in (result.get("alternatives") or []) if a["pack_id"] not in excluded and a["pack_id"] not in offered),
            None,
        )
        if candidate is None:
            return False
        offered.append(candidate["pack_id"])

        orig = rejected_proposal.get("original") or {}
        args = {
            "reason_code": "requested_pack_unavailable",
            "original": {
                "product_id": requested.get("product_id") or orig.get("product_id"),
                "pack_id": requested.get("pack_id") or orig.get("pack_id"),
                "quantity": requested.get("quantity", orig.get("quantity", 1)),
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
        }
        state.record_action(
            "request_confirmation",
            _args_digest(args),
            "Buyer rejected the previous alternative; proposing the next-ranked candidate (decision seam).",
        )
        issued = tools.execute_tool("request_confirmation", args, state)
        _audit(
            action="agent_request_confirmation",
            decision="approved" if issued.get("success") else "declined",
            summary="Proposed the next-ranked alternative after buyer rejection (decision seam).",
            state=state,
            payload={"tool": "request_confirmation", "args": _args_digest(args), "result": _result_digest(issued)},
            rules_fired=[] if issued.get("success") else [issued.get("error", {}).get("code", "UNKNOWN")],
        )
        if not issued.get("success"):
            return False
        self._note_for_llm(
            state,
            {
                "event": "buyer_rejected_proposal",
                "instruction": (
                    "The buyer rejected the previous substitution. The next-ranked alternative from "
                    "the SAME observed candidate list was proposed to the buyer automatically; the "
                    "agent is paused for that structured decision."
                ),
            },
        )
        return self._post_action(state, "request_confirmation", issued)

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
        from merchant.domain import checkout as checkout_service

        db = SessionLocal()
        try:
            return checkout_service.get_session_status(db, state.checkout_session_id)
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
        if ok:
            # A buyer-accepted alternative that landed a policy-clean basket only
            # has one correct follow-up (complete/hand off) — take it
            # deterministically on the next turn, not via another reasoning round.
            state.fallback_ctx["pending_continuation"] = True
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
        from merchant.domain import checkout as checkout_service

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
        if ok:
            # Confirmed terms leave the basket LIVE and policy-clean — the next
            # turn completes/hands off deterministically (decision seam).
            state.fallback_ctx["pending_continuation"] = True

    def _reject_terms(self, state: MerchantAgentState) -> None:
        """Buyer declined new terms -> cancel session, graceful decline."""
        from merchant.persistence.db import mutate
        from merchant.domain import checkout as checkout_service

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