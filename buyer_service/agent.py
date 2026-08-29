"""The buyer agent brain: NIM tool-loop + deterministic approval gate.

Flow ownership:
    LLM        -> intent, discovery, basket construction, decline adaptation
    Service    -> bill presentation, human approval pause, payment execution
    Merchant   -> validation, gating, money movement (always deterministic)
"""
import json
import re
import uuid
from collections.abc import Generator
from typing import Any

from buyer_service.config import settings
from buyer_service.prompts import SYSTEM_PROMPT, TOOL_SCHEMAS
from buyer_service.tools import MerchantClient

BILL_PAUSE = "__BILL_PAUSE__"

# A candidate substitutes a declined item only if it serves the same role:
# same category, or shares a dietary-role tag (protein etc.).
ROLE_TAGS = {"protein"}

# Model-compat: some NIM models occasionally leak their tool-call template
# into plain text instead of emitting structured tool_calls (NIM docs:
# "function call present in the content field"). Detect and retry.
_LEAKED_TOOLCALL_RE = re.compile(r"<\|?(?:tool_call|eom_id)|call:\s*\w+\s*\{", re.IGNORECASE)


def _client():
    from openai import OpenAI

    return OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=60.0)


def _shorten_discovery(result: dict) -> str:
    candidates = result.get("candidates", [])
    names = [f"{p['name']} ₹{p['price']}" for p in candidates]
    unmatched = result.get("unmatched") or []
    text = ", ".join(names) if names else "nothing found"
    if unmatched:
        text += f" (no match: {', '.join(unmatched)})"
    return text


def _summarize_args(name: str, args: dict) -> str:
    if name == "discover_products":
        return f"searching for “{args.get('query', '')}”"
    if name == "create_checkout_session":
        n = len(_coerce_items(args.get("items", [])))
        return f"creating checkout with {n} item(s)"
    if name == "cancel_checkout_session":
        return f"cancelling {args.get('checkout_session_id', '')}"
    if name == "purchase_via_merchant_agent":
        return f"negotiating purchase with merchant agent: “{str(args.get('request_text', ''))[:60]}”"
    if name == "respond_to_merchant_proposal":
        verdict = "accepting" if args.get("accept") else "rejecting"
        return f"{verdict} the merchant's proposal"
    return name


def _coerce_items(value) -> list:
    """Small models sometimes emit array parameters as JSON strings — coerce."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, dict):
        value = [value]
    return value if isinstance(value, list) else []


class BuyerAgent:
    def __init__(self, merchant: MerchantClient | None = None) -> None:
        self.merchant = merchant or MerchantClient()

    # -------------------------------------------------- tool execution

    def _execute_tool(self, conv, name: str, args: dict) -> dict[str, Any]:
        if name == "discover_products":
            query = str(args.get("query", ""))
            conv.last_query = query
            status, payload = self.merchant.discover(query)
            if status != 200:
                return {"error": payload.get("error", {}).get("message", "discovery failed")}
            proposal = payload.get("basket_proposal", [])
            compact = [
                {
                    "product_id": p["product_id"],
                    "pack_id": p["pack_id"],
                    "name": p["name"],
                    "category": p.get("category", ""),
                    "matched_for": p["matched_for"],
                    "quantity": p["quantity"],
                    "price": p["price"],
                    "size": f"{p['size']}{p['unit']}",
                }
                for p in proposal
            ]
            conv.last_candidates = compact
            # Never re-propose packs previously declined as out of stock.
            blocked = set(conv.declined_packs)
            usable = [c for c in compact if c["pack_id"] not in blocked]
            dropped = len(compact) - len(usable)
            payload_out = {
                "candidates": usable,
                "estimated_total": sum(c["price"] * c["quantity"] for c in usable),
                "unmatched": payload.get("unmatched", []),
                "instruction": (
                    "Build the basket ONLY from these candidates using their exact "
                    "product_id/pack_id values, then call create_checkout_session."
                ),
            }
            if blocked:
                payload_out["instruction"] += (
                    f" NOTE: these packs are OUT OF STOCK and must NOT be reordered: {sorted(blocked)}. "
                    "If no candidate fits, pick a genuinely different alternative product."
                )
            if dropped:
                payload_out["filtered_out_of_stock"] = dropped
            return payload_out

        if name == "create_checkout_session":
            items = _coerce_items(args.get("items", []))
            if len(items) > 15:
                return {
                    "created": False,
                    "declined": True,
                    "code": "INVALID_REQUEST",
                    "message": "Too many line items; build a basket only from pack_ids returned by discover_products",
                }
            if items and conv.last_candidates:
                known = {c["pack_id"] for c in conv.last_candidates}
                unknown = sorted({str(i.get("pack_id")) for i in items} - known)
                if unknown:
                    return {
                        "created": False,
                        "declined": True,
                        "code": "INVALID_REQUEST",
                        "message": (
                            f"Unknown pack_ids {unknown}. Call discover_products again and use "
                            "ONLY returned candidate pack_ids."
                        ),
                    }
            status, payload = self.merchant.create_checkout(items)
            session = payload.get("checkout_session")
            if status == 201 and session and session.get("status") == "requires_payment":
                conv.active_session_id = session["checkout_session_id"]
                # AP2: bind the buyer's intent scope to a signed Intent Mandate so
                # completion can prove the execution stays within what was authorized.
                intent_text = (conv.last_query or "").strip() or "Purchase the selected grocery basket"
                if not conv.intent_mandate_id:
                    im_status, im_payload = self.merchant.create_intent_mandate(intent_text)
                    if im_status == 201:
                        conv.intent_mandate_id = im_payload.get("mandate_id")
                return {
                    "created": True,
                    "checkout_session_id": session["checkout_session_id"],
                    "status": session["status"],
                    "line_items": session["line_items"],
                    "totals": session["totals"],
                    "intent_mandate_id": conv.intent_mandate_id,
                }
            if "error" in payload:
                err = payload["error"]
                return {
                    "created": False,
                    "declined": True,
                    "code": err.get("code"),
                    "message": err.get("message"),
                    "details": err.get("details", {}),
                }
            return {"created": False, "raw": payload}

        if name == "cancel_checkout_session":
            csid = args.get("checkout_session_id") or conv.active_session_id
            if not csid:
                return {"cancelled": False, "reason": "no active session"}
            status, payload = self.merchant.cancel_checkout(csid)
            session = payload.get("checkout_session", {})
            ok = status == 200 and session.get("status") == "cancelled"
            if ok:
                conv.active_session_id = None
            return {"cancelled": ok, "status": session.get("status")}

        return {"error": f"unknown tool {name}"}

    # ------------------------------------------- merchant-agent negotiation

    def _map_agent_result(self, conv, payload: dict) -> dict:
        """Translate a Merchant Agent session payload into an LLM-visible
        result. When the merchant basket is ready, stash bill data for the
        deterministic pause (the LLM never triggers payment)."""
        status = payload.get("status")
        conv.active_agent_session_id = payload.get("agent_session_id")

        if status == "completed":
            outcome = payload.get("outcome") or {}
            conv.active_session_id = None
            conv.active_agent_session_id = None
            return {
                "fulfilled": True,
                "reason_code": outcome.get("reason_code"),
                "message": outcome.get("message", ""),
                "total": outcome.get("total"),
                "payment_id": (outcome.get("payment") or {}).get("payment_id"),
                "instruction": "The order is PAID and placed. Tell the user it succeeded with the total.",
            }

        if status == "ready_for_completion":
            cs = payload.get("checkout_session") or {}
            csid = cs.get("checkout_session_id") or payload.get("checkout_session_id")
            if not csid:
                return {"error": "merchant returned no checkout session"}
            conv.active_session_id = csid
            outcome = payload.get("outcome") or {}
            note = outcome.get("message") or ""
            return {
                "bill_ready": True,
                "note": note,
                "session": {
                    "checkout_session_id": csid,
                    "line_items": cs.get("line_items", []),
                    "totals": cs.get("totals", {}),
                },
            }

        if status == "awaiting_buyer_confirmation":
            proposal = payload.get("proposal") or {}
            prop = proposal.get("proposal") or {}
            orig = proposal.get("original") or {}
            return {
                "awaiting_proposal_decision": True,
                "agent_session_id": payload.get("agent_session_id"),
                "reason_code": proposal.get("reason_code"),
                "message_from_merchant": proposal.get("message", ""),
                "requested": orig,
                "proposed": {
                    k: prop.get(k)
                    for k in ("product_id", "pack_id", "name", "size", "unit", "quantity", "price", "line_total")
                },
                "instruction": (
                    "The MERCHANT proposes this substitution. Decide whether it satisfies the "
                    "USER'S ORIGINAL INTENT (same item/role, sensible quantity). If acceptable, "
                    "call respond_to_merchant_proposal with accept=true; otherwise reject with a "
                    "short reason."
                ),
            }

        # declined | unable_to_fulfill | awaiting_payment_authorization
        outcome = payload.get("outcome") or {}
        return {
            "fulfilled": False,
            "status": status,
            "reason_code": outcome.get("reason_code"),
            "message": outcome.get("message", ""),
            "instruction": (
                "Explain the outcome to the user in one short line. Do NOT retry the same "
                "request automatically."
            ),
        }

    def _execute_agent_tools(self, conv, name: str, args: dict) -> dict[str, Any]:
        if name == "purchase_via_merchant_agent":
            request_text = str(args.get("request_text", "")).strip()
            if not request_text:
                request_text = conv.last_query or ""
            if not request_text:
                return {"error": "request_text is required"}
            items = _coerce_items(args.get("items")) or None
            try:
                status, payload = self.merchant.start_agent_session(request_text, requested_items=items)
            except Exception as exc:  # noqa: BLE001 — transport failure is not a decline
                return {"error": f"merchant agent unreachable: {exc}"}
            if status != 201:
                err = payload.get("error", {}) if isinstance(payload, dict) else {}
                return {"error": err.get("message", f"merchant agent failed ({status})")}
            return self._map_agent_result(conv, payload)

        if name == "respond_to_merchant_proposal":
            sid = conv.active_agent_session_id
            if not sid:
                return {"error": "no merchant agent negotiation in progress"}
            accept = bool(args.get("accept"))
            reason = str(args.get("reason", ""))[:200]
            try:
                status, payload = self.merchant.respond_proposal(sid, "accept" if accept else "reject", reason)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"merchant agent unreachable: {exc}"}
            if status != 200:
                detail = payload.get("detail") or payload.get("error", {}).get("message", "")
                return {"error": str(detail) or f"decision failed ({status})"}
            return self._map_agent_result(conv, payload)

        return {"error": f"unknown tool {name}"}

    # -------------------------------------------------- LLM loop

    def llm_loop(self, conv, instruction: str) -> Generator[dict, None, None]:
        """Run the NIM tool-loop. Yields events; pauses on a created checkout."""
        conv.llm_history.append({"role": "user", "content": instruction})
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *conv.llm_history]
        leaked_replies = 0
        ungrounded_nudges = 0

        for _ in range(settings.max_agent_iterations):
            try:
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
                    import pathlib

                    pathlib.Path("buyer_service_debug.log").open("a", encoding="utf-8").write(
                        f"TOOL {name} ARGS({len(tc.function.arguments or '')}): {(tc.function.arguments or '')[:400]}\n"
                    )
                    yield {"type": "tool", "tool": name, "summary": _summarize_args(name, args), "ok": True}
                    if name in ("discover_products", "create_checkout_session", "cancel_checkout_session"):
                        result = self._execute_tool(conv, name, args)
                    else:
                        result = self._execute_agent_tools(conv, name, args)
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

    def _post_tool_events(self, conv, name: str, result: dict) -> Generator[dict, None, None]:
        """Human-friendly echoes of tool outcomes."""
        if name == "discover_products" and "candidates" in result:
            yield {"type": "found", "summary": _shorten_discovery(result), "candidates": result["candidates"]}
        if name == "cancel_checkout_session" and result.get("cancelled"):
            yield {"type": "status", "checkout_session_id": "", "status": "cancelled"}

    # -------------------------------------------------- approval-gated payment

    def present_bill(self, conv, session: dict, note: str | None = None, change: dict | None = None) -> dict:
        approval_id = f"apr_{uuid.uuid4().hex[:8]}"
        conv.pending_approval = {
            "approval_id": approval_id,
            "checkout_session_id": session["checkout_session_id"],
            "kind": "revised" if change else "initial",
        }
        if not change:
            conv.last_basket_items = [dict(li) for li in session.get("line_items", [])]
        return {
            "type": "bill",
            "approval_id": approval_id,
            "checkout_session_id": session["checkout_session_id"],
            "line_items": session["line_items"],
            "totals": session["totals"],
            "note": note,
            "change": change,
        }

    def resume_after_decision(self, conv, approved: bool) -> Generator[dict, None, None]:
        """Continue after the user answers the bill card."""
        pending = conv.pending_approval
        conv.pending_approval = None
        if not pending:
            yield {"type": "message", "content": "There's no bill waiting for approval."}
            return

        csid = pending["checkout_session_id"]

        if not approved:
            status_code, payload = self.merchant.cancel_checkout(csid)
            conv.active_session_id = None
            yield {
                "type": "status",
                "checkout_session_id": csid,
                "status": payload.get("checkout_session", {}).get("status", "cancelled"),
            }
            yield {"type": "message", "content": "No problem — I cancelled that order. Nothing was charged."}
            return

        yield {"type": "message", "content": "Thanks! Processing your payment…"}
        yield from self._complete_flow(conv, csid)

    def _complete_flow(self, conv, csid: str) -> Generator[dict, None, None]:
        status_code, payload = self.merchant.complete_checkout(csid, intent_mandate_id=conv.intent_mandate_id)

        if status_code == 200:
            session = payload.get("checkout_session", {})
            status = session.get("status")
            yield {"type": "status", "checkout_session_id": csid, "status": status, "payment": session.get("payment", {})}
            if status == "completed":
                total = session.get("totals", {}).get("total")
                pay = session.get("payment", {})
                saved = pay.get("saved_card")
                driver = pay.get("driver")
                if saved:
                    how = f"using your saved {saved.get('network') or 'card'} ••••{saved.get('last4')}"
                else:
                    how = "via Razorpay"
                yield {
                    "type": "message",
                    "content": f"✅ Order placed! You paid ₹{total} {how}. Receipt: {csid}. "
                    f"It'll be delivered soon.",
                }
                conv.active_session_id = None
            elif status == "payment_pending":
                rec_status, rec_payload = self.merchant.reconcile(csid)
                rec_session = (
                    rec_payload.get("checkout_session", {})
                    if isinstance(rec_payload, dict)
                    else {}
                )
                if rec_session.get("status") == "completed":
                    total = rec_session.get("totals", {}).get("total")
                    yield {
                        "type": "status",
                        "checkout_session_id": csid,
                        "status": "completed",
                        "payment": rec_session.get("payment", {}),
                    }
                    yield {
                        "type": "message",
                        "content": f"✅ Good news — reconciliation confirmed your payment of ₹{total} "
                        f"went through. Receipt: {csid}.",
                    }
                else:
                    yield {
                        "type": "message",
                        "content": (
                            "⏳ The connection to the payment gateway dropped while creating your order, "
                            "so this attempt was parked safely. Nothing was charged and no order exists "
                            f"at Razorpay yet. Receipt reference: {csid}. Tell me what you'd like again "
                            "and I'll place a fresh order."
                        ),
                    }
                conv.active_session_id = None
            elif status == "requires_payment" and session.get("payment", {}).get("awaiting_authorization"):
                pay = session.get("payment", {})
                total = session.get("totals", {}).get("total") or 0
                yield {
                    "type": "bill_redirect",
                    "checkout_session_id": csid,
                    "razorpay_order_id": session.get("razorpay_order_id"),
                    "key_id": pay.get("key_id"),
                    "customer_id": pay.get("customer_id"),
                    "save_card": bool(pay.get("save_card")),
                    "amount": total,
                    "amount_paise": total * 100,
                }
                hint = (
                    " One quick thing: a secure Razorpay window will open — complete it once and "
                    "tick **Save card** so future orders are charged automatically."
                    if pay.get("save_card")
                    else " Please complete the secure Razorpay payment shown below."
                )
                yield {"type": "message", "content": f"To finish, I need your payment authorization.{hint}"}
            return

        error = payload.get("error", {})

        if error.get("code") == "PRICE_CHANGED":
            session = payload.get("checkout_session", {})
            change = error.get("details", {})
            yield {
                "type": "status",
                "checkout_session_id": csid,
                "status": session.get("status"),
            }
            old, new = change.get("old_total"), change.get("new_total")
            yield from self.present_bill_and_pause(
                conv,
                self.merchant.get_checkout(csid)[1].get("checkout_session", session),
                note=f"Heads up — the price just changed from ₹{old} to ₹{new}.",
                change=change,
            )
            return

        if error.get("code") == "STOCK_UNAVAILABLE":
            session = payload.get("checkout_session", {})
            detail = error.get("message", "")

            # Identify WHICH items died (error names them: "<name>: requested=…").
            failed_names = {
                part.split(":")[0].strip()
                for part in detail.split(";")
                if ":" in part and "requested=" in part
            }
            basket = conv.last_basket_items or session.get("line_items", [])
            declined_lines = [li for li in basket if li["name"] in failed_names] or list(basket)
            declined_packs = [li["pack_id"] for li in declined_lines]
            for pk in declined_packs:
                if pk not in conv.declined_packs:
                    conv.declined_packs.append(pk)

            yield {"type": "status", "checkout_session_id": csid, "status": session.get("status")}
            names = ", ".join(li["name"] for li in declined_lines)
            yield {
                "type": "message",
                "content": f"⚠️ Sorry — {names} went out of stock right before payment. "
                "No money was charged. Let me find alternatives for you.",
            }
            conv.active_session_id = None
            # Deterministic recovery: propose options, ASK, wait. No LLM, no auto-reorder.
            yield from self._propose_substitutes(conv, declined_lines)
            return

        # Hard declines (spend limit, category, delegation…) — explain, don't retry.
        yield {"type": "status", "checkout_session_id": csid, "status": payload.get("checkout_session", {}).get("status")}
        friendly = {
            "SPEND_LIMIT_EXCEEDED": "that order exceeds the spending limit allowed on this account.",
            "CATEGORY_BLOCKED": "one of those items can't be purchased through this merchant.",
            "DELEGATION_EXPIRED": "the shopping authorization on this account has expired.",
            "UNAUTHORIZED_AGENT": "this shopping session isn't authorized to place orders.",
            "PAYMENT_FAILED": "the payment was rejected by the gateway.",
        }
        reason = friendly.get(error.get("code", ""), error.get("message", "the merchant refused it."))
        yield {"type": "message", "content": f"❌ Sorry — I couldn't place this order because {reason} Nothing was charged."}
        conv.active_session_id = None

    def present_bill_and_pause(
        self, conv, session: dict, note: str | None = None, change: dict | None = None
    ) -> Generator[dict, None, None]:
        yield self.present_bill(conv, session, note=note, change=change)

    # -------------------------------------------------- substitute consent flow

    def _propose_substitutes(self, conv, declined_lines: list[dict]) -> Generator[dict, None, None]:
        """Find alternatives for a declined item and ASK the user before proceeding."""
        primary = declined_lines[0]
        words = primary["name"].split()
        # Broaden progressively: "Fresh Chicken Breast" -> "Chicken Breast" -> "Chicken".
        queries = [" ".join(words[i:]) for i in range(len(words))]
        queries = [q for q in dict.fromkeys(queries) if len(q) >= 4]

        options: list[dict] = []
        excluded = sorted(set(conv.declined_packs) | set(conv.offered_pack_ids))
        deprioritize = sorted({d.get("product_id") for d in declined_lines if d.get("product_id")})
        primary_cat = primary.get("category", "")
        primary_tags = set(primary.get("tags", []))

        def qualifies(p: dict) -> bool:
            if p.get("category") == primary_cat:
                return True
            return bool(set(p.get("tags", [])) & ROLE_TAGS & primary_tags) or (
                not primary_tags and set(p.get("tags", [])) & ROLE_TAGS
            )

        for query in queries:
            try:
                status, payload = self.merchant.discover(
                    query.lower(),
                    exclude_pack_ids=excluded,
                    deprioritize_product_ids=deprioritize,
                    exclude_product_ids=deprioritize,
                    skip_llm_parse=True,
                )
            except Exception as exc:  # noqa: BLE001
                yield {"type": "message", "content": f"I couldn't search for alternatives ({exc}). Please try again in a moment."}
                return
            if status != 200:
                continue
            blocked = set(conv.declined_packs) | set(conv.offered_pack_ids)
            candidates = [
                {
                    "product_id": p["product_id"],
                    "pack_id": p["pack_id"],
                    "name": p["name"],
                    "category": p.get("category", ""),
                    "tags": p.get("tags", []),
                    "price": p["price"],
                    "size": f"{p['size']}{p['unit']}",
                    "available_stock": p.get("available_stock", 0),
                }
                for p in payload.get("basket_proposal", [])
                if p["pack_id"] not in blocked
                and p.get("available_stock", 0) >= primary["quantity"]
                and qualifies(p)
            ]
            # Same-category alternatives first, then shared-role (protein) items.
            candidates.sort(key=lambda c: 0 if c["category"] == primary_cat else 1)
            options = candidates[:3]
            if options:
                break

        if not options:
            yield {
                "type": "message",
                "content": f"I couldn't find any alternative for {primary['name']} right now. "
                "Nothing was charged. Would you like to order something else?",
            }
            return

        conv.substitution_rounds += 1
        conv.offered_pack_ids.extend(o["pack_id"] for o in options)
        kept_items = [
            {"product_id": li["product_id"], "pack_id": li["pack_id"], "quantity": li["quantity"]}
            for li in (conv.last_basket_items or [])
            if li["pack_id"] not in set(d["pack_id"] for d in declined_lines)
        ]
        proposal_id = f"sub_{uuid.uuid4().hex[:8]}"
        conv.pending_substitution = {
            "proposal_id": proposal_id,
            "options": options,
            "kept_items": kept_items,
            "unavailable": {"name": primary["name"], "quantity": primary["quantity"]},
            "round": conv.substitution_rounds,
        }
        yield {
            "type": "substitute_proposal",
            "proposal_id": proposal_id,
            "unavailable": {"name": primary["name"], "quantity": primary["quantity"]},
            "options": options,
            "kept_items": kept_items,
            "original_total": sum(li["unit_price"] * li["quantity"] for li in (conv.last_basket_items or [])),
        }

    def choose(self, conv, choice) -> Generator[dict, None, None]:
        """User answered a substitute_proposal card: index | 'other' | 'cancel'."""
        pending = conv.pending_substitution
        conv.pending_substitution = None
        if not pending:
            yield {"type": "message", "content": "There's no substitution waiting for your answer."}
            return

        unavailable = pending["unavailable"]
        options = pending["options"]
        kept_items = pending["kept_items"]

        if choice == "cancel":
            yield {"type": "message", "content": "Okay — I've dropped that order. Nothing was charged."}
            return

        if choice == "other":
            if pending.get("round", 1) >= 2:
                yield {
                    "type": "message",
                    "content": f"That was my last idea for replacing {unavailable['name']}. "
                    "Nothing was charged — want to order something different?",
                }
                return
            yield {"type": "message", "content": "Let me look for other options…"}
            yield from self._propose_substitutes(
                conv,
                [{"name": unavailable["name"], "quantity": unavailable["quantity"], "pack_id": ""}],
            )
            return

        try:
            idx = int(choice)
        except (TypeError, ValueError):
            yield {"type": "message", "content": "I didn't understand that choice — please use the buttons on the card."}
            return
        if idx < 0 or idx >= len(options):
            yield {"type": "message", "content": "That option is no longer available on the card above."}
            return

        chosen = options[idx]
        items = [*kept_items, {"product_id": chosen["product_id"], "pack_id": chosen["pack_id"], "quantity": unavailable["quantity"]}]
        yield {
            "type": "message",
            "content": f"Great choice — swapping in {chosen['name']} ({chosen['size']}) ₹{chosen['price']}…",
        }
        status_code, payload = self.merchant.create_checkout(items)
        session = payload.get("checkout_session")
        if status_code == 201 and session and session.get("status") == "requires_payment":
            conv.active_session_id = session["checkout_session_id"]
            note = f"Swapped {unavailable['name']} → {chosen['name']} at your suggestion."
            yield self.present_bill(conv, session, note=note)
            return
        err = payload.get("error", {})
        friendly = {
            "SPEND_LIMIT_EXCEEDED": "the swap pushed the basket over the spending limit.",
            "CATEGORY_BLOCKED": "that item isn't allowed on this account.",
            "STOCK_UNAVAILABLE": "that option sold out seconds ago.",
        }
        reason = friendly.get(err.get("code", ""), err.get("message", "the merchant refused it"))
        yield {"type": "message", "content": f"❌ I couldn't build the new basket because {reason} Nothing was charged."}
