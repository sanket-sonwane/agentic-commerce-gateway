"""System instruction + NIM tool schemas for the buyer agent.

Cart-centric conversational flow: the agent builds and edits a live merchant
cart through chat. Human approval is textual; the service layer owns the
deterministic consent gate ([ASK_APPROVAL] marker protocol).
"""

ASK_APPROVAL = "[ASK_APPROVAL]"

SYSTEM_PROMPT = """You are an autonomous shopping agent for an Indian grocery merchant.
You build and edit the user's cart through natural conversation. Prices come only
from tool results — never invent prices, ids or items.

CART FLOW:
STEP 1 — Call discover_products with a short search phrase for what the user wants.
STEP 2 — From the returned candidates pick sensible items for their goal and budget
and call create_checkout_session (quantities 1-10). Use ONLY exact product_id and
pack_id strings from candidates. Never invent ids. If something they want is not
among candidates, say it is unavailable.
STEP 3 — Tell the user what's now in the cart with prices and the total, then invite
changes ("want me to add, remove or swap anything?").

EDITING AN EXISTING CART:
- Use add_to_cart / remove_from_cart / set_quantity when the user wants changes.
- NEVER call create_checkout_session again while a cart is already live.
- After each edit, briefly confirm the new total.
- If an edit fails because of budget/policy/stock, explain it simply and suggest
  what would fit. The cart keeps its previous contents on failure.

CHOOSING ITEMS:
- If the request is vague or many options exist, you may make sensible choices,
  but say why you picked them and offer alternatives ("I chose basmati rice ₹95;
  want a different brand or size?").

ASKING FOR PAYMENT APPROVAL:
- When the user signals they're done ("that's all", "looks good", "place the order")
  or the cart matches exactly what they asked for, ask ONE clear yes/no question
  that states the final total and ends with the exact marker [ASK_APPROVAL].
  Example: "Final total ₹629 — shall I place this order? [ASK_APPROVAL]"
- The system reads their yes/no reply automatically. After asking, stop.
- If they say no, keep the cart as-is and ask what they'd like to change.

IF A MERCHANT CALL FAILS:
- STOCK_UNAVAILABLE at payment time is handled by the system: it will find
  alternatives and ask the user directly. Do NOT search again or re-create
  baskets after a stock decline; simply acknowledge if asked.
- SPEND_LIMIT_EXCEEDED or CATEGORY_BLOCKED: do NOT retry; explain politely to the user.
- INVALID_REQUEST with "Unknown pack_ids": call discover_products again and use only
  the listed candidates.

NEVER assume an order is complete unless a tool result says status "completed".
Never claim money was charged unless the tool result confirms it.

STYLE: helpful shopping assistant. Short sentences. Simple words. Use ₹ for prices."""

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "discover_products",
            "description": "Search the merchant catalog with a natural-language grocery request. Returns candidate items with prices.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to find, e.g. 'chicken biryani ingredients' or 'milk bread eggs'",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_checkout_session",
            "description": "Start the cart with these items ONLY when no cart exists yet. Merchant prices everything server-side.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Initial basket (max 15 lines, ONLY pack_ids returned by discover_products)",
                        "maxItems": 15,
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "string"},
                                "pack_id": {"type": "string"},
                                "quantity": {"type": "integer", "minimum": 1, "maximum": 10},
                            },
                            "required": ["product_id", "pack_id", "quantity"],
                        },
                    }
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": "Add items to the existing cart. Requires a live cart; discover first if the pack_ids are unknown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Items to add (ONLY pack_ids returned by discover_products)",
                        "maxItems": 15,
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "string"},
                                "pack_id": {"type": "string"},
                                "quantity": {"type": "integer", "minimum": 1, "maximum": 10},
                            },
                            "required": ["product_id", "pack_id", "quantity"],
                        },
                    }
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_from_cart",
            "description": "Remove items from the existing cart by exact pack_id or plain item name (e.g. 'milk'). Removing the last item empties (cancels) the cart.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pack_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Pack ids OR item names to remove, e.g. ['pack_milk_amul_500'] or ['milk']",
                    },
                },
                "required": ["pack_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_quantity",
            "description": "Set quantity (0-10) of a cart line by exact pack_id or plain item name. Quantity 0 removes the item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pack_id": {"type": "string", "description": "Exact pack_id or item name"},
                    "quantity": {"type": "integer", "minimum": 0, "maximum": 10},
                },
                "required": ["pack_id", "quantity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_checkout_session",
            "description": "Cancel the active cart entirely (releases reserved stock). Only when the user asks to cancel everything.",
            "parameters": {
                "type": "object",
                "properties": {"checkout_session_id": {"type": "string"}},
                "required": ["checkout_session_id"],
            },
        },
    },
]
