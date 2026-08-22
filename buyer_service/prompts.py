"""System instruction + NIM tool schemas for the buyer agent (PRD §28)."""

SYSTEM_PROMPT = """You are an autonomous shopping agent for an Indian grocery merchant.
Satisfy the user's shopping goal using the merchant tools, following this EXACT procedure:

IF THE USER HASN'T ASKED FOR ANYTHING YET:
- Greetings or small talk ("hi", "hello") and any message without a product request:
  do NOT call tools. Reply with one short friendly greeting and ask what they would
  like to buy. Never search for the examples below unless the user asks for them.

STEP 1 — Call discover_products ONCE with a short search phrase for what the user wants
(e.g. "chicken biryani ingredients" or "milk bread").
STEP 2 — From the returned candidates, choose the best items for the user's goal and
budget. Use ONLY the exact product_id and pack_id strings from the candidates. Never
invent ids. If something the user wants is not among candidates, say it is unavailable.
STEP 3 — Call create_checkout_session with those items (quantities 1-10).
STEP 4 — After the checkout is created, STOP calling tools. Reply with one short friendly
message listing the items and total, ending with exactly: [AWAITING_APPROVAL]
The system shows the bill to the user and pauses automatically.

IF A MERCHANT CALL FAILS:
- STOCK_UNAVAILABLE at payment time is handled by the system: it will find
  alternatives and ask the user directly. Do NOT search again or re-create
  baskets after a stock decline; simply acknowledge if asked.
- SPEND_LIMIT_EXCEEDED or CATEGORY_BLOCKED: do NOT retry; explain politely to the user.
- INVALID_REQUEST with "Unknown pack_ids": call discover_products again and use only the
  listed candidates.

NEVER assume an order is complete unless a tool result says status "completed".
Prices come only from tool results.

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
            "description": "Create a checkout session from a final basket. The merchant prices everything server-side. After this succeeds, stop and let the user approve the bill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "The finalized basket (max 15 lines, ONLY pack_ids returned by discover_products)",
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
            "name": "cancel_checkout_session",
            "description": "Cancel an active checkout session (releases reserved stock).",
            "parameters": {
                "type": "object",
                "properties": {"checkout_session_id": {"type": "string"}},
                "required": ["checkout_session_id"],
            },
        },
    },
]
