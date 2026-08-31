"""Substitute-consent flow: deterministic recovery for declined items.

When completion is rejected with STOCK_UNAVAILABLE, the agent must NOT invent
alternatives or move money. Instead it searches for same-role candidates and
presents a ``substitute_proposal`` card; only the human's explicit choice can
build a new checkout. ``choose`` routes that answer (accept / other / cancel)
to the deterministic create path and parks the bill for approval.

Instances are composed into ``BuyerAgent`` (see buyer_service/agent.py) as a
mixin; bill presentation for the rebuilt basket comes from the bill mixin.
"""
import uuid
from collections.abc import Generator

from buyer_service.routing import _state_note
from buyer_service.tool_exec import _stable_basket_key

# A candidate substitutes a declined item only if it serves the same role:
# same category, or shares a dietary-role tag (protein etc.).
ROLE_TAGS = {"protein"}


class SubstituteFlowMixin:
    """Item-substitution consent flow (declined-item recovery)."""

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
            conv.llm_history.append(_state_note({"event": "substitution_cancelled"}))
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
        key = _stable_basket_key(items)
        if conv.active_session_id and conv.last_create_key and conv.last_create_key != key:
            try:
                self.merchant.cancel_checkout(conv.active_session_id)
            except Exception:  # noqa: BLE001 — best-effort cleanup of a stale live session
                pass
            conv.active_session_id = None
        status_code, payload = self.merchant.create_checkout(items, idempotency_key=key)
        session = payload.get("checkout_session")
        if status_code in (200, 201) and session and session.get("status") == "requires_payment":
            conv.active_session_id = session["checkout_session_id"]
            conv.last_create_key = key
            note = f"Swapped {unavailable['name']} → {chosen['name']} at your suggestion."
            conv.llm_history.append(
                _state_note(
                    {
                        "event": "substitution_accepted",
                        "replaced": unavailable["name"],
                        "chosen": chosen["name"],
                        "checkout_session_id": session["checkout_session_id"],
                    }
                )
            )
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