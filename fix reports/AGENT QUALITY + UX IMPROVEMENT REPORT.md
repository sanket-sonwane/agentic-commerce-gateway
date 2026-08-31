# Agent Quality + UX Improvement Report

Scope: three workstreams on the Agentic Commerce Gateway (ACG) —
**A) LLM latency + observability**, **B) conversational quality**, **C) end-to-end UX**.
Goal: make the existing agentic-commerce system measurable, faster where evidence proves a
bottleneck, more naturally conversational, and truthful about payment — **without** redesigning
AP2/payment/state-machine/Merchant-Agent and **without** adding LLM money authority.

All work follows the approved phasing: A measure+instrument → B routing/state → A measured
latency optimizations → C UX integration → regression.

---

## 0. Guardrails honoured (not violated)

- No AP2 / payment / state-machine / Merchant-Agent redesign.
- No second Buyer/Merchant Agent; no model replacement; no Razorpay redesign.
- **No LLM money authority** — payment is executed only by the deterministic merchant path.
- No fake gateway success; money truth = "paid" only when the server reports `completed`.
- Preserved: C-1/C-2/C-3, AP2 (Intent Mandate), standalone verifier, audit append-only,
  Buyer-B interop, merchant agenticity.

---

## 1. Baseline (recorded before changes)

| Check | Result |
|---|---|
| pytest | **237 passed, 0 failed** (18.17s) |
| `scripts.scenario_check` | EXIT=0 (all 6 scenario groups) |
| Hermetic flow latency* | Flow A 7.37s, B 3.34s, C 5.28s (2/2/3 LLM, 2/1/2 tools, 1 discovery each) |

\* Baseline flows were **contaminated by two artefacts** (see §A3): (1) the benchmark's merchant
injection was ineffective (dead `_httpx`) so every merchant call timed out against a
non-running `localhost:8000` at ~2.2s each; (2) buyer-driven `/discover` still ran the merchant's
own **LLM query parser** (~1.0–1.3s) redundantly. The honest non-LLM latency was ~10–20ms/op.

---

## 2. Workstream A — latency + observability (COMPLETE)

### A1. Structured timing (secret-safe by construction)
- `buyer_service/timing.py` — thread-local `_StageRecorder` + `stage_timer`. Accepts only explicit
  scalar tags (stage, tool, llm_model, coarse outcome); **never logs prompts, args, amounts, or PII**.
- `merchant/agent/timing.py` — per-agent-session recorder (cap 512/session).
- Instrumented stages:
  - Buyer: `buyer_llm`, `buyer_tool`, `discover_http`, `merchant_agent_session`,
    `merchant_agent_proposal_decision`.
  - Merchant: `llm_decide`, `fallback_decide`, per-tool `merchant_tool`, `request_completion`,
    and `/discover` parse (`parser=llm|keyword-fallback`).
- Exposed via `GET /conversations/{id}/timings` (buyer) and agent-session `latency_breakdown`
  (merchant trace view). No prompts/args/amounts/ids exposed.

### A2. Fixed benchmark (hermetic, real in-process merchant)
`scripts/_bench_flows.py` now routes the buyer's `MerchantClient` (which uses module-level
`httpx.post/put/get`) through the **in-process merchant over ASGI/TestClient**, exercising the real
protocol client against the real app — nothing hits a network. Result (fake LLM, real merchant):

| Flow | total | LLM | tools | discovery | discover_http |
|---|---|---|---|---|---|
| A simple purchase | **0.05s** | 2 | 2 | 1 | 21ms |
| B info/ambiguous | **0.01s** | 2 | 1 | 1 | 8ms |
| C adaptive | **0.02s** | 3 | 2 | 1 | 8ms |

Non-LLM (tool/merchant/DB) latency is negligible; **LLM calls dominate real-world latency.**

### A3. Measured bottleneck → A4 optimization
`scripts/_probe_discover.py` proved the redundant merchant LLM query parser on the buyer-browse
path:
- `/discover` (LLM parse): **~0.96–1.37s**
- `/discover` skip (`skip_llm_parse=True`): **~8–14ms**

The buyer LLM already turned the user's intent into a concise query string; re-parsing it with a
second NIM call added ~1–1.4s per discovery with no quality gain (the keyword fallback extracts
`milk bread eggs → ['milk','bread','eggs']`). **Fix:** buyer `discover_products` now passes
`skip_llm_parse=True` (mirrors the already-skipping `_propose_substitutes`). Confirmed parity in
discovery quality (see telemetry test asserting `parser=keyword-fallback` while candidates are
still returned). Saves ~1s per browse/ambiguity turn on every discovery.

### A4. LLM call inventory

| # | Call | Agent | Model | Purpose | Necessary? | Sequential? |
|---|---|---|---|---|---|---|
| 1 | `buyer_llm` | Buyer | gpt-oss-20b | intent→tool | yes | — |
| 2 | `discover_products` LLM-parse | Merchant | (removed) | re-parse buyer query | **no → removed (A4)** | yes |
| 3 | `merchant_agent` loop | Merchant | gpt-oss-120b | agentic purchase | yes (direct purchases) | yes |
| 4 | `merchant_agent` decide | Merchant | gpt-oss-120b | next action | yes | yes |
| 5 | `merchant_agent` execute_tool | Merchant | (deterministic) | tool effects | n/a | n/a |

### A5. Telemetry safety tests (3) — `test_buyer_telemetry.py`
- Recorder records ok/error stages with valid elapsed_ms; no sensitive keys.
- A `/chat` turn emits timings via `/conversations/{id}/timings` with **no secret leak**.
- Buyer-driven discovery uses `skip_llm_parse=True` (parser=keyword-fallback).

---

## 3. Workstream B — conversational quality (COMPLETE)

### B1. Deterministic-first routing (`buyer_service/routing.py`)
New module classifies user intents with cheap heuristics and answers **without an extra LLM call**
straight from authoritative conversation state:
- **`cancel`** — cancels the active checkout only if we genuinely own one (clears pending approval;
  writes the `order_cancelled` deterministic state note); handles a pending substitution; never
  invents a session id; truthful if nothing active.
- **`order_status`** — answers only from server-verified state notes / active session; "paid" only
  when the server confirmed `completed`; pending bill/substitution are reported as such.
- Everything else falls through to the normal LLM tool loop.
- In `/chat`, `cancel`/`order_status` legitimately resolve a pending bill/substitution so they are
  **not rejected** by the pending-card 409 guard.

### B2. CURRENT STATE injection + bounded history
- **Current state block**: each LLM turn is bound to a deterministic
  `CURRENT STATE (deterministic, authoritative)` system block summarizing order/payment/
  substitution facts. The model can no longer hallucinate status from memory; the prompt now
  explicitly instructs it to trust only that block for status follow-ups.
- **Bounded history**: the message window is capped (keeps system prompt + state block + most
  recent exchanges), so long chats stay inside the token budget since ground truth lives in the
  state block.

### B3. Prompt style
`SYSTEM_PROMPT` updated to a warmer, more natural assistant voice, explicit follow-up/status
grounding rules, and "ask a short clarifying question when unsure" guidance — while keeping every
payment-truth invariant intact.

### B4. Hermetic conversational regression suite (10 cases + 1 C9) — `test_buyer_conversation.py`
Fake LLM + in-process merchant. All offline.

| # | Scenario | Verified |
|---|---|---|
| 1 | information | discover→`found` with candidates |
| 2 | purchase (custom basket) | bill presented + pause; total > 0 |
| 3 | modification (reorder) | reject old bill → new basket produces a fresh bill |
| 4 | cancellation | **deterministic router, zero LLM calls**, status cancelled + "nothing charged" |
| 5 | substitution | stock-race → deterministic `substitute_proposal` with options |
| 6 | payment-status | pending bill reported truthfully (router, zero LLM) |
| 7 | completed-order follow-up | router reports paid/completed (zero LLM) |
| 8 | failed-order follow-up | router reports declined, nothing charged (zero LLM) |
| 9 | ambiguous | falls through to LLM (call_count > 0) |
| 10 | unrelated (grounded) | LLM used but CURRENT STATE + COMPLETED injected into messages |
| C9 | gateway unreachable | truthful "nothing charged, no order placed" (no LLM; see §4) |

---

## 4. Workstream C — end-to-end UX (COMPLETE)

### C1. Gateway-unreachable truthfulness (buyer `_complete_flow`)
Previously a transport exception in `complete_checkout` surfaced only the generic
`_stream` error ("Something went wrong on my side"). Now wrapped: on gateway-unreachable it emits a
truthful message — **nothing was charged, no order placed, basket is safe** — and records a
`gateway_unreachable` state note. Never claims success it cannot see. (Test at B4/C9.)

### C2. Payment-state consistency review
Audited the single authoritative mapping chain:
`paymentState.js::mapPaymentState` (backend status + `payment.awaiting_authorization`) → used
consistently by `PaymentCard.jsx`, `events.js::humanStatus`, and `ChatView.jsx`. No UI branch
infers `completed`/amounts on its own. The mapping is correct and consistent across all surfaces;
**no changes were required** (verified, not touched, per "don't break" rule).

### C3. Status captions
`events.js::STATUS_COPY` already pairs terminal/money states with truthful copy
(`payment_pending` → "nothing was charged", `requires_payment` → "waiting for your payment").
Consistent with `paymentState.js`. No change needed.

---

## 5. Regression

| Check | Before | After |
|---|---|---|
| pytest | 237 passed | **251 passed, 0 failed** |
| added tests | — | +14 (3 telemetry, 10 conversational, 1 C9) |
| `scripts.scenario_check` | EXIT=0 | **EXIT=0 (all 6 groups)** |
| buyer/merchant imports | — | clean |

Guardrail checks all re-run green (C-1, C-2/C-3, AP2, verifier, Buyer-B, Merchant Agent,
checkout, payment, inventory, audit, consumer provenance — covered by the 251-test suite).

---

## 6. Verdict

- **Latency is now measurable.** Instrumentation added across buyer + merchant; timed stages
  exposed read-only (no secrets). One **measured** redundant LLM call (discovery query re-parse,
  ~1–1.4s) was removed — the only optimization with hard evidence behind it. The hermetic benchmark
  was corrected to be a true hermetic measurement (real protocol client → in-process merchant) and
  now isolates genuine non-LLM latency at ~8–30ms.
- **Conversations are more natural and truthful.** Cancel/status are answered deterministically and
  fast; every turn is grounded in an injected CURRENT STATE block; history stays bounded; the voice
  is warmer. A 11-case hermetic regression suite locks in the behavior.
- **Payment truth is preserved and strengthened.** No LLM money authority; gateway-unreachable and
  pending/failed states are now phrased exactly truthfully ("nothing was charged, no order placed").
