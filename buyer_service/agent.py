"""The buyer agent brain: NIM tool-loop + deterministic approval gate.

Flow ownership:
    LLM        -> intent, discovery, basket construction, decline adaptation
    Service    -> bill presentation, human approval pause, payment execution
    Merchant   -> validation, gating, money movement (always deterministic)

Module layout (buyer_service):
    agent.py       BuyerAgent orchestration + NIM tool loop (this module)
    tool_exec.py   deterministic merchant-tool execution + agent-led negotiation
    bill.py        approval-gated bill presentation + payment/decline handling
    proposals.py   substitute-consent flow (declined-item recovery)
    routing.py     deterministic-first routing + authoritative CURRENT STATE

The tool-execution, bill, and substitute mixins are composed below so
``BuyerAgent`` keeps the exact same public surface as before the split; the
module-level ``_client`` and ``BILL_PAUSE`` stay here because the harnesses
patch ``buyer_service.agent._client``.
"""
import json
import re
from collections.abc import Generator

from buyer_service.bill import BillFlowMixin
from buyer_service.config import settings
from buyer_service.prompts import SYSTEM_PROMPT, TOOL_SCHEMAS
from buyer_service.proposals import SubstituteFlowMixin
from buyer_service.routing import current_state_block
from buyer_service.timing import recorder, stage_timer
from buyer_service.tools import MerchantClient
from buyer_service.tool_exec import (
    ToolExecutorMixin,
    _stable_basket_key,  # noqa: F401  (re-export: tests import from buyer_service.agent)
    _summarize_args,
    _summary_outcome,
)

BILL_PAUSE = "__BILL_PAUSE__"

# Model-compat: some NIM models occasionally leak their tool-call template
# into plain text instead of emitting structured tool_calls (NIM docs:
# "function call present in the content field"). Detect and retry.
_LEAKED_TOOLCALL_RE = re.compile(r"<\|?(?:tool_call|eom_id)|call:\s*\w+\s*\{", re.IGNORECASE)


def _client():
    from openai import OpenAI

    return OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=60.0)


class BuyerAgent(ToolExecutorMixin, BillFlowMixin, SubstituteFlowMixin):
    def __init__(self, merchant: MerchantClient | None = None) -> None:
        self.merchant = merchant or MerchantClient()

    # -------------------------------------------------- LLM loop

    def llm_loop(self, conv, instruction: str) -> Generator[dict, None, None]:
        """Run the NIM tool-loop. Yields events; pauses on a created checkout."""
        recorder.begin_turn(conv.id)
        conv.llm_history.append({"role": "user", "content": instruction})
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *conv.llm_history]
        # B: bind every turn to the deterministic, server-verified CURRENT STATE
        # so the model can never hallucinate order/payment/substitution status.
        messages.append({"role": "system", "content": current_state_block(conv)})
        # B: bounded history — authoritative CURRENT STATE preserves the ground
        # truth, so we can safely cap the raw message window to keep long chats
        # inside the token budget. Keep the system prompt, the state block, and
        # the most recent exchanges (always including the just-added instruction).
        _MAX_HISTORY = 40
        if len(messages) > _MAX_HISTORY:
            head = messages[:1]                      # system prompt
            state = messages[-1]                     # CURRENT STATE block
            tail = messages[-(_MAX_HISTORY - 2):]    # recent exchanges
            messages = [head[0], *tail, state]
        leaked_replies = 0
        ungrounded_nudges = 0

        for _ in range(settings.max_agent_iterations):
            try:
                with stage_timer("buyer_llm", llm_model=settings.buyer_llm_model):
                    resp = _client().chat.completions.create(
                        model=settings.buyer_llm_model,
                        messages=messages,
                        tools=TOOL_SCHEMAS,
                        temperature=0.2,
                        max_tokens=700,
                    )
            except Exception as exc:  # noqa: BLE001
                yield {"type": "message", "content": f"I hit a problem reaching my AI backend ({exc}). Please try again."}
                return

            msg = resp.choices[0].message

            if msg.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": (msg.content or "").strip() or None,  # model-compat: "" rejected by some NIM endpoints
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                            }
                            for tc in msg.tool_calls
                        ],
                    }
                )
                for tc in msg.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    # Telemetry is handled by the secret-safe timing recorder; do
                    # NOT write raw tool arguments to disk (avoids leaking queries/
                    # potential PII into a repo-root log).
                    yield {"type": "tool", "tool": name, "summary": _summarize_args(name, args), "ok": True}
                    with stage_timer("buyer_tool", tool=name) as st:
                        if name in ("discover_products", "create_checkout_session", "cancel_checkout_session"):
                            result = self._execute_tool(conv, name, args)
                        else:
                            result = self._execute_agent_tools(conv, name, args)
                        st.outcome = _summary_outcome(result)
                    yield from self._post_tool_events(conv, name, result)
                    if result.get("bill_ready"):
                        # Merchant basket is policy-approved; deterministic gate shows the bill.
                        yield self.present_bill(
                            conv,
                            result["session"],
                            note=result.get("note") or None,
                        )
                        conv.llm_history = messages[1:]
                        conv.llm_history.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": json.dumps({"bill_presented": True}),
                            }
                        )
                        yield BILL_PAUSE
                        return
                    if result.get("created") is True:
                        # Deterministic gate: show the bill, park the loop.
                        session_like = {
                            "checkout_session_id": result["checkout_session_id"],
                            "line_items": result["line_items"],
                            "totals": result["totals"],
                        }
                        yield self.present_bill(conv, session_like)
                        conv.llm_history = messages[1:]  # persist progress
                        conv.llm_history.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": json.dumps(result),
                            }
                        )
                        yield BILL_PAUSE
                        return
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
                continue

            # No tool calls -> final assistant message for this turn.
            content = (msg.content or "").strip()

            if _LEAKED_TOOLCALL_RE.search(content) and leaked_replies < 2:
                leaked_replies += 1
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your last reply was not a valid tool call. Respond ONLY by calling one of "
                            "the provided tools (proper tool_calls), never by writing call-syntax as text."
                        ),
                    }
                )
                continue

            # F3: never answer availability questions from memory — force one
            # discovery observation before any catalog claim.
            has_observation = any(m.get("role") == "tool" for m in messages)
            if (
                not has_observation
                and ungrounded_nudges < 1
                and len(instruction.split()) >= 3
            ):
                ungrounded_nudges += 1
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Do NOT answer from memory or guess what the merchant stocks. Call "
                            "discover_products first with the item or category, then answer only "
                            "from its result."
                        ),
                    }
                )
                continue

            conv.llm_history.append({"role": "assistant", "content": content or None})
            yield {"type": "message", "content": content}
            return

        yield {"type": "message", "content": "I couldn't finish within my step budget. Please rephrase or try again."}