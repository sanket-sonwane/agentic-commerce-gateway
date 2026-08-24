"""Merchant Agent prompts + tool schemas (PRD §11, §13, §10.3).

The LLM sees ONLY: the objective, tool schemas, and structured observations.
It never sees Razorpay credentials, delegation tokens, or DB internals.
"""
from typing import Any

SYSTEM_PROMPT = """You are the Merchant Agent for a grocery merchant. Your goal is to fulfill \
authorized buyer requests using the merchant's catalog, inventory, policies, and checkout \
capabilities. Observe current merchant state before deciding what to do. When the requested \
fulfillment is unavailable, search for a valid alternative or clearly decline. You may propose \
changes, but do not silently override buyer intent. Never override deterministic merchant \
policies, modify authoritative prices, invent inventory, or bypass the gated payment flow.

Operating rules:
- Act ONLY through tools. Every product_id/pack_id you use MUST come from a tool result; \
never invent identifiers. If a tool fails with PRODUCT_NOT_FOUND / PACK_NOT_FOUND / \
CHECKOUT_NOT_FOUND, you used a bad id — re-observe (search_catalog / get_checkout) and copy \
ids EXACTLY from the latest tool result.
- BE EFFICIENT — you have a hard budget of ~8 turns. search_catalog already returns each \
pack's price, size and live availability; do NOT re-check via get_product / \
get_current_price / repeat searches. A typical path is only 2-4 tool calls total.
- Prices, stock and policy outcomes are authoritative from tools. Never assume them.
- If the requested pack is out of stock, call find_alternative. If a valid alternative exists, \
call request_confirmation with a structured proposal and pause for the buyer's decision. \
Never update the basket silently.
- If no valid alternative exists, finish with outcome "declined" and a clear reason_code \
(use policy_declined after a gate rejection, no_valid_alternative when nothing fits, \
no_matching_product when the catalog has nothing relevant). No payment may be attempted \
for an unfulfillable request.
- Policy results (gating) are final. If gating rejects the basket, do NOT retry the same \
basket; decline or propose a compliant alternative.
- You have a limited action budget (8 iterations / 10 tool calls). Be decisive; do not repeat \
failed actions.
- A TRANSACTION STATE block is refreshed above your history every turn: its checkout_session_id \
and mode rule are authoritative over memory. In buyer_completes mode you cannot request payment: \
after create_checkout/update_checkout succeeds, call finish with outcome "ready_for_completion".
- requested_quantity comes from the buyer's request (default 1). NEVER copy stock/inventory \
numbers from check_inventory into quantity arguments.
- When the checkout is ready: in agent_completed mode call request_completion; in \
buyer_completes mode finish with outcome "ready_for_completion".
- Always terminate by calling the `finish` tool."""

FINISH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": (
            "Terminate the session with a final outcome. Call this once fulfillment "
            "is complete, declined, or handed to the buyer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["completed", "ready_for_completion", "declined"],
                    "description": "completed: paid/fulfilled. ready_for_completion: checkout live, buyer completes payment. declined: cannot fulfill.",
                },
                "reason_code": {
                    "type": "string",
                    "description": "Machine-readable reason, e.g. fulfilled_as_requested | requested_pack_unavailable_proposed_alternative | no_matching_product | policy_declined",
                },
                "message": {"type": "string", "description": "One-sentence human-readable summary."},
            },
            "required": ["outcome", "reason_code", "message"],
        },
    },
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_catalog",
            "description": "Search the merchant catalog for products matching a natural-language grocery query. Returns candidate packs with authoritative prices and live availability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "e.g. 'Amul milk 1L'"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product",
            "description": "Fetch one product with all of its packs (sizes, prices, availability).",
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": "string"}},
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_inventory",
            "description": "Live inventory for one pack: stock_actual, reserved, available. Never guess stock.",
            "parameters": {
                "type": "object",
                "properties": {"pack_id": {"type": "string"}},
                "required": ["pack_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_price",
            "description": "Authoritative current price for one pack.",
            "parameters": {
                "type": "object",
                "properties": {"pack_id": {"type": "string"}},
                "required": ["pack_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_alternative",
            "description": "Find in-stock alternatives satisfying the same buyer intent when the requested pack is unavailable. Returns machine-readable reasons and suggested quantities.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "pack_id": {"type": "string"},
                    "quantity": {"type": "integer", "minimum": 1},
                },
                "required": ["product_id", "pack_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_checkout",
            "description": "Create a checkout session for exact line items. Deterministic gating runs automatically; the response tells you if policy passed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "string"},
                                "pack_id": {"type": "string"},
                                "quantity": {"type": "integer", "minimum": 1},
                            },
                            "required": ["product_id", "pack_id", "quantity"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_checkout",
            "description": "Replace the line items of the active live checkout (e.g. after the buyer accepts an alternative). Re-runs deterministic gating.",
            "parameters": {
                "type": "object",
                "properties": {
                    "checkout_session_id": {"type": "string"},
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "string"},
                                "pack_id": {"type": "string"},
                                "quantity": {"type": "integer", "minimum": 1},
                            },
                            "required": ["product_id", "pack_id", "quantity"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_checkout",
            "description": "Read the current checkout session state (status, items, totals, gating).",
            "parameters": {
                "type": "object",
                "properties": {"checkout_session_id": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_gating",
            "description": "Dry-run the deterministic policy engine against the intended/current basket. Read-only preview of delegation/spend/quantity/category/stock checks.",
            "parameters": {
                "type": "object",
                "properties": {"checkout_session_id": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_confirmation",
            "description": "Pause and ask the buyer to confirm a substitution or changed terms. The proposal must use real product/pack ids and quantities; price is filled authoritatively.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason_code": {
                        "type": "string",
                        "description": "e.g. requested_pack_unavailable | price_changed",
                    },
                    "original": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "string"},
                            "pack_id": {"type": "string"},
                            "quantity": {"type": "integer"},
                        },
                        "required": ["product_id", "pack_id", "quantity"],
                    },
                    "proposal": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "string"},
                            "pack_id": {"type": "string"},
                            "quantity": {"type": "integer"},
                        },
                        "required": ["product_id", "pack_id", "quantity"],
                    },
                    "message": {"type": "string"},
                },
                "required": ["reason_code", "original", "proposal", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_checkout",
            "description": "Cancel the active checkout session and release reservations.",
            "parameters": {
                "type": "object",
                "properties": {"checkout_session_id": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_completion",
            "description": "Hand the transaction to the deterministic financial control plane: full revalidation then payment execution. Only call when the basket is confirmed and policy-clean.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]
