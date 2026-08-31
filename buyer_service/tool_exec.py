"""Deterministic merchant-tool execution for the Buyer Agent (Workstream A).

Owns the only place a buyer turn may call the merchant's read/write tools, and
the merchant-agent hand-off (purchase_via_merchant_agent / respond_to_merchant_
proposal). The LLM never reaches the money path directly — it only emits tool
names/args that resolve through this module against the confirmed session.

Instances are composed into ``BuyerAgent`` (see buyer_service/agent.py) as a
mixin, so the methods here are identical to the pre-split BuyerAgent methods.
"""
import hashlib
import json
from collections.abc import Generator
from typing import Any

from buyer_service.timing import stage_timer

# Role semantics used when mapping proposals / substitutions:
# a candidate substitutes a declined item only if it serves the same role
# (same category, or shares a dietary-role tag such as protein).
ROLE_TAGS = {"protein"}


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


def _stable_basket_key(line_items: list[dict]) -> str:
    """Stable per-basket idempotency key (E2). Identical baskets must map to one
    key so a repeated create replays the existing session instead of reserving a
    duplicate. Sorting by pack_id keeps canonical_arg equivalence (product+pack
    +qty) order-insensitive."""
    canon = sorted(
        (str(i.get("product_id")), str(i.get("pack_id")), int(i.get("quantity", 1)))
        for i in line_items
    )
    digest = hashlib.sha256(json.dumps(canon, default=str).encode("utf-8")).hexdigest()[:16]
    return f"create_{digest}"


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


def _summary_outcome(result) -> str:
    """Coarse success/outcome label for telemetry (no amounts/ids/PII)."""
    if not isinstance(result, dict):
        return "ok" if result is not None else "empty"
    if result.get("error"):
        return "error"
    if result.get("error", {}):
        return "error"
    if result.get("bill_ready") or result.get("fulfilled") or result.get("created"):
        return "ok"
    return "ok"


class ToolExecutorMixin:
    """Tool execution: catalog/checkout tools + merchant-agent negotiation."""

    def _execute_tool(self, conv, name: str, args: dict) -> dict[str, Any]:
        if name == "discover_products":
            query = str(args.get("query", ""))
            conv.last_query = query
            # A4 (measure-proven): the buyer LLM already parsed the user's intent
            # into a concise query string. Re-running the merchant's own LLM query
            # parser here is a redundant NIM call (~1.2s per discovery) that the
            # deterministic keyword parser serves equally well for grocery browse
            # terms. Skip it for buyer-driven discovery; the merchant agent's
            # search_catalog tool already uses the deterministic parser.
            with stage_timer("discover_http", tool="discover_products", query_len=len(query)):
                status, payload = self.merchant.discover(query, skip_llm_parse=True)
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
            # E2: stable basket-derived idempotency key. Re-creating the same
            # basket replays the existing session instead of reserving a
            # duplicate; creating a different basket auto-cancels any prior live
            # session we own (no leftover reservations).
            key = _stable_basket_key(items)
            if conv.active_session_id and conv.last_create_key and conv.last_create_key != key:
                try:
                    self.merchant.cancel_checkout(conv.active_session_id)
                except Exception:  # noqa: BLE001 — best-effort cleanup of a stale live session
                    pass
                conv.active_session_id = None
            status, payload = self.merchant.create_checkout(items, idempotency_key=key)
            session = payload.get("checkout_session")
            if status in (200, 201) and session and session.get("status") == "requires_payment":
                conv.active_session_id = session["checkout_session_id"]
                conv.last_create_key = key
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
                with stage_timer("merchant_agent_session"):
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
                with stage_timer("merchant_agent_proposal_decision"):
                    status, payload = self.merchant.respond_proposal(sid, "accept" if accept else "reject", reason)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"merchant agent unreachable: {exc}"}
            if status != 200:
                detail = payload.get("detail") or payload.get("error", {}).get("message", "")
                return {"error": str(detail) or f"decision failed ({status})"}
            return self._map_agent_result(conv, payload)

        return {"error": f"unknown tool {name}"}

    def _post_tool_events(self, conv, name: str, result: dict) -> Generator[dict, None, None]:
        """Human-friendly echoes of tool outcomes."""
        if name == "discover_products" and "candidates" in result:
            yield {"type": "found", "summary": _shorten_discovery(result), "candidates": result["candidates"]}
        if name == "cancel_checkout_session" and result.get("cancelled"):
            yield {"type": "status", "checkout_session_id": "", "status": "cancelled"}