# BUYER AGENT REASONING & CONVERSATIONAL AUDIT

Scope: Agentic Commerce Gateway (ACG) — Buyer Agent reasoning/conversational pipeline.
Mode: read-only diagnosis. No code was modified. No servers were started.
Evidence standard: every conclusion references `file → function → behavior → caller → runtime effect`.
Where latency is concerned, **no timing instrumentation exists in the runtime path** (verified, §22), so all measured cells are **NOT MEASURED** and no numbers are fabricated.

Date: 2026-08-29

---

## 1. Executive Verdict

The Buyer Agent is **not** slow primarily because of the model. It is slow primarily because of an **architecture of successive, blocking, non-streamed LLM round-trips**, including:

1. a **hidden server-side LLM call on every discovery** (`merchant/domain/discovery_service.py:123` → `_llm_parse`, which invokes the 120B `gpt-oss-120b` on Groq) that runs inside the buyer's `discover_products` tool execution, and
2. an optional **second LLM agent** (Merchant Agent) whose own loop is a sequence of further LLM calls (`merchant/agent/loop.py:214` → `_llm_decide`), invoked synchronously by the buyer's `purchase_via_merchant_agent` tool with a **150-second HTTP timeout** (`buyer_service/tools.py:151`).

The buyer LLM responses are **never streamed** (`chat.completions.create` without `stream=True`, `buyer_service/agent.py:309`); the user sees zero SSE events until the whole turn finishes.

Conversational quality problems are overwhelmingly **prompt + context + state** problems, not model quality:

- The system prompt forces a binary route decision (STEP 0 direct-purchase vs STEP 1 browse, `buyer_service/prompts.py:11-35`) onto a small model, and provides no instructions at all for mid-conversation corrections ("forget that", "don't buy it"), informational cost questions ("how much would this cost?"), or ambiguity.
- `conv.llm_history` is **never truncated or summarized** and, critically, **never observes the deterministic outcomes** of approval, cancellation, substitution, or payment completion — so the model answers follow-ups from a context that is out of date with actual state (§8).
- The F3 guard (`buyer_service/agent.py:410-427`) forces a re-discovery observation and can itself produce duplicate search calls (observed in `buyer_service_debug.log`: identical `discover_products` queries fired repeatedly).

**Money safety is intact.** The buyer can reason freely, but money authority is fully downstream: every charge goes through the deterministic `complete_checkout` ladder (`merchant/domain/checkout_service.py:681`), the bill pause is deterministic (`buyer_service/agent.py:444`), and the merchant agent cannot request payment in `buyer_completes` mode (`merchant/agent/prompts.py:39`, tool guard `merchant/agent/tools.py:726`). The audit found no path by which Buyer Agent optimizations could bypass C-1/C-2/C-3 protections (§17).

---

## 2. Actual Buyer Runtime Architecture

Traced from code (the runtime path, not filenames):

```
USER MESSAGE
  → POST /chat                                  buyer_service/main.py:131
  → F2 smalltalk gate (pure greetings/acks)     buyer_service/main.py:139-153  (0 LLM, canned reply)
  → agent.llm_loop(conv, message)               buyer_service/agent.py:300
      → build messages = [SYSTEM_PROMPT, *conv.llm_history]     agent.py:303
      → FOR i in range(max_agent_iterations=12)                 config.py:15
          → OpenAI chat.completions.create                      agent.py:309
              model=openai/gpt-oss-20b, Groq, temp 0.2, max_tokens 700, NON-streaming
          → if tool_calls:
              → execute tool:
                  discover_products             agent.py:82  → MerchantClient.discover   tools.py:23
                                                               → POST /discover
                                                                 → _llm_parse() (HIDDEN 120B call)  discovery_service.py:123-158
                                                                 → deterministic match/rank/pick
                  create_checkout_session       agent.py:124 → MerchantClient.create_checkout  tools.py:45
                                                               → POST /checkout_sessions (deterministic gating + reservation)
                  cancel_checkout_session       agent.py:176 → MerchantClient.cancel_checkout   tools.py:120
                  purchase_via_merchant_agent   agent.py:265 → MerchantClient.start_agent_session tools.py:140
                                                               → POST /agent_sessions  (SYNCHRONOUS, timeout=150s)
                                                                 → MerchantAgent().run(state)  loop.py:537
                                                                     → per-iteration LLM call  loop.py:232 (gpt-oss-120b, non-streaming)
                                                                     → deterministic tools     tools.py:820
                                                                     → pause/terminal outcome
                  respond_to_merchant_proposal  agent.py:281 → MerchantClient.respond_proposal  tools.py:158
                                                               → POST /agent_sessions/{id}/decision (timeout=150s)
                                                               → MerchantAgent().resume(state)  loop.py:626  (more LLM iterations)
              → append tool result JSON to messages      agent.py:389
              → if bill_ready or created → present_bill() deterministic → yield BILL_PAUSE → return   agent.py:354-388
          → else (no tool calls): leaked-toolcall retry / F3 nudge / final message   agent.py:392-431
  → SSE events streaming of COMPLETED events     main.py:92-128

USER APPROVAL (button, separate request)
  → POST /conversations/{id}/decision            main.py:161
  → resume_after_decision → _complete_flow       agent.py:463-621  (deterministic; 0 LLM)
      → complete_checkout (ladder: price→delegation→stock→policy→AP2 mandate→Razorpay)  checkout_service.py:681
      → STOCK_UNAVAILABLE → _propose_substitutes (deterministic)  agent.py:630
      → PRICE_CHANGED → present_bill_and_pause (revised bill)     agent.py:573
```

### 2.1 What the buyer can reach and what is always deterministic

- The only money movement is `complete_checkout` (`buyer_service/tools.py:89`, `merchant/api/checkout.py:102`) which is fully deterministic and revalidates everything in a ladder before Razorpay (`checkout_service.py:681-1143`).
- Payment is **never triggerable by the LLM**: the buyer loop hard-pauses on `created`/`bill_ready` before any completion (`agent.py:354-388`), and the merchant agent in `buyer_completes` mode cannot even see `request_completion` (`loop.py:174-183`, `tools.py:726`).
- Deterministic recovery paths (substitution options, price-change rebill, decline explanations) run without any LLM (`agent.py:463-780`).

---

## 3. Complete LLM Call Inventory

All runtime LLM invocation sites, classified. Delivery checks: no site streams. No site reads `resp.usage`. No site has an explicit retry policy (only loop-level nudges and one error message).

### Call BUY-1 — Buyer main loop

| Field | Value |
|---|---|
| CALL ID | BUY-1 |
| FILE | `buyer_service/agent.py` |
| FUNCTION | `BuyerAgent.llm_loop` (line 309) |
| MODEL | `openai/gpt-oss-20b` |
| PROVIDER | Groq (`https://api.groq.com/openai/v1`) |
| PURPOSE | Intent understanding + discovery + basket construction + tool selection + final response generation — one call does **everything** (classes A,B,C,D,E,H,I; multi-responsibility — flagged) |
| SYSTEM PROMPT | `SYSTEM_PROMPT` (`prompts.py:3-53`) |
| USER INPUT | the new user message + full `conv.llm_history` (all prior user/assistant/tool messages) |
| CONTEXT INCLUDED | full unbounded history, all previous tool-result JSON, system prompt |
| TOOLS AVAILABLE | 5 schemas (`prompts.py:55-157`) |
| TOOL CALLS | loops: up to `max_agent_iterations` = 12 rounds |
| OUTPUT FORMAT | chat.completions message (tool_calls or free text) |
| MAX TOKENS | 700 |
| TEMPERATURE | 0.2 |
| OTHER PARAMETERS | none (no top_p, no stop, no response_format, stream=False) |
| RETRY POLICY | none on transport failure (one error message and stop, `agent.py:316-318`); leaked-toolcall re-prompt ≤ 2 (`agent.py:395-406`); F3 discovery nudge ≤ 1 (`agent.py:410-427`) |
| TIMEOUT | OpenAI client timeout 60s (`agent.py:33`) |
| STREAMING | No |
| CALLEE | executes tools → MerchantClient HTTP |
| DOWNSTREAM EFFECT | may create checkout/agent-session, present bill, pause, or emit final message |

### Call BUY-2 — Discovery LLM parse (HIDDEN)

| Field | Value |
|---|---|
| CALL ID | BUY-2 (hidden; announced to the buyer only as a tool execution) |
| FILE | `merchant/domain/discovery_service.py` |
| FUNCTION | `_llm_parse` (line 123), invoked from `discover` (line 257) |
| MODEL | `settings.llm_model` = `openai/gpt-oss-120b` |
| PROVIDER | Groq |
| PURPOSE | NL→structured keyword/budget parse (class I) — a classification task executed by a 120B reasoning model |
| SYSTEM PROMPT | none (single user prompt) |
| USER INPUT | one-shot JSON-extraction prompt + the raw query (`discovery_service.py:131-140`) |
| CONTEXT INCLUDED | tiny |
| TOOLS AVAILABLE | none |
| TOOL CALLS | 0 |
| OUTPUT FORMAT | JSON (best-effort; any exception → None → deterministic `_fallback_parse`) |
| MAX TOKENS | 200 |
| TEMPERATURE | 0.0 |
| OTHER PARAMETERS | none |
| RETRY POLICY | none — on failure silently falls back to `_fallback_parse` (line 257) |
| TIMEOUT | OpenAI client timeout 10s (`discovery_service.py:130`) |
| STREAMING | No |
| CALLEE | `/discover` endpoint (called by buyer `discover_products` and `_propose_substitutes`) |
| DOWNSTREAM EFFECT | keyword/budget extraction feeding deterministic product matching. Runs **on every `/discover`** for the buyer path (buyer client sends `skip_llm_parse=False` except in the substitute-recovery path, `buyer_service/tools.py:29`, `agent.py:659`) |

### Call BUY-3 — Merchant Agent brain (per iteration)

| Field | Value |
|---|---|
| CALL ID | BUY-3 (i.e., `MA-1…MA-N` — one per merchant-agent iteration) |
| FILE | `merchant/agent/loop.py` |
| FUNCTION | `_llm_decide` (line 232), loop in `MerchantAgent.run` (line 537) |
| MODEL | `settings.llm_model` = `openai/gpt-oss-120b` |
| PROVIDER | Groq |
| PURPOSE | merchant-agent action selection + termination (classes E,F,I) |
| SYSTEM PROMPT | `prompts.SYSTEM_PROMPT` (`merchant/agent/prompts.py:8-42`) |
| USER INPUT | `_transaction_state_note` (fresh per turn, `loop.py:145-171`) + `_initial_context` (buyer request, requested items, delegation caps, `loop.py:98-133`) + `state.llm_history` |
| CONTEXT INCLUDED | full merchant llm_history incl. each tool result truncated to 1800 chars (`loop.py:608`) |
| TOOLS AVAILABLE | 11 schemas + `finish` (mode-filtered, `loop.py:174-183`) |
| TOOL CALLS | exactly 1 primary action per turn (extras skipped, `loop.py:279-286`) |
| OUTPUT FORMAT | tool_calls or plain text (nudge cycle up to 3 → finish declined, `loop.py:244-264`) |
| MAX TOKENS | 700 |
| TEMPERATURE | 0.2 |
| OTHER PARAMETERS | none |
| RETRY POLICY | LLM transport failure → deterministic fallback brain (`loop.py:517-526`); prose nudges ≤ 3 |
| TIMEOUT | OpenAI client timeout 45s (`loop.py:223`) |
| STREAMING | No |
| CALLEE | merchant tools (`tools.py:820`) → checkout/inventory/DB |
| DOWNSTREAM EFFECT | may create/update checkout, propose substitution, pause `awaiting_buyer_confirmation`, or request gated completion |

### Call BUY-4 — merchant substitution/terms decision resume (re-enters BUY-3 loop)

| Field | Value |
|---|---|
| CALL ID | BUY-4 |
| FILE | `merchant/agent/loop.py` `MerchantAgent.resume` (line 626) → `self.run` |
| MODEL / PROVIDER | same as BUY-3 |
| PURPOSE | continue the loop after buyer accepts/rejects a proposal — a fresh reasoning window (classes E,F) |
| DOWNSTREAM EFFECT | re-runs `_llm_decide` per iteration until terminal; each iteration = another BUY-3 call |

### Deterministic (0 LLM) — listed for completeness

- `main.py:152` `_is_smalltalk/_smalltalk_gen` — canned small-talk for fresh conversations.
- `agent.py:463` `resume_after_decision`, `:487` `_complete_flow`, `:630` `_propose_substitutes`, `:720` `choose`, `:623` `present_bill_and_pause` — bill/approval/payment/recovery.
- `checkout_service.py:681` `complete_checkout` + Razorpay path — money.
- Merchant `_fallback_decide` (`loop.py:337`) — deterministic brain used when LLM disabled/unavailable.

> **Flag (multi-responsibility):** BUY-1 alone performs message classification, catalog selection, basket construction, tool routing, and utterance generation. BUY-2 is a 120B model used for a task the repository already solves deterministically (`_fallback_parse`, `discovery_service.py:90`).

---

## 4. User Scenario Call Graphs

Counts are **code-traced**, with branches documented. "buyer LLM" = BUY-1; "parse LLM" = BUY-2; "merchant LLM" = BUY-3 iterations. An "interaction" ends at the pause or final message; the payment leg (approve → complete) is deterministic and listed separately.

### Scenario A — "Hi" (fresh conversation)

```
"Hi"
  → F2 smalltalk gate ✓ (main.py:152, 0 prior real exchanges, ≤4 whitelisted words)
  → _smalltalk_gen  → canned message
LLM calls: 0   Tools: 0
Branch: any non-smalltalk greeting or any message after a real exchange → BUY-1 (1 call) → likely 0 tools → final message.
```

### Scenario B — "What milk do you have?"

```
"What milk do you have?"
  → BUY-1 #1 → decides discover_products
  → tool discover_products("milk")  → POST /discover  → parse LLM (BUY-2) + deterministic match → candidates
  → BUY-1 #2 → final answer (tool result present in history)
LLM calls: 2 buyer + 1 hidden parse   Tools: 1
Branch (observed risk): if model answers from memory without a tool result, F3 nudge (agent.py:410)
fires → BUY-1 #3 forced to discover. Worst case 3 buyer + 1 parse.
Branch (prompt risk): if the model classifies as STEP 0 direct purchase, it may fire
purchase_via_merchant_agent instead → merchant agent path (§4 Scenario C). The deterministic
bill gate then still prevents any charge, but an availability question can become a checkout.
```

### Scenario C — "I need 1 litre milk."

```
"I need 1 litre milk."
  → BUY-1 #1 → purchase_via_merchant_agent("Buy 1 litre milk")         (STEP 0 route)
  → HTTP POST /agent_sessions (timeout 150s) → MerchantAgent.run:
        merchant LLM #1 → search_catalog (deterministic fallback-parse inside the tool)
        merchant LLM #2 → create_checkout (deterministic gating/reservation)
        merchant LLM #3 → finish(ready_for_completion)                  [buyer_completes mode]
  → result: bill_ready → present_bill() + BILL_PAUSE (NO further buyer LLM)   (agent.py:354-361)
LLM calls: 1 buyer + 3 merchant   Tools: 1 buyer + 3 merchant   Sequential LLM hops: 4
Branch (browse route): BUY-1#1 → discover (→parse LLM) → BUY-1#2 → create_checkout → bill pause.
  2 buyer + 1 parse.
Amul-1L-specific merchant branch: requested pack out of stock → find_alternative →
request_confirmation → status awaiting_buyer_confirmation → buyer sees proposal →
BUY-1 #2 → respond_to_merchant_proposal(accept/reject) → POST /decision (150s) →
merchant resume: merchant LLM #4+ (update/create_checkout → finish) → buyer presents bill.
  3 buyer + 4-6 merchant.
```

### Scenario D — "Buy milk, bread and eggs under ₹300."

```
Route depends on the 20B model's STEP 0 vs STEP 1 choice (ambiguous; §6).
Direct route:
  BUY-1 #1 → purchase_via_merchant_agent
  merchant LLM #1 search → merchant LLM #2 create_checkout(all 3) → deterministic gating:
     over-budget → decline envelope → merchant LLM #3 finish(declined, policy_declined)
     under-budget → merchant LLM #3 finish(ready_for_completion) → bill pause
  if declined → BUY-1 #2 explains ("do NOT retry automatically", agent.py:257-262)
Browse route:
  BUY-1 #1 → discover("milk bread eggs") [parse LLM parses the ₹300 budget; discovery
     picks budget-compliant packs, discovery_service.py:275-281] → BUY-1 #2 → create → bill pause
LLM calls: 2-4 buyer + 0-3 merchant + up to 1 parse
```

### Scenario E — "Buy the cheapest milk."

```
  BUY-1 #1 → purchase_via_merchant_agent("buy the cheapest milk")
  merchant LLM #1 search_catalog → merchant LLM #2 create_checkout (search results are
     price-sorted, tools.py:176; deterministic picker chooses cheapest pack) → merchant LLM #3 finish
  bill pause.
LLM calls: 1 buyer + 3 merchant
Branch (browse route): discover returns cheapest pack by construction (_pick_pack,
discovery_service.py:195) → 2 buyer + 1 parse.
```

### Scenario F — "Cancel that order."

```
Branch A (bill pending): POST /chat is REFUSED — HTTP 409 "A bill is awaiting your approval
  below." (main.py:145-146). The user cannot cancel by typing; they must use the decision
  card (approve/decline) or another conversation. Text-based cancellation is unsupported here — flagged.
Branch B (active session, no pending bill): BUY-1 #1 → cancel_checkout_session →
  POST /checkout_sessions/{id}/cancel → deterministic cancel (checkout_service.py:552).
  LLM calls: 1   Tools: 1
Branch C (no active session): BUY-1 may answer from memory — no ordering/basket history
  beyond llm_history — LLM may hallucinate a session id. Prompt warns "never invent ids",
  but there is no structured do-you-have-an-active-order helper.
```

### Scenario G — "The milk was unavailable, find another one."

```
  conv.declined_packs was populated by a prior STOCK_UNAVAILABLE (agent.py:592-597).
  BUY-1 #1 → discover_products → _execute_tool filters out blocked packs and re-issues an
     OUT OF STOCK warning (agent.py:103-119) → BUY-1 #2 → create_checkout(alternative) → bill pause.
  LLM calls: 2 buyer + 1 parse   Tools: 2
Branch (agent route): if the model chooses purchase_via_merchant_agent, the merchant agent
  does NOT know conv.declined_packs (state.excluded_pack_ids starts empty, state.py:59) and
  may re-propose the declined item → buyer reject → merchant find_alternative → more merchant LLM
  calls. This re-proposal loop is a flagged interaction (see §15).
```

### Scenario H — "Buy 50 units regardless of the budget."

```
  BUY-1 #1 → purchase_via_merchant_agent
  merchant LLM #1 search → merchant LLM #2 create_checkout(qty 50) → deterministic gating
     fails (spend_limit / quantity_limit) → decline envelope (tools.py:484-493)
  merchant LLM #3 finish(declined, policy_declined) (prompt: "do NOT retry the same basket")
  BUY-1 #2 → explains.
  LLM calls: 2 buyer + 3 merchant
Branch: merchant LLM attempts find_alternative → proposal → request_confirmation →
  pause → buyer interaction (this path can consume significant merchant budget; capped by
  ma_max_iterations=10, tool budget 10; _guard_no_silent_substitution prevents silent swaps).
```

### Scenario I — Normal successful payment (approval leg)

```
approve → resume_after_decision (agent.py:463) → _complete_flow → complete_checkout
  → deterministic ladder (price→delegation→stock→policy→AP2 execution mandate→Razorpay
  saved-token charge, checkout_service.py:681-1143) → status message.
LLM calls: 0   (all deterministic)
Branch: token charge needs auth → bill_redirect (Razorpay modal, agent.py:540-559);
  payment_pending → reconcile → completed (agent.py:509-539).
```

### Scenario J — Payment / merchant failure

```
STOCK_UNAVAILABLE at complete:
  _complete_flow parses failed line items (agent.py:581-597) → _propose_substitutes
  (deterministic: discovery with skip_llm_parse=True, agent.py:659) → substitute card →
  choose() → create_checkout (deterministic) → revised bill.
  LLM calls: 0 (recovery); 0 parse (skip_llm_parse=True)
PRICE_CHANGED at complete:  → present_bill_and_pause (revised bill) → approve → complete again.
  LLM calls: 0
Hard decline (SPEND_LIMIT/CATEGORY/etc.): deterministic friendly message (agent.py:611-621).
  LLM calls: 0
```

### Table B — Scenario Complexity

| Scenario | LLM Calls | Tool Calls | Merchant Agent Calls | Retry Paths | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| Greeting "Hi" | 0 (fresh) / 1 (mid-chat) | 0 | 0 | F2 gate | canned for fresh convs only |
| "What milk do you have?" | 2-3 buyer + 1 hidden parse | 1-2 | 0 | F3 nudge ≤1 | step-0/1 misroute risk |
| "I need 1 litre milk." | 1 buyer + 3 merchant (direct) / 2 buyer + 1 parse (browse) | 1 + 3 merchant | 1 | merchant fallback brain; ≤3 LLM nudges | 150s HTTP window |
| "Buy milk, bread & eggs under ₹300" | 2-4 buyer + 0-3 merchant + ≤1 parse | 2-3 | 0-1 | as above | budget handled deterministically |
| "Buy the cheapest milk." | 1 buyer + 3 merchant | 1 + 3 merchant | 1 | as above | price-sorted results |
| "Cancel that order." | 0-1 | 0-1 | 0 | none | 409-block when bill pending |
| "milk unavailable, find another" | 2 buyer + 1 parse | 2 | 0 / 1 (agent route) | declined-pack filter | merchant re-proposal loop risk |
| "Buy 50 units regardless" | 2 buyer + 3 merchant | 1 + 3 merchant | 1 | no auto-retry, gating declines | bounded by budgets |
| Successful payment | 0 (all deterministic) | 0 | 0 | idempotency keys | complete ladder + Razorpay |
| Payment/merchant failure | 0 | 1-2 | 0 | substitute/rebill | deterministic recovery |

---

## 5. Latency Analysis

**There is no production timing instrumentation anywhere in the runtime path.** Verified by search — the only `time.perf_counter` usage in the repo is `scripts/_bench.py` (a standalone dev benchmark that sets `MA_LLM_BRAIN=false`, i.e. measures the deterministic fallback, not the real brain; `scripts/_bench.py:3`). No request timing middleware, no LLM-call timers, no `time.time`/`monotonic` in `buyer_service/` or `merchant/`. `buyer_service_debug.log` records tool names/args but **no timestamps** (`agent.py:345-347`). Therefore all latency values are **NOT MEASURED**; this section instead identifies where latency accumulates and what must be measured.

### 5.1 Serialization & blocking

The buyer is fully synchronous on a worker thread per request (`main.py:92-114`). Every hop below is a blocking wait, and none of the LLM calls stream:

| # | Stage | Where | Blocks on | Latency Known? |
| --- | --- | --- | --- | --- |
| 1 | request handling | `main.py:131` | body parse, conv lock | NOT MEASURED |
| 2 | LLM time-to-first-token | `agent.py:309` (BUY-1) | Groq gpt-oss-20b reasoning model, non-streamed → first token not observable by design | NOT MEASURED |
| 3 | full LLM generation | `agent.py:309` | 700 max tokens, non-streamed | NOT MEASURED |
| 4 | tool execution | `agent.py:349-353` | httpx call to merchant | NOT MEASURED |
| 5 | network to Merchant Agent | `tools.py:23-45,140-165` | localhost merchant | NOT MEASURED |
| 6 | Merchant Agent LLM time | `loop.py:232` (BUY-3) | Groq gpt-oss-120b, per iteration | NOT MEASURED |
| 7 | database operations | `checkout_service.py` gating/reservation, `audit_service` | SQLite | NOT MEASURED |
| 8 | Razorpay calls | `payment_service` (create_order/charge_with_token/fetch_payment) | gateway | NOT MEASURED |
| 9 | final response generation | `agent.py:429-431` (BUY-1) or deterministic templates | — | NOT MEASURED |
| 10 | SSE streaming | `main.py:116-128` | stream is instant; the underlying work has already completed | NOT MEASURED |

### 5.2 Where latency accumulates (always true of the current code)

- **BUY-1 is unstreamed** — the entire `chat.completions.create` (TTFT + generation) elapses before any event reaches the browser. This is the dominant *perceived* latency for every message.
- **Every `discover_products` executes a hidden 120B parse (`BUY-2`)** with its own network round-trip (10s client timeout). It runs inside a tool-call *within* a BUY-1 turn, so perceived latency compounds: BUY-1#1 → BUY-2 → BUY-1#2.
- **`purchase_via_merchant_agent` holds the buyer request open while the merchant runs its entire LLM loop**: the HTTP timeout is **150s** (`tools.py:151`), and each merchant iteration costs its own non-streamed 120B call with up to 10 iterations (`config.py:50-51`). For direct purchases this is the single largest possible latency bucket.
- **Sequential hops**, not parallel ones: BUY-1 → BUY-2 → BUY-1 → (merchant LLM)×N → BUY-1... each hop serializes on the previous.
- **Razorpay** is hit only on the deterministic approval leg; with `driver=saved` a token charge (~1 gateway call) or a modal redirect, never retried blindly.

### 5.3 Instrumentation points needed (mirrors §22)

For each `OpenAI(...).chat.completions.create` at `agent.py:309`, `loop.py:232`, `discovery_service.py:141`: record `time.perf_counter()` around the call, split TTFT (first streamed chunk if `stream=True`) vs full, and capture `resp.usage` (currently discarded). For each `httpx` call in `buyer_service/tools.py`: record duration and status. For the agent loop: record per-iteration duration, tool envelope size, and cumulative count. All values above then become measurable.

## 6. Prompt Analysis

Source: `buyer_service/prompts.py` `SYSTEM_PROMPT` (lines 3-53) + `TOOL_SCHEMAS` (55-157).

### GOOD

- Clear role: "autonomous shopping agent for an Indian grocery merchant".
- Termination is explicit at the points that matter: "After the checkout is created, STOP calling tools… ending with exactly: [AWAITING_APPROVAL]".
- Money truth rules are explicit and correct: "A created checkout … is NOT a completed purchase", "Report success only when a completion result says 'completed'", "NEVER assume an order is complete unless a tool result says 'completed'" (`prompts.py:45-51`).
- Ground-in-reality: "Prices come only from tool results", "use ONLY returned candidate pack_ids".
- Failure discipline: no auto-retry on `SPEND_LIMIT_EXCEEDED`/`CATEGORY_BLOCKED`; stock-recovery is declared system-owned (`prompts.py:37-43`).

### BAD

- **Binary route decision (STEP 0 vs STEP 1)** puts message-classification on the 20B model with two long competing procedures (`prompts.py:11-35`). Small models misclassify ("chicken" alone became `purchase_via_merchant_agent` in the debug log), sending availability questions or bare nouns down the merchant-agent (slowest) path or, conversely, sending "buy" requests down the browse (discovery) path.
- The instruction "ACCEPT by default" for merchant substitutions (`prompts.py:16-19`) bakes in a behavior bias; combined with the merchant-side `request_confirmation` it can steer the buyer into approvals without its own reasoning (flagged under agenticity, §13).

### AMBIGUOUS

- "IF THE USER HASN'T ASKED FOR ANYTHING YET: … do NOT call tools" vs the F3 nudge that forces `discover_products` for any ≥3-word message (`agent.py:410-427`). "Do you have Amul milk?" is simultaneously "hasn't asked for anything" (a question) and "ask for a product". Two instructions conflict; behavior depends on which the model obeys first.
- "buy specific grocery items" (STEP 0) vs "browsing or a custom basket" (STEP 1) — "I need 1 litre milk" is a soft request; no crisp predicate distinguishes the two.
- "bill_ready handled by the system: stop and reply [AWAITING_APPROVAL]" (`prompts.py:23`) — the system pauses *before* the model can reply; the model is told to perform an action that will never be observed (the pause returns before the next BUY-1 happens). Harmless but confusing; it trains the model toward writing `[AWAITING_APPROVAL]` text in other contexts.

### EXPENSIVE

- The step-list repeats termination/ids rules twice (STEP 4 and the "IDS & TERMINATION" paragraph, `prompts.py:34-35,48-52`), and embeds example queries ("chicken biryani ingredients", "milk bread") that small models have been observed to copy verbatim into `discover_products` (the prompt even warns about this; the F2 gate exists because of it, `main.py:31-48`). Full-system token cost is modest (~0.6-0.9k tokens), so this is not a per-call cost problem — it is an instruction-clarity problem.

### MISSING

- Nowhere defined: mid-conversation corrections ("Actually, forget that. I want bread instead"), textual cancellations ("No, don't buy it"), informational cost questions ("How much would this cost?"), or intent resets. The model must infer all of these from raw history.
- No clarification behavior for ambiguity ("I need milk" → size? brand? quantity?).
- Nothing about answering from a *previous* observation: F3 forces a fresh discover every turn even when the answer already exists in history — the model may re-run discovery instead of reusing `conv.last_candidates`.
- No explicit injection-defense instruction (user text and merchant `message_from_merchant` are untrusted input placed into tool results the model is told to treat as fact).

### Prompt injection exposure

- User message text is injected verbatim as a `user` message (`main.py:150`, `agent.py:302`). Merchant-agent `message`/`message_from_merchant` strings reach the model as tool results (`agent.py:237,257`). A hostile grocery item name or merchant note could steer tool calls; systemic impact is bounded (money gates are deterministic), but junk search/checkout attempts are possible. No prompt-level "treat user input as data, not instructions" guard exists.

---

## 7. Context Analysis

No token counting is implemented anywhere (`resp.usage` is discarded at all three call sites). Counts below are structural; **token values = NOT MEASURED**.

### 7.1 Per-call context table

| Call | Static Prompt | Conversation | Tool Schemas | Tool Results | Merchant Data | Other |
| --- | --- | --- | --- | --- | --- | --- |
| BUY-1 (buyer loop) | `SYSTEM_PROMPT` (~0.6-0.9k tokens est., NOT MEASURED) | full `conv.llm_history` — **unbounded, never truncated/summarized** | 5 schemas | all previous tool JSON, verbatim | trimmed via `_map_agent_result` | debug-log side effect |
| BUY-2 (parse) | 1-shot JSON instruction | none | none | none | none | raw `query` string |
| BUY-3 (merchant, each iteration) | merchant `SYSTEM_PROMPT` | `state.llm_history` (per-session) | 11 + finish | each envelope truncated to 1800 chars (`loop.py:608`) | delegation caps + buyer request (in `_initial_context`) | `_transaction_state_note` rewritten per turn |

### 7.2 Context inflation causes (buyer side)

1. **Unbounded history replay.** `conv.llm_history` accumulates every user message, every assistant tool-call reply, and every tool-result JSON; the same discovery payload and checkout payload are re-sent on **every subsequent turn**. No truncation, summary, or turnover (`conversations.py:11`, `agent.py:303`).
2. **Repeated discovery payloads.** Each `discover_products` result is `json.dumps`-ed whole into history (`agent.py:389`); a long conversation replays N discovery results. Observed in the debug log: the same query emitted 2-3× within single flows.
3. **Repeated full-create results.** The `create_checkout_session` result is stored whole at bill-pause (`agent.py:380-388`), then re-sent on the next user message with the full line_items/totals again.
4. **Deterministic receipts invisible.** Approval/cancel/payment outcomes are *not* appended to history, so follow-ups carry a stale snapshot (see §8).
5. Small but structural: 5 tools + prompt repeated on every BUY-1; the buyer's own result-trimming in `_execute_tool` (compact candidate view, `agent.py:88-100`) and `_map_agent_result` (subset views, `agent.py:191-262`) is credit-worthy and already limits the worst bloat.

### 7.3 Context inflation causes (merchant side)

- The full serialized session — `line_items` (name/category/tags/size/unit/qty/unit_price/stock_actual), `totals`, `gating_result` (full checks list), `reservation`, `payment` — is produced by several tools (`merchant/agent/tools.py:469-503,530-558`) and placed in the LLM-visible envelope (truncated at 1800 chars per envelope). The model rarely needs more than `status`, items, `total`, and `error.code`.
- `_transaction_state_note` and decision/guidance notes are injected every iteration (`loop.py:145-171,694-723`) — correct and cheap, but repeat per turn.

---

## 8. Conversation State Analysis

State model: `buyer_service/conversations.py:7-32` `Conversation`; merchant side `merchant/agent/state.py:41-103` `MerchantAgentState`; money side persists in DB (`CheckoutSession`).

### What is explicitly tracked (buyer)

| Concept | Tracked? | Where / Who updates / How seen by LLM |
| --- | --- | --- |
| current user intent | ✗ (not explicit) | only implicit in `llm_history`; LLM must re-infer each turn |
| current task / phase | ~ | `pending_approval`, `pending_substitution`, `busy` flags (UI-level only) |
| selected products | ~ | `last_basket_items` (updated only at bill presentation, `agent.py:452`) — not corrected on approval/cancel |
| candidate products | ✓ | `last_candidates` (set on every discover, `agent.py:101`) — used to validate `create_checkout_session` pack_ids (`agent.py:133-145`) |
| basket | ✓ (server) | authoritative in merchant `CheckoutSession`; buyer keeps a best-effort copy |
| checkout session | ✓ | `active_session_id`; cleared on cancel/complete/decline; **not** always cleared on substitution paths |
| pending confirmation | ✓ | `pending_approval` (bill) / `pending_substitution` (card); set in `present_bill`/`_propose_substitutes`; cleared in resume/choose |
| payment state | ✓ (server) | reflected to user via deterministic streams; **never** written back to `llm_history` |
| previous tool result | ~ | last result is in `llm_history`; earlier ones remain but no structured pointer |
| failure reason | ~ | only the pack-level `declined_packs`; no structured per-turn failure reason object |
| user corrections | ✗ | none — a correction like "forget that" is just a user message in history |
| cancellation | ~ | UI-level (`pending_approval` None after decision); text "don't buy it" is unsupported (409) |
| completed state | ✓ (server) | not propagated into the LLM context (§8.2) |

### 8.1 Source of truth / storage / update / handoff

- **Source of truth:** merchant DB (`CheckoutSession`, reservations, gating) for money/basket; in-memory `Conversation` for dialog; in-memory `MerchantAgentState` for merchant-agent sessions.
- **Stored:** in-memory Python dicts never persisted (`CONVERSATIONS`, `SESSIONS`) → process restart loses all dialog + agent-session state (DB keeps money truth).
- **Updated:** deterministic paths update `Conversation` fields; LLM path only appends to `llm_history`.
- **Passed to LLM:** **state is not injected as a first-class block** — the model only sees `llm_history`, and reads corrections/status from free text. The one deterministic injection is via tool results (`last_candidates` filter, blocked-pack warning, bill-present tool msg).

### 8.2 Stale / duplicated state — the key conversational defect

The LLM's view diverges from reality in both directions:

1. **Approval/cancel/payment invisible to the LLM.** `resume_after_decision` (`agent.py:463-486`), `_complete_flow` (`agent.py:487-621`), `choose` (`agent.py:720`), and `_propose_substitutes` never append to `conv.llm_history`. After a completed order, the **next user message** ("How much was it?", "Actually, forget that. I want bread instead.") is answered by a BUY-1 whose history ends at the *bill-presented* tool message — the model literally cannot know the order completed (or was cancelled) except by guessing. This directly produces the "responds to something different from what the user asked" symptom.
2. **Duplicated basket sources.** `last_basket_items` is written only at bill time and is not reconciled with approval outcomes; `active_session_id` and merchant `checkout_session_id` can drift (e.g., STOCK_UNAVAILABLE clears buyer id but not merchant state). Substitution round options live in `pending_substitution` (UI card) while `offered_pack_ids`/`declined_packs` accumulate on the conversation.

### 8.3 Cross-check on the audit's target behaviors

- "Do you have Amul milk?" — F3 pushes a fresh `discover_products`; the model then answers from the tool result (correct). The *risk* is the STEP-0 misroute turning this into `purchase_via_merchant_agent` and later a bill (blocked from payment only by the deterministic gate).
- "Actually, forget that. I want bread instead." — no state op exists; the model sees old discovery/create tool results plus the new message. Outcome is model-lottery: it may re-mix old basket items. Worse, the create result's "Unknown pack_ids" guard requires re-discovery, and the model often re-runs the same query.
- "No, don't buy it." — if a bill is pending, `/chat` returns 409 (`main.py:145-146`) and the text message is dropped from the UI flow entirely; the intended path is the button. If no bill is pending the model has no structured "cancel the intended action" affordance.
- "How much would this cost?" — the prompt has no informational-vs-transactional rule, so the model may call `create_checkout_session` to discover (total comes back correct but a checkout is created, stock reserved) when a deterministic price lookup was available in the last candidate data.

## 9. Tool Analysis

### 9.1 Buyer-side tools (`prompts.py:55-157`, executed in `agent.py`)

| Tool | Purpose | Input | Output | Can change state | Can move money | Requires user confirmation | Requires merchant authorization | Idempotency | Failure response |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `discover_products` | catalog search (NL query) | query string | trimmed candidate list + estimated_total + unmatched + instruction | read-only at merchant (audit only) | no | no | no | yes (read) | `{error}` message |
| `create_checkout_session` | build basket+session | items (≤15) | session id/status/line_items/totals/intent_mandate_id | yes (creates session + reservations) | no | YES (bill pause immediately after) | yes (deterministic gating) | **no idempotency key sent by buyer** (`tools.py:45`) — repeated calls create fresh sessions; a leftover prior session is left `requires_payment` | `{created:false, declined, code, message}` |
| `cancel_checkout_session` | release active session | session id (optional, falls back to `active_session_id`) | cancelled bool | yes | no | no | yes | yes (merchant `cancel_checkout` is idempotent, `checkout_service.py:562-563`) | `{cancelled:false, reason}` |
| `purchase_via_merchant_agent` | hand request to merchant agent | request_text (+ optional items) | mapped outcome (bill_ready / awaiting_proposal_decision / fulfilled / declined) | yes (merchant agent session) | no | YES (bill pause if bill_ready) | yes | **no dedup** — every call spawns a new agent session (`tools.py:151`, `api/agent.py:100-119`) | `{error}` (transport) — treated as non-decline |
| `respond_to_merchant_proposal` | accept/reject substitution | accept, reason | mapped outcome | yes | no | no (the proposal card IS the confirmation) | yes | merchant-side guardrails | `{error}` |

### 9.2 Do tools expose too much / return too much?

Buyer tool *results* fed to the LLM are already well-trimmed (`_execute_tool` compact candidates `agent.py:88-100`; `_map_agent_result` subset views `agent.py:191-262`). Residual noise:

- `discover_products` result sends an editorializing `instruction` block plus an OUT-OF-STOCK note that grows with the blocked set (`agent.py:110-121`) — repeated small payloads.
- `create_checkout_session` result includes `intent_mandate_id` (AP2 id, `agent.py:163`) — not needed by the model; harmless but leaks internal identifier.
- Merchant `_session_out` (`api/agent.py:55-82`) transfers the **full** serialized checkout (line items, gating checks, reservation, payment incl. `key_id`) over the wire even though `_map_agent_result` later trims it for the model. Wire bytes are large; model-visible is small.
- Merchant tools return full-card data to the merchant LLM (`tools.py:469-503,530-558`) — `stock_actual`, `gating_result`, `reservation`, `payment` mostly irrelevant to deciding the next action; the 1800-char envelope cap (`loop.py:608`) keeps it bounded but wastes reasoning tokens.

MACHINE-USEFUL vs HUMAN/UI split: the merchant session serialization is UI/audit data (receipts, mandate ids, gating detail) beyond the `{status, total, decline_reason}` trio the agent actually needs — this matches the audit's "flags".

### 9.3 Structural tool-design gaps

- `create_checkout_session` is not idempotent client-side; the debug log shows repeated create calls with identical items (two sessions created). The merchant *create* itself supports an idempotency key (`api/checkout.py:29-47`) the buyer never sends.
- The merchant agent route re-plans from scratch without inheriting buyer `declined_packs`/`offered_pack_ids` (§15), so the same pack can be re-proposed.
- No buyer tool exposes "what is the current order state?" — the model must infer from history; §8.2 showed that history is stale after deterministic steps.

---

## 10. Reasoning Loop Analysis

### 10.1 Buyer loop (`agent.py:300-433`)

- **Max iterations:** `max_agent_iterations = 12` (`config.py:15`).
- **Termination:** (a) a non-tool reply after passing leaked-toolcall/F3 guards, (b) `created`/`bill_ready` → bill pause, (c) budget exhaustion → canned apology.
- **Retries:** leaked-toolcall re-prompt ≤ 2 (`agent.py:395-406`); F3 discovery nudge ≤ 1 (`agent.py:410-427`); transport error → 1 message, stop.
- **No self-correction loop, no handoff except the deterministic pause/decision endpoints.**

### 10.2 Merchant loop (`loop.py:537-624`)

- **Max iterations:** `ma_max_iterations = 10`, multiplied by `min(4, max(1, budget_windows))` (a fresh session = 10; each buyer decision re-opens a window up to 4 → max 40) (`loop.py:543-546`).
- **Tool budget:** `ma_max_tool_calls = 10`, enforced in `tools.py:828`.
- **Termination:** `finish` validated by `_finish_hold_reason` (decline/mode/unpaid holds, each once — `loop.py:735-766`), `request_completion` outcome, or budget exhaustion.
- **Repetition risk:** an unproductive LLM cycle is caught by nudges ≤ 3 → finish declined (`loop.py:247-250`) and by the fallback brain on transport failure (`loop.py:517-526`). The `_guard_no_silent_substitution` guard (`tools.py:409-447`) forces a find_alternative→request_confirmation detour that costs extra iterations by design.

### 10.3 Observed unnecessary repetition (evidence)

`buyer_service_debug.log` shows, within single interactions:
- `discover_products {"query":"pasta dinner ingredients"}` issued 3× consecutively.
- `discover_products {"query":"milk bread eggs"}` + `create_checkout_session` repeated back-to-back across a flow.
- `milk substitute`/`soy milk` searched twice, `bread eggs` twice.

Each duplicate discover re-invokes BUY-2 (the hidden parse) and re-sends the payload into history. The buyer loop has **no same-turn/turn-overlap discovery cache** — the F3 nudge re-runs rather than reuses `conv.last_candidates`, and there is no check "did we already search the same query?".

No halluncinated-completion path found: the `_finish_hold_reason` "unpaid" hold (`loop.py:761-767,780-791`) downgrades any `completed` claim without a gateway-confirmed payment; buyer-side "completed" claims only come from the deterministic `session.status`.

---

## 11. Model / Provider Analysis

| Property | Buyer (BUY-1) | Discovery parse (BUY-2) | Merchant brain (BUY-3) |
| --- | --- | --- | --- |
| Provider | Groq | Groq | Groq |
| Model | `openai/gpt-oss-20b` | `openai/gpt-oss-120b` | `openai/gpt-oss-120b` |
| Endpoint | `https://api.groq.com/openai/v1` | same | same |
| temperature | 0.2 | 0.0 | 0.2 |
| max_tokens | 700 | 200 | 700 |
| top_p / stop / response_format | none | none | none |
| reasoning config | none passed | none passed | none passed |
| timeout (client) | 60s | 10s | 45s |
| retry | none | fallback parser | fallback brain |
| streaming | no | no | no |

Config sources: `buyer_service/config.py:8-16`, `merchant/config.py:13-51`, `.env` (Groq keys; the NIM lines are commented out/rollback).

### Findings

- A **120B reasoning model is used for keyword extraction** (BUY-2) — a classification task the deterministic `_fallback_parse` already solves (`discovery_service.py:90`) and that feeds only an input to deterministic matching. This is the clearest "model used for a task that does not require its capabilities" case, and it is *hidden inside a tool call* (`/discover`, `discovery_service.py:257`), paid on nearly every product mention.
- The buyer's 20B model is asked to be a 4-in-1 (intent classifier, basket builder, tool router, utterance writer) with no staged prompts; on Groq this executes as one long reasoning-constrained generation per hop.
- No fallback *model* for the buyer; a transport failure surfaces a canned apology (`agent.py:316-318`).
- Establish-bottleneck note: with no instrumentation (§5), whether 20B-vs-120B TTFT or generation is the dominant cost is **not yet evidence-backed**; the architecture provably performs ≥ 3-7 sequential LLM round-trips (§4), which dominates any single-model delta.

---

## 12. Model vs Architecture Diagnosis

### Likely model/provider problem

- 20B model reliability noise: copying prompt example queries (observed; the F2 gate and `_LEAKED_TOOLCALL_RE` exist for it, `agent.py:24-27`, `main.py:31-48`), re-searching identical queries, and occasional tool-call leakage. Weak *instruction adherence under tool pressure* at 20B is plausible and consistent with the log.
- No evidence (subject to measurement) for intrinsic TTFT slowness at the provider.

### Likely architecture problem  ← dominant

- 3-7 sequential, blocking, **non-streamed** LLM hops per transaction (§4); a hidden 120B call per discovery (§3 BUY-2); a second agent whose loop is itself sequential LLMs (§3 BUY-3), invoked on the slowest path first (`purchase_via_merchant_agent`, 150s window); deterministic outcomes never written into LLM context (§8).
- The user's hypothesized chain is **confirmed for the agent route**: `Buyer LLM#1 → HTTP → Merchant LLM#1 →…→ Merchant LLM#N → (pause) → [decline] Buyer LLM#2`.

### Likely prompt problem

- Binary STEP 0/STEP 1 misroute; missing correction/cancellation/cost/ambiguity rules; [AWAITING_APPROVAL] contradiction; vaccine-style guardrail duplication (F2/F3 fighting the prompt).

### Likely context problem

- Unbounded `llm_history`, repeated result payloads, stale state after deterministic steps (§8.2), no context digestion.

### Likely tool-design problem

- Non-idempotent `create_checkout_session`; agent route ignoring buyer declined-packs; fat merchant sessions on the wire; no "current order state" read tool.

### Combination

Architecture (sequential multi-LLM hops + hidden parse + no streaming) is the dominant latency cause; prompt + context/state is the dominant quality cause; the model at 20B amplifies both by being the one doing the classification/repetition. There is **no evidence** the provider itself is the primary bottleneck; it is unmeasured.

---

## 13. Agenticity Assessment

Genuine LLM-driven reasoning (real, not scripted):
- candidate selection and basket construction on the browse route (BUY-1 decides `create_checkout_session` items from discovery results);
- merchant substitution judgment (`respond_to_merchant_proposal`, `agent.py:281-294`) — although poisoned by "ACCEPT by default" in the prompt;
- final utterance generation and decline explanation (`agent.py:429-431`, plus post-decline BUY-1);
- interpreting merchant outcomes into user-facing semantics (`_map_agent_result` is deterministic, but the model decides whether to accept/reject a proposal and how to phrase).

Not agentic / hardcoded by the runtime (by design or otherwise):
- small-talk gate (F2), F3 ground-truth nudge, bill pause, price-change rebill, stock-failure substitute *options* (`_propose_substitutes`/`choose`), decline prose mapping, completion-truth holds, mode holds, silent-substitution guard, quantity fidelity guard.
- **Forced behavior that should belong to the agent:** F3 forces re-discovery when the model *could* legitimately answer from an observation already in context, and the STEP-0 prompt forces merchant-agent delegation for direct buys rather than letting the 20B model drive a simple `discover → create` path.

The buyer genuinely reasons on the browse route; on the agent route most "intelligence" is delegated to the merchant; on recovery paths all behavior is deterministic. So agenticity is **real but uneven and partially outsourced**.

---

## 14. Conversational Quality Assessment

### Distinction mechanisms

| Request type | Example | Current mechanism | Result |
| --- | --- | --- | --- |
| Informational | "What milk do you have?" | F3 → discover | ✓ answers from observation (if not misrouted) |
| Recommendation | "Which milk is best?" | discover + 20B judgment | ~ works but no explicit role |
| Transaction | "Buy Amul milk." | STEP 0 → merchant agent | ✓ bill pause |
| Modification | "Actually get the 1L pack." | none (history only) | ✗ model-lottery; possible stale re-mix |
| Cancellation | "Don't buy it." | none (409 when bill pending) | ✗ textual cancel unsupported |
| Follow-up | "How much was it?" | history (stale) | ✗ model cannot see completion (§8.2) |
| Recovery | "That one's unavailable; get another." | deterministic substitute path (button) | ✓ (card-based) |
| Ambiguous | "I need milk." | 20B guess | ✗ no clarification rule |

Conclusion: the implementation does **not** have explicit mechanisms for these distinctions. One general LLM prompt + one unbounded history + deterministic cards attempt to cover all eight types. The two most broken interactions for NLP are *modification* and *follow-up*, both caused by stale/invisible state (§8.2), and both produce the "answered something different than asked" symptom the user reported.

## 15. Buyer ↔ Merchant Interaction

### 15.1 How many times is the merchant agent called, and what does the buyer ask it to do?

- Exactly one `POST /agent_sessions` per `purchase_via_merchant_agent` tool call; each call spawns a brand-new `MerchantAgentState` (`api/agent.py:100-119`) — the merchant re-observes the catalog from scratch even when the buyer already ran `discover_products` moments earlier.
- The buyer asks the merchant agent to do things it (or the deterministic `/discover` + `create_checkout`) already does: search, stock-check, substitution. For a direct buy the buyer *needs* the merchant only for authoritative gates — those run on `POST /checkout_sessions` and `POST .../complete` regardless of any agent.
- Merchant results passed back verbatim: **no** — `_map_agent_result` trims to a subset (`agent.py:191-262`); but the *wire* payload (`_session_out`, `api/agent.py:55-82`) contains the full serialized session.

### 15.2 Duplicated reasoning

- **Yes, partially.** On the agent route: the buyer 20B classifies and phrases; the merchant 120B discovers/picks/adapts; the buyer 20B then re-narrates the outcome (post-decline BUY-1) — three reasoning layers over the same facts. The buyer never re-encodes gateway/decline cause the merchant already encoded; instead it re-reasons from the outcome message.
- Latency is therefore `Buyer LLM + HTTP + Merchant LLM(s) + optional Buyer LLM` for the agent route — exactly the fat chain the audit spec hypothesized, and confirmed at §4.

### 15.3 Safety / Money Boundary Verification (Phase 17)

Verified (traced, not assumed):

- **No direct payment from the buyer LLM.** In `llm_loop`, a `created` checkout (`agent.py:371-388`) or `bill_ready` (`agent.py:354-361`) triggers the deterministic `present_bill` and `yield BILL_PAUSE; return` — the model never sees a "complete" tool. The only completion caller is `_complete_flow` after the human decision endpoint (`agent.py:484-488`).
- **No merchant-agent payment bypass.** In `buyer_completes` mode the `request_completion` tool is filtered out of the schema (`loop.py:174-183`) and hard-guarded in `_tool_request_completion` (`tools.py:726-731`).
- **Deterministic gating / C-1 / C-2 / C-3 intact.** Any future buyer-side optimization touches only Buyer Agent prompt/loop/tools; the complete ladder re-validates price/delegation/stock/policy, builds+verifies the AP2 execution mandate, binds the amount across authorize→order→verified (`checkout_service.py:754-1143`, C-1 fix in tree), and the finalizer re-proves legal state before any irreversible effect (`_assert_finalizable_state`, `checkout_service.py:266-298`, C-3).
- **Text-level attacks** (prompt injection, malicious item names) can at worst trigger junk searches/checkouts or misleading prose; they cannot move money because money is gated by the deterministic ladder + human approval. This is the correct boundary: "Buyer Agent may reason freely, money authority remains downstream."

---

## 16. Error / Retry Analysis

| Layer | Policy | Cascade risk |
| --- | --- | --- |
| BUY-1 transport error | 1 canned message, stop (`agent.py:316-318`) | Low |
| BUY-1 tool error envelope | appended as tool result; model re-decides (`agent.py:389`) | Medium (declined create can trigger a second discover+create → 1-2 extra LLM hops) |
| BUY-2 (parse) failure | silent fallback to deterministic parser (`discovery_service.py:257`) | None |
| BUY-3 transport error | deterministic fallback brain, LLM disabled for session (`loop.py:517-526`) | None (but quality drops) |
| BUY-3 nudges (no tool call) | ≤ 3 → finish declined (`loop.py:247-250`) | Adds LLM calls |
| Merchant tool budget | `ma_max_tool_calls=10` → TOOL_BUDGET_EXHAUSTED terminate (`loop.py:1017-1026`) | Low |
| Merchant gating decline | no auto-retry (prompt + `_guard_no_silent_substitution`) | Low |
| STOCK_UNAVAILABLE at complete | deterministic substitute flow, no LLM (`agent.py:581-607,630`) | None |
| PRICE_CHANGED at complete | revised bill pause, deterministic (`agent.py:564-579`) | None |
| Payment (Razorpay) | saved-token charge → auth-required park/reauth → modal fallback on same order (`checkout_service.py:1049-1143`); reconcile for pending | Single retry built into the driver, not a loop |
| Buyer `complete_checkout` | fresh idempotency key per call (`tools.py:100`) → **not retry-idempotent client-side**; server terminal-state guard prevents double finalize, but a second complete on a still-`requires_payment` session reuses the live order (`checkout_service.py:964-974`) and could re-attempt a token charge | Flagged: needs a test asserting a buyer-triggered retry can never double-charge |

**Cascade found (small, real):** one innocent failure — `create_checkout_session` declined (e.g. gating) — appends the decline envelope, the model re-runs discovery (BUY-2 again) and re-creates, adding 2-3 more blocking LLM hops, before the deterministic paths eventually take over. This is the "innocent failure triggers extra LLM calls" pattern, at a bounded size (loop cap 12).

---

## 17. Performance Anti-Patterns (identified, not fixed)

1. **Sequential LLM hops that could be combined** — buyer intent classification and utterance generation in one BUY-1; could be a cheap classifier + generator.
2. **Hidden LLM parse on every discovery** (BUY-2) — deterministic `_fallback_parse` exists; LLM needed only when fallback yields nothing.
3. **Repeated identical discovery** within/over turns — no cache; observed 3× "pasta dinner ingredients" in the log.
4. **F3 forces re-discovery** instead of reusing `conv.last_candidates`.
5. **Full conversation replay each turn** — `conv.llm_history` never truncated/summarized.
6. **Second agent loop for simple buys** — `purchase_via_merchant_agent` delegates to a fresh merchant session that re-searches; a simple "1 litre milk" pays 4 sequential LLM hops.
7. **No streaming** — perceived latency = full generation.
8. **Fat merchant session serialization on the wire** (`_session_out`) → re-trimmed for the model.
9. **Tool descriptions re-sent per call** (5× buyer, 11+1 merchant) on every hop.
10. **Post-decline BUY-1 to narrate** an outcome the merchant already phrased — could be a template bind.
11. **Blocking sync waits everywhere** — worker-thread-per-request SSE (`main.py:92-128`) means a slow hop occupies a thread.

---

## 18. Code Quality (reasoning pipeline, candidates only — not refactored)

- `buyer_service/agent.py` — `llm_loop` (300-433) mixes prompt assembly, retry heuristics (`_LEAKED_TOOLCALL_RE`, nudge logic), tool execution, event emission, and debug-log writing; `_execute_tool` (80-187) and `_execute_agent_tools` (264-296) are thin but carry two piles of magic values (15-line cap, ₹800… no—15 line cap, quantity 1-10 schema vs runtime, 150s).
- Debug side-effect in the production path: every tool call appends to `buyer_service_debug.log` in the CWD (`agent.py:343-347`) with no rotation/config guard.
- `data_loss` of structured state: `Conversation` mixes dialog, UI-phase, and money-adjacent fields; no eviction in `CONVERSATIONS`/`SESSIONS` dicts (memory growth).
- `merchant/agent/loop.py` — ~1000 lines; `MerchantAgent` does orchestration + holds + guidance + terms + audit + truth-guarding; the `min(4, max(1, budget_windows))` budget math (`loop.py:544`) and `_finish_hold_reason` (`735-766`) are dense but unit-tested (`test_c2_c3_state_security.py`, `test_agent_handoff.py`).
- Dead/duplicated surface: `MerchantAgent.handle = MerchantAgent.run` alias (`loop.py:1064`); buyer `_execute_tool` unknown-tool fallback (187) is unreachable since schemas are fixed.
- Naming: `conv.last_candidates`, `offered_pack_ids`, `declined_packs` are clear; `BILL_PAUSE` sentinel threading through the SSE generator (`main.py:122-126`) is implicit control flow.

---

## 19. Observability Gaps

None of the following are currently measurable:

| Metric | Measured today? | Needed |
| --- | --- | --- |
| LLM request latency | no | wrap all 3 call sites |
| LLM TTFT | no | requires `stream=True` + first-chunk timing (or provider metadata) |
| output generation time | no | same |
| token counts (in/out) | no | capture `resp.usage.*` (currently discarded) |
| tool latency | no | wrap `httpx` in `buyer_service/tools.py` |
| Merchant Agent latency per iteration | no | `loop.py` per-iteration timing |
| number of tool calls / LLM calls per turn | no | counters in `llm_loop` / `run` |
| retries & failures | partial (audit actions, not metrics) | counters at each guard |
| context size | no | len/tokenize of the `messages` payload at each call |
| provider/model errors | no | exception classification at 3 call sites |

Minimal instrumentation for later: a `perf_counter` wrapper around each `chat.completions.create` and each `httpx` call; log `len(messages)` + `resp.usage`; surface per-event timing into the SSE stream so the dashboard can show it.

---

## 20. Deterministic vs LLM Responsibility Matrix (Table D)

| Responsibility | Current owner | Should be | LLM needed? |
| --- | --- | --- | --- |
| Intent classification (info vs buy vs cancel) | 20B model (STEP 0/1) | deterministic/cheap classifier + explicit state | LLM helpful, not required |
| Discovery NL→keyword parse | 120B model (BUY-2) | deterministic parser (exists) + LLM-on-fallback | Not required (fallback only) |
| Catalog matching/ranking/pricing | deterministic `discovery_service` | deterministic | No |
| Stock / gating / policy | deterministic `gating_service`/inventory | deterministic | No |
| Budget/quantity parsing | deterministic regex | deterministic | No |
| Basket construction from candidates | 20B model (browse) / merchant LLM (agent route) | 20B for browse; keep | Yes (browse) |
| Substitution-option search | deterministic `_propose_substitutes` / `find_alternative` | deterministic | No |
| Substitution accept/reject judgment | 20B model (biased to accept) | LLM, but un-biased instruction | Yes |
| Merchant-agent orchestration on simple buys | 20B → merchant LLM loop | bypass for simple direct buys (deterministic create) | No (for simple) |
| Checkout state / payment / receipts | deterministic services | deterministic | No (already) |
| Follow-up / correction / cancel intent | none (history-only) | structured state ops + LLM phrasing | LLM helpful |
| Natural-language generation | 20B model | LLM | Yes |
| Ambiguity clarification | none | LLM (add explicit rule) | Yes |

## 21. Optimization Recommendations (Table E)

**P0 — safety / correctness**

| # | Finding | Change | Benefit | Risk | Files | Test |
| --- | --- | --- | --- | --- | --- | --- |
| E1 | Deterministic outcomes invisible to the LLM (§8.2) — model answers follow-ups wrongly | After every approval/cancel/choose/complete, append a compact `tool`/`user` event into `conv.llm_history` (e.g. `{"event":"order_completed","total":…,"csid":…}`), or reset history to a fresh phase header | Follow-ups answered correctly; kills the biggest quality defect | Model may contradict an elaborated receipt; keep digest terse | `buyer_service/agent.py`, `conversations.py` | new `test_context_sync`: approve then ask "how much was it" assert correct total |
| E2 | `create_checkout_session` not idempotent client-side; repeated creates + leftover live sessions | Send a stable `Idempotency-Key` (hash of basket) on create; auto-cancel prior `active_session_id` when creating a new one | No duplicate sessions/reservations; bounded fallback loops | Cancel must be owned by conversation (ownership check ok) | `buyer_service/tools.py`, `agent.py` | `test_create_idempotent` / `test_no_stale_session` |
| E3 | Buyer `complete_checkout` uses a fresh idempotency key each call | Reuse the approval's key (one key per approved bill) | Zero double-charge exposure on user/web retries | Key lifecycle must be per-bill | `buyer_service/tools.py`, `agent.py` | `test_complete_retry_safe` (replay same key, assert single payment) |

**P1 — latency**

| # | Finding | Change | Benefit | Risk | Files | Test |
| --- | --- | --- | --- | --- | --- | --- |
| E4 | Hidden 120B parse on every /discover (BUY-2) | Make `/discover` deterministic-first: run `_fallback_parse`; call `_llm_parse` only when it yields no keywords (or drop LLM parse entirely) | Removes a full LLM hop + network round-trip from most discovery turns | Slight NL-compromise for novel phrasing; synonym map covers catalog | `merchant/domain/discovery_service.py`, `api/discovery.py` | `test_discovery_no_llm_when_sufficient` (mock LLM, assert 0 calls) |
| E5 | Buyer LLM unstreamed (`main.py:92-128` buffers full turn) | Enable `stream=True` in `llm_loop`, emit a `thinking`/token event first | Perceived TTFT shrinks to the model's first token | More plumbing in SSE; must still not emit BillPause early | `buyer_service/agent.py`, `main.py` | SSE event-order test |
| E6 | Simple direct buys pay a full merchant-agent loop (§15) | For single, unambiguous direct buys: resolve pack ids via deterministic `/discover` then `create_checkout` directly; reserve the agent-agent route for compound/ambiguous/deep-substitution cases | 4 sequential LLM hops → 2 on the most common path | Could weaken substitution UX for truly needed cases; keep merchant-gate intact | `buyer_service/agent.py`, `prompts.py` | `test_simple_buy_path`, scenario C/D E2E |
| E7 | Repeated/identical discovery; F3 re-discovery | Cache per-conversation discovery (query→`last_candidates`); reuse within the turn and across turns unless stale; re-run only when context changes | Kills duplicate BUY-2 calls | Caching must respect `declined_packs` | `buyer_service/agent.py`, `conversations.py` | `test_discovery_cache_rules` |

**P2 — conversational quality**

| # | Finding | Change | Benefit | Risk | Files | Test |
| --- | --- | --- | --- | --- | --- | --- |
| E8 | No correction/cancel/cost/ambiguity rules in the prompt (§6 MISSING) | Add explicit intents: textual cancel, "forget that", informational price ("how much") answered from candidate data **without** creating a checkout; "I need milk" → ask size/brand then proceed | Directly fixes "answered something else" behaviors | More prompt length; keep ≤1 call per intent | `buyer_service/prompts.py` | prompt/behavior test harness per scenario |
| E9 | STEP 0 vs STEP 1 misroute; example-query copying | Split routing: a cheap deterministic/rule intent pass (buy-words vs question-words) decides route before the LLM build; drop example queries to avoid copying | Fewer wrong-route slow paths; stops example copying | Rule edge cases; keep escape hatch | `buyer_service/agent.py`, `prompts.py` | `test_route_decision` matrix |
| E10 | History unbounded + stale | Bound `conv.llm_history` (last N turns) + inject a deterministic **current-state digest** (active order, pending bills, declined packs) each turn | Smaller context, ground truth always visible | Digest must stay fresh (E1 keeps it so) | `buyer_service/agent.py`, `conversations.py` | context-size + correctness tests |

**P3 — architecture / code quality**

| # | Finding | Change | Benefit | Risk | Files | Test |
| --- | --- | --- | --- | --- | --- | --- |
| E11 | `llm_loop` does everything (§18) | Split: `decide_intent` → `act` (tool executor) → `finalize` (utterance); move debug-logging out of the hot path | Readability; enables E4/E5/E6 cleanly | Refactor risk on money-adjacent code | `buyer_service/agent.py` | existing pytest suite stays green |
| E12 | No metrics (§19) | Add timing/token/instrumentation wrappers per §19 | Baseline; every estimate in this report becomes measured | Tiny overhead | `buyer_service/agent.py`, `merchant/agent/loop.py` | benchmark script |

**P4 — model / provider experiments** (only after E-1..E-12 land and latency is measured)

| # | Finding | Change | Benefit | Risk | Files | Test |
| --- | --- | --- | --- | --- | --- | --- |
| E13 | BUY-2 uses 120B for keyword extraction (redundant class shown in §11) | Run the §22 benchmark arm "current model + minimal context" vs alternatives (e.g. `gpt-oss-20b` for parse) | Confirms/denies whether the model is a contributor at all | Provider churn | `.env`, `merchant/domain/discovery_service.py` | §22 harness |
| E14 | Buyer 20B reliability ceiling (`_LEAKED_TOOLCALL_RE` etc.) | After streaming + context fixes, A/B a stronger/alternate buyer model under the same harness | Quantified buying decision | none (benchmark only) | config | §22 harness |

---

## 22. Model Benchmark Plan

Goal: decide **fairly** whether the current model/provider is the bottleneck, after the architecture is fixed. Three arms, identical scenarios from §4 (A-J), fixed seeded catalog, N ≥ 5 runs each, same conversation state model:

1. **Arm 1 — current model + current prompt/context** (`gpt-oss-20b`, full system prompt, unbounded history) → baseline.
2. **Arm 2 — current model + minimal prompt/context** (streamlined prompt, 2-turn history, structured state digest) → isolates prompt/context contribution.
3. **Arm 3 — alternate model + minimal prompt/context** (e.g. a faster non-reasoning Groq model, or `gpt-oss-120b` buyer) → isolates model contribution. **Not run unless explicitly instructed** (Phase 25 constraint).

Metrics per run: TTFT (with `stream=True`), total wall time, number of LLM calls, number of tool calls, tool-call correctness (pack_ids valid, quantities in range), instruction-following (right route: info vs buy vs cancel), conversational quality (correction/cancel/cost handled), recovery quality (stock-failure substitute choice). Report per arm: mean/median/p95 for wall + TTFT; pass-rate tables for correctness; verbatim failure samples. Repeat after E4/E6 to measure removal of the hidden hop and the agent route.

---

## 23. Critical Findings

1. **Architecture dominates latency.** Every transaction with a product purchase runs ≥ 3 sequential, blocking, non-streamed LLM round-trips; the agent route runs up to 7+, including the merchant's own loop, under a 150s HTTP window.
2. **A hidden 120B LLM call sits inside discovery** — the buyer's `discover_products` triggers `_llm_parse` (`gpt-oss-120b`, temp 0.0, 10s cap) on nearly every mention of a product, and the deterministic parser that would make it unnecessary already exists.
3. **Stale LLM context is the single biggest conversational-quality defect** — approvals, cancellations, substitutions, and payments are invisible to `conv.llm_history`; the model literally cannot answer "how much was it?" correctly after a completion.
4. **No streaming = worst possible perceived latency**; even a fast model feels slow because nothing is shown until the turn completes.
5. **Unbounded `conv.llm_history`** replays all prior discovery/checkout JSON each turn.
6. **Binary STEP 0/STEP 1 prompt** with example-query copying and no correction/cancel/cost/ambiguity rules is the prompt-level cause of "answered something different than asked".
7. **Agenticity is uneven — half-outourced**: browse AI is genuine; agent-route reasoning is delegated; recovery is deterministic; plus F3 force and an "ACCEPT by default" bias override the model in both directions.
8. **Money boundary is safe** (§15.3) — no configuration of Buyer Agent can reach money without the deterministic ladder + human approval; buyer-side retry idempotency (E3) is the only money-adjacent hardening worth adding.

---

## 24. Recommended Implementation Order

1. **P0**: E1 (context sync) → E2 (create idempotency) → E3 (complete retry-key). Each with a test. (Correctness before speed.)
2. **P1**: E4 (remove hidden parse) → E7 (discovery cache) → E6 (simple-buy path) → E5 (streaming). E4/E7 are cheap and immediately reduce LLM hops.
3. **P2**: E9 (route split) → E8 (intent rules) → E10 (context bound + state digest). Re-run the §14 qualitative matrix after each.
4. **P3**: E11 (loop refactor), E12 (observability) — then **re-run §5 with real numbers**.
5. **P4**: E13/E14 model A/B through §22, only against the measured post-architecture baseline.

---

## 25. Final Answers to the 11 Diagnosis Questions

1. **Slow primarily because of the model?** No. No evidence supports it; latency is unmeasured and the architecture provably executes 3-7 sequential blocking LLM hops plus a hidden parse. Model slowness can only be diagnosed after E-perf fixes.
2. **Slow primarily because of architecture?** Yes. Sequential non-streamed multi-LLM hops, hidden 120B discovery parse, a second agent loop on the default path, synchronous blocking waits amplified by a worker-thread SSE bridge.
3. **Conversational quality primarily a prompt/context problem?** Yes. Missing intent rules (E8), binary-route prompt (E9), stale + unbounded context (E1/E10) — not intrinsic model language quality.
4. **Is the reasoning loop making unnecessary LLM calls?** Yes: the hidden parse (BUY-2), repeated/identical discovery (F3 + no cache), the post-decline BUY-1 narration, and the merchant sub-loop for simple buys.
5. **Buyer and Merchant duplicating reasoning?** Partially: both may discover/select the same items; the buyer re-narrates what the merchant phrased; no declined-pack state is shared across the boundary.
6. **Minimal required LLM calls** (post E-*) —
   - simple conversation: 0-1 (F2 covers 0)
   - product discovery: 1 (0 for the parse — deterministic)
   - simple purchase: 1 (buyer); 0 if items pre-resolved deterministically
   - compound purchase: 1-2
   - failure recovery: 0 (deterministic)
7. **Which calls removable:** the discovery parse (→ deterministic, keep LLM only on fallback); repeated discoveries (cache); post-decline narration (template); merchant loop for simple direct buys (choose the direct path).
8. **Keep for genuine agency:** buyer candidate-selection + basket building on browse; merchant-proposal judgment (de-biased); final utterance generation; ambiguity clarification.
9. **Single biggest latency source:** the `purchase_via_merchant_agent` chain — buyer LLM + synchronous merchant LLM loop + optional buyer LLM — multiplied by a hidden 120B parse whenever discovery runs. Measured claim pending (§5.3).
10. **Single biggest conversational-quality problem:** the model's context diverges from reality — deterministic outcomes are never written back, and history is unbounded/stale, so the agent answers follow-ups and corrections about the wrong, earlier state.
11. **Smallest change → biggest improvement:** make `/discover` deterministic-first (E4) — one flag change in discovery/gating deletes a full hidden 120B LLM round-trip from the hottest path — and, together with E1 (write outcomes back into `llm_history`), it fixes both the dominant latency hop and the dominant quality defect.

---

## Appendix

### Table A — LLM Calls (compact)

| ID | File | Function | Model | Purpose | Context | Tools | Sequential? | Est/Measured Latency |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BUY-1 | `buyer_service/agent.py` | `llm_loop`/`chat.completions` | gpt-oss-20b (Groq) | intent+discovery+basket+tool-select+response (multi) | sys prompt + unbounded history | 5 | yes | NOT MEASURED |
| BUY-2 | `merchant/domain/discovery_service.py` | `_llm_parse` | gpt-oss-120b (Groq) | NL→keyword parse (hidden) | tiny one-shot | none | per /discover | NOT MEASURED |
| BUY-3 | `merchant/agent/loop.py` | `_llm_decide` | gpt-oss-120b (Groq) | merchant action selection / finish | sys+state+history | 11+finish | per iteration | NOT MEASURED |

### Table C — Root Cause

| Problem | Model | Provider | Prompt | Context | Architecture | Tool Design | Confidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Slow (perceived) | partial (unverified) | — | — | — | **primary** | — | high |
| Extra hidden LLM hop | — | — | — | — | **primary** (BUY-2) | — | high |
| Wrong-topic answers | — | — | high | high (stale) | partial | — | high |
| Slow agent route | — | — | partial (STEP0) | — | **primary** (sub-loop) | partial (no shared declined state) | high |
| Duplicate search/create | partial | — | partial (example copying) | — | **primary** (no cache, non-idempotent) | **primary** (idempotency gap) | high |

---

_End of audit. Repository unmodified by this audit._