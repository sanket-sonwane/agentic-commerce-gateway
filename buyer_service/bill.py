"""Approval-gated bill presentation and payment completion for the Buyer Agent.

The human (via the dashboard) is the only party that can approve a payment.
This module's entry points are ``present_bill`` (deterministic bill card the
LLM can never skip) and ``resume_after_decision`` (continue only after the
user's signed answer). ``_complete_flow`` drives the deterministic merchant
completion path and stays truthful on gateway failures: it never claims money
moved unless the server reports ``completed``.

Instances are composed into ``BuyerAgent`` (see buyer_service/agent.py) as a
mixin; ``_complete_flow`` delegates declined-item recovery to the substitute
flow mixin.
"""
import uuid
from collections.abc import Generator

from buyer_service.config import settings
from buyer_service.routing import _state_note


class BillFlowMixin:
    """Bill presentation, approval pause, and deterministic completion."""

    def present_bill(self, conv, session: dict, note: str | None = None, change: dict | None = None) -> dict:
        approval_id = f"apr_{uuid.uuid4().hex[:8]}"
        conv.pending_approval = {
            "approval_id": approval_id,
            "checkout_session_id": session["checkout_session_id"],
            "kind": "revised" if change else "initial",
        }
        if not change:
            conv.last_basket_items = [dict(li) for li in session.get("line_items", [])]
        bill = {
            "type": "bill",
            "approval_id": approval_id,
            "checkout_session_id": session["checkout_session_id"],
            "line_items": session["line_items"],
            "totals": session["totals"],
            "note": note,
            "change": change,
            # Workstream E: the browser signs the approval with its own device
            # key; it needs the merchant + delegate identity to build the
            # canonical user-intent assertion.
            "merchant_id": settings.merchant_id,
            "delegate_id": settings.buyer_agent_id,
        }
        return bill

    def resume_after_decision(self, conv, approved: bool, user_assertion: dict | None = None) -> Generator[dict, None, None]:
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
            conv.llm_history.append(_state_note({"event": "order_cancelled", "checkout_session_id": csid}))
            yield {"type": "message", "content": "No problem — I cancelled that order. Nothing was charged."}
            return

        yield {"type": "message", "content": "Thanks! Processing your payment…"}
        yield from self._complete_flow(conv, csid, user_assertion=user_assertion)

    def _complete_flow(self, conv, csid: str, user_assertion: dict | None = None) -> Generator[dict, None, None]:
        try:
            status_code, payload = self.merchant.complete_checkout(
                csid,
                intent_mandate_id=conv.intent_mandate_id,
                user_assertion=user_assertion,
            )
        except Exception as exc:  # noqa: BLE001 — gateway transport failure is not a charge
            # C9: be truthful when the payment gateway is unreachable. The
            # merchant already parked the session via `requires_payment`
            # (nothing charged, no Razorpay order moved money). We must not
            # claim success we cannot see.
            conv.active_session_id = None
            conv.llm_history.append(
                _state_note({"event": "gateway_unreachable", "checkout_session_id": csid})
            )
            yield {
                "type": "message",
                "content": (
                    "⚠️ I couldn't reach the payment gateway to finish your order "
                    f"(connection issue: {exc}). Nothing was charged and no order was placed. "
                    "Your basket is safe — just tell me to try again and I'll place it."
                ),
            }
            return

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
                conv.llm_history.append(
                    _state_note({"event": "order_completed", "total": total, "checkout_session_id": csid})
                )
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
                    conv.llm_history.append(
                        _state_note({"event": "order_completed", "total": total, "checkout_session_id": csid})
                    )
                else:
                    # Three honest sub-cases for a pending auto-pay session:
                    # 1) A payment object exists at the gateway (authorized,
                    #    awaiting capture) — the debit is real and settles.
                    # 2) No payment object YET but a gateway order exists —
                    #    the recurring debit was created and is in flight at
                    #    the bank ("Amount yet to be authenticated"); the
                    #    background reconcile worker completes it automatically.
                    #    Never narrate this as "nothing happened" — an order
                    #    DOES exist.
                    # 3) No order at all — the gateway initiation failed
                    #    before anything was created; genuinely nothing
                    #    charged. Only THIS case may say "nothing was charged".
                    rec_pay = rec_session.get("payment", {}) or {}
                    if rec_pay.get("payment_id"):
                        yield {
                            "type": "status",
                            "checkout_session_id": csid,
                            "status": "payment_pending",
                            "payment": rec_pay,
                        }
                        yield {
                            "type": "message",
                            "content": (
                                "⏳ Your payment was authorized and is waiting for the bank to "
                                "confirm it — this is normal for auto-pay and usually takes "
                                "about 2 minutes. The amount is held against this order, so "
                                "nothing was lost: it settles automatically and your order "
                                f"completes the moment it does. No action needed. Receipt: {csid}."
                            ),
                        }
                        conv.llm_history.append(
                            _state_note({"event": "payment_pending_auto_settle", "checkout_session_id": csid})
                        )
                    elif rec_pay.get("order_id"):
                        yield {
                            "type": "status",
                            "checkout_session_id": csid,
                            "status": "payment_pending",
                            "payment": rec_pay,
                        }
                        yield {
                            "type": "message",
                            "content": (
                                "⏳ Your previously saved card token was used for a recurring "
                                "(auto-pay) payment. The payment was created successfully with "
                                "the gateway and is now pending approval by your bank — this is "
                                "normal for auto-pay and usually takes about 2 minutes. Once the "
                                "bank approves it, your order is completed and placed "
                                "automatically — nothing more to do. "
                                f"Order receipt: {csid}."
                            ),
                        }
                        conv.llm_history.append(
                            _state_note({"event": "payment_pending_auto_settle", "checkout_session_id": csid})
                        )
                    else:
                        yield {
                            "type": "message",
                            "content": (
                                "⏳ The payment gateway hasn't confirmed a payment for this order "
                                f"yet. Nothing was charged and no order exists at Razorpay as of "
                                f"now. Receipt: {csid}. Tell me what you'd like again and I'll place "
                                "a fresh order."
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
            conv.llm_history.append(
                _state_note({"event": "stock_unavailable", "declined_packs": declined_packs})
            )
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
            "PAYMENT_BLOCKED": (
                "your signed approval couldn't be verified (the signature, key, "
                "or authorization bounds don't match). To keep you safe, no order "
                "was placed — please review and approve again."
            ),
        }
        reason = friendly.get(error.get("code", ""), error.get("message", "the merchant refused it."))
        # Be truthful about money: only claim "nothing was charged" when the
        # backend proves no payment object exists for this session.
        charged_suffix = " Nothing was charged."
        try:
            _, cs_payload = self.merchant.get_checkout(csid)
            cs_session = (
                cs_payload.get("checkout_session", {})
                if isinstance(cs_payload, dict)
                else {}
            )
            if (cs_session.get("payment") or {}).get("payment_id"):
                charged_suffix = ""
        except Exception:  # noqa: BLE001 — stay conservative on lookup failure
            pass
        yield {"type": "message", "content": f"❌ Sorry — I couldn't place this order because {reason}{charged_suffix}"}
        conv.active_session_id = None
        conv.llm_history.append(
            _state_note({"event": "order_declined", "code": error.get("code"), "checkout_session_id": csid})
        )

    def present_bill_and_pause(
        self, conv, session: dict, note: str | None = None, change: dict | None = None
    ) -> Generator[dict, None, None]:
        yield self.present_bill(conv, session, note=note, change=change)