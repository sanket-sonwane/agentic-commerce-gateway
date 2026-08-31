"""Merchant Agent deterministic fallback brain (PRD §10, §30).

The policy-first decider used when the LLM is unavailable or disabled (keeps
demos/tests hermetic; mirrors the discovery keyword-fallback philosophy). It
plans a single next tool call from the last execution envelope, always
deferring money movement to `request_completion` and always proposing buyer
confirmable alternatives instead of silent substitutions (PRD §25).
"""
from typing import Optional

from merchant.agent.state import MerchantAgentState
from merchant.domain.catalog import parse_target_pack


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
    target = parse_target_pack(state.buyer_request)
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
        target_size = parse_target_pack(state.buyer_request)
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