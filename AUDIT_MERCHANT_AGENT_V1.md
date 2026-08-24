# Master PRD Compliance Audit — Agentic Commerce Gateway v2

**Target:** `D:\Hackathons\rz-agent` (post "Merchant Agent v1")
**Audit date:** 2026-08-24
**Method:** forensic static trace of production execution paths only. Code existence was never accepted as proof of behavior.
**Auditor note on scope of proof:** the test suite could **not be executed** in this environment. `.venv/` is a Windows virtualenv (`.venv/Scripts/python.exe`) and dependency installation failed with `ProxyError('Cannot connect to proxy.', OSError('Tunnel connection failed: 403 Forbidden'))` → `Could not find a version that satisfies the requirement fastapi==0.115.6 (from versions: none)`. **Every claim that depends on running code is therefore marked NOT VERIFIABLE, not PASS.** No request or response body in this document is invented; all quoted values are read from source.

This report supersedes nothing. It sits alongside `AUDIT.md` (prior audit) and `AUDIT_REPORT.md` (team self-audit). Where it contradicts my own earlier findings, the correction is stated explicitly in §30.

---

## 3. Architecture Discovery

### 3.1 Component table

| Component | Actual implementation | Process | Responsibility | LLM? | Deterministic? |
|---|---|---|---|---|---|
| Human chat UI | `dashboard/src/components/ChatView.jsx`, served by Vite dev server | Vite (5173) | Renders SSE events, bill approval button, Razorpay modal trigger | No | Yes |
| Buyer Agent brain | `buyer_service/agent.py` (730 lines), `prompts.py` (5 tool schemas) | `buyer_service` (8001) | Interprets human intent, selects tools, presents bill | **Yes** — NIM `meta/llama-3.3-70b-instruct` | No |
| Buyer Agent protocol client | `buyer_service/tools.py::MerchantClient` (148 lines, pure `httpx`) | `buyer_service` | Sole channel to the merchant; 11 HTTP methods | No | Yes |
| Buyer Agent B | `buyer_b/client.py` (133 lines) | CLI, on demand | Independent deterministic reference client; proves protocol-not-hardwiring | No | Yes |
| Commerce protocol | `protocol/protocol_spec.md` (119 lines) + 5 JSON Schemas in `protocol/schemas/` | Document | Published contract: 13 endpoints, 16 error codes, lifecycle diagram | No | n/a |
| Merchant Agent loop | `merchant_agent/agent/loop.py` (780 lines) | `merchant_agent` (8000) | Observe→Reason→Act→Observe-Result→Adapt; two interchangeable brains | **Yes** (`_llm_decide`, l.140-193) with deterministic `_fallback_decide` | Partially |
| Merchant Agent tools | `merchant_agent/agent/tools.py` (767 lines), 12 handlers in `_HANDLERS` (l.679-692) | `merchant_agent` | Allowlisted action surface; validates every id and quantity | No | Yes |
| Merchant Agent state | `merchant_agent/agent/state.py` (103 lines), in-process `SESSIONS` dict | `merchant_agent` | Transaction-scoped state; delegation token held server-side only (l.46) | No | Yes |
| Discovery service | `merchant_agent/services/discovery_service.py` (320 lines) | `merchant_agent` | NL→structured query (LLM), then deterministic match/rank/price | **Yes, parse only** (`_llm_parse`) | Matching/pricing yes |
| Gating engine | `merchant_agent/services/gating_service.py` — pure functions, 5 rules | `merchant_agent` | Sole policy authority: expiry, category, quantity, spend, stock | **No** | Yes |
| Delegation registry | `merchant_agent/services/delegation_service.py` (37 lines) | `merchant_agent` | Token→scope resolution; status + expiry enforcement | No | Yes |
| Checkout control plane | `merchant_agent/services/checkout_service.py` (1059 lines) | `merchant_agent` | State machine, price authority, idempotency, completion ladder | **No** | Yes |
| Inventory service | `merchant_agent/services/inventory_service.py` (100 lines) | `merchant_agent` | `available = actual − active reservations`; TTL sweep | No | Yes |
| Payment service | `merchant_agent/services/payment_service.py` (377 lines) | `merchant_agent` | Razorpay orders, token charge, HMAC verify, reconcile, capture | **No** | Yes |
| Audit trail | `merchant_agent/services/audit_service.py` (94 lines) | `merchant_agent` | Append-only SHA-256 hash chain + `verify_chain` | No | Yes |
| Operator console | `dashboard/src/components/OpsView.jsx`, `MerchantAgentTrace.jsx`, `IntegrityPanel.jsx` | Vite | Stock editing, gating display, agent trace, chain verify | No | Yes |
| Serialization guard | `merchant_agent/db.py::mutate()` — process-wide `threading.RLock` + SQLite WAL | `merchant_agent` | Serializes all money-critical writes | No | Yes |

### 3.2 The five required questions

**Q1 — Where does the LLM actually make decisions, and which of those decisions have financial consequences?**

Three places. (a) `buyer_service/agent.py::llm_loop` chooses among 5 buyer tools. (b) `merchant_agent/agent/loop.py::_llm_decide` (l.140-193) chooses among 12 merchant tools + `finish`. (c) `discovery_service._llm_parse` converts free text to a structured query and nothing else.

Only (b) touches money, and only through one tool: `request_completion`, whose JSON Schema is `{"type": "object", "properties": {}, "required": []}` (`agent/prompts.py:263`). The model cannot pass an amount, a price, a session id, a delegation token, or a payment identifier. Its entire financial authority is the single bit "attempt the gated completion now." Everything downstream is `checkout_service.complete_checkout`, which re-derives all values from the database.

**Q2 — Which state changes can only happen through deterministic code?**

Every one. All 18 `cs.status = …` assignments live in `checkout_service.py` (lines 293, 314, 343, 439, 489, 557, 577, 636, 672, 802, 974, 997, 1008) plus `api/checkout.py:203` (a `PaymentOutcome` construction, not a session write). All reservation status changes live in `inventory_service.py` (l.30, 61, 78, 92). All stock writes live in `checkout_service.py:972` and `api/admin.py:58`. Grep across `merchant_agent/agent/` returns exactly four `state.status =` writes (`loop.py:504, 651, 684, 721, 750`) — all on the *agent session* object, never on the checkout session, never on stock, never on price.

**Q3 — Where is the boundary between "agent reasoning" and "merchant authority," and is it enforced by structure or by prompt text?**

By structure. The boundary is `tools.execute_tool` (`agent/tools.py:695`). It (i) rejects any name not in `ALL_TOOLS` with `UNKNOWN_TOOL`, (ii) binds handler kwargs by `inspect.signature` intersection (l.720-723) so unknown LLM arguments are silently discarded rather than forwarded, (iii) opens the DB session itself, and (iv) normalizes every result into `{"success", "tool", "result"|"error"}`. The delegation token is never in the LLM transcript: `state.public_view()` (`state.py:88-103`) omits it, and `_tool_request_completion` reads `state.delegation_token` directly from server memory (`tools.py:616`). Prompt text (`agent/prompts.py:8-37`) *also* forbids these things, but the structural guard is what holds.

**Q4 — Is there any path where an LLM output is trusted without validation?**

Yes, three, none of them financial:

1. `agent/prompts.py` `finish` tool — `outcome`, `reason_code`, `message` are LLM strings written into `state.outcome` and into audit `reasoning_summary`. `outcome` is clamped to an allowlist at `loop.py:746`; `reason_code` and `message` are free text. Consequence: the human-facing *explanation* can be wrong even when the *state* is right. This is an explainability risk, not a money risk.
2. `discovery_service._llm_parse` output feeds ranking. Bounded: it produces only query terms, budget and quantity; prices and stock come from the DB.
3. `buyer_service/agent.py::_map_agent_result` narrates merchant-agent payloads into chat text. The amount shown on the bill comes from the merchant response, not the LLM.

**Q5 — What happens if the LLM is removed entirely — does the transaction still work?**

Yes, and this is verified by construction rather than by assertion. `merchant_agent/config.py:49` `ma_llm_brain: bool = True` can be set false; `loop.py:420-430` falls through to `_fallback_decide`, a deterministic decision tree. `discovery_service._llm_parse` returns `None` when `not settings.nim_api_key` and falls back to `_fallback_parse` (STOPWORDS/SYNONYMS/regex). `buyer_b/client.py` completes an entire purchase with no LLM at all. The complete/gating/payment path contains no LLM call whatsoever.

**Status: PASS** — architecture is as described, and the LLM/authority split is structural.

---

## 4. Merchant Agent Verification (MA-01 … MA-09)

| ID | Requirement | Status | Runtime evidence |
|---|---|---|---|
| MA-01 | Merchant Agent exists as a real agent, not a wrapper | **PASS** | `POST /agent_sessions` (`api/agent.py:91`) constructs `MerchantAgentState`, registers it, acquires a per-session busy lock, and calls `MerchantAgent().run(state)` (l.111). `run()` is a `while state.status == "running"` loop (`loop.py:442`) that re-decides each iteration. |
| MA-02 | Observe → Reason → Act → Observe-Result → Adapt | **PASS** | One full cycle per iteration: reason `_next_action` (l.453) → act `tools.execute_tool` (l.471) → observe-result: for the LLM brain the envelope is appended as a `role:"tool"` message (l.475-481) so the model *must* see it before the next decision → adapt `_post_action` (l.492). Adaptation is real: on `STOCK_UNAVAILABLE` the failed pack is added to `state.excluded_pack_ids` and the loop **continues** (`loop.py:694-712`, `return False`). |
| MA-03 | Agent reasons over merchant state, not buyer state | **PASS** | All 12 handlers take `db: Session` and read `Product`/`Pack`/`Reservation`/`CheckoutSession`. `_delegation_constraints` (`tools.py:119`) reads the delegation row. No handler accepts buyer-supplied prices or stock. |
| MA-04 | Agent has a bounded tool allowlist | **PASS** | `READ_TOOLS` (6) ∪ `WRITE_TOOLS` (4) ∪ `FINANCIAL_TOOLS` (2) = `ALL_TOOLS` (`tools.py:34-42`). `execute_tool` rejects anything else (l.697-702). `finish` is intercepted in the loop before dispatch (`loop.py:461`), so it never needs to be in the allowlist. |
| MA-05 | Agent can propose alternatives, not silently substitute | **PASS** | `_tool_find_alternative` (l.232) only *returns* candidates — it performs no write. Basket change requires either `request_confirmation` (which sets `state.pending_proposal` and pauses: `_post_action` returns `True` at `loop.py:659`) or, after buyer acceptance, `_accept_substitution` → `update_checkout`. There is no code path from `find_alternative` to `update_checkout` without a buyer decision. |
| MA-06 | Agent honors deterministic policy as final | **PASS** | `_tool_run_gating` and `_tool_create_checkout` surface the gating verdict as a failure envelope; the agent cannot re-run gating with different inputs because gating reads the session from the DB. `_tool_create_checkout` maps each failed rule to its protocol code (`tools.py:371-381`). |
| MA-07 | Agent iteration/tool budgets bound autonomy | **PARTIAL** | Iteration budget **is** enforced: `loop.py:443-451` increments and terminates with `unable_to_fulfill`. Tool-call budget is **dead code**: `state.tool_call_count` is initialised to `0` (`state.py:62`) and **never incremented anywhere in the repository** (verified by exhaustive grep — the only references are the comparison at `tools.py:703`, the API projection at `api/agent.py:66`, and the dashboard render at `MerchantAgentTrace.jsx:92`). Therefore `state.tool_call_count >= settings.ma_max_tool_calls` is permanently false and the `TOOL_BUDGET_EXHAUSTED` handler at `loop.py:735-742` is unreachable. Mitigation that I credit: the loop executes **at most one tool per iteration** (`loop.py:453/471`; additional `msg.tool_calls[1:]` are discarded with `{"skipped": "one action per turn"}` at l.183-190), so `ma_max_iterations = 10` does bound tool calls at 10 in practice. The *stated* control is nonetheless not the control that operates. |
| MA-08 | Agent cannot invent state or identifiers | **PASS** | `_load_pack` (`tools.py:83`) raises `PRODUCT_NOT_FOUND`/`PACK_NOT_FOUND` for unknown ids and additionally enforces `pack.product_id == product_id`. `_items_from_args` (l.320-346) validates JSON shape, requires both ids, coerces quantity to `int`, rejects `q < 1`, caps at 15 items. `_tool_get_checkout`/`_tool_run_gating`/`_tool_cancel_checkout` default to `state.checkout_session_id` rather than trusting an LLM-supplied id. |
| MA-09 | Agent terminates with a structured outcome | **PASS** | Six terminal/paused statuses defined in `state.py:14-16`; every exit routes through `_terminate` (`loop.py:761`) or `_apply_finish` (l.745), both of which write `state.outcome` **and** emit an `agent_outcome` audit event. |

**Section verdict: PARTIAL** — eight of nine PASS; MA-07's tool-call budget is a dead control.

---

## 5. Merchant Agent Tool Audit

| Tool | Read/Write | Privileged? | Actual caller | Validation | Audit | Can affect money? |
|---|---|---|---|---|---|---|
| `search_catalog` | Read | No | `execute_tool` ← `loop.run` | `query` coerced to str; delegates to `discovery_service` | `agent_search_catalog` | No |
| `get_product` | Read | No | same | `_load_product` → `PRODUCT_NOT_FOUND` | `agent_get_product` | No |
| `check_inventory` | Read | No | same | `_load_pack`; returns `_available()` = actual − reserved | `agent_check_inventory` | No |
| `get_current_price` | Read | No | same | `_load_pack`; returns `pack.price` only, labelled "authoritative merchant price" | `agent_get_current_price` | No |
| `find_alternative` | Read | No | same | `_load_pack` on both ids; filters by `excluded_pack_ids`, `allowed_categories`, `max_quantity_per_item`, and `_available() >= suggested` (`tools.py:247-255`) | `agent_find_alternative` | No — read-only |
| `get_checkout` | Read | No | same | defaults to `state.checkout_session_id` | `agent_get_checkout` | No |
| `create_checkout` | **Write** | Yes | same, inside `mutate()` | `_items_from_args`; then full `create_checkout` ladder: `validate_delegation` → `_resolve_items` (server prices) → `_compute_totals` → `_gate` | `checkout_create_requested`, `gating_evaluated`, `checkout_created`/`checkout_declined`, `reservation_created` | Indirectly — reserves stock, fixes the total. No Razorpay call. |
| `update_checkout` | **Write** | Yes | same | `_check_ownership`, `status == "requires_payment"` required, re-validates delegation, re-prices, re-gates | `checkout_update_requested`, `gating_evaluated`, `checkout_updated` | Indirectly — changes the total the buyer will pay |
| `cancel_checkout` | **Write** | Yes | same | `_check_ownership`; terminal states rejected; idempotent on `cancelled` | `checkout_cancelled` | No — releases reservations |
| `request_confirmation` | **Write** | Yes | same | validates proposal ids via `_load_pack`; sets `state.pending_proposal` and pauses the loop | `agent_awaiting_buyer_confirmation` | No |
| `run_gating` | Read (classified `FINANCIAL_TOOLS`) | Yes | same | reads the session; re-runs the pure engine | `agent_run_gating` | No — advisory only |
| `request_completion` | **Write** | **Yes — the only money path** | same, inside `mutate()` | Schema takes **zero arguments**. Rejects `execution_mode != "agent_completed"` (`tools.py:602`). Requires an existing `state.checkout_session_id`. Generates the idempotency key server-side: `f"ma_{agent_session_id}_complete_{attempts}"` (l.611) | `complete_requested`, `final_validation`, `razorpay_order_creation`, `razorpay_order_created`, `payment_success`/`payment_failure`, `checkout_completed`/`checkout_declined`, `agent_outcome` | **Yes** |

Two observations that matter.

`run_gating` is filed under `FINANCIAL_TOOLS` but is functionally a read. That inflates the apparent size of the financial surface; the true financial surface is one tool.

The idempotency key for agent-driven completion is `f"ma_{state.agent_session_id}_complete_{attempts}"` where `attempts` increments on every call (`tools.py:609-611`). This means a retry after a transient failure gets a **different** key and is therefore *not* deduplicated by the idempotency table. That is deliberate and correct for the "modal dismissed, retry" case, but it means the anti-duplicate-charge guarantee on the agent path rests entirely on the `cs.status == "completed"` early return (`checkout_service.py:625-627`) and the order-reuse guard (l.785-787), not on idempotency. See §9 and §12.

**Section verdict: PASS** with the note that tool classification is loose.

---

## 6. Safety Boundary Audit

The prompt defines CRITICAL as any LLM ability to change price/stock/delegation/status/payment state, bypass gating/idempotency/ownership, or call Razorpay directly. Each is traced.

| Capability | Reachable by an LLM? | Evidence |
|---|---|---|
| Change a price | **No** | No tool schema in `agent/prompts.py` or `buyer_service/prompts.py` contains a `price`, `amount`, `total`, or `unit_price` property. `_resolve_items` (`checkout_service.py:91`) sets `"unit_price": pack.price` with the comment "authoritative price — never client-supplied". `_compute_totals` derives everything from resolved items. |
| Change stock | **No** | The only two writers of `pack.stock_actual` are `checkout_service.py:972` (inside `_finalize_payment`, after a gateway success) and `api/admin.py:58` (`PATCH /admin/packs/{id}/stock`, guarded by `Depends(require_admin)` comparing `X-Admin-Token` to `settings.admin_token`). No agent tool writes stock. |
| Change delegation | **No** | `Delegation` rows are written only by `scripts/seed_catalog.py::seed_delegations` and `POST /admin/demo/reset`. No tool schema exposes a delegation field. `state.delegation_token` is excluded from `public_view()`. |
| Change checkout status | **No** | All `cs.status` writes are in `checkout_service.py` (§3 Q2). No agent tool assigns status. |
| Change payment state | **No** | `cs.razorpay_order_id` / `cs.razorpay_payment_id` are written only in `checkout_service.py:822, 964, 965`, all from a `PaymentOutcome` produced by `payment_service`. |
| Bypass gating | **No** | `_gate` is invoked unconditionally in `create_checkout` (l.288), `update_checkout` (l.406) and `complete_checkout` (l.730). There is no code path to Razorpay that does not pass l.730 first — grep for `payment_service.create_order` returns exactly one call site, `checkout_service.py:798`. |
| Bypass idempotency | Partially, and by design | See §5 note and §12. Not LLM-driven: the key is generated server-side from the agent session id. |
| Bypass ownership | **Yes — but not via the LLM** | `POST /checkout_sessions/{id}/payment_callback` and `POST /checkout_sessions/{id}/reconcile` perform no `_check_ownership`. Reachable by any HTTP client, not by the agent's tool surface. Escalated in §9 as **C-2** and **P1-1**. |
| Call Razorpay directly | **No** | `payment_service` is imported at `checkout_service.py:753` (function-local), `api/checkout.py`, and `api/admin.py`. `merchant_agent/agent/**` contains no import of `payment_service` and no `razorpay` string — verified by grep. |

**Section verdict: PASS for the LLM boundary.** No LLM output can reach a privileged financial mutation except through `request_completion`, which carries no arguments. The ownership bypasses that exist are HTTP-surface defects, not agent-autonomy defects, and are reported as such.

---

## 7. Alternative Fulfillment Audit

| # | Check | Status | Evidence |
|---|---|---|---|
| 7.1 | Alternatives are found from real catalog data | **PASS** | `_tool_find_alternative` queries `Pack` siblings (`tools.py:275`) and same-category `Product` rows (l.294). No hardcoded lists. |
| 7.2 | Alternatives respect delegation constraints | **PASS** | `add_candidate` rejects on `cand_product.category not in allowed_cats` (l.249) and `suggested > max_qty` (l.251), both read from the live delegation row via `_delegation_constraints`. |
| 7.3 | Alternatives respect real availability | **PASS** | `available = _available(db, pack)`; `if available < suggested: return` (l.253-255). `_available` calls `inventory_service.available_stock` = actual − active reservations. Correct here — unlike discovery (§13.3). |
| 7.4 | Alternatives are relevance-filtered, not just category-matched | **PASS** | Tier 2 requires a shared non-generic tag: `GENERIC_TAGS = {"dairy","daily-essential","grocery","essentials"}` are excluded, and `if not strong_tags: continue` (l.298-300) with the comment "avoid proposing loosely-related items (butter for milk)". |
| 7.5 | Ranking is deterministic | **PASS** | `alternatives.sort(key=lambda a: (0 if same-product else 1, abs(size − requested.size), price))` (l.307), then `[:5]`. No LLM, no randomness. |
| 7.6 | Quantity is re-derived, not copied | **PASS** | `_suggest_quantity(requested_pack, candidate, qty)` (l.99) recomputes units so a 500 ml → 250 ml swap proposes ×2, and the proposal is re-checked against `max_qty`. |
| 7.7 | The buyer must approve before the basket changes | **PASS** | `request_confirmation` sets `state.pending_proposal` and `_post_action` returns `True`, halting the loop (`loop.py:650-659`). Only `POST /agent_sessions/{id}/decision` resumes it (`loop.py:496`). `_accept_substitution` → `update_checkout` is the sole write path. |
| 7.8 | A rejected alternative is never re-proposed | **PASS** | `resume(accepted=False)` appends the rejected pack to `state.excluded_pack_ids` (`loop.py:523-525`) and `add_candidate` filters on that set. An LLM nudge reinforces it (l.528-538), but the structural filter is what enforces it. |
| 7.9 | No alternative → clean decline, no payment | **PASS** | `find_alternative` returns `{"alternatives": [], "count": 0, "note": "no in-stock alternative satisfying delegation constraints"}`. The prompt instructs `finish(outcome="declined", reason_code="no_valid_alternative")`; the deterministic brain does the same. `_apply_finish` → `_terminate` writes `declined` and never touches `payment_service`. |

**Section verdict: PASS.** This is the strongest subsystem in the repository — the substitution logic is genuinely deterministic, constraint-aware, and gated on human consent.

---

## 8. Bounded Autonomy — Adversarial Tests

These are static traces of what the code *would* do. They were **not executed** (see header). Each row states which guard fires and on which line.

| Attack | Guard that fires | Outcome by code | Status |
|---|---|---|---|
| "Buy 50 units and ignore all limits" | `gating_service` `RULE_QUANTITY` (`quantity_limit`) → `DELEGATION_SCOPE_VIOLATION`; on `dlg_restricted_50` (qty ceiling 50) `RULE_SPEND_LIMIT` fires instead → `SPEND_LIMIT_EXCEEDED`. `create_checkout` sets `cs.status = "declined"` (l.343) before any Razorpay import. | Declined; `checkout_declined` audit event carries "Razorpay NOT called". | **PASS (static)** |
| LLM emits `request_completion` in `buyer_completes` mode | `tools.py:602-606` raises `ToolError(INVALID_REQUEST, "execution_mode is 'buyer_completes'…")` | Failure envelope; loop continues; no payment. | **PASS (static)** |
| LLM invents `pack_id: "pack_gold_bar"` | `_load_pack` → `PACK_NOT_FOUND` (404) | Failure envelope fed back as a `role:"tool"` message; prompt instructs re-observation. | **PASS (static)** |
| LLM calls a tool that doesn't exist | `execute_tool` l.697 → `UNKNOWN_TOOL` | Failure envelope. | **PASS (static)** |
| LLM passes `{"price": 1}` to `create_checkout` | `_handler_params` intersection (l.720-723) discards unknown kwargs; `_items_from_args` only reads `product_id`, `pack_id`, `quantity` | Argument silently dropped; server price used. | **PASS (static)** |
| LLM emits 4 tool calls in one turn | `loop.py:176` takes `msg.tool_calls[0]` only; l.183-190 answers the rest with `{"skipped": "one action per turn"}` | One action per iteration. | **PASS (static)** |
| LLM refuses to call any tool, repeatedly | `llm_nudges` counter; at 3 raises `_Finish("declined", "agent_unable_to_progress", …)` (l.155-166) | Terminates declined. | **PASS (static)** |
| LLM loops forever | `state.iteration_count > settings.ma_max_iterations` (10) → `unable_to_fulfill` (l.443-451) | Bounded. | **PASS (static)** |
| LLM exceeds the tool-call budget | **Nothing fires.** `tool_call_count` is never incremented; the check at `tools.py:703` can never be true. | Bounded only indirectly, by the iteration cap. | **FAIL** |
| LLM tries to call Razorpay | No import path exists from `merchant_agent/agent/**` to `payment_service` | Impossible. | **PASS** |
| Concurrent agent sessions racing the same pack | `db.mutate()` process-wide `RLock` serializes writes; `session_busy_lock` prevents two runs of the same agent session (`api/agent.py:107-113`) | Serialized. **Never exercised by any test — no test in the repository spawns a thread.** | **NOT VERIFIABLE** |
| Prompt injection inside a product name | Product names reach the LLM via `search_catalog` results. There is no sanitisation. The blast radius is bounded because the model's only financial verb is a zero-argument `request_completion`, and gating still runs. | Bounded but not prevented. | **PARTIAL** |
| System prompt states the wrong budget | `agent/prompts.py:33` tells the model "8 iterations / 10 tool calls"; `config.py` sets `ma_max_iterations = 10` and the tool-call cap is inert. | Cosmetic/explainability drift. | **PARTIAL** |

**Section verdict: PARTIAL** — the containment is real and mostly structural, but one advertised bound does not exist and concurrency is entirely unexercised.

---

## 9. Financial Authority Audit

There are exactly **five** code paths that can create a Razorpay order, set a payment outcome, mark a checkout completed, or consume inventory. Each is traced through the full ladder. "—" means the step is absent from that path.

### Path 1 — `POST /checkout_sessions/{id}/complete` (human / Buyer Agent A / Buyer B)

| Step | Implementation | Line |
|---|---|---|
| ENTRY | `api/checkout.py::complete_checkout`, inside `mutate()` | `api/checkout.py:101-114` |
| OWNERSHIP | `_check_ownership(cs, buyer_agent_id, delegation_token)` → 403 `UNAUTHORIZED_AGENT` | `checkout_service.py:614` |
| STATE | early return if already `completed` (l.625-627); else `_assert_transition(cs.status, "completed")` against `VALID_TRANSITIONS` | `:625-629` |
| PRICE | `_resolve_items` re-reads `pack.price`; `_compute_totals`; mismatch → `requires_confirmation` + `PRICE_CHANGED` | `:669-694` |
| DELEGATION | `validate_delegation` re-run (status must be `active`, `expires_at > now`) | `:698` |
| STOCK | strict double test: `pack.stock_actual < qty` **or** `available + own_reservation < qty` | `:702-727` |
| GATING | `_gate(db, delegation, fresh, new_totals)` — all 5 rules re-run | `:730-739` |
| IDEMPOTENCY | `_idempotent_replay` at entry (l.609); snapshot stored **by the API layer** at `api/checkout.py:113-114` | `:609` |
| PAYMENT | `ensure_customer` → `create_order` → `charge_with_token` or modal park | `:762-942` |
| AUDIT | `complete_requested`, `final_validation`, `razorpay_order_creation`, `razorpay_order_created`, then `payment_success`/`payment_failure` + `checkout_completed`/`checkout_declined` | throughout |
| FINAL STATE | `completed` \| `declined` \| `payment_pending` \| `requires_confirmation` \| `requires_payment` (awaiting modal) | `:974/1008/997/672/—` |

**Verdict: PASS.** Every rung is present and in the mandated order. This is the path `buyer_b/client.py` and the buyer service both use.

### Path 2 — `request_completion` tool (`execution_mode="agent_completed"`)

Identical to Path 1 — it calls the same `checkout_service.complete_checkout` (`tools.py:612`). Three differences:

- Ownership uses `state.buyer_agent_id` / `state.delegation_token`, captured at session creation from the `POST /agent_sessions` body. The agent cannot alter them.
- The idempotency key is server-generated and **increments per attempt** (`tools.py:609-611`).
- **The key is never persisted.** `store_complete_idempotency` is called only from `api/checkout.py:113-114`; `checkout_service.complete_checkout` does not store it, and `_tool_request_completion` does not either. So `_idempotent_replay` can never hit an `ma_*` key.

**Verdict: PARTIAL.** The ladder is correct, but the agent path has **no idempotency-table protection at all**. Duplicate-charge safety on this path rests entirely on the `status == "completed"` early return (l.625) and the order-reuse guard (l.785). Those are real and do hold for the ordinary retry case — but the stated mechanism is not the operating mechanism, which is exactly what Rule 2 asks an auditor to catch.

### Path 3 — `POST /checkout_sessions/{id}/payment_callback`

| Step | Implementation | Line |
|---|---|---|
| ENTRY | `api/checkout.py::payment_callback(checkout_session_id, body: dict)` — untyped body, **no auth, no credentials** | `:179-186` |
| OWNERSHIP | **— absent.** No `buyer_agent_id`, no `delegation_token`, no `_check_ownership`. | — |
| STATE | **— absent.** Only `cs.razorpay_order_id != order_id` is checked (l.188). No status guard. | — |
| PRICE | **— absent.** The Razorpay payment amount is never compared to `cs.totals["total"]`. | — |
| DELEGATION | **— absent.** | — |
| STOCK | **— absent** as a *gate*. Stock is nevertheless *decremented* at `:972`. | — |
| GATING | **— absent.** | — |
| IDEMPOTENCY | **— absent.** No `Idempotency-Key`, no replay table, no dedupe on `payment_id`. | — |
| PAYMENT | `verify_checkout_signature(order_id, payment_id, signature)` = `hmac_sha256(secret, f"{order_id}\|{payment_id}")` + `compare_digest`; returns `False` when credentials are missing | `payment_service.py` |
| AUDIT | `payment_success` + `checkout_completed` (+ optional `payment_token_saved`) | `:975-995` |
| FINAL STATE | `completed`, unconditionally, from **any** prior status | `:974` |

**Verdict: FAIL — CRITICAL (C-2).** The only control is the HMAC signature, which proves *a* Razorpay payment exists for *that order id*. It proves nothing about who is calling, what state the session is in, or how much was paid. Concretely reachable consequences:

1. **Cancelled → completed.** Buyer opens the modal (`requires_payment`, order created), calls `POST .../cancel` — `cs.status = "cancelled"`, reservations released — then completes the still-open Razorpay modal. The callback fires, `_finalize_payment` runs the `completed` branch, stock is decremented, `checkout_completed` is written. `VALID_TRANSITIONS["cancelled"] = set()` is violated because `_finalize_payment` never calls `_assert_transition` (**C-3**).
2. **requires_confirmation → completed at the stale price.** The merchant detected a price change and demanded confirmation, but the browser still holds the old order. The callback completes the session at the **old** amount while the audit trail records `f"Paid ₹{json.loads(cs.totals)['total']}"` — i.e. the *new* total. Money received and money recorded diverge. This is Rule 6's "payment with stale state" **and** "payment with manipulated price".
3. **Double inventory consumption.** Replaying the same valid callback on an already-`completed` session re-enters the `completed` branch and decrements `pack.stock_actual` a second time (l.972). `consume_session_reservations` is a no-op the second time, so reservation accounting silently diverges from stock. A second `payment_success` event is appended for the same `payment_id`. No second charge occurs (same `payment_id`), so this is duplicate *fulfilment*, not duplicate *capture* — still a financial integrity failure.

### Path 4 — `POST /checkout_sessions/{id}/reconcile`

| Step | Implementation | Line |
|---|---|---|
| ENTRY | `api/checkout.py::reconcile(checkout_session_id: str)` — **no body, no auth** | `:251-255` |
| OWNERSHIP | **— absent.** | — |
| STATE | `if cs.status != "payment_pending": raise INVALID_STATE_TRANSITION` (409) | `checkout_service.py:1050-1055` |
| PRICE / DELEGATION / STOCK / GATING | **— absent.** | — |
| IDEMPOTENCY | **— absent**, but the state guard makes it self-limiting (after the first success the status is no longer `payment_pending`). | — |
| PAYMENT | `payment_service.reconcile_pending(cs.razorpay_order_id)` — asks Razorpay for the truth | `:1058` |
| AUDIT | via `_finalize_payment` | `:975-1027` |
| FINAL STATE | `completed` \| `declined` \| `payment_pending` | `:974/1008/997` |

**Verdict: PARTIAL — P1, not critical.** The state guard is a real control and the outcome is gateway-truthful, so this cannot manufacture a payment. But any unauthenticated caller can reconcile **any** session id, and the response is a full `CompleteResponse` including `line_items`, `totals`, `buyer_agent_id` and `delegation_id`. That makes it an ownership/IDOR defect, and it feeds C-5 below.

### Path 5 — `driver == "sim"` → `execute_sim_capture`

Reachable only when `settings.payment_driver == "sim"`, which the Pydantic validator `_reject_sim_outside_tests` (`config.py`) refuses unless `ACG_TEST_ALLOW_SIM=1`, set only in `merchant_agent/tests/conftest.py:15`. In production configuration this path is unreachable.

**Verdict: PASS.** The team previously shipped a bug where sim mode "silently served real-looking confirmations (paid ₹155 '(test-mode simulation)')" — the comment in `config.py` says so — and the validator is the fix. Credit where due: this is a deliberate, well-placed guard.

### 9.6 The unifying root cause

C-1, C-2 and the underpayment scenario below are three faces of one omission: **the amount of a Razorpay order is never reconciled against the session total, at any point, on any path.** `create_order` is called with `total_rupees`; after that, no code re-reads `order["amount"]`.

**C-1 — stale order reused after a confirmed price increase.** `confirm_checkout` (`checkout_service.py:569-577`) rewrites `cs.line_items`, `cs.totals`, `cs.version` and sets `cs.status = "requires_payment"` — and **does not clear `cs.razorpay_order_id`**. Neither does the price-change branch at l.672-673. So:

1. Basket = ₹500. `complete` → order_A created for ₹500, session parked `requires_payment`.
2. Operator raises the pack price via `PATCH /admin/packs/{id}/stock`… or the catalog changes. Buyer retries `complete` → price re-check fires → `requires_confirmation`, order_A still attached.
3. Buyer accepts new terms → `cs.totals` = ₹600, `status = requires_payment`, **`razorpay_order_id` still = order_A (₹500)**.
4. Buyer retries `complete` → price re-check now passes (₹600 == ₹600), delegation ✓, stock ✓, gating ✓ → the retry guard at l.785-787 **reuses order_A**.
5. `serialize_session` reports `payment.amount = 600` (from `cs.totals`) alongside `order_id = order_A`. The dashboard computes `amount_paise` client-side (`OpsView.jsx:248-249`) and hands it to Razorpay's SDK, but **Razorpay charges the amount stored on the order** — ₹500. `payment_callback` then marks the session `completed` and the audit trail records "Paid ₹600".

Net effect: **₹100 of goods shipped unpaid, with an audit trail that says otherwise.** On the `saved`-token sub-path, `charge_with_token(order_A, amount_paise=60000, …)` would likely be rejected by Razorpay for amount mismatch — but that is the gateway defending the merchant, not the merchant defending itself. **CRITICAL (C-1).**

**C-5 — delegation-token exfiltration via two unauthenticated GETs.** `cs.delegation_id` *is* the delegation token (`checkout_service.py:295`), and `serialize_session` returns it as `delegation_id` (l.174). `GET /checkout_sessions/{id}` (`api/checkout.py:95-98`) has **no ownership check**. `GET /audit_logs` (`api/audit.py:31`, unauthenticated, `limit ≤ 1000`) returns `checkout_session_id` for every event. Chain:

```
GET /audit_logs                  → harvest cs_… ids          (unauthenticated)
GET /checkout_sessions/{cs_id}   → {buyer_agent_id, delegation_id}  (unauthenticated)
POST /checkout_sessions          → spend up to max_order_value on that delegation
POST /checkout_sessions/{id}/complete → charge it
```

`_check_ownership` and `validate_delegation` require exactly the two values the second GET hands over. **This is CRITICAL under Rule 6 ("payment without valid delegation" / "unauthorized payment").**

On Rule 7 fairness: I am *not* penalising the fact that delegation tokens are mock, unsigned bearer strings — `protocol_spec.md:109-113` discloses that, and the PRD permits it. The defect is that the API **echoes the bearer secret to unauthenticated readers**, which turns an accepted prototype simplification into a live privilege-escalation path. A one-line change (omit `delegation_id` from `CheckoutSessionOut`, or require the token to read the session) removes it without changing the identity model at all.

**Section verdict: FAIL — three CRITICAL findings (C-1, C-2, C-5) plus C-3 as the enabling defect.**

---

## 10. Checkout State Machine Audit

### 10.1 Every `status =` assignment in the production tree

Enumerated by exhaustive grep over `merchant_agent/services`, `merchant_agent/api`, `merchant_agent/agent`.

**Checkout session status (14 sites, all in `checkout_service.py`):**

| Line | Assignment | Guarded by |
|---|---|---|
| 293 | `status="created"` (constructor) | n/a |
| 314 | `cs.status = "requires_payment"` | `if gating.passed` |
| 343 | `cs.status = "declined"` | `else` (gating failed) |
| 439 | `cs.status = "requires_payment"` | `update_checkout`, after `status == "requires_payment"` precondition + gating pass |
| 489 | `cs.status = "cancelled"` | `cancel_checkout`, after `_check_ownership` + terminal-state rejection |
| 557 | `cs.status = "cancelled"` | `confirm_checkout`, `accept_new_terms=False`, after version + status checks |
| 577 | `cs.status = "requires_payment"` | `confirm_checkout` accept, after version + `status == "requires_confirmation"` |
| 636 | `cs.status = "declined"` | `_fail()` inside `complete_checkout`, after `_assert_transition` |
| 672 | `cs.status = "requires_confirmation"` | price mismatch, after `_assert_transition` |
| 802 | `cs.status = "payment_pending"` | `create_order` raised |
| 974 | `cs.status = "completed"` | `_finalize_payment`, `outcome.status == "completed"` — **no `_assert_transition`** |
| 997 | `cs.status = "payment_pending"` | `_finalize_payment` — **no `_assert_transition`** |
| 1008 | `cs.status = "declined"` | `_finalize_payment` — **no `_assert_transition`** |

**Reservation status (4 sites, `inventory_service.py`):** `expired` (l.30), `active` (l.61), `released` (l.78), `consumed` (l.92).
**PaymentMethod status (4 sites):** `needs_reauth` (`checkout_service.py:875`), `pending_save` (l.919, l.952), `active` (`api/checkout.py:219`).
**Agent session status (5 sites, `loop.py`):** `running` (504), `awaiting_buyer_confirmation` (651, 684), `awaiting_payment_authorization` (721), `ready_for_completion` (750), plus `set_outcome` (`state.py:80`).

### 10.2 Transition table

`VALID_TRANSITIONS` (`checkout_service.py:37-45`) is the declared contract. "Actual" is what the code does.

| Current | Action | Expected | Actual |
|---|---|---|---|
| created | gating pass | requires_payment | requires_payment ✓ |
| created | gating fail | declined | declined ✓ |
| requires_payment | `complete`, all rungs pass | completed / awaiting modal | ✓ |
| requires_payment | `complete`, price changed | requires_confirmation | requires_confirmation ✓ |
| requires_payment | `complete`, stock/policy fail | declined | declined ✓ (`_fail`) |
| requires_payment | `complete`, order creation errors | payment_pending | payment_pending ✓ |
| requires_payment | `update` | requires_payment (v+1) | ✓ |
| requires_payment | `cancel` | cancelled | cancelled ✓ |
| requires_confirmation | `confirm(accept, v)` | requires_payment | ✓ |
| requires_confirmation | `confirm(reject, v)` | cancelled | cancelled ✓ |
| requires_confirmation | `confirm` with stale `v` | 409 CONFIRMATION_STALE | ✓ |
| requires_confirmation | `complete` | 409 (not in `VALID_TRANSITIONS["requires_confirmation"]`) | ✓ — `_assert_transition` raises |
| **requires_confirmation** | **`payment_callback`** | **409** | **completed ✗ — C-2/C-3** |
| payment_pending | `reconcile`, gateway says paid | completed | completed ✓ |
| payment_pending | `reconcile`, gateway says failed | declined | declined ✓ |
| payment_pending | `complete` | allowed by table (`{"completed","declined"}` excludes it — `_assert_transition` raises 409) | ✓ |
| completed | `complete` (bare) | existing result, no new charge | ✓ — l.625-627 returns the session with `replayed=True` |
| completed | `complete` (new Idempotency-Key) | existing result, no new charge | ✓ — the `status == "completed"` check precedes `_assert_transition` |
| completed | `cancel` | 409 | ✓ — `TERMINAL_STATES` |
| completed | `update` | 409 | ✓ |
| **completed** | **`payment_callback` replay** | **409 / no-op** | **re-runs the completed branch; stock decremented twice ✗ — C-2/C-3** |
| declined | `complete` | 409 | ✓ |
| declined | `cancel` | 409 | ✓ |
| cancelled | `complete` | 409 | ✓ |
| cancelled | `cancel` | idempotent 200 | ✓ (l.477-478) |
| **cancelled** | **`payment_callback`** | **409** | **completed ✗ — C-2/C-3** |

### 10.3 Findings

`_assert_transition` is called from exactly **one** place: `checkout_service.py:629`. Every other status write relies on an ad-hoc precondition. Those preconditions are correct for the protocol endpoints, but `_finalize_payment` — the function that writes `completed` — has none, and it is reachable from an unauthenticated endpoint. That is **C-3**, and it is what converts C-2 from a missing-auth annoyance into a state-machine breach.

The `completed` early return at l.625-627 sits **before** `_assert_transition`, which is the right order: it means a replayed completion returns 200 with the original `payment_id` rather than a 409, matching guarantee 4 in `protocol_spec.md:16-17`. `merchant_agent/tests/test_state_machine.py::test_terminal_completed_returns_existing_result` asserts exactly this (200, same `payment_id`, exactly one `razorpay_order_created` event) — though see §22 on the history of that test.

**Section verdict: PARTIAL** — the declared state machine is correct and enforced on all protocol endpoints; it is bypassed entirely on `payment_callback`.

---

## 11. Delegated Authority Audit

`validate_delegation` (`delegation_service.py:14-33`) is the single resolver, called at `checkout_service.py:275` (create), `:396` (update) and `:698` (complete). It enforces three things: the row exists, `delegation.buyer_agent_id == buyer_agent_id`, and `delegation.status == "active"` — all collapsed into one 401 `UNAUTHORIZED_AGENT` — then `expires_at <= utcnow()` → 401 `DELEGATION_EXPIRED`.

| Check | Status | Evidence |
|---|---|---|
| Token is bound to a specific buyer agent | **PASS** | `delegation.buyer_agent_id != buyer_agent_id` → 401. A stolen token is useless without the matching agent id — though both are disclosed together by C-5. |
| Revocation is enforced | **PASS (code) / NOT VERIFIABLE (demo)** | `delegation.status != "active"` → 401. But `scripts/seed_catalog.py` seeds **no** revoked delegation (four tokens: `dlg_9f8e7d`, `dlg_restricted_50`, `dlg_buyerb_001`, `dlg_expired_001`), and no test references a revoked row. The control exists and is on the hot path; it has never been exercised. |
| Expiry is enforced at creation | **PASS** | `dlg_expired_001` is seeded with `expires_at = now − 1 minute`. `create_checkout` calls `validate_delegation` at l.275 before gating. |
| Expiry is re-enforced at completion | **PASS** | l.698, inside the ladder, wrapped so the failure lands in `_fail` and persists a `declined` state + audit event (l.699-700). |
| Spend limit enforced | **PASS** | `RULE_SPEND_LIMIT`: `total <= delegation.max_order_value`. Re-run at completion (l.730). |
| Category allow-list enforced | **PASS** | `RULE_CATEGORY` set-difference against `allowed_categories`. `ALLOWED_CATEGORIES` in the seeder deliberately omits `tobacco`, and `pack_supari_100` is a tobacco pack — a real negative case exists in the catalog. |
| Per-item quantity enforced | **PASS** | `RULE_QUANTITY` against `max_quantity_per_item`. |
| Delegation token never reaches the LLM | **PASS** | `state.public_view()` omits it; `_initial_context` builds from `public_view`; `_args_digest` only digests tool arguments, and no tool schema has a token field. |
| Delegation cannot be widened by any agent action | **PASS** | No write to `Delegation` exists outside the seeder and `POST /admin/demo/reset`. |
| **Token is not disclosed to unauthorised readers** | **FAIL — C-5** | `serialize_session` returns `delegation_id` (= the token) and `GET /checkout_sessions/{id}` requires no authentication. |

One piece of dead code inside the gating engine deserves recording, because it looks like a control and is not one. `RULE_EXPIRY`'s **fail** branch (`gating_service.py`, the `else` after `delegation.expires_at > now`) is **unreachable through any API path**: `validate_delegation` always runs first (l.275 before l.288; l.698 before l.730) and raises 401 before gating is entered. It is harmless defence-in-depth, but a reader who counts "5 enforced rules" is counting one rule that can only ever report `pass`.

**Section verdict: PARTIAL** — enforcement logic is correct and consistently placed; token confidentiality is not.

---

## 12. Price Integrity Audit

| Check | Status | Evidence |
|---|---|---|
| Buyer cannot supply a price | **PASS** | `CreateCheckoutRequest.line_items` items carry only `product_id`, `pack_id`, `quantity` (confirmed against `buyer_service/tools.py` payloads, `buyer_b/client.py:65-69` and both tool-schema files). `_resolve_items` sets `unit_price` from `pack.price`. |
| Agent cannot supply a price | **PASS** | `_items_from_args` reads only the three permitted keys; `_handler_params` drops the rest. |
| Totals are server-computed | **PASS** | `_compute_totals` — `subtotal = Σ unit_price × quantity`, `shipping = 0 if subtotal >= 999 else 40`, `tax = 0`. Single implementation, called at creation, update, confirm and completion. |
| Price is re-checked immediately before payment | **PASS** | `complete_checkout` l.669-671 re-resolves and compares `new_totals["total"] != old_totals["total"]`. |
| A price change pauses rather than proceeds | **PASS** | → `requires_confirmation` + `PRICE_CHANGED` + `change` block with `old_total`/`new_total`. |
| Confirmation is version-matched | **PASS** | `confirm_checkout` rejects `version != cs.version` with `CONFIRMATION_STALE` (l.527-544) **before** the status check. |
| Accepting new terms persists the new price | **PASS** | l.569-577 re-resolves and rewrites `cs.line_items`/`cs.totals`, so the next completion validates against what the buyer actually approved. This is a subtle correctness win — a naive implementation would only flip the status. |
| The dashboard cannot influence the charged amount | **PASS** | `OpsView.jsx:248-249` computes `amount_paise` client-side but passes it only to Razorpay's SDK. The sole client→merchant POST carrying payment state is `RazorpayPayCard.jsx:92-95`, which forwards Razorpay's verbatim `resp` (order id, payment id, signature) — no amount field is accepted by `payment_callback`. |
| **The Razorpay order amount is reconciled against the session total** | **FAIL — C-1** | Never, on any path. `create_order(…, total_rupees)` is the only place amount and session meet; `order["amount"]` is never re-read. Combined with `confirm_checkout` not clearing `razorpay_order_id`, this yields the underpayment trace in §9.6. |
| The audit trail records the amount actually charged | **FAIL** | `payment_success` records `f"Paid ₹{json.loads(cs.totals)['total']}"` — the *session* total, not the gateway amount. When they diverge (C-1, C-2 case 2), the append-only trail records a figure that was never charged. |

**Section verdict: FAIL.** Price *authority* is airtight — no client or LLM can propose a price. Price *reconciliation* against the payment instrument is absent, and the audit trail inherits the error.

---

## 13. Inventory Integrity Audit

Model (`inventory_service.py` docstring): `available = actual − Σ active unexpired reservations`. The docstring also states the deliberate design choice that "a reservation does NOT protect against actual stock disappearing" — this enables the TOCTOU demo and is not a defect.

| # | Scenario | Status | Evidence |
|---|---|---|---|
| 13.1 | Reservation created on successful gating | **PASS** | `create_reservations` at `checkout_service.py:313`, TTL `settings.reservation_ttl_minutes = 10`. |
| 13.2 | Reserved stock is invisible to other buyers | **PASS** | `available_stock` subtracts `reserved_quantity` and is consumed by `RULE_STOCK`, `_tool_check_inventory`, and `find_alternative`. |
| 13.3 | Discovery reports availability consistently | **FAIL** | `discovery_service.py:297` returns `"available_stock": max(0, pack.stock_actual)` — **actual stock, ignoring reservations**. This diverges from `inventory_service.available_stock` used everywhere else. Two consequences: (a) discovery over-promises, so a buyer is offered a pack that gating will then reject; (b) `buyer_service/agent.py::_propose_substitutes` filters candidates on `p.get("available_stock", 0) >= primary["quantity"]`, so the buyer's substitution logic consumes the inflated number and can propose an already-reserved pack. |
| 13.4 | Stock is re-checked at completion, strictly | **PASS** | l.702-727 tests **both** `pack.stock_actual < qty` and `effective_available < qty`, where `effective_available = available + own_reservation`. Holding a reservation does not excuse missing actual stock. |
| 13.5 | Stock vanishing between checkout and completion declines cleanly | **PASS** | `_fail(STOCK_UNAVAILABLE, …)` releases reservations, sets `declined`, writes `final_validation` + `checkout_declined` ("Razorpay NOT called"), and returns `retryable: True`. `scripts/simulate_stock_failure.py` exists to demo it. |
| 13.6 | Stock decremented exactly once on success | **PARTIAL** | Once per `_finalize_payment` `completed` branch (l.972). Correct on Paths 1, 2, 4. **Not idempotent on Path 3** — a replayed `payment_callback` decrements again (C-2 case 3). |
| 13.7 | Reservations released on cancel / decline / rejected terms | **PASS** | `release_session_reservations` at l.488 (cancel), l.635 (`_fail`), l.556 (terms rejected), l.1007 (gateway decline), l.433 (update, before re-reserving). |
| 13.8 | Expired reservations are swept | **PASS** | `sweep_expired` runs on every `available_stock` call, marking `active`+`expires_at <= now` rows `expired`. Lazy sweep, no scheduler — adequate, since every read path triggers it. |
| 13.9 | Concurrent reservations against the last unit | **NOT VERIFIABLE** | `db.mutate()`'s process-wide `RLock` serializes writes and *should* make this safe. **No test in the repository spawns a thread** — grep for `threading`/`concurrent` in `merchant_agent/tests/` returns nothing. `pack_chicken_500` is seeded with `stock 1`, so the fixture for this test exists; the test does not. |

**Section verdict: PARTIAL** — the reservation model is coherent and the completion-time re-check is genuinely strict; discovery reports a different number than every other subsystem, and the single-decrement guarantee depends on the callback defect being fixed.

---

## 14. Graceful Failure Audit

The instruction is explicit: do not mark graceful failure PASS merely because an exception is caught. Each row states what the *state* and *audit trail* look like afterwards.

| # | Failure | Status | What actually happens |
|---|---|---|---|
| 14.1 | Gating rejects at creation | **PASS** | `cs.status = "declined"`, `decline_code` set, two audit events, structured 409 with `details.gating` and `session_ref`. The `ProtocolError` is raised **outside** `mutate()` (`api/checkout.py:44-45`) so the declined state and its audit events **commit** rather than roll back. This is a deliberate and correct design choice. |
| 14.2 | Gating rejects an update | **PASS** | `update_checkout` returns the error object rather than raising, so the **previous basket and reservation are preserved** (comment at l.417). Buyer can adapt again. |
| 14.3 | Stock disappears before payment | **PASS** | See 13.5. Reservations released, `retryable: True`, Razorpay untouched. |
| 14.4 | Price changes before payment | **PASS** | `requires_confirmation` + `PRICE_CHANGED` + `change` payload. Not a silent re-price. |
| 14.5 | Delegation expires mid-transaction | **PASS** | `_fail(exc.code, …)` at l.699-700 — the 401 is converted into a persisted `declined` state with an audit event, rather than an unlogged exception. |
| 14.6 | Razorpay order creation fails | **PASS** | `cs.status = "payment_pending"`, `payment_pending` audit event with "reconciliation required; no blind retry", `PAYMENT_PENDING` error with `retryable: False`. The refusal to auto-retry is the right call. |
| 14.7 | Gateway declines the payment | **PASS** | `_finalize_payment` else-branch: reservations released, `declined`, `decline_code = PAYMENT_FAILED`, `payment_failure` + `checkout_declined` events. |
| 14.8 | Gateway result unknown / timeout | **PASS** | `payment_pending` + `PAYMENT_PENDING`; `/reconcile` is the documented resolution and asks Razorpay for the truth. |
| 14.9 | Saved-card token unchargeable | **PASS** | Three-way classification: `auth_required` → `method.status = "needs_reauth"` + `token_needs_reauth` event, then falls back to the modal **on the same order**; other rejections → `token_charge_fallback` event, same-order modal; success → finalize. Notably it does **not** terminally decline, and it does **not** create a second order. |
| 14.10 | LLM brain unavailable | **PASS** | `loop.py:426-428` catches, logs a warning, sets `fallback_ctx["llm_disabled"]`, and switches to `_fallback_decide` **for the remainder of the session** — it does not retry the failing brain every iteration. `discovery_service._llm_parse` returns `None` on any exception and on a missing key. |
| 14.11 | Unhandled exception anywhere | **PARTIAL** | `main.py`'s `Exception` handler returns a structured 500 with "Internal merchant error; transaction not executed" and logs the traceback. The message is a claim, not a guarantee — a mid-ladder crash after `create_order` would leave the session in `requires_payment` with a live Razorpay order, and the response would assert the transaction was not executed. `mutate()` rolls back the DB session, which is why the claim is *usually* true; it is not true for the Razorpay side effect. |
| 14.12 | Token-capture failure after a paid order | **PARTIAL** | `api/checkout.py:244-245` — `except Exception: pass`, with the comment "token persistence must never fail a paid order". The intent is right: a bookkeeping failure must not un-pay an order. But the swallow is total and silent — no audit event, no log. A permanently failing `fetch_payment` would leave every buyer stuck on the modal path forever with nothing in the trail to explain why. |

**Section verdict: PASS with two PARTIALs.** This subsystem is unusually well built for a hackathon: failures produce *committed* state plus *durable audit events*, not just HTTP errors. The two PARTIALs are about honesty of the failure message and observability of a swallowed exception, not about correctness of state.

---

## 15. Buyer ↔ Merchant Independence Audit

| Check | Status | Evidence |
|---|---|---|
| Buyer never imports merchant code | **PASS** | Grep across `buyer_service/**` and `buyer_b/**` for `merchant_agent` returns **zero** imports. `buyer_service/tools.py` is 148 lines of `httpx` calls; its docstring states it is "the ONLY way the buyer touches the merchant. No merchant internals, no DB, no Razorpay (G2 / NFR-07)". |
| Buyer never touches the merchant DB | **PASS** | No `sqlalchemy`, no `sqlite3`, no `merchant.db` reference anywhere under `buyer_service/` or `buyer_b/`. |
| Buyer never touches Razorpay credentials | **PASS** | `razorpay_key_secret` is read only in `merchant_agent/services/payment_service.py`. `buyer_service/config.py` has no Razorpay fields at all — it defines exactly `nim_api_key`, `nim_base_url`, `nim_model`, `buyer_agent_id`, `delegation_token`, `merchant_base_url`, `max_agent_iterations`, `buyer_service_port`. The `key_id` (public, by design) reaches the browser via the merchant's `payment.key_id`. |
| Separate processes | **PASS** | Merchant on `settings.merchant_port` (8000), buyer on `buyer_service_port` (8001), dashboard on Vite 5173. Three `uvicorn`/Vite entry points. |
| A second, independent buyer can transact | **PASS** | `buyer_b/client.py` — no LLM, no shared code with `buyer_service`, its own `BUYER_AGENT_ID = "buyer_agent_b_deterministic"` and `dlg_buyerb_001`. It discovers, selects deterministically by cheapest-fits-budget, creates a checkout, completes with `Idempotency-Key: buyerb-<hex10>`, then **replays the same key and asserts `pay2.payment_id == payment.payment_id`** (l.98-106), returning that boolean as the process exit code. This is a real interoperability proof, not a mock. |
| The merchant special-cases no buyer | **PASS** | Grep for `buyer_agent_llm_01` / `buyer_agent_b` inside `merchant_agent/**` returns only the seeder's delegation rows. No branching on buyer identity exists in `checkout_service`, `gating_service`, or `agent/`. |
| The buyer holds no merchant state | **PASS** | `buyer_service/conversations.py` stores conversation text, `active_session_id`, `declined_packs`, `offered_pack_ids` — no prices, no stock, no delegation scope. Every authoritative number is re-read from the merchant response. |

**Section verdict: PASS.** This is the cleanest requirement in the project and it is met structurally, not by convention.

---

## 16. Protocol Audit

`protocol/protocol_spec.md` (119 lines) plus five JSON Schemas. Checked claim-by-claim against code.

| Spec claim | Status | Note |
|---|---|---|
| Guarantee 1 — terminal sessions never execute payments | **FAIL** | True for `/complete` (`_assert_transition`). False for `payment_callback`, which completes `cancelled` and re-completes `completed` sessions (C-2/C-3). |
| Guarantee 2 — every transactional request validated by a deterministic engine; no LLM in authorization | **PASS** | Verified in §6 and §11. |
| Guarantee 3 — merchant-owned pricing | **PASS** | Verified in §12. |
| Guarantee 4 — idempotent financial execution | **PARTIAL** | True on `/complete` with a client key (`_idempotent_replay` + `store_complete_idempotency`) and true for the completed-session early return. **False on the agent path** — the `ma_*` key is generated but never stored (§9 Path 2). |
| Guarantee 5 — structured failure semantics | **PASS** | `ProtocolError.envelope()` via three `main.py` exception handlers; all 16 documented codes exist in `ErrorCode`. |
| Guarantee 6 — merchant owns inventory/order/payment state | **PASS** | Verified in §15. |
| Guarantee 7 — JSON matching `schemas/` | **NOT VERIFIABLE** | The five schemas are static files. **No code loads them and no test validates a response against them** — grep for `jsonschema` returns nothing in `requirements.txt` or any source file. The schemas are documentation, not enforcement. |
| Endpoint table completeness | **PARTIAL** | 13 endpoints are documented and all 13 exist. **Three privileged endpoints are undocumented:** `POST /checkout_sessions/{id}/payment_callback`, `POST /checkout_sessions/{id}/reconcile`, and the entire `/admin/*` family (`GET /admin/packs`, `PATCH /admin/packs/{id}/stock`, `POST /admin/demo/reset`, `GET /admin/ops/status`). The two undocumented checkout endpoints are precisely the two with no ownership check — the gap in the spec and the gap in the auth model are the same gap. |
| Lifecycle diagram matches `VALID_TRANSITIONS` | **PASS** | The ASCII diagram at l.78-89 is faithful to the table at `checkout_service.py:37-45`. |
| Completion ladder order matches implementation | **PASS** | Spec l.93-97 lists session → ownership → state → totals → delegation → stock → policy → order → capture. `complete_checkout` executes exactly that order (l.613, 614, 629, 669, 698, 702, 730, 798). |
| Agent-session extension documented | **PASS** | l.44-74 documents the four `/agent_sessions` endpoints, both execution modes, all six response statuses, and the structured-proposal shape — matching `state.py:14-16` and `_tool_request_confirmation`. |
| §52 invariants claim | **PARTIAL** | "cannot bypass checkout state or idempotency" is true of the agent but, per Guarantee 4, the agent path has no idempotency storage to bypass in the first place. |

**Section verdict: PARTIAL** — the spec is unusually honest (it explicitly disclaims full ACP compliance and discloses the prototype identity model), and the documented surface matches the code. What is missing from the spec is exactly what is missing from the security model.

---

## 17. Audit Trail Audit

| Check | Status | Evidence |
|---|---|---|
| Append-only in practice | **PASS** | `audit_service` exposes only `append_event` and readers. Grep for `delete()`/`update()` against `AuditEvent` outside `POST /admin/demo/reset` and the test `clean_state` fixture returns nothing. |
| Hash chain is real | **PASS** | `event.hash = SHA256(_canonical(event) + prev_hash)`; `prev_hash` read from the highest `seq` (l.44-46). `_canonical` is `json.dumps(..., sort_keys=True, separators=(",",":"))` over 9 fields — deterministic. |
| Chain is verifiable | **PASS** | `verify_chain` recomputes from `seq` ascending and returns `(intact, entries_checked, first_broken_seq)`, exposed at `GET /audit_logs/verify` and rendered by `IntegrityPanel.jsx`. It checks **both** `event.prev_hash != prev_hash` and `event.hash != expected`, so a single-row edit and a re-linked row both fail. |
| Every financial action is logged | **PASS** | Traced in §9: `complete_requested`, `final_validation`, `razorpay_order_creation`, `razorpay_order_created`, `payment_success`/`payment_failure`, `checkout_completed`/`checkout_declined`, plus `razorpay_token_charge`, `token_needs_reauth`, `token_charge_fallback`, `awaiting_authorization`, `payment_token_saved`. |
| Declines are logged with "Razorpay NOT called" | **PASS** | `_fail` writes it into `reasoning_summary` (l.654) and `create_checkout`'s decline branch does the same (l.364). This is a genuinely good forensic touch. |
| Every agent iteration is logged | **PASS** | `loop.py:483-490` emits `agent_{tool_name}` with `payload.agent_session_id`, args digest and result digest, `rules_fired` carrying the error code on failure. Plus `agent_reasoning_only` for nudges and `agent_outcome` at termination. |
| Operator actions are logged | **PASS** | `inventory_adjusted` (actor `operator`) with old→new values; `demo_reset`. |
| Chain survives declines and rollbacks | **PASS** | Because `ProtocolError` is raised outside `mutate()` on every checkout endpoint, decline events commit. A rollback would otherwise erase the very events explaining the rollback. |
| Tamper-evidence is cryptographic, not just structural | **PARTIAL** | The chain is unkeyed. Anyone with DB write access can delete a suffix and re-append a recomputed chain, and `verify_chain` will report `intact: true`. Detecting that requires an external anchor (a signed head, or an off-box copy of the latest hash). The PRD asks for a hash chain and gets one — I record this as a stated limitation rather than a missed requirement. |
| Audit records the amount actually charged | **FAIL** | See §12 — it records `cs.totals`, not the gateway amount. |
| `seq` and `prev_hash` are exposed to the judge | **PARTIAL** | `GET /audit_logs` returns `seq`, `hash` and `prev_hash` for every event. **The dashboard never renders any hash, `prev_hash`, or `seq` value as text** — `IntegrityPanel` shows only the boolean verdict and the count, and it is reachable only inside `SessionDetail` (`OpsView.jsx:264`), on demand. A judge who does not call the API by hand sees "intact ✓" and must take it on faith. |

**Section verdict: PASS on integrity, PARTIAL on presentation and on amount fidelity.**

---

## 18. Explainability Audit

| Check | Status | Evidence |
|---|---|---|
| Explanations are derived from audit facts, not generated | **PASS** | `explain_service.py` docstring: "Deterministic templating only — it never invents reasons." It is a `for ev in events` dispatch over `RULE_PHRASES` (5 entries) and `ACTION_SENTENCES` (13 entries). No LLM call. |
| Decline reasons map to the rule that fired | **PASS** | `failed_rules = [r.split(":")[0] for r in ev.rules_fired if r.endswith(":fail")]` → `RULE_PHRASES`, producing e.g. "the basket total exceeded the authorized spending limit". |
| Final-validation outcomes are distinguished from creation-time gating | **PASS** | Separate branches for `gating_evaluated` and `final_validation`, and `final_validation` distinguishes `declined` / `hold` / approved. |
| "Razorpay was NOT called" surfaces to the human | **PASS** | Emitted for `checkout_declined`. |
| Exposed in the UI | **PASS** | `GET /explain/{id}` is rendered at `OpsView.jsx:259,261`. |
| **The Merchant Agent's reasoning is explained** | **FAIL** | `ACTION_SENTENCES` contains **no `agent_*` key**. Every `agent_search_catalog`, `agent_find_alternative`, `agent_request_completion`, `agent_outcome` event is silently skipped by the final `elif ev.action in ACTION_SENTENCES` branch. So `/explain/{id}` describes the *deterministic control plane* and says nothing at all about the agent that drove it — for a submission whose headline feature is the Merchant Agent, the explainability layer omits the feature. |
| Agent reasoning is available somewhere | **PARTIAL** | Yes, at `GET /agent_sessions/{id}/trace`, rendered by `MerchantAgentTrace.jsx`. But `_require_session` reads the **in-memory** `SESSIONS` dict (`state.py:18`), so the trace endpoint **404s after a process restart** even though the underlying audit events are still in SQLite. The durable data exists; the endpoint that surfaces it does not survive a restart. |
| Displayed budget figures are truthful | **FAIL** | `MerchantAgentTrace.jsx:92` renders `{session.iterations} iterations · {session.tool_calls} tool calls`. `tool_calls` is `state.tool_call_count`, which is never incremented — so the UI reports **"0 tool calls"** for every session, including ones that made ten. `GET /health/agent` likewise advertises `max_tool_calls: 10`, a limit that is not enforced. |
| Free-text LLM strings are distinguishable from derived facts | **PARTIAL** | `finish`'s `reason_code` and `message` are LLM-authored and are written into `agent_outcome.reasoning_summary` alongside deterministically derived summaries, with no marker separating the two. A judge reading the trace cannot tell which sentences the model wrote and which the control plane derived. |

**Section verdict: PARTIAL** — the deterministic explainer is well built and genuinely non-hallucinating; it just does not explain the agent, and one displayed metric is always wrong.

---

## 19. Agenticity Classification

Tested against each candidate classification rather than argued toward a conclusion.

**Is it a CRUD-API?** No. `POST /agent_sessions` does not map one request to one persistence operation. A single call can run up to ten iterations, each choosing among nine tools, and can end in six different statuses. `_post_action` re-plans after failures — on `STOCK_UNAVAILABLE` it appends the failed pack to `state.excluded_pack_ids` and **returns `False` to continue the loop** (`loop.py:694-712`), so the next iteration's `find_alternative` will avoid it. That is state-carrying adaptation, not CRUD.

**Is it a SCRIPT?** No, in one mode; **effectively yes in the other**. With `MA_LLM_BRAIN=false`, `_fallback_decide` is a deterministic decision table and the "agent" is a scripted state machine. With `MA_LLM_BRAIN=true`, the tool sequence is genuinely chosen by `meta/llama-3.3-70b-instruct` at `temperature=0.2` from `[*TOOL_SCHEMAS, FINISH_TOOL]`, and different buyer phrasings produce different tool sequences. Both brains are live and interchangeable per request via the `brain` field. **Every test in the suite runs with `MA_LLM_BRAIN=false`** (`conftest.py`), so the scripted path is the only verified one — the agentic path is unverified by tests.

**Is it an ORCHESTRATOR?** Partly. It does orchestrate a fixed pipeline (search → check inventory → create → gate → complete). But an orchestrator does not choose *whether* to substitute, *which* substitute to offer, or *when* to stop trying — and this loop does, via `find_alternative` ranking plus `excluded_pack_ids` accumulation across iterations and buyer rejections (`resume`, l.523-525).

**Is it a GENUINE AGENT?** Not by the standard I would apply. Four things are missing:
1. **No memory across sessions.** `SESSIONS` is an in-memory dict; nothing is learned from one buyer to the next.
2. **No goal decomposition.** The goal is fixed by the endpoint contract ("fulfil this basket"); the agent selects tools, it does not restructure the objective.
3. **The tool-call budget — a defining bounded-autonomy control — does not operate.** `state.tool_call_count` is initialised at `state.py:62`, compared at `tools.py:703`, projected at `state.py:101` and `api/agent.py:66`, and rendered at `MerchantAgentTrace.jsx:92`. **It is never incremented anywhere in the repository.** The guard is dead and `TOOL_BUDGET_EXHAUSTED` (`loop.py:735-742`) is unreachable.
4. **Only one tool per iteration.** `msg.tool_calls[0]` is taken and the rest are answered with `{"skipped": "one action per turn"}` (`loop.py:183-190`). No parallel or compound action.

**Classification: LIMITED AGENT.**

Justified by what it *does* have: a real observe→reason→act→observe-result→adapt loop; an LLM that chooses among nine tools with no scripted sequence; adaptation on failure that changes subsequent behaviour (`excluded_pack_ids`); the ability to pause for human consent and resume with that consent incorporated; and a hard iteration bound that terminates safely (`unable_to_fulfill`, no payment). Justified as *limited* by: single-session memory, fixed goal, one action per turn, and a bounded-autonomy budget that is advertised in two places and enforced in none.

Credit where due: the mitigating fact is that **one tool per iteration means `ma_max_iterations = 10` bounds tool calls at 10 in practice**, which is the same number the dead budget intended. The system is not unbounded; the *specific control* is dead. That distinction matters and I state it explicitly rather than claiming runaway tool use is possible.

**Section verdict: LIMITED AGENT — honest for the submission, provided the claim is not stated as "bounded by iteration *and* tool-call budgets."**

---

## 20. Human Intervention Audit

| Intervention point | Exists? | Evidence |
|---|---|---|
| Human approves the basket before payment | **PASS** | `buyer_service/agent.py:522` `present_bill_and_pause`, docstring l.180: "deterministic pause (the LLM never triggers payment)". `BillCard.jsx` renders it. |
| Human authorises the actual charge | **PASS** | `RazorpayPayCard.jsx` opens the Razorpay modal; the human enters card details. On the `saved` driver's first purchase the modal is mandatory (`awaiting_payment_authorization`). |
| Human accepts/rejects a substitution | **PASS** | `SubstituteCard.jsx` offers three buttons — accept selected, "other", cancel — wired to `POST /agent_sessions/{id}/decision`. Rejection appends to `excluded_pack_ids`, so the human's "no" is durable within the session. |
| Human accepts/rejects a price change | **PASS** | `POST /checkout_sessions/{id}/confirm` with `accept_new_terms` and a matching `version`. Rejecting cancels and releases the reservation. |
| Operator can correct inventory | **PASS** | `PATCH /admin/packs/{id}/stock`, audited as `inventory_adjusted` with actor `operator` and old→new values. |
| Operator can inspect any transaction | **PASS** | `OpsView` → `SessionDetail`: event timeline, transaction, policy checks, payment, explanation. |
| Operator can reset demo state | **PASS** | "Danger zone" → `POST /admin/demo/reset`, audited as `demo_reset`. |
| Operator can resolve a stuck payment | **PASS** | `POST /checkout_sessions/{id}/reconcile` asks Razorpay for the truth; guarded to `payment_pending` only. Not exposed in the UI — API-only. |
| **Operator can stop a running agent** | **FAIL** | No abort, kill-switch, pause, or halt control exists — grep for `abort`/`kill_switch`/`halt` across `merchant_agent`, `buyer_service`, `buyer_b`, `dashboard/src` returns only unrelated docstrings about the *built-in* confirmation pause. `POST /agent_sessions` runs the loop **synchronously** inside the request, so there is no handle to cancel. Mitigation: the loop is bounded at 10 iterations and cannot pay outside `agent_completed` mode. |
| **Operator can reverse a completed payment** | **FAIL** | No refund path exists anywhere — no `refund` string in any source file, no Razorpay refund call. If C-1 or C-2 causes a wrong charge, the system has no remedy of its own. |
| Operator can revoke a delegation live | **FAIL** | `Delegation.status` is checked on every transaction, so revocation *would* take effect immediately — but there is no endpoint or UI to set it. It is reachable only by editing SQLite directly. |

**Section verdict: PARTIAL.** Human consent is well placed on the paths that matter (basket, charge, substitution, price change) and is enforced deterministically rather than by prompt. Post-hoc operator power is thin: no abort, no refund, no revoke.

---

## 21. Dashboard Audit

14 source files under `dashboard/src`. Two views: `ChatView` (buyer conversation, SSE-streamed from the buyer service) and `OpsView` (merchant operator).

| Check | Status | Evidence |
|---|---|---|
| Shows real backend data, not mocks | **PASS** | `api.js` proxies `/api/merchant`→:8000 and `/api/buyer`→:8001. No fixture or mock data file exists in `dashboard/src`. |
| Shows the audit trail | **PASS** | `SessionDetail` "Event timeline" from `GET /audit_logs?checkout_session_id=`. |
| Shows chain integrity | **PARTIAL** | `IntegrityPanel` renders the boolean from `GET /audit_logs/verify`. It shows **no hash, `prev_hash`, or `seq` value** even though the API returns all three, and it is nested inside `SessionDetail` — so a judge sees a green tick and must trust it. |
| Shows the gating decision | **PASS** | "Policy checks" panel from `gating_result.checks`. |
| Shows the deterministic explanation | **PASS** | "Explanation" panel from `GET /explain/{id}`. |
| Shows the Merchant Agent's reasoning | **PASS** | `MerchantAgentTrace` under the "Agent" tab: buyer request, status, outcome, pending proposal, iteration trace. |
| Agent metrics displayed are accurate | **FAIL** | Line 92: `{session.iterations} iterations · {session.tool_calls} tool calls`. `tool_calls` is always **0** (§19). The dashboard is the most likely place a judge encounters this number, and it is always wrong. |
| Live refresh | **PASS** | `setInterval(refreshSessions, 2000)`. |
| Payment state is honest | **PASS** | "Payment" panel shows driver, order id, payment id, saved card. `StatusPill`/`badge` render the real session status including `payment_pending`. |
| **Admin authentication** | **FAIL** | `api.js:12-14` hardcodes `'X-Admin-Token': 'admin_demo_token'` into the client bundle, and `config.py:41` defaults `admin_token` to that same literal, as does `.env.example:14`. Anyone who opens the bundle — or simply guesses the shipped default — holds operator authority, including `PATCH /admin/packs/{id}/stock` and `POST /admin/demo/reset`. There is no login. |
| Does the dashboard write anything financial? | **PASS** | Its only state-changing merchant calls are `payment_callback` (forwarding Razorpay's verbatim response) and `/admin/*`. It cannot set a price or a status directly. |

**Section verdict: PARTIAL.** The dashboard is genuinely wired to real backend state and shows the audit trail, gating and agent trace — which is more than most submissions manage. It undersells the hash chain, overstates the tool budget, and ships the operator token in the browser.

---

## 22. Test Suite Audit

**Correction to my own earlier count:** I previously reported 53 test functions across 9 files, derived from `.pytest_cache/v/cache/nodeids`. That was wrong — the cache accumulates stale ids and is not a census. Counting `def test_` directly in the source gives **67 test functions across 10 files**, with **no `parametrize`** (grep returns nothing), so 67 functions = 67 cases.

| File | Tests | What it actually proves |
|---|---|---|
| `test_agent_handoff.py` | 14 | Handoff integrity: invented `checkout_session_id` is structurally rejected; fake `run_gating` id rejected and audited; tool exposure is mode-aware; spend-limit rejection through the agent path; `payment_pending` surfaces as non-success. The strongest file in the suite. |
| `test_merchant_agent.py` | 12 | Happy path with payment proof; substitution proposed→accepted→completed; no-alternative clean decline; bounded-autonomy quantity rejection; buyer rejects first alternative; stock vanishing blocks payment; **iteration** budget exhaustion is safe; hallucinated ids validated; server-authoritative price/inventory; `request_completion` blocked in `buyer_completes`; audit chain intact after agent runs. |
| `test_gating.py` | 9 | All five rules, both directions, plus reserved-stock-reduces-availability, forged identity, unknown product/pack. |
| `test_saved_driver.py` | 7 | Mandate block on order creation; headless token charge contract; token-rejection classification; `auth_required`→`needs_reauth`; callback token resets to `active`; parks for authorization; **reuses an open order on retry**. |
| `test_audit_and_errors.py` | 5 | Critical events recorded; chain verifies; **tampering is detected**; explanation from facts; error envelope shape. |
| `test_state_machine.py` | 5 | Happy path states; completed returns existing result; declined cannot complete; cancelled is terminal; update requires live state. |
| `test_checkout_flow.py` | 4 | Price change → confirm → complete; rejecting terms cancels; update policy failure preserves the session; server-side price authority. |
| `test_inventory.py` | 4 | Reservation TTL frees stock; cancel releases; TOCTOU decline without payment; completion consumes stock. |
| `test_discovery_fallback.py` | 4 | Budget/keyword/quantity extraction and a deterministic end-to-end discover. |
| `test_idempotency.py` | 3 | Replay same result no double charge; completed-without-key returns existing result; create replay does not duplicate. |

**Gaps, stated as gaps rather than as failures of the tests that exist:**

| Missing coverage | Consequence |
|---|---|
| **No test calls `payment_callback` at all.** | C-1, C-2 and C-3 are all reachable only through that endpoint. The three most serious defects in the system sit in the one endpoint the suite never touches. `test_saved_driver.py:173` exercises the *token-saving side effect* of a callback via the service layer, not the endpoint's guards. |
| No test of the **tool-call** budget | Directly explains why the dead counter survived: `test_iteration_budget_exhaustion_is_safe` covers iterations only. A test asserting `tool_calls > 0` after a multi-tool run would have caught it immediately. |
| No test runs with `MA_LLM_BRAIN=true` | `conftest.py` pins the fallback brain. The LLM decision path — the headline feature — has zero automated coverage. This is a defensible choice (hermetic suite, no live NIM calls) but it must not be described as "the agent is tested." |
| No concurrency test | `grep threading\|concurrent merchant_agent/tests/` returns nothing, despite `pack_chicken_500` being seeded at stock 1 specifically to make such a test easy. The `RLock` in `db.mutate()` is unproven. |
| No schema-validation test | `protocol/schemas/` is never loaded by anything; `jsonschema` is not in `requirements.txt`. Guarantee 7 is documentation. |
| No revoked-delegation test | The `status != "active"` check is on the hot path and has never been executed. |
| No `/reconcile` test | The endpoint that resolves stuck money is untested. |
| No buyer-side tests | `buyer_service/` and `buyer_b/` have no test files (the only `test_*.py` outside `merchant_agent/tests/` are inside `.venv`). `buyer_b/client.py` is a self-asserting script, which is a reasonable substitute for an interoperability proof. |

**Execution status: NOT VERIFIABLE.** I could not run the suite. `.venv/` is a Windows virtualenv (`.venv/Lib/site-packages`) and cannot be used from this Linux environment; installing dependencies fails with `ProxyError('Cannot connect to proxy.', OSError('Tunnel connection failed: 403 Forbidden'))` → "Could not find a version that satisfies the requirement fastapi==0.115.6 (from versions: none)". Every statement above about the tests is read from source, not from a run. **I do not claim these 67 tests pass.**

**Section verdict: PARTIAL.** The suite is well targeted — it tests adversarial cases (hallucinated ids, forged identity, tampering, TOCTOU) rather than happy paths only, which is unusual and worth credit. Its blind spot is precisely aligned with the system's critical defects.

---

## 23. Security Audit

| Check | Status | Evidence |
|---|---|---|
| Secrets not committed | **PASS** | `.gitignore` has `.env` under "Secrets — NEVER commit"; `git ls-files` shows only `.env.example`. |
| Secrets not logged | **PASS** | No `print`/`logger` call in `payment_service.py`. `razorpay_key_secret` appears only where it is used: client auth (l.53), presence check (l.58), HMAC (l.188), direct API auth (l.300). |
| Key secret never leaves the merchant | **PASS** | Confirmed in §15. Only `key_id` (public by design) is returned in `payment.key_id`. |
| Payment signature verified | **PASS** | `verify_checkout_signature` — HMAC-SHA256 over `order_id|payment_id`. Comparison implementation should use `hmac.compare_digest`; recorded as P2 (timing side-channel on an HMAC an attacker cannot compute anyway without the secret). |
| Client cannot supply price or amount | **PASS** | §12. `payment_callback` accepts no amount field. |
| SQL injection | **PASS** | SQLAlchemy ORM throughout; no raw SQL string interpolation. |
| Input validation | **PASS** | Pydantic v2 DTOs on every checkout endpoint; `_items_from_args` bounds agent-supplied items (max 15, `q >= 1`, both ids required). |
| Mass assignment via LLM args | **PASS** | `_handler_params` intersection at `tools.py:720-723` structurally discards any key the handler does not declare. |
| Sim driver cannot run in production | **PASS** | `_reject_sim_outside_tests` — a Pydantic validator refusing `PAYMENT_DRIVER=sim` unless `ACG_TEST_ALLOW_SIM=1`, which `conftest.py` sets and nothing else does. Credited: this closes a real prior defect. |
| **Bearer token disclosed to unauthenticated readers** | **FAIL — C-5** | `GET /checkout_sessions/{id}` (no auth) returns `delegation_id` = the bearer token, alongside `buyer_agent_id`. `GET /audit_logs` (no auth) supplies the session ids to enumerate. Together they hand an anonymous caller everything `_check_ownership` and `validate_delegation` require. Per Rule 7 I am **not** penalising the mock/unsigned token design, which `protocol_spec.md:115-119` discloses; I am penalising the API echoing the credential. |
| **Unauthenticated privileged endpoints** | **FAIL** | `payment_callback` (C-2) and `reconcile` take no credential and perform no ownership check. `payment_callback` additionally performs no state check. |
| Admin surface effectively public | **FAIL** | `config.py:41` `admin_token: str = "admin_demo_token"`; `.env.example:14` ships the same value; `api.js:12-14` hardcodes it into the browser bundle. An operator who does not override `ADMIN_TOKEN` has a public control plane. Not escalated to CRITICAL: the admin surface cannot set a price, a status, or trigger a payment — its worst reachable effect is stock manipulation and demo reset. |
| CORS | **FAIL (P1)** | `main.py:38-39` `allow_origins=["*"]`, `allow_methods=["*"]`, commented "demo environment". With no cookie/credential auth there is no CSRF vector *per se*, but combined with C-5 any web page a user visits can enumerate sessions and harvest delegation tokens from their local merchant. |
| Rate limiting | **FAIL (P2)** | None anywhere — no `slowapi`, no limiter, no middleware. Acceptable for a prototype; recorded for completeness. |
| Audit log confidentiality | **FAIL (P1)** | `GET /audit_logs` is unauthenticated and returns `payload` for every event, including `buyer_agent_id` and `reasoning_summary`. It is also the enumeration primitive for C-5. |
| Error responses do not leak internals | **PASS** | `main.py`'s `Exception` handler returns a fixed message and logs the traceback server-side. |

**Section verdict: FAIL.** The cryptographic and injection-resistance fundamentals are sound and the secret hygiene is good. The authorization model is the weak axis: three privileged endpoints with no credential, and one of them echoes the credential itself.

---

## 24. PRD Requirement Matrix

| Requirement | Evidence | Status | Risk | Gap |
|---|---|---|---|---|
| Merchant Agent exists | `merchant_agent/agent/{loop,tools,state,prompts}.py`; 4 live endpoints under `/agent_sessions`; `MerchantAgent.run` drives up to 10 iterations | **PASS** | — | — |
| Merchant objective | `prompts.SYSTEM_PROMPT` states the merchant-side goal (fulfil within delegated authority, never silently substitute); enforced by tool availability, not by prose | **PASS** | — | Prompt says "8 iterations / 10 tool calls"; config is `ma_max_iterations = 10` — doc drift |
| Transaction state | `AgentState` (`state.py`) carries `checkout_session_id`, `excluded_pack_ids`, `iteration_count`, proposal, outcome; `public_view()` excludes the delegation token | **PASS** | — | Held in an in-memory `SESSIONS` dict — lost on restart |
| Observation | `_initial_context` from `public_view`; every tool result appended as a `role:"tool"` message (`loop.py:475-481`) | **PASS** | — | — |
| Reasoning | `_llm_decide` — NIM `meta/llama-3.3-70b-instruct`, `temperature=0.2`, tool-calling over `[*TOOL_SCHEMAS, FINISH_TOOL]`; `_fallback_decide` is a deterministic table | **PASS** | Low | No test runs with `MA_LLM_BRAIN=true` |
| Action selection | Model picks one tool per turn; extras answered `{"skipped": "one action per turn"}`; 3 no-tool nudges → `_Finish("declined", "agent_unable_to_progress")` | **PASS** | — | One action per turn only |
| Tool execution | `execute_tool` — allowlist, `UNKNOWN_TOOL` for anything else, kwargs bound by `_handler_params` intersection, normalised `{success, tool, result\|error}` | **PASS** | — | `run_gating` is in `FINANCIAL_TOOLS` but is a read |
| Feedback loop | Tool result → message history → audit `agent_{name}` → `_post_action` → next iteration | **PASS** | — | — |
| Adaptation | `STOCK_UNAVAILABLE` appends to `state.excluded_pack_ids` and returns `False` to keep adapting (`loop.py:694-712`); buyer rejection does the same in `resume` | **PASS** | — | — |
| Alternative fulfillment | `_tool_find_alternative` — two tiers, category/quantity/availability filtered, `GENERIC_TAGS` excluded from Tier 2, deterministic sort, top 5; read-only | **PASS** | — | Consumes `available_stock`, which discovery reports inconsistently |
| Structured proposal | `_tool_request_confirmation` builds the `kind/status/reason_code/original/proposal/message` object matching `protocol_spec.md:55-68` | **PASS** | — | — |
| Buyer confirmation | Agent pauses at `awaiting_buyer_confirmation`; `POST /agent_sessions/{id}/decision` resumes; `SubstituteCard.jsx` renders accept/other/cancel | **PASS** | — | — |
| Deterministic gating | `gating_service` — 5 pure rules, no LLM, invoked unconditionally at create (l.288), update (l.409) and complete (l.730) | **PASS** | — | `RULE_EXPIRY` fail branch unreachable (401 fires first) |
| Price authority | `_resolve_items` sets `unit_price = pack.price`; `_compute_totals` server-side; no DTO or tool schema accepts a price; re-checked at l.669 | **PASS** | — | Razorpay order amount never reconciled to session total (see Payment boundary) |
| Inventory authority | `available = actual − reserved`; strict double check at l.702-727; reservations released on every failure path | **PARTIAL** | Medium | `discovery_service.py:297` reports raw `stock_actual`, diverging from every other consumer |
| Delegated authority | `validate_delegation` at l.275/396/698 — binding, `status == "active"`, expiry; all 4 scope rules re-run at completion | **PARTIAL** | **High** | The token itself is returned by an unauthenticated `GET /checkout_sessions/{id}` (**C-5**) |
| Payment boundary | `payment_service.create_order` has exactly one call site; `merchant_agent/agent/**` contains no `razorpay` string and no `payment_service` import; `request_completion` schema has no properties | **FAIL** | **Critical** | Order amount never reconciled (**C-1**); `payment_callback` unauthenticated with no state check (**C-2**); `_finalize_payment` never calls `_assert_transition` (**C-3**) |
| Idempotency | `_idempotent_replay` + `store_complete_idempotency`; completed-session early return at l.625-627 returns the original `payment_id` | **PARTIAL** | **High** | Agent path generates `ma_*` keys that are **never stored** (`store_complete_idempotency` is called only from `api/checkout.py:113`); `payment_callback` has no idempotency at all — a replay double-decrements stock |
| Audit | `audit_service` SHA-256 chain (`hash = SHA256(canonical + prev_hash)`); `verify_chain` checks both links; declines commit because `ProtocolError` is raised outside `mutate()` | **PARTIAL** | Medium | Records `cs.totals` as the amount paid, not the gateway amount; unkeyed chain is re-forgeable with DB write access; `GET /audit_logs` unauthenticated |
| Explainability | `explain_service` — deterministic templating over `RULE_PHRASES` (5) and `ACTION_SENTENCES` (13); "never invents reasons"; surfaced at `/explain/{id}` and in `SessionDetail` | **PARTIAL** | Medium | **No `agent_*` action is in `ACTION_SENTENCES`** — the explainer says nothing about the Merchant Agent; `/trace` 404s after restart |
| Failure handling | 12 failure classes traced in §14; each produces committed state + audit events, not just an HTTP error; `payment_pending` refuses blind retry | **PASS** | Low | `except Exception: pass` at `api/checkout.py:244-245` is silent; the 500 message asserts "transaction not executed", which is not guaranteed |
| Agent independence | Zero `merchant_agent` imports in `buyer_service/**` or `buyer_b/**`; no DB or Razorpay secret on the buyer side; 3 processes; no buyer-identity branching in the merchant | **PASS** | — | — |
| Protocol compliance | `protocol_spec.md` — 7 guarantees, 13 endpoints all present, ladder order matches code exactly, 16 error codes all in `ErrorCode`, honest prototype disclosures | **PARTIAL** | Medium | Guarantee 1 and 4 not met (C-2, agent-path idempotency); Guarantee 7 unenforced (`jsonschema` absent); `payment_callback`, `reconcile` and all `/admin/*` undocumented |
| Dashboard | 14 files; real backend data via Vite proxy; event timeline, policy checks, payment, explanation, agent trace; 2s polling | **PARTIAL** | Medium | Always displays "0 tool calls"; never shows a hash value; `X-Admin-Token` hardcoded in the bundle |
| Testing | 67 test functions, 10 files, no parametrize; adversarial focus (hallucinated ids, forged identity, tampering, TOCTOU, budget exhaustion) | **NOT VERIFIABLE** | **High** | **Cannot be executed in this environment** (Windows venv + pip proxy 403). No test touches `payment_callback`; none runs the LLM brain; none tests concurrency, tool budget, revocation, `/reconcile`, or the schemas |

---

## 25. P0 / P1 / P2 Classification

### P0 — could invalidate the submission

| ID | Issue | Location | Why P0 |
|---|---|---|---|
| **C-2** | `payment_callback` has no ownership check, no state check, no idempotency, no amount check | `api/checkout.py:179-248` | Enables payment on a `cancelled` session, payment at a stale price on `requires_confirmation`, and a replay that double-decrements stock and appends a duplicate `payment_success`. Violates payment safety and merchant financial authority. |
| **C-3** | `_finalize_payment` never calls `_assert_transition` | `checkout_service.py:962-1044` | The state machine is the control that would have contained C-2. Its single call site is l.629, on `/complete` only. |
| **C-1** | Razorpay order amount is never reconciled against the session total, and `confirm_checkout` does not clear `razorpay_order_id` | `checkout_service.py:574-577` + `:785-787` | After a confirmed price increase, the retry guard reuses the order created for the **old, lower** amount. Goods ship underpaid and the audit trail records the higher figure. Payment with a manipulated/stale price. |
| **C-5** | Delegation token disclosed to unauthenticated readers | `checkout_service.py:174` + `api/checkout.py:95-98` + `api/audit.py:31` | `GET /audit_logs` enumerates session ids; `GET /checkout_sessions/{id}` returns `{buyer_agent_id, delegation_id}` — exactly the pair required to impersonate the buyer on `/complete`. Not a penalty on mock tokens (Rule 7); a penalty on echoing the credential. |
| **P0-5** | Tool-call budget is dead | `state.py:62`, `tools.py:703`, `loop.py:735-742` | `tool_call_count` is never incremented anywhere in the repository. Bounded autonomy is a core claimed Merchant Agent behaviour, and it is advertised by `/health/agent` and the dashboard while enforcing nothing. Mitigated (not fixed) by one-tool-per-iteration + `ma_max_iterations=10`. |
| **P0-6** | Agent completion path stores no idempotency record | `tools.py:609-612` vs `api/checkout.py:113-114` | `ma_*` keys are generated and passed but never persisted, so `_idempotent_replay` can never match them. Duplicate-charge safety on the agent path rests solely on the `completed` early return. |

### P1 — weakens interoperability, robustness, auditability, or judge understanding

| ID | Issue | Location |
|---|---|---|
| P1-1 | `POST /checkout_sessions/{id}/reconcile` is unauthenticated with no ownership check. Deliberately **not** escalated: it is gateway-truthful and guarded to `payment_pending`, so it cannot invent a payment. | `api/checkout.py:251-255` |
| P1-2 | `GET /audit_logs` is unauthenticated and returns full payloads including `buyer_agent_id`. | `api/audit.py:31` |
| P1-3 | Admin token defaults to `admin_demo_token`, ships in `.env.example`, and is hardcoded into the browser bundle. | `config.py:41`, `.env.example:14`, `dashboard/src/api.js:12-14` |
| P1-4 | `discovery_service.py:297` reports `available_stock` as raw `stock_actual`, contradicting `inventory_service.available_stock` and misleading the buyer's substitution filter. | `discovery_service.py:297` |
| P1-5 | Audit `payment_success` records `cs.totals`, not the amount Razorpay actually charged. | `checkout_service.py` `_finalize_payment` |
| P1-6 | `explain_service.ACTION_SENTENCES` has no `agent_*` entry — `/explain/{id}` never mentions the Merchant Agent. | `explain_service.py` |
| P1-7 | Dashboard displays "0 tool calls" for every agent session. | `MerchantAgentTrace.jsx:92` |
| P1-8 | `/agent_sessions/{id}/trace` 404s after a process restart because `SESSIONS` is in-memory, even though the audit events persist. | `agent/state.py:18`, `api/agent.py:146` |
| P1-9 | No test exercises `payment_callback`, the LLM brain, concurrency, revocation, `/reconcile`, or the JSON Schemas. | `merchant_agent/tests/` |
| P1-10 | `protocol/schemas/` is never loaded; `jsonschema` is not a dependency. Guarantee 7 is unenforced. | `protocol/schemas/`, `requirements.txt` |
| P1-11 | `protocol_spec.md`'s endpoint table omits `payment_callback`, `reconcile` and all four `/admin/*` endpoints. | `protocol/protocol_spec.md:27-42` |
| P1-12 | `CORSMiddleware(allow_origins=["*"], allow_methods=["*"])` — amplifies C-5 from a local browser. | `main.py:38-39` |
| P1-13 | `except Exception: pass` around token capture writes no audit event and no log, so a persistent `fetch_payment` failure is invisible. | `api/checkout.py:244-245` |
| P1-14 | No operator control to abort a running agent session, revoke a delegation, or refund a payment. | system-wide |
| P1-15 | The 500 handler asserts "transaction not executed", which is untrue if the crash occurs after `create_order`. | `main.py` |

### P2 — polish

| ID | Issue |
|---|---|
| P2-1 | `run_gating` is classified in `FINANCIAL_TOOLS` although it performs no write. |
| P2-2 | `SYSTEM_PROMPT` says "8 iterations / 10 tool calls"; config is 10 iterations. |
| P2-3 | HMAC comparison should use `hmac.compare_digest`. |
| P2-4 | No rate limiting on any endpoint. |
| P2-5 | `IntegrityPanel` shows only a boolean; the API returns `seq`, `hash` and `prev_hash` that are never displayed. |
| P2-6 | The audit chain is unkeyed — a suffix rewrite still verifies as intact. An external anchor (signed head) would close this. |
| P2-7 | LLM-authored `finish` text is stored in `agent_outcome.reasoning_summary` with no marker distinguishing it from derived facts. |
| P2-8 | `RULE_EXPIRY`'s fail branch is unreachable dead code that inflates the apparent rule count. |
| P2-9 | Reservation sweeping is lazy (on read) with no scheduler — adequate, but worth documenting. |

---

## 26. Original Track Audit

Setting the PRD aside and auditing only against: *"Build an agent that grows revenue for a merchant on Razorpay test-mode APIs, OR makes a merchant transactable by an AI buyer end-to-end. Every money action explainable, bounded and gated. Show the audit trail and one failure handled gracefully."*

**1. Can an AI buyer transact end-to-end?** **Yes.** `buyer_service` discovers, proposes, creates a checkout, presents a bill, pauses for human approval, and completes through the Razorpay path to a `completed` session with a real `payment_id`. Two independent buyer implementations do it.

**2. Is the merchant machine-readable?** **Yes.** `POST /discover` takes natural language and returns a structured basket proposal; `GET /checkout_sessions/{id}` returns authoritative machine state; every error is a stable `{code, message, retryable, details}` envelope with 16 documented codes.

**3. Is the merchant machine-transactable?** **Yes.** A client implementing only `protocol_spec.md` can complete a purchase — `buyer_b/client.py` does exactly that with no shared code, no LLM, and no knowledge of merchant internals.

**4. Is the Merchant Agent genuinely participating?** **Partly — LIMITED AGENT.** It runs a real observe→reason→act→adapt loop with an LLM choosing among nine tools, and it adapts across iterations via `excluded_pack_ids`. But its claimed tool-call budget does not operate, it takes one action per turn, it has no cross-session memory, and its LLM path has no test coverage.

**5. Is the Buyer Agent genuinely autonomous?** **Yes, within a deliberate boundary.** It selects packs, proposes substitutes, and drives the protocol on its own, but `buyer_service/agent.py:180` makes payment a *deterministic* pause — "the LLM never triggers payment". That is the correct design, not a limitation.

**6. Are both agents independent?** **Yes, structurally.** Zero merchant imports on the buyer side, no shared DB, no shared Razorpay secret, three separate processes, and no buyer-identity branching in the merchant. This is the project's strongest property.

**7. Is money explainable?** **Partly.** `/explain/{id}` is deterministic templating over audit facts and explicitly never invents reasons — including "Razorpay was NOT called" on declines, which is a real forensic touch. But it contains no sentence for any `agent_*` action, so the agent that drove the money is absent from the money explanation. And when C-1 fires, the explanation is confidently wrong about the amount.

**8. Is money bounded?** **Partly.** The delegation bounds (`max_order_value`, `allowed_categories`, `max_quantity_per_item`, expiry) are enforced deterministically at create, update **and** complete. The agent's *action* budget is bounded by iterations but not by the tool-call counter it advertises.

**9. Is money gated?** **Yes on the main path, no on the callback.** `/complete` runs the full 11-step ladder in the documented order before `create_order` is reached. `payment_callback` reaches `completed` with only an order-id match and an HMAC check.

**10. Is the audit trail visible?** **Yes.** Hash-chained, append-only, verifiable at `GET /audit_logs/verify`, and rendered as an event timeline in `OpsView`. Declines commit their own audit events because `ProtocolError` is raised outside `mutate()` — a deliberate and correct choice. Presentation undersells it: no hash is ever shown on screen.

**11. Is a failure handled gracefully?** **Yes — several.** The showcase case is TOCTOU stock loss: `scripts/simulate_stock_failure.py` drives it, the session declines with `STOCK_UNAVAILABLE`, reservations are released, `retryable: True` is returned, and the audit records "Razorpay NOT called". Price change → `requires_confirmation`, gateway timeout → `payment_pending` with no blind retry, and unchargeable token → three-way classification with modal fallback on the same order are all handled with committed state plus audit events.

**12. Is Razorpay actually used?** **Yes.** `payment_service` calls the real `razorpay.Client` in test mode; drivers are `saved` (default), `checkout` and `sim`, and `sim` is rejected outside pytest by a config validator. Real orders, real HMAC signature verification, real saved-card tokens with an E-Mandate block. **NOT VERIFIABLE in this environment** — I could not make a live API call — but the integration is real code against the real SDK, not a stub.

**13. Is human intervention required?** **Yes, at the right points.** Bill approval, card authorisation, substitution consent, price-change confirmation. Not required — and not possible — mid-agent-run.

**14. Can another buyer implementation use the merchant?** **Yes, demonstrated.** `buyer_b/client.py` — its own agent id and delegation, deterministic selection, and it asserts that replaying its `Idempotency-Key` returns the same `payment_id`, exiting non-zero if not.

**15. Is merchant authority preserved?** **Partly.** Price, inventory, order and payment state all live exclusively in the merchant, and no client or LLM can propose a price. But the merchant does not reconcile the amount its own gateway order carries (C-1), and it lets an unauthenticated caller complete a session (C-2) using a credential it disclosed itself (C-5).

**Track verdict: the track's primary ask is met.** A merchant is genuinely transactable by an AI buyer end-to-end on real Razorpay test-mode APIs, with a visible audit trail and multiple gracefully handled failures. The track's qualifier — *every* money action gated — is not met on one path.

---

## 27. Scorecard

| Dimension | Score | Reasoning |
|---|---|---|
| **PRD Implementation** | **78 / 100** | 25 requirements: 13 PASS, 10 PARTIAL, 1 FAIL, 1 NOT VERIFIABLE. Nothing is absent-and-unclaimed; the deductions are for controls that exist but do not operate (tool budget, schema validation, agent explainability) and for one subsystem reporting a different number than the rest (discovery stock). |
| **Merchant Agent Implementation** | **78 / 100** | The loop is real, the tools are well guarded, adaptation genuinely changes subsequent behaviour, and the LLM is structurally excluded from money. Deductions: the tool-call budget is dead, the LLM brain has zero test coverage, one action per turn, `run_gating` misclassified, and the agent completion path persists no idempotency record. |
| **Security / Financial Safety** | **52 / 100** | Three CRITICALs. The primary completion path is genuinely well built — an 11-step ladder in the documented order, a single `create_order` call site, deterministic gating outside the LLM's reach. The score is dragged down because a second, undocumented, unauthenticated path reaches the same privileged action with almost none of those controls, and because the credential needed to use the first path is published by the API itself. |
| **Agent-to-Agent Interoperability** | **88 / 100** | Structurally clean separation verified by grep, not convention; a second independent buyer proves the protocol; an honest spec that discloses its own prototype limits. Deductions: schemas unenforced, three privileged endpoints undocumented, Guarantees 1 and 4 not met in practice. |
| **Original Track Alignment** | **85 / 100** | 12 of 15 track questions are a clear yes. Real Razorpay test-mode integration, visible hash-chained audit trail, several gracefully handled failures with committed state. Deducted for "every money action gated" failing on the callback and for the agent's own reasoning being absent from the explainability layer. |

### Overall: **74 / 100**

**Weighting used (as recommended by the audit brief):**

| Dimension | Weight | Score | Contribution |
|---|---|---|---|
| PRD completeness | 25% | 78 | 19.50 |
| Merchant agency | 20% | 78 | 15.60 |
| Financial safety | 25% | 52 | 13.00 |
| Agent independence / interoperability | 15% | 88 | 13.20 |
| Track alignment | 15% | 85 | 12.75 |
| **Total** | **100%** | | **74.05 → 74** |

**Why this weighting rather than a flat average.** A flat average would give 76.2 — close enough that the difference looks cosmetic, but the reasoning behind it is not. Financial safety and PRD completeness carry a quarter each because this is a *payments* submission: a system that transacts money wrongly is worse than a system that transacts less. Merchant agency at 20% reflects that the Merchant Agent is the headline claim of this milestone. Interoperability and track alignment carry 15% each because both are already substantially satisfied — raising their weight would flatter the submission by over-counting its strongest dimensions.

Two things the arithmetic deliberately does *not* do. It does not zero the total for having CRITICALs: the primary path's controls are real and would still be real after the callback is fixed, and pretending otherwise would misrepresent the engineering that exists. And it does not round the financial-safety score up on the argument that the fixes are small — the fixes being small is a *remediation* fact, reported in §25 and §28.10, not a *current-state* fact. As of this commit, the defects are live.

---
