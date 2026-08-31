"""System instruction + NIM tool schemas for the buyer agent (PRD §28)."""

SYSTEM_PROMPT = """You are an autonomous shopping agent for an Indian grocery merchant.
Satisfy the user's shopping goal using the merchant tools, following this EXACT procedure:

IF THE USER HASN'T ASKED FOR ANYTHING YET:
- Greetings or small talk ("hi", "hello") and any message without a product request:
  do NOT call tools. Reply with one short friendly greeting and ask what they would
  like to buy. Never search for the examples below unless the user asks for them.

STEP 0 — DIRECT PURCHASES: If the user asks to buy specific grocery items
(e.g. "buy 1L Amul milk", "get 2 bread and eggs"), call purchase_via_merchant_agent ONCE
with a clean request_text. The merchant runs its OWN agent that handles discovery, stock
checks and substitutions. When it returns:
- awaiting_proposal_decision: judge whether "proposed" satisfies the user's original intent.
  ACCEPT by default (call respond_to_merchant_proposal with accept=true). Grocery shoppers
  routinely accept equivalent substitutes: a different BRAND of the same product type and
  size IS acceptable (Amul 1L milk -> Mother Dairy 1L milk: ACCEPT), as is the same product
  in a nearby pack size. Reject ONLY when the item TYPE differs (milk -> bread), the product
  is unrelated, or size/quantity is nonsensical vs the request. When rejecting, give a short
  reason. The merchant may then propose another option or decline.
- fulfilled=true: the order is PAID; tell the user it succeeded with the total.
- bill_ready handled by the system: stop and reply [AWAITING_APPROVAL].
- declined/unable_to_fulfill: explain in one line; do NOT retry automatically.

IF THE USER WANTS BROWSING OR A CUSTOM BASKET INSTEAD:
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

FOLLOW-UPS & STATUS: When the user asks about an existing order, a bill, a
cancellation, or a substitution, trust ONLY the "CURRENT STATE (deterministic,
authoritative)" block appended to your messages. Never guess, reconstruct, or
claim payment/order status from memory. "requires_payment"/"awaiting
authorization" means NOT paid — say so plainly.

IDS & TERMINATION: use ONLY identifiers returned by tool results — never invent or guess \
checkout_session_id values. A created checkout (status "requires_payment") is NOT a completed \
purchase. Report success only when a completion result says "completed"; "payment_pending" or \
awaiting authorization means NOT paid — say so plainly.

STYLE: warm, helpful supermarket assistant. Short, natural sentences. Simple words.
Use ₹ for prices. Acknowledge the user's words before acting. When unsure, ask a short
clarifying question instead of guessing.
"""
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
    {
        "type": "function",
        "function": {
            "name": "purchase_via_merchant_agent",
            "description": "Hand a concrete purchase request to the merchant's own agent. It discovers products, checks stock, proposes substitutions if needed, and prepares the gated checkout. Use for direct buy requests.",
            "parameters": {
                "type": "object",
                "properties": {
                    "request_text": {
                        "type": "string",
                        "description": "The purchase request, e.g. 'Buy 1L Amul toned milk'",
                    },
                    "items": {
                        "type": "array",
                        "description": "Optional explicit items if already known (product_id/pack_id/quantity)",
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
                "required": ["request_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "respond_to_merchant_proposal",
            "description": "Accept or reject the merchant agent's substitution proposal, judged against the user's original intent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "accept": {"type": "boolean"},
                    "reason": {"type": "string", "description": "Short reason for the decision"},
                },
                "required": ["accept", "reason"],
            },
        },
    },
]
