# FINAL TECHNICAL AUDIT — Agentic Commerce Gateway

**Repository:** `D:\Hackathons\rz-agent`
**Audit date:** 2026-08-23
**Auditor mandate:** verification against implementation evidence only. No redesign, no feature suggestions beyond fixes required to make existing claims true.
**Track:** AI Growth & Agentic Commerce — "Make a merchant transactable by an AI buyer end to end."
**Bar being audited against:** *every money action explainable, bounded and gated; show the audit trail and one failure handled gracefully.*

**Headline scores:** PRD conformance **74 / 100** · Track alignment **76 / 100**
**Critical money-safety gaps found:** **3** (C-1, C-2, C-3 in Part 8)  
**Additional critical submission risk:** **1** (C-4 — the entire Merchant Agent is uncommitted; see Part 8)

---

## PART 0 — AUDIT METHOD AND VERIFICATION ENVIRONMENT

### 0.1 What was actually done

Every Python file in the product surface was read in full, not sampled. Total product code is ~6,610 lines of Python across `merchant_agent/` (services, api, agent, models), `buyer_service/`, `buyer_b/`, `scripts/`, plus a React dashboard and a test suite. At that size, exhaustive reading was cheaper than sampling, so no finding in this document rests on a filename, a docstring, or a schema.

Specifically performed:

- Full read of all 5 service modules, all 6 API routers, all 4 merchant-agent modules, all 4 buyer-service modules, the ORM entity module, the DTO module, config, db bootstrap, both clients, the protocol spec, the test suite, and the dashboard API client.
- Call-site enumeration for every Razorpay entry point (`grep` for `razorpay`, `api.razorpay.com`, `httpx.post`, `create_order`, `charge_with_token`, `execute_sim_capture`, `reconcile_pending`, `verify_checkout_signature`).
- Call-site enumeration for every checkout-state mutation (`grep` for `cs.status =`, `\.status = "`, `_assert_transition`).
- Call-site enumeration for every gating invocation (`_gate(`, `evaluate(`, `run_gating`) and every audit write (`append_event(`).
- Direct inspection of the live SQLite database (catalog, delegations, checkout sessions, reservations, 46 audit events) and independent recomputation of the audit hash chain outside the application code.
- Git history inspection (3 commits), `git status`, `git ls-files` and `git show HEAD:` comparison of the committed tree against the working tree, plus secret scanning of tracked files. This last step produced C-4 and materially changed the assessment of `AUDIT_REPORT.md`.

### 0.2 What could NOT be executed, and why

Live execution was impossible in this environment. Three independent, verified causes:

1. **The committed virtualenv is Windows-only.** `.venv/pyvenv.cfg` has `home = C:\...\Python312`; the tree contains `_pydantic_core.cp312-win_amd64.pyd` and no `bin/` directory. It cannot be used from the Linux side.
2. **All outbound network access is blocked by an allowlist proxy.** DNS fails and `pypi.org` is unreachable (`X-Proxy-Error: blocked-by-allowlist`), so `pip install -r requirements.txt` cannot rebuild the environment, and no Razorpay or NVIDIA NIM call can be made.
3. **No offline wheel cache** exists in the repo to install from.

Consequence: **the server was never started, no endpoint was exercised, and `pytest` was never run during this audit.** Every item that depends on live behaviour is marked **NOT VERIFIABLE** and listed in Appendix A, with the exact Windows commands needed to close each one. Nothing was assumed to work because a test is named after it.

One artefact of a *previous local run* survives in `.pytest_cache`: **37 tests collected**, with one recorded failure — `test_terminal_completed_cannot_complete_again`. That name does not exist in the current tree. **Follow-up resolved this benignly:** `tests/test_state_machine.py:18-39` now contains `test_terminal_completed_returns_existing_result` and `test_terminal_declined_cannot_complete`, which assert the same invariants correctly (same `payment_id` on replay, exactly one order event, `409 INVALID_STATE_TRANSITION` on a declined session). The evidence points to a rename that fixed a mis-specified assertion, not a suppressed failure. Recorded here only for completeness; no adverse inference is drawn.

### 0.3 A note on the PRD

**There is no PRD document in the repository.** Requirement identifiers are referenced in code docstrings (`PRD §19, §29, §43`, `FR-MA-18/19`, `NFR-07`, `G2`, `G6`) but the document that defines them is not committed — only `README.md`, `AUDIT_REPORT.md` (the team's own self-audit) and `protocol/protocol_spec.md` exist.

Therefore, in Part 2 the requirement column is the **topic** for each identifier as named in the audit brief, reconstructed from the section headings plus what the code claims to satisfy. Where a verdict would turn on the PRD's exact wording rather than on observable behaviour, that is stated in the row. No PRD requirement text has been invented to make a verdict look better-founded than it is.

The missing PRD is itself a traceability gap: nothing in the repo lets a judge (or a future maintainer) check code against the requirement it cites.

---

## PART 1 — REPOSITORY AND ARCHITECTURE DISCOVERY

### 1.A What is actually an agent

Two components meet a defensible definition of agent — a goal, a tool surface, a loop, and observable adaptation to results it did not predict.

**The Buyer Agent** (`buyer_service/agent.py`, 731 lines) is a real LLM agent. It runs an NVIDIA NIM tool-calling loop against `meta/llama-3.3-70b-instruct` (`llm_loop:286-382`) over five tool schemas (`buyer_service/prompts.py:46-147`), and it adapts: when the merchant returns `STOCK_UNAVAILABLE`, the loop parses the failed line items out of the error, records them in `conv.declined_packs`, and deterministically proposes substitutes (`agent.py:532-559`, `_propose_substitutes:581-669`). When the merchant returns `PRICE_CHANGED`, it surfaces the new terms for a human decision (`agent.py:515-530`). It is not a chatbot with a single API call behind it.

**The Merchant Agent** (`merchant_agent/agent/loop.py`, 722 lines) is a real agent loop, and — importantly — it is agentic by *structure* rather than by the mere presence of an LLM. The docstring states the shape and the code implements it: observe → reason → act → observe result → adapt, with two interchangeable brains. `_llm_decide:131-188` asks NIM for exactly one action per turn (with a 3-nudge limit before it gives up and declines); `_fallback_decide:236-410` is a fully deterministic decider keyed off the last tool result. `_next_action:413-422` degrades LLM → fallback on any exception, so the loop survives NIM being down. `run:433-476` enforces an iteration budget and records every action. This is the strongest architectural decision in the project: **the same agent behaviour is reachable with the LLM switched off** (`ma_llm_brain`, `config.py:26`).

**What is not an agent, and is correctly not claimed as one:** the discovery LLM (`discovery_service._llm_parse:123-158`) is a single-shot natural-language-to-JSON parser at `temperature=0.0` that returns `None` on any failure and falls back to a deterministic keyword parser. It has no loop and no tools. The module docstring is accurate: *"LLM (NIM) is allowed ONLY to parse natural language into a structured query. Product matching, ranking and pricing are deterministic."*

### 1.B What is merely an API

Everything under `merchant_agent/api/` is a deterministic request/response surface with no agency: `checkout.py` (7 protocol routes plus callback and reconcile), `discovery.py`, `audit.py`, `admin.py`, `agent.py` (there is no catalog router — see A7). The Merchant Protocol itself is plain REST; the "agent-to-agent" framing lives one layer above it in `api/agent.py`, which is a thin transport over the loop.

`buyer_b/client.py` (132 lines) is deliberately *not* an agent — a raw `httpx` script with no LLM, existing to prove the protocol is implementable by an independent client.

### 1.C What is deterministic

All money-critical logic is deterministic, and this is the project's real substance:

- **Pricing** — `checkout_service._resolve_items:76-95`. Line 91 is the load-bearing line: `"unit_price": pack.price,  # authoritative price — never client-supplied`. No request DTO in `models/dto.py` accepts a price field at all, so client price injection is not merely rejected, it is *inexpressible*.
- **Totals** — `_compute_totals:98-108` (shipping waived at `free_shipping_threshold`, tax zero).
- **Gating** — `gating_service.py`, five pure functions, no LLM, no I/O beyond a stock read: `delegation_expiry`, `category_allowed`, `quantity_limit`, `spend_limit`, `stock_check` (lines 39-111).
- **Delegation validation** — `delegation_service.validate_delegation:15-31`.
- **Reservations** — `inventory_service.py`, TTL-based, with `available_stock = stock_actual − Σ active reservations` (46-48).
- **State machine** — `VALID_TRANSITIONS` table plus `_assert_transition` (`checkout_service.py:37-45`, `206-213`).
- **Idempotency** — `Idempotency-Key` header with full response-snapshot replay.
- **Audit chain** — SHA-256 `hash = sha256(canonical_content + prev_hash)` (`audit_service.py:59`).
- **Payment execution** — every Razorpay call originates in `payment_service.py`; no LLM output reaches it.

### 1.D What is LLM-driven

LLMs are used for natural-language understanding, action *selection*, and explanation text: buyer intent parsing and tool choice; buyer-facing prose; the merchant agent's choice of which allowlisted tool to call next; discovery query parsing. None of them set a price, evaluate a policy, or move money.

One exception must be named, because it is a real finding rather than a design choice: the merchant agent's terminal outcome string is LLM-authored and **trusted without verification**. `_apply_finish:687-701` accepts `finish(outcome="completed")` and calls `_terminate(state, "completed", ...)` with no check that a payment ever occurred, and the tool schema advertises `"completed: paid/fulfilled."` (`prompts.py:32-57`). The buyer then maps that status to `"The order is PAID and placed"` (`buyer_service/agent.py:184-195`). No money moves — but a false success is claimable by the model. See H-7.

### 1.E Where state is stored

Authoritative state is SQLite at `data/merchant.db` in WAL mode (`db.py:37-43`), holding catalog, delegations, checkout sessions, reservations, payment methods, idempotency keys and the audit chain. Mutations are serialised process-wide by a `threading.RLock` and wrapped in `db.mutate()`, which commits on success and rolls back on exception (`db.py:48`, `51-63`).

Two volatile stores exist. Merchant-agent sessions live in a plain in-memory dict (`agent/state.py:18`) and are lost on restart — acceptable for a demo, but it means an agent session that paused for buyer confirmation cannot survive a server restart, while its financial effects (the `CheckoutSession`) do. Buyer conversation state is likewise in-memory in `buyer_service`.

**Money state is DB-only.** No financial fact is stored solely in an agent's memory.

### 1.F Where financial authority lives

Financial authority is server-side and narrow. Every outbound Razorpay call originates in exactly one module, `merchant_agent/services/payment_service.py`: `create_order:52-70`, `execute_sim_capture:73-98`, `reconcile_pending:109-143`, `verify_checkout_signature:146-155`, `ensure_customer:165-178`, `charge_with_token:192-281`.

There are exactly **five money entry points** into that module:

| # | Entry point | Route / caller | Reaches Razorpay via |
|---|---|---|---|
| 1 | `POST /checkout_sessions/{id}/complete` | `api/checkout.py:101-136` | `checkout_service.complete_checkout:596-876` |
| 2 | `POST /agent_sessions` in `agent_completed` mode | `api/agent.py:91-114` → `loop.run` → `tools._tool_request_completion:595-667` | same `complete_checkout` |
| 3 | `POST /agent_sessions/{id}/decision` | `api/agent.py:122-139` → `loop.resume:478-512` | same `complete_checkout` |
| 4 | `POST /payment_callback` | `api/checkout.py:179-239` | `finalize_with_outcome:892` + best-effort capture `209-236` |
| 5 | `POST /checkout_sessions/{id}/reconcile` | `api/checkout.py:242-246` | `reconcile_session:981-993` |

Paths 1–3 converge on the same guarded ladder, which is the correct design. Paths 4 and 5 do **not** go through that ladder, and path 4 is the source of critical finding C-2.

Nothing in `buyer_service/` or `buyer_b/` imports `payment_service`, `razorpay`, any merchant model, or any merchant service. Verified by import inspection: `buyer_service/tools.py` is pure `httpx` against the protocol base URL, and its docstring — *"the ONLY way the buyer touches the merchant. No merchant internals, no DB, no Razorpay"* — is accurate.

---

## PART 2 — REQUIREMENT VERIFICATION (SECTIONS A–V)

**Reading note.** As stated in 0.3, the PRD is not in the repository. The *Requirement* column below is the topic for each identifier, reconstructed from the audit brief's section headings and from what the code itself claims to satisfy. Verdicts are assigned on observable implementation only. `PASS` means the production runtime path enforces or demonstrates the behaviour. `PARTIAL` means it exists but is incomplete, inconsistent, or reachable-around. `FAIL` means it is absent or broken in the runtime path. `NOT VERIFIABLE` means the code looks right but requires a live service (Razorpay, NIM) or a running server to confirm, and this environment could not provide one.

### Section A — Catalog (A1–A8)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| A1 | A realistic grocery catalog exists | **PASS** | `merchant_agent/seed/catalog.json` — 45 products, 74 packs, 9 categories. Real brands and descriptions, not lorem. |
| A2 | Products carry a category taxonomy | **PASS** | Categories: `bakery, beverages, dairy, household, meat_eggs, produce, snacks, staples, tobacco`. `tobacco` exists specifically so category gating has something to block. |
| A3 | Multiple pack sizes per product with distinct prices | **PASS** | e.g. `prod_milk_amul` → `pack_milk_amul_500` (₹32) and `pack_milk_amul_1l` (₹60), `catalog.json`. |
| A4 | Prices are merchant-owned, stored server-side | **PASS** | `Pack.price` column (`models/entities.py`); resolved server-side at `checkout_service._resolve_items:91`. No price field exists in any request DTO. |
| A5 | Stock is tracked per pack | **PASS** | `Pack.stock_actual`; seeded from `pack.stock`. |
| A6 | Catalog is reproducibly seedable | **PASS** | `scripts/seed_catalog.py` + `seed/catalog.json`; also re-seeded by `POST /admin/demo/reset` (`api/admin.py:76-95`). |
| A7 | Buyer reaches the catalog only through the protocol | **PASS** | There is **no** catalog-browse endpoint at all; the only read path is `POST /discover`. `buyer_service` never imports a merchant model. |
| A8 | Catalog metadata supports matching (tags, brand, synonyms) | **PASS** | `tags[]` and `brand` per product; `discovery_service.SYNONYMS:26-61` includes Hindi aliases. |

Observation, not a defect: because there is no catalog listing endpoint, a judge cannot browse the merchant's inventory directly — they see it only through discovery results. If a demo wants to show "the merchant has a real catalog", that has to be shown via `/discover` or the dashboard.

### Section B — Discovery (B1–B9)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| B1 | Natural-language query accepted | **PASS** | `POST /discover` (`api/discovery.py:1-28`), `DiscoverRequest.query`. |
| B2 | LLM used only to parse NL → structured query | **PASS** | `discovery_service._llm_parse:123-158`, `temperature=0.0`, returns `None` on any failure. Module docstring states the constraint and the code honours it. |
| B3 | Deterministic fallback when the LLM is unavailable | **PASS** | `_fallback_parse:90-120` (stopwords, 5-char prefix matching "to avoid collisions like breast/bread"); `skip_llm_parse` flag forces it. Covered by `tests/test_discovery_fallback.py` (4 tests). |
| B4 | Matching/ranking/pricing deterministic, never LLM | **PASS** | `_score_product:164-192`, `_pick_pack:195-199`, `_match_keyword:202-234`. Price comes from the pack row. |
| B5 | Budget constraint extracted from NL | **PASS** | `BUDGET_RE:63-67`, applied in `discover:237-319`. |
| B6 | Unmatched items reported rather than hallucinated | **PASS** | `discover` returns `unmatched[]`; no invented SKUs possible since packs are looked up by id. |
| B7 | Exclusions supported so a buyer can re-discover after a decline | **PASS** | `exclude_pack_ids`, `deprioritize_product_ids`, `exclude_product_ids` (`buyer_service/tools.py:23-43` → `discovery_service.discover`). This is what makes substitution work. |
| B8 | Discovery is audited | **PASS** | `discovery_requested` (`discovery_service.py:247`) and `discovery_result_generated` (`:308`), persisted inside `mutate()` (`api/discovery.py:15`). |
| B9 | Discovery never initiates payment or reserves stock | **PASS** | No `payment_service`, no `create_reservations`, no status mutation anywhere in `discovery_service.py`. |

Discovery is the cleanest subsystem in the project: the LLM boundary is exactly where the docstring says it is, and the fallback is real.

### Section C — Buyer Agent (C1–C15)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| C1 | Buyer is an LLM agent, not a script | **PASS** | `buyer_service/agent.py:llm_loop:286-382` — NIM `meta/llama-3.3-70b-instruct` tool-calling loop with 5 schemas (`prompts.py:46-147`). |
| C2 | Buyer has a bounded tool surface | **PASS** | Exactly 5 tools: `discover_products`, `create_checkout_session`, `cancel_checkout_session`, `purchase_via_merchant_agent`, `respond_to_merchant_proposal`. |
| C3 | Buyer holds no merchant DB access | **PASS** | `buyer_service/tools.py` is pure `httpx`; no SQLAlchemy import anywhere under `buyer_service/`. |
| C4 | Buyer holds no Razorpay credentials or API access | **PASS** | No `razorpay`, no `api.razorpay.com`, no key material under `buyer_service/`. Only the public `key_id` arrives inside a protocol response for the browser modal. |
| C5 | Buyer cannot express or influence a price | **PASS** | No price field in `create_checkout_session` schema; server re-resolves regardless (`_resolve_items:91`). |
| C6 | Buyer cannot invoke payment from LLM reasoning | **PASS** | `complete_checkout` is **absent from the LLM tool registry**; it is only reachable from `_complete_flow:436-572`, called after an explicit human approval, and `start_agent_session` hardcodes `execution_mode="buyer_completes"` (`tools.py:129`). |
| C7 | Human approval gate before money moves | **PASS** | `BILL_PAUSE` sentinel (`agent.py:17`), `present_bill:393-410`, `resume_after_decision:412-434`. The loop parks; it cannot self-approve. |
| C8 | Buyer adapts to stock failure | **PASS** | `agent.py:532-559` parses failed items from the `STOCK_UNAVAILABLE` error, records `declined_packs`, calls `_propose_substitutes:581-669` (deterministic). |
| C9 | Buyer adapts to price change | **PASS** | `agent.py:515-530` handles `PRICE_CHANGED` by re-presenting terms for confirmation. |
| C10 | Buyer handles hard declines with a human explanation | **PASS** | `agent.py:561-572` maps decline codes to friendly text. |
| C11 | Buyer handles `payment_pending` | **PASS** | `_complete_flow` routes `payment_pending` → `reconcile` (`agent.py`, pending branch). |
| C12 | Buyer maintains conversation/basket state across turns | **PASS** | `buyer_service/conversations.py`, `conv.declined_packs` tracking (`agent.py:96-116`). |
| C13 | Buyer must never claim success without merchant evidence | **FAIL** | The prompt says exactly this — *"NEVER assume an order is complete unless a tool result says status 'completed'"* (`prompts.py:41`) — but `_map_agent_result:184-195` returns `"The order is PAID and placed. Tell the user it succeeded with the total."` purely on the **merchant agent's** `status == "completed"` string, which `loop._apply_finish:687-701` will set on the LLM's word without verifying payment. A prompt instruction plus a status string is not evidence of payment. See H-7. |
| C14 | Buyer validates ids it passes to the merchant | **PARTIAL** | Client-side pack allowlist at `agent.py:127-139` is a courtesy check; the real enforcement is server-side (`tools._load_pack:83-92`, `_resolve_items`). Correct order of trust, so the risk is low. |
| C15 | Buyer-side debug output does not leak into production behaviour | **PASS** | `agent.py:327-331` writes unconditionally to `buyer_service_debug.log` (38 KB present locally). **Correction on re-check:** `.gitignore` lists both `*.log` and `buyer_service_debug.log` explicitly, so it is *not* shipped. No credentials are written. Unconditional local logging is a minor hygiene note, not a finding. |

### Section D — Merchant Agent (D1–D10)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| D1 | Merchant agent runs an observe→reason→act→adapt loop | **PASS** | `agent/loop.py:MerchantAgent.run:433-476`; `_post_action:590-685` feeds results back and continues looping (e.g. `STOCK_UNAVAILABLE` recovery returns `False` to keep going). |
| D2 | Two interchangeable brains (LLM and deterministic) | **PASS** | `_llm_decide:131-188`, `_fallback_decide:236-410`, selected by `settings.ma_llm_brain` and degraded automatically on exception at `_next_action:413-422`. |
| D3 | Bounded tool allowlist | **PASS** | 12 tools; `execute_tool:689-755` rejects anything not in the registry. |
| D4 | Tools classified by privilege | **PASS** | `READ_TOOLS:32-39`, `WRITE_TOOLS:40`, `FINANCIAL_TOOLS = {"run_gating", "request_completion"}:41`. |
| D5 | Iteration and tool-call budgets enforced server-side | **PASS** | `ma_max_iterations=8`, `ma_max_tool_calls=10` (`config.py:27-28`); enforced in `run:433-476` (→ `unable_to_fulfill`) and `execute_tool` (`TOOL_BUDGET_EXHAUSTED`). Enforcement is in code, not only in the prompt. |
| D6 | Agent cannot alter policy outcomes | **PASS** | `_tool_run_gating:485-525` is an explicitly read-only dry run — *"authoritative policy evaluation; cannot be modified by the agent"* — and the authoritative re-run happens inside `complete_checkout:730-739` regardless of what the agent saw. |
| D7 | Every agent action is audited | **PASS** | `_audit:48-79` writes with its own short-lived `mutate()`; `run` emits `agent_<tool_name>` events per action. Deliberately swallows audit exceptions so logging cannot kill a loop mid-payment — a defensible trade-off, though it means an audit write can silently fail. |
| D8 | Concurrency protection per agent session | **PASS** | `session_busy_lock` → HTTP 409 (`api/agent.py:107-113`, `132-138`). |
| D9 | Structured proposal / buyer-decision pause | **PASS** | `awaiting_buyer_confirmation` + `POST /agent_sessions/{id}/decision` (`api/agent.py:122-139`), `resume:478-512`, `_accept_substitution:529-549`, `_reject_terms:576-588`. |
| D10 | Reconstructable decision trace | **PASS** | `GET /agent_sessions/{id}/trace` rebuilds from hash-chained audit events including `prev_hash`/`hash` (`api/agent.py:142-163`). |

Two defects sit inside an otherwise strong module:

- **`_apply_finish:687-701` trusts the LLM's terminal claim.** The code coerces the outcome to one of three strings and then, for `"completed"`, terminates the session as completed with no check that any `CheckoutSession` reached `completed` or that any payment id exists. See H-7 — this is the single most demo-dangerous LLM-trust issue in the project.
- **`_confirm_terms:551-574` calls `checkout_service.confirm_checkout` directly** inside its own `mutate()`. That is the same function the protocol route calls, so authority is not bypassed — but it means the agent module reaches into the service layer rather than going through the protocol, which weakens the "agent-to-agent over a protocol" story internally.

### Section E — Merchant Commerce Protocol (E1–E8)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| E1 | A written protocol spec exists | **PASS** | `protocol/protocol_spec.md` plus JSON schemas in `protocol/schemas/`. |
| E2 | Protocol covers the full transaction lifecycle | **PASS** | 7 routes in `api/checkout.py`: create, update, get, complete, confirm, cancel (+ callback, reconcile). |
| E3 | Structured error envelope on every failure | **PASS** | `main.py:59-92` — `ProtocolError`, validation, and catch-all handlers all emit `{error:{code,message,retryable,details}}`. The 500 handler's message is well chosen: *"Internal merchant error; transaction not executed"*. |
| E4 | Stable machine-readable error codes | **PASS** | `ErrorCode` enum in `models/dto.py`, used consistently (`STOCK_UNAVAILABLE`, `PRICE_CHANGED`, `SPEND_LIMIT_EXCEEDED`, `CATEGORY_BLOCKED`, `QUANTITY_LIMIT_EXCEEDED`, `DELEGATION_EXPIRED`, `UNAUTHORIZED_AGENT`, `INVALID_STATE_TRANSITION`, `CONFIRMATION_STALE`, `PAYMENT_FAILED`, `PAYMENT_PENDING`). |
| E5 | Spec matches the implemented surface | **PARTIAL** | The endpoint table in `protocol_spec.md` **omits `/payment_callback` and `/reconcile`** — the two routes that mutate money state outside the guarded ladder. A client implementing only the spec would not know they exist, and a judge reading only the spec would not audit them. |
| E6 | Spec guarantee: terminal sessions never execute payments | **FAIL** | Guarantee #1 in the spec is violated in code: `payment_callback` (`api/checkout.py:179-239`) has no session-status guard, and `_finalize_payment:896-978` sets `completed` with no transition check. See C-2/C-3. |
| E7 | Spec guarantee: every error carries `details` | **PARTIAL** | Guarantee #5 is not upheld on the flagship failure path — `_fail` emits `"details": {}` (`checkout_service.py:661`), as do the payment error envelopes (`:969`, `:976`). Structurally present, substantively empty. |
| E8 | Protocol responses are JSON matching the published schemas | **NOT VERIFIABLE** | Response models are typed Pydantic DTOs so shape conformance is likely, but no server could be started to diff actual responses against `protocol/schemas/`. |

### Section F — Agent Independence (F1–F11)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| F1 | Buyer and merchant are separate processes | **PASS** | Two FastAPI apps: `merchant_agent/main.py` (port 8000) and `buyer_service/main.py`. |
| F2 | Buyer has no import of merchant code | **PASS** | Verified by inspection of every import in `buyer_service/*.py` — only `httpx`, stdlib, `openai` SDK, and its own modules. |
| F3 | Buyer has no database access | **PASS** | No SQLAlchemy, no `merchant.db` reference under `buyer_service/`. |
| F4 | Buyer cannot read merchant inventory internals | **PASS** | Only `available` counts that the merchant chooses to return in discovery/session payloads. |
| F5 | Buyer cannot call gating functions | **PASS** | `gating_service` is not importable or reachable from the buyer; there is no HTTP route that runs gating on demand for the buyer. |
| F6 | Buyer cannot read merchant policy internals | **PARTIAL** | Delegation limits are not exposed to the buyer, but decline reasons legitimately leak policy shape (e.g. `SPEND_LIMIT_EXCEEDED`). That is intended and desirable for explainability. |
| F7 | Buyer holds no Razorpay credentials | **PASS** | See C4. `.env` keys are read only by `merchant_agent/config.py`. |
| F8 | Buyer cannot call Razorpay directly | **PASS** | No Razorpay host string anywhere under `buyer_service/` or `buyer_b/`. |
| F9 | Communication is over the protocol only | **PASS** | `buyer_service/tools.py` — every method is an `httpx` call to `settings.merchant_base_url`. |
| F10 | Merchant does not depend on the buyer's implementation | **PASS** | Merchant has no import of `buyer_service`; `buyer_b` proves the point independently. |
| F11 | Delegation token never enters LLM context | **PASS** | `agent/state.py:46` — `delegation_token: str  # server-side only; never exposed to the LLM`; `public_view:88-103` omits it; the LLM-visible observation is built from `public_view`. This is a genuinely careful piece of work. |

Agent independence is the requirement this project satisfies most convincingly. There is no path by which the buyer touches merchant internals, and the separation is structural rather than conventional.

### Section G — Delegated Authority (G1–G11)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| G1 | Delegation tokens exist and are persisted | **PASS** | `Delegation` entity; tokens `dlg_*` seeded by `scripts/seed_catalog.py:seed_delegations`. |
| G2 | Buyer authority is scoped, not unlimited | **PASS** | Columns: `max_order_value`, `allowed_categories`, `max_quantity_per_item`, `expires_at`, `active`, `buyer_agent_id`. |
| G3 | Token validated on every authority-bearing call | **PASS** | `validate_delegation` called in `create_checkout:275`, `update_checkout:396`, and again inside `complete_checkout:696-700`. |
| G4 | Unknown / mismatched / inactive token rejected | **PASS** | `delegation_service.py:15-31` → 401 `UNAUTHORIZED_AGENT`. |
| G5 | Expired token rejected | **PASS** | Same function → 401 `DELEGATION_EXPIRED`; also a gating rule (`gating_service.py:39+`). |
| G6 | Token bound to a specific buyer agent id | **PASS** | Mismatch between `buyer_agent_id` and the token's owner is a 401. |
| G7 | Session ownership enforced on mutation | **PASS** | `_check_ownership:197-203` compares **both** `buyer_agent_id` and `delegation_id` → 403; called by update, cancel, confirm, complete. |
| G8 | Session ownership enforced on read | **FAIL** | `GET /checkout_sessions/{id}` (`api/checkout.py:95-98`) has **no** ownership check and no credentials at all. Any party with a session id reads the full basket, totals, gating result and payment ids. Information disclosure, not money movement. |
| G9 | Authority re-validated at payment time, not just at creation | **PASS** | `complete_checkout:696-700` re-validates the delegation after the price re-check and before Razorpay. |
| G10 | Delegation limits enforced server-side, never by prompt | **PASS** | All five limits are evaluated in `gating_service.py` (pure Python) and re-run at `complete_checkout:730-739`. No prompt text participates. |
| G11 | Delegation is revocable | **PARTIAL** | `entities.py:46` declares `status` with the comment `# active \| revoked`, and `validate_delegation:15-31` correctly rejects anything that is not `active`. But a repository-wide search finds no code that ever writes `revoked` — no endpoint, no script, no admin route. Revocation is a manual DB edit. |

### Section H — Deterministic Gating (H1–H6)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| H1 | Gating is deterministic, no LLM | **PASS** | `gating_service.py` — 5 pure functions, lines 39-111. No model call, no randomness, no clock dependence except expiry. |
| H2 | All five policy dimensions implemented | **PASS** | `delegation_expiry`, `category_allowed`, `quantity_limit`, `spend_limit`, `stock_check`, evaluated in that source order; `GatingResult.passed = all(...)` (`:113`). |
| H3 | Gating runs at session creation | **PASS** | `create_checkout:288`; declines set `status="declined"` and a `decline_code` (`:343-344`) and never reach Razorpay (`:364` — *"Create declined: …; Razorpay NOT called"*). |
| H4 | Gating re-runs immediately before payment | **PASS** | `complete_checkout:730-739`, after price, delegation and stock re-checks and **before** any `payment_service` call at `:755+`. This ordering is correct and is the project's strongest money-safety property. |
| H5 | Gating runs on basket update | **PASS** | `update_checkout:406`; on failure the **previous basket and reservation are preserved** (`:416-431`) rather than half-applied — a good defensive choice. |
| H6 | First failing rule determines the error code | **PASS** | `first_failure_code:116-120` returns the first failing rule in check order; mapped to HTTP by `_declined_at_create:49-72`. |

Gating re-runs on **create**, **update** and **complete**. It does **not** re-run on **confirm** (`confirm_checkout:569-577` re-resolves prices and returns the session to `requires_payment` without calling `_gate`). That is tolerable only because `complete_checkout` re-gates before paying — but it means the session can sit in `requires_payment` in a state that policy has never approved. Combined with C-1 that becomes materially worse.

### Section I — Price Integrity (I1–I8)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| I1 | Client can never supply a price | **PASS** | No price field in any request DTO; `_resolve_items:91` always takes `pack.price`. Injection is inexpressible, not merely rejected. |
| I2 | Totals computed server-side | **PASS** | `_compute_totals:98-108`. |
| I3 | Price re-checked at payment time | **PASS** | `complete_checkout:668-694` re-resolves from the catalog and compares against stored totals. |
| I4 | Price change forces buyer re-confirmation | **PASS** | On mismatch the session moves to `requires_confirmation` and `PRICE_CHANGED` is returned (`api/checkout.py:101-136` maps it to 409). |
| I5 | Confirmation is version-bound to prevent stale accepts | **PASS** | `confirm_checkout:527-544` → `CONFIRMATION_STALE` if `version != cs.version`. |
| I6 | The amount charged equals the amount the buyer approved | **FAIL** | **This is critical finding C-1.** `complete_checkout:759-763` reuses an existing `cs.razorpay_order_id` whenever `status == "requires_payment"`, **with no comparison against the freshly computed `total_rupees`**. Because neither `update_checkout` nor `confirm_checkout` ever clears `razorpay_order_id`, the order that gets authorised can carry an amount from a previous basket. Full trace in Part 3.2. |
| I7 | The authorised amount is persisted for later comparison | **FAIL** | `models/entities.py` has `razorpay_order_id` and `razorpay_payment_id` but **no order-amount column**, so the merchant cannot detect the C-1 mismatch even after the fact. Compounding this, `serialize_session:150` reports `payment.amount` as the *session total*, not the amount actually sent to Razorpay — so the API, the dashboard and any judge reading the response are shown a number that may never have been authorised. |
| I8 | Price authority covered by tests | **PARTIAL** | `tests/test_checkout_flow.py:90-96` `test_price_authority_server_side` is a genuine assertion (its comment — *"request contains NO price fields at all"* — is accurate). But no test covers I6/I7: there is no test that a re-priced session cannot pay against a stale order. |

### Section J — Inventory and Reservation (J1–J10)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| J1 | Stock held per pack, server-side | **PASS** | `Pack.stock_actual`. |
| J2 | Reservations exist with a TTL | **PASS** | `inventory_service.create_reservations:51-66`; `reservation_ttl_minutes=10` (`config.py:23`). |
| J3 | Effective availability accounts for active reservations | **PASS** | `available_stock:46-48` — `stock_actual − Σ active reservations`. |
| J4 | Reservation created when a session passes gating | **PASS** | `create_checkout:313`. |
| J5 | Reservation released on cancel | **PASS** | `cancel_checkout:488`. |
| J6 | Reservation released on decline | **PASS** | `_fail:635` and `_finalize_payment:941`. |
| J7 | Reservation replaced on basket update | **PASS** | `update_checkout:433-434` — release then recreate, inside one locked transaction. |
| J8 | Reservation consumed on successful payment | **PASS** | `_finalize_payment:907` + `stock_actual` decrement at `:906`. |
| J9 | Expired reservations swept | **PASS** | `sweep_expired:23-33`. |
| J10 | Reservation lifecycle is auditable | **PARTIAL** | `inventory_service.py` contains **zero** `append_event` calls. Reservation events are audited only indirectly, by the caller (`reservation_created` at `create_checkout:325-333`). Release, consume, expire and sweep produce **no audit record at all**, so the audit trail cannot explain why availability changed. For a project whose thesis is "every money action explainable", inventory is the weakest link in the trail. |

Design note worth crediting: reservations deliberately do **not** override actual stock, and `complete_checkout:702-727` re-checks **both** (`pack.stock_actual < qty or effective_available < qty`, line 717, with the comment *"A reservation does not override reality"*). That is what makes the TOCTOU stock-failure demo real rather than staged.

One genuine defect in the same area: `_finalize_payment:906` uses `max(0, pack.stock_actual - item["quantity"])`. This prevents negative stock, but it also **silently absorbs an oversell** — if the decrement would go negative, the system quietly clamps instead of recording that it sold stock it did not have. Nothing is audited about the clamp. Low probability given the guards at `:717`, but the payment-callback path (C-2) reaches `_finalize_payment` with no stock check whatsoever, so the clamp is reachable.

### Section K — Checkout State Machine (K1–K14)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| K1 | An explicit state set exists | **PASS** | `created`, `requires_payment`, `requires_confirmation`, `payment_pending`, `completed`, `declined`, `cancelled`. |
| K2 | Legal transitions declared as data | **PASS** | `VALID_TRANSITIONS:37-45`. |
| K3 | A transition guard exists | **PASS** | `_assert_transition:206-213`. |
| K4 | Terminal states declared | **PASS** | `TERMINAL_STATES:47` — `completed`, `declined`, `cancelled` map to empty sets. |
| K5 | Guard enforced on the complete path | **PASS** | `complete_checkout:629`. |
| K6 | Idempotent re-complete of a completed session | **PASS** | The completed short-circuit at `:625-627` is deliberately placed **before** `_assert_transition` at `:629`, so replaying a completion returns the existing result instead of erroring. Correct and subtle. |
| K7 | Update restricted to live sessions | **PASS** | `update_checkout:388` requires `requires_payment`, else 409 `INVALID_STATE_TRANSITION`. |
| K8 | Cancel restricted, idempotent | **PASS** | `cancel_checkout:477` (idempotent) and `:479` (terminal → 409). |
| K9 | Confirm restricted to `requires_confirmation` | **PASS** | `confirm_checkout:546-553`. |
| K10 | Version increments on every terms change | **PASS** | `update_checkout:438`, `confirm_checkout:576`. |
| K11 | **No path may set a terminal state without passing the guard** | **FAIL** | **Critical finding C-3.** `_finalize_payment` sets `cs.status = "completed"` (`:908`), `"payment_pending"` (`:931`) and `"declined"` (`:942`) and **never calls `_assert_transition`**. It is reachable from `payment_callback` and from `reconcile`, i.e. from outside `complete_checkout` entirely. A `declined` or `cancelled` session can therefore be flipped to `completed`. |
| K12 | State mutations are serialised | **PASS** | Process-wide `mutation_lock` (`db.py:48`) plus `db.mutate()` (`:51-63`); SQLite in WAL with `busy_timeout=5000`. |
| K13 | Session state is consistent after a failure | **PARTIAL** | `_fail` releases reservations and sets `declined` atomically — good. But it does **not clear `razorpay_order_id`**, which is precisely what makes C-1 and C-2 exploitable. A declined session retains a live, payable Razorpay order. |
| K14 | State machine covered by tests | **PARTIAL** | `tests/test_state_machine.py` has 5 tests. Two are real negative tests: `test_terminal_completed_returns_existing_result:18-28` (replay returns the same `payment_id`, exactly one `razorpay_order_created` event) and `test_terminal_declined_cannot_complete:31-39` (`409 INVALID_STATE_TRANSITION`). The stale `.pytest_cache` failure noted in Part 0.2 corresponds to an earlier name for the first of these — a rename, not a removal. What is genuinely missing is coverage of the **other two entry points** into the finaliser: no test touches `payment_callback` or `/reconcile`, which is exactly why C-2 and C-3 went unnoticed. |

### Section L — Final Revalidation Before Payment

**Status: PASS on ordering, with one FAIL on the step that follows it.**

The completion ladder in `complete_checkout:596-876` is ordered correctly, and this is the part of the project that most deserves credit. In sequence:

1. Idempotent replay (`:609-611`)
2. Ownership (`:614`) → 403
3. `complete_requested` audit event (`:616-622`)
4. Already-completed short-circuit (`:625-627`)
5. Transition guard (`:629`)
6. **Price re-check** against the live catalog (`:668-694`) → `requires_confirmation` + `PRICE_CHANGED`
7. **Delegation re-validation** (`:696-700`)
8. **Strict stock re-check**, both actual and effective (`:702-727`)
9. **Full deterministic policy re-run** (`:730-739`)
10. `final_validation` audit event with the full check payload (`:741-750`)
11. **Only then** any Razorpay call (`:755+`)

Every one of steps 6–9 short-circuits through `_fail`, which releases reservations, sets `declined`, and writes an audit event whose summary literally ends *"Razorpay NOT called"* (`:654`). No LLM participates in any step. This is a real gate, not a prompt.

Two defects attach to the ladder rather than to its order:

- **The step after the ladder undoes part of it (C-1).** Steps 6–9 establish a fresh authoritative total, and then `:759-763` may authorise a *stale order for a different amount*. The ladder's conclusions are not carried into the payment call.
- **`_fail`'s audit event omits its evidence.** The approved path writes `payload={"checks": [...]}` (`:741-750`); `_fail` writes no payload at all (`:638-646`) and `"details": {}` in the error (`:661`). So the failure the demo is built around is the *only* validation event with no machine-readable reasons attached. See H-6.

A further structural concern: the Razorpay call at `:774` / `:815-829` happens **inside** the `db.mutate()` transaction opened by the route. If any code after the gateway call raises, the transaction rolls back — including the row that records the order id — while the money-side effect at Razorpay persists. The window is small and no such raise is currently reachable, but the pattern is the classic one for lost payments. See H-4.

### Section M — Razorpay Integration (M1–M12)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| M1 | Razorpay test mode used | **NOT VERIFIABLE** | `.env` holds `rzp_test_*` credentials (test-mode prefix) and `PAYMENT_DRIVER=saved`, but no call could be made from this environment. |
| M2 | Orders API used to create an order | **PASS (code) / NOT VERIFIABLE (live)** | `payment_service.create_order:52-70`, `amount = amount_rupees * 100`. |
| M3 | All gateway calls confined to one module | **PASS** | Every Razorpay call site is in `payment_service.py`; enumerated in Part 1.F. |
| M4 | Credentials read from environment only | **PASS** | `config.py` via `pydantic_settings`; `.env` is gitignored and **no secret is committed** (verified against all 3 commits). |
| M5 | Multiple payment drivers supported | **PASS with a caveat** | Three drivers exist — `sim` (`:808-810`), `checkout` (modal, `:847-876`), `saved` (token charge, `:815-829`). **Fairness correction:** the README documents all three, including `saved` as the default, with a driver-comparison table; the stale artefact is the inline comment at `config.py:16` (`# sim \| checkout`), not the documentation. The real caveat is coverage: the live configured driver (`saved`) has the thinnest suite (`tests/test_saved_driver.py`, 2 tests) and its modal-fallback branch at `:830-845` — the newest code in the repo, added in commit `88fb70b` — is the C-1 overcharge path and is untested. |
| M6 | Signature verification on the browser callback | **PASS** | `verify_checkout_signature:146-155` — HMAC-SHA256 over `f"{order_id}\|{payment_id}"`, compared against `razorpay_signature`. |
| M7 | Verification fails closed without credentials | **PASS** | `:148-149` returns **False** when no credentials are present. Correct direction. |
| M8 | Order creation fails closed without credentials | **FAIL** | `create_order:52-60` **fabricates** `order_sim_<hex>` with `"_simulated_no_credentials": True` instead of raising. A misconfigured deployment therefore reports successful orders that do not exist at Razorpay. This is fail-**open** on the money path, and it is inconsistent with M7's fail-closed choice three functions later. |
| M9 | Callback bound to the session's own order | **PARTIAL** | `api/checkout.py:188` rejects a mismatched `razorpay_order_id` — but that is the *only* binding. There is no session-status check and no amount check. See C-2. |
| M10 | Amount charged is verified against the amount authorised | **FAIL** | Nowhere. No amount column (`entities.py`), no comparison at `:759-763`, none in the callback. See C-1/I6/I7. |
| M11 | Pending payments can be reconciled | **PARTIAL** | `reconcile_pending:109-143` queries the order and resolves; but it **never retries** and `POST /checkout_sessions/{id}/reconcile` (`api/checkout.py:242-246`) requires **no authentication and no ownership** — any caller can drive reconciliation of any session id. |
| M12 | Gateway failures degrade safely | **PASS (mostly)** | Network error on order create → `payment_pending` with an audit event (`:776-794`); network error in `charge_with_token` → `pending` (`payment_service.py:220-226`) rather than a false success. Correct instinct. The exception is `:830-845`, where a **declined** token charge is deliberately treated as non-terminal and retried as a modal on the same order — see C-1. |

### Section N — Idempotency (N1–N7)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| N1 | `Idempotency-Key` header accepted | **PASS** | `api/checkout.py:27-46` (create) and `:101-136` (complete). |
| N2 | Keys persisted with the response snapshot | **PASS** | `IdempotencyKey(key PK, endpoint, session_id, response_snapshot)`; `_store_idempotent:228-239`, `store_complete_idempotency:242-254`. |
| N3 | Replay returns the identical response, no second charge | **PASS** | `_idempotent_replay:219-225` + `complete_checkout:609-611`. Verified by assertion in `tests/test_idempotency.py:5` and independently by `buyer_b/client.py:98-106`, which replays the same key and asserts `same_payment`. |
| N4 | Re-completion without a key is still safe | **PASS** | The completed short-circuit at `:625-627` returns the existing payment id even with no key at all — `tests/test_state_machine.py:18-28` asserts the same `payment_id` and exactly **one** `razorpay_order_created` event. |
| N5 | Create replay does not duplicate sessions | **PASS** | `create_checkout:269-273`; `tests/test_idempotency.py:34`. |
| N6 | Keys scoped to endpoint and session | **FAIL** | `_idempotent_replay:219-225` is `db.get(IdempotencyKey, key)` — it **ignores the persisted `endpoint` and `session_id` columns entirely**. The columns are written and never read. A key reused across endpoints or sessions therefore replays the wrong snapshot: reusing a `create` key on `complete` will attempt `CompleteResponse(**{"checkout_session_id": ...})`; reusing a key from session A on session B returns A's response for B's request. The buyer generates random keys (`buyer_service/tools.py:82`), so this is not reachable by the demo clients — but it is reachable by any protocol client, and the spec does not warn against it. |
| N7 | Idempotency written before the money call | **PARTIAL** | `api/checkout.py:113-114` stores the completion snapshot **after** `complete_checkout` returns. Because both run inside the same locked transaction, a crash between the gateway call and the commit loses the key **and** the session row while the charge stands at Razorpay. Same root cause as H-4. |

### Section O — Failure Handling (O1–O12)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| O1 | Stock disappearing mid-transaction (TOCTOU) | **PASS** | `complete_checkout:702-727` checks **both** actual and effective stock, declines via `_fail`, releases reservations, and never calls Razorpay. Asserted by `tests/test_inventory.py:71-90` (which checks `razorpay_order_created not in actions`). This is the flagship failure demo and it is real. |
| O2 | Price changing mid-transaction | **PASS** | `:668-694` → `requires_confirmation` + `PRICE_CHANGED`; `tests/test_checkout_flow.py:16` walks the full change→confirm→complete cycle. |
| O3 | Buyer rejecting new terms | **PASS** | `confirm_checkout:555-567` cancels and releases; `tests/test_checkout_flow.py:57`. |
| O4 | Policy-failing basket update | **PASS** | Previous basket and reservation preserved (`update_checkout:416-431`); `tests/test_checkout_flow.py:72`. |
| O5 | Gateway network failure on order create | **PASS** | `:776-794` → `payment_pending` + audit event, no false success. |
| O6 | Gateway network failure during token charge | **PASS** | `payment_service.py:220-226` → `pending`, not `completed`. |
| O7 | Gateway declining a charge | **PARTIAL** | A non-200 from the Payments API becomes `status="declined"` (`payment_service.py:234-242`), but `checkout_service.py:830-845` deliberately treats that as **non-terminal** and falls back to the modal **on the same order**. Recoverable UX; the money-safety consequence is C-1. |
| O8 | LLM unavailable | **PASS** | Merchant: `_next_action:413-422` degrades to the deterministic brain. Discovery: `_llm_parse` returns `None` → deterministic parser. Both are real fallbacks, not `try/except pass`. |
| O9 | Agent budget exhaustion | **PASS** | `run:433-476` → `unable_to_fulfill` with no payment; `execute_tool` → `TOOL_BUDGET_EXHAUSTED`. Asserted by `tests/test_merchant_agent.py:249`. |
| O10 | Invalid / hallucinated ids | **PASS** | `tools._load_product:72-80`, `_load_pack:83-92`, `_resolve_items`; asserted by `tests/test_merchant_agent.py:261` and `tests/test_gating.py:92`. |
| O11 | Forged identity / expired delegation | **PASS** | 401 paths in `delegation_service.py:15-31`; asserted by `tests/test_gating.py:79` and `:85`. |
| O12 | Unexpected server error must not imply a charge | **PASS (message) / PARTIAL (mechanics)** | The 500 envelope says *"Internal merchant error; transaction not executed"* (`main.py:87`) — but if the exception is raised **after** a successful Razorpay call, that sentence is false, because the rollback undoes the local record and not the charge. See H-4. |

Additional gap, not covered by any O item: **`POST /checkout_sessions/{id}/payment_callback` has no test at all.** The only place it is exercised anywhere in the repo is `scripts/audit_bypasses.py:112`, which sends a deliberately invalid signature and correctly gets rejected. The valid-signature-on-a-terminal-session case — C-2 — is untested and unguarded.

### Section P — Audit Trail

**Status: PASS with two material omissions.**

`audit_service.append_event` is the single write path, and it is used consistently across the money-critical surface. Event types actually written by the runtime: `discovery_requested`, `discovery_result_generated`, `checkout_create_requested`, `gating_evaluated`, `reservation_created`, `checkout_created`, `checkout_update_requested`, `checkout_updated`, `confirmation_requested`, `confirmation_accepted`, `confirmation_rejected`, `complete_requested`, `final_validation`, `razorpay_order_creation`, `razorpay_order_created`, `razorpay_token_charge`, `payment_success`, `payment_failure`, `payment_pending`, `checkout_completed`, `checkout_declined`, `checkout_cancelled`, `inventory_adjusted`, `demo_reset`, plus `agent_*` action events and `agent_outcome`.

Every event carries actor, action, decision, `rules_fired`, `reasoning_summary`, JSON payload, timestamp, `prev_hash` and `hash`. Actors are distinguished (`buyer_agent`, `merchant_agent`, `razorpay`, `operator`), which is what makes the trail readable as a narrative rather than a log dump.

The two omissions:

1. **Reservation lifecycle is unaudited** (see J10). Release, consume, expire and sweep write nothing.
2. **The declined path's evidence payload is missing** (see H-6). `_fail:638-646` writes `final_validation` with no `payload`, while the approved path writes the full check array at `:741-750`. The dashboard's "Policy checks" panel reads `latestGating.payload?.checks` where `latestGating` is the **last** `gating_evaluated` **or** `final_validation` event (`dashboard/src/views/OpsView.jsx:159-160`). On a completion-time decline the last such event is the payload-less `final_validation`, so the panel renders an **empty** policy grid — on precisely the transaction the demo is built to showcase.

### Section Q — Explainability (Q1–Q5)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| Q1 | An explain endpoint exists | **PASS** | `GET /explain/{checkout_session_id}` (`api/audit.py:50`). |
| Q2 | Explanations are template-derived, never LLM-invented | **PASS** | `explain_service.py` — `RULE_PHRASES`, `ACTION_SENTENCES`, deterministic templating. The docstring claim *"it never invents reasons"* is true. |
| Q3 | Explanation is grounded in audit facts | **PASS in intent / BROKEN in fact** | See below. |
| Q4 | Declines are explained by rule | **FAIL** | Type bug at `explain_service.py:45`, `:54`, `:64`. |
| Q5 | Explanation is judge-visible | **PASS (and this is why Q4 matters)** | Rendered as the Ops "Explanation" headline and fact list (`OpsView.jsx:151`, `258-263`). |

**The defect, precisely.** `AuditEvent.rules_fired` is a `Mapped[str]` holding a JSON string (`audit_service.py:53` — `json.dumps(rules_fired or [])`). Every other consumer decodes it first: `api/audit.py:22` and `api/agent.py:45` both call `json.loads`. `explain_service` does not — it iterates the raw string, so each "rule" is a single character.

Consequences, derived by reading the code path per decline type:

- **Create-time policy declines** (`SPEND_LIMIT_EXCEEDED`, `CATEGORY_BLOCKED`, `QUANTITY_LIMIT_EXCEEDED`, `DELEGATION_EXPIRED`): the only gating event is `gating_evaluated` with `decision="declined"`. At `:45` no character ends with `":fail"`, so `failed_rules` is empty and the code takes the **else** branch at `:51`, emitting **"All deterministic policy checks passed."** `decline_reason` is never set, so the headline falls through to `:81` — **"This transaction is still in progress."** on a terminally declined session. Both statements are false, and both are shown to a judge.
- **Completion-time declines** survive by accident: at `:56` the empty phrase list falls back to `or ev.reasoning_summary`, so the real reason appears.
- **Every decline** additionally renders `:64` as **"The transaction was declined ([). Razorpay was NOT called."** — the `[` is character zero of the JSON string.

**Why the test suite does not catch this** (and why it must not be counted as evidence): `tests/test_audit_and_errors.py:48-55` is the only explain test, it uses a **completion-time stock** decline — the one case the `:56` fallback rescues — and its assertions are substring checks (`"declined" in explanation.lower()`). `"declined ([)"` contains `"declined"`, so the test passes over corrupted output. This is exactly the pattern of a test named after a requirement without verifying it.

Minor: `approved` (`:41`, `:69-70`) is dead code — the `elif` at `:69` is unreachable because `:44` already matches `gating_evaluated`.

### Section R — Audit Integrity (R1–R5)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| R1 | Append-only hash chain | **PASS** | `audit_service.py:59` — `hash = sha256(canonical + prev_hash)`, canonicalised with `sort_keys=True, separators=(",",":")` (`:18-30`). |
| R2 | Chain verifiable | **PASS** | `verify_chain:81-90` returns `(intact, count, first_broken_seq)`; exposed at `GET /audit_logs/verify` and surfaced by a "Verify hash chain" button in the Ops view (`OpsView.jsx:270-282`). |
| R3 | Chain is genuinely intact in the live database | **PASS — independently confirmed** | The 46 events in `data/merchant.db` were re-hashed **outside the application code** during this audit and the chain reproduced exactly. This is one of very few claims in the project verified without trusting the project's own code. |
| R4 | Tampering is detected | **PASS** | `tests/test_audit_and_errors.py:33` mutates a row and asserts detection. |
| R5 | No application path updates or deletes audit rows | **FAIL** | The docstring at `audit_service.py:5` states *"No application workflow ever updates or deletes rows; the API only appends."* `POST /admin/demo/reset` (`api/admin.py:74-95`) executes `db.query(model).delete()` over a model list that **includes `AuditEvent`**, and the dashboard calls it from a button (`OpsView.jsx:112`) using an admin token hardcoded in shipped client JavaScript (`api.js:13`). The same wipe happens per-test in `conftest.py:53`. Append-only is a property of the *write path*, not of the system. |

### Section S — Operator Dashboard (S1–S10)

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| S1 | A dashboard exists | **PASS** | React + Vite, 1,099 lines across `App.jsx`, 2 views, 8 components. |
| S2 | Chat surface for the buyer agent | **PASS** | `views/ChatView.jsx` with SSE streaming (`api.js:30-59`). |
| S3 | Ops surface showing the audit timeline | **PASS** | `views/OpsView.jsx:176-194` renders every event with actor icon, action, timestamp and reasoning summary. |
| S4 | Policy checks visible per transaction | **PARTIAL** | `:219-232` — correct for approvals, **blank for completion-time declines** (see Part P). |
| S5 | "Was Razorpay called?" visible | **PASS** | `:235` derives `Razorpay invoked YES/NO` from the presence of a `razorpay_order_created` audit event. This is the single most effective judge-facing artefact in the project. |
| S6 | Explanation visible | **PASS (rendering) / see Q4 (content)** | `:257-263`. |
| S7 | Hash-chain verification visible | **PASS** | `:270-282`, shows `✓ VERIFIED — chain intact across N entries`. |
| S8 | Payment authorization surfaced for the modal | **PASS (mechanically)** | `:239-253` renders `RazorpayPayCard` when `requires_payment && awaiting_authorization && razorpay_order_id`. |
| S9 | The amount shown is the amount charged | **FAIL** | `:248-249` builds the modal from `session.totals.total`, while Checkout.js charges the **order's** amount for `razorpay_order_id` at `:244`. Under C-1 these differ, and the UI shows the wrong one with full confidence. |
| S10 | Live refresh | **PASS** | 1.5 s polling of session, audit log and explanation (`:144-157`). |

Security note carried into Part U: `adminFetch` (`api.js:12-14`) hardcodes `X-Admin-Token: admin_demo_token` into client-side JavaScript, and the Ops view uses it to call `/admin/demo/reset`.

### Section T — Second Independent Client

**Status: PASS — this requirement is satisfied more convincingly than most.**

`buyer_b/client.py` (132 lines) is a genuinely independent implementation: raw `httpx`, its own identity (`BUYER_AGENT_ID = "buyer_agent_b_deterministic"`), its own delegation (`dlg_buyerb_001`), **zero imports from `buyer_service`**, and no LLM. It performs discovery → budget-filtered selection → create → complete with an `Idempotency-Key`, then **replays the same key and asserts `same_payment`** (`:98-106`).

Two things this proves that a second copy of the same client would not: the protocol is implementable without the reference agent's code, and idempotency holds for an independently generated key. The protocol spec's claim — *"Any client implementing only this document can transact"* — is therefore substantially true, with the caveat from E5 that the spec omits two live routes.

### Section U — Security and Boundary Enforcement

| Area | Status | Evidence |
|---|---|---|
| Secrets not committed | **PASS** | `.env` is gitignored; all 3 commits scanned; no key material in tracked files. `.env.example` carries placeholders only. |
| Credentials confined to the merchant | **PASS** | Only `merchant_agent/config.py` reads Razorpay keys. |
| Buyer cannot reach privileged internals | **PASS** | See Section F. |
| Delegation token kept out of LLM context | **PASS** | `agent/state.py:46`, `public_view:88-103`. |
| Session read authorization | **FAIL** | `GET /checkout_sessions/{id}` requires nothing (`api/checkout.py:95-98`). See G8. |
| Audit read authorization | **FAIL** | `/audit_logs`, `/audit_logs/verify` and `/explain/{id}` are all unauthenticated (`api/audit.py:31`, `:44`, `:50`). Acceptable for a demo; must not ship. |
| Reconcile authorization | **FAIL** | `POST /checkout_sessions/{id}/reconcile` (`api/checkout.py:242-246`) takes no credentials and no ownership check, yet drives `_finalize_payment`. |
| Admin authorization | **PARTIAL** | `require_admin` uses a plain `!=` string comparison (`api/admin.py:23-24`) against a default token `admin_demo_token` (`config.py:20`) that is **hardcoded in shipped dashboard JS** (`dashboard/src/api.js:13`), with `allow_origins=["*"]` (`main.py:38`). Combined, any page in any browser tab can wipe the audit trail. |
| Payment callback authorization | **PARTIAL/FAIL** | Signature verification is correct and fails closed (`payment_service.py:146-155`), but there is no session-state guard and no amount check. See C-2. |
| Fail-closed on missing credentials | **INCONSISTENT** | `verify_checkout_signature` fails **closed** (`:148-149`); `create_order` fails **open** by fabricating `order_sim_*` (`:52-60`). Two opposite choices on the same money path. |
| CORS | **FAIL for production, acceptable for demo** | `allow_origins=["*"]`, all methods, all headers (`main.py:36-41`), with the comment `# demo environment`. |

### Section V — Test Quality (V1–V10)

The suite is **48 test functions** across 9 files, all running in-process against a real FastAPI `TestClient` with an isolated temp SQLite DB and a per-test clean-state fixture (`conftest.py:42-73`). That is a real integration suite, not unit-test theatre — a majority of tests drive actual HTTP routes and then assert on the resulting **audit events**, which is the right way to test this system.

| ID | Requirement (topic) | Status | Evidence |
|---|---|---|---|
| V1 | Tests exist and are runnable | **PASS (code) / NOT VERIFIABLE (execution)** | 48 tests; could not be run here (Part 0.2). Last recorded local run: 37 collected, 1 failure. |
| V2 | Gating rules each covered | **PASS** | `tests/test_gating.py` — all five rules plus forged identity, expired delegation, unknown ids (9 tests). |
| V3 | Price authority covered | **PASS** | `test_checkout_flow.py:90-96`; the comment *"request contains NO price fields at all"* is accurate. |
| V4 | State machine covered | **PARTIAL** | `test_terminal_declined_cannot_complete:31-39` asserts 409 `INVALID_STATE_TRANSITION`, and `test_terminal_completed_returns_existing_result:18-28` asserts idempotent replay with exactly one order created. Both are meaningful. What is **not** covered is any terminal-state protection on the `payment_callback` / `reconcile` paths — i.e. exactly the invariant C-2/C-3 break. |
| V5 | Idempotency covered | **PASS** | `test_idempotency.py` (3 tests) plus the independent `buyer_b` replay assertion. |
| V6 | The TOCTOU failure demo covered | **PASS** | `test_inventory.py:71-90` asserts `razorpay_order_created not in actions` and `payment_success not in actions` — meaningful negative assertions on the audit trail. One dead line at `:90` (`assert any(... or True for _ in [0])`) is a tautology and asserts nothing; the surrounding assertions carry the test. |
| V7 | Merchant agent covered | **PASS** | 12 tests including budget exhaustion, hallucinated ids, server-authoritative price/inventory, and `test_request_completion_blocked_in_buyer_completes_mode:305` — which verifies the second money path's mode gate. |
| V8 | Tests are hermetic | **PASS, with a consequence** | `conftest.py:11-16` forces `PAYMENT_DRIVER=sim`, blanks both Razorpay keys, and sets `MA_LLM_BRAIN=false`. Correct for CI determinism — but it means **every test runs through the fabricated-order code path** (`payment_service.py:52-60`), so the suite has never once exercised a real gateway response, and **no test exercises the LLM brain at all**. |
| V9 | Tests would catch the findings in this audit | **FAIL** | Nothing covers: the stale-order amount mismatch (C-1), `payment_callback` in any form (C-2), transition enforcement in `_finalize_payment` (C-3), the `explain_service` type bug on create-time declines (H-1), cross-endpoint idempotency reuse (H-2), or the reported-vs-authorised amount discrepancy (H-3). |
| V10 | Adversarial/bypass testing exists | **PARTIAL** | `scripts/audit_bypasses.py` attempts 9 bypasses — client-set `status`, over-budget update, complete-after-cancel, anonymous discovery, reservation release, unknown ids, forged callback signature, stale confirm version. Real effort, and the coverage is thoughtful. But it contains **zero `assert` statements** — it prints, so nothing fails CI — and it does not attempt the three cases that actually break: a *valid* callback on a terminal session, order reuse after a re-price, or a key reused across endpoints. |

**On the previously failing test.** `.pytest_cache` records `test_terminal_completed_cannot_complete_again` failing in the last local run, and that name is absent from the tree. The current suite contains `test_terminal_completed_returns_existing_result` covering the same scenario with the **opposite** expectation. Read together, the most reasonable interpretation is a deliberate correction: re-completing a completed session **should** return the existing result rather than error, which is what `complete_checkout:625-627` implements and what N4 requires. This is a legitimate fix, not a suppressed failure, and it is recorded here as such.

---

## PART 3 — MONEY-ACTION TRACE

Five entry points can cause a Razorpay call or a payment-state mutation (enumerated in 1.F). Each is traced below through the required checkpoints. "—" means the checkpoint is **absent** on that path.

### 3.1 Path 1 — `POST /checkout_sessions/{id}/complete` (the primary, correct path)

| Checkpoint | Implementation | Verdict |
|---|---|---|
| **ENTRY POINT** | `api/checkout.py:101-136`, inside `db.mutate()` (locked transaction) | OK |
| **AUTHORIZATION** | `_check_ownership:197-203` — both `buyer_agent_id` **and** `delegation_id` must match → 403; then `validate_delegation` re-run at `:696-700` | OK |
| **STATE CHECK** | completed short-circuit `:625-627`, then `_assert_transition:629` against `VALID_TRANSITIONS` | OK |
| **PRICE CHECK** | `:668-694` — re-resolve from catalog, compare to stored totals → `requires_confirmation` + `PRICE_CHANGED` on drift | OK |
| **STOCK CHECK** | `:702-727` — **both** `pack.stock_actual` and `effective_available` must cover the quantity (`:717`) | OK, strict |
| **GATING** | `:730-739` — full deterministic 5-rule re-run, no LLM | OK |
| **IDEMPOTENCY** | replay at `:609-611`; snapshot stored at `api/checkout.py:113-114` (after the fact) | OK, see N7 |
| **RAZORPAY** | `:755-845` — driver-dependent: `sim` → `execute_sim_capture:808-810`; `saved` → `charge_with_token:815-829`; `checkout` → park for modal `:847-876` | Reached **only** after all of the above |
| **AUDIT** | `complete_requested:616-622`, `final_validation:741-750` (with full check payload), `razorpay_order_creation` / `razorpay_order_created`, then `payment_success` / `payment_failure` / `payment_pending` | OK |
| **FINAL STATE** | `completed` / `payment_pending` / `declined` / parked `requires_payment` | OK |

**On this path the gate is real.** Every decline exits through `_fail`, which releases reservations, sets `declined`, and writes an audit event ending *"Razorpay NOT called"* (`:654`) — and the code genuinely has not called it. No LLM output participates in any checkpoint. This is the project's central claim and it holds.

### 3.2 Path 1-variant — the same path after a re-price (CRITICAL: C-1)

The ladder above establishes a fresh authoritative total, and then the payment step may authorise a **different** amount.

```
checkout_service.py:759-763
    # Retry guard: if a previous attempt already created an open order for this
    # live session (modal dismissed, agent retried), reuse it — never duplicate.
    if cs.status == "requires_payment" and cs.razorpay_order_id:
        order = {"id": cs.razorpay_order_id}
        cs.payment_driver = driver
```

There is **no comparison between the reused order and `total_rupees`** computed three lines earlier at `:756`. And neither `update_checkout` (`:435-439`) nor `confirm_checkout` (`:574-577`) clears `razorpay_order_id` when it changes the totals and bumps the version.

**Reproduction, entirely through the public protocol, in the configured default driver (`PAYMENT_DRIVER=saved`):**

1. `POST /checkout_sessions` with basket B1 → total **T1**; gating passes → `requires_payment`, reservation held.
2. `POST /complete` → ladder passes → no existing order → `create_order(T1)` → **order O1 with amount T1×100** persisted to `cs.razorpay_order_id` (`:796`). With `saved` and no stored token the session **parks** for modal authorization, status stays `requires_payment` (`:847-876`; asserted by `tests/test_saved_driver.py:15-28`).
3. Change the terms by either public route:
   - `PUT /checkout_sessions/{id}` with basket B2 → total **T2** (`:435-439`), or
   - an operator price change → next `complete` returns `PRICE_CHANGED` → `POST /confirm` with `accept_new_terms=true` → totals re-resolved to **T2** (`:571-577`).
   Both set `version += 1`, both return the session to `requires_payment`, **neither clears O1**.
4. `POST /complete` again → the full ladder re-validates against **T2** and passes → `:759-763` **reuses O1 (amount T1)**.
5. `saved` driver: `charge_with_token(order_id=O1, amount_paise=T2×100)` (`payment_service.py:207-219`). Razorpay rejects an amount that disagrees with the order → non-200 → `PaymentOutcome(status="declined")` (`:234-242`). `checkout_service.py:830-845` then deliberately treats that decline as **non-terminal** and falls back to modal authorization **on the same order O1**.
6. The dashboard renders the modal with `razorpay_order_id: O1` but displays and passes `amount: session.totals.total` = **T2** (`OpsView.jsx:244-249`).
7. The human authorises. Checkout.js charges the **order's** amount — **T1**. Razorpay returns a genuine signature. `payment_callback` accepts it (order id matches, signature valid) → `_finalize_payment` → `completed`.
8. `_finalize_payment:916` writes the audit line **`Paid ₹{session total}`** — i.e. **T2** — while **T1** was actually charged. `serialize_session:150` likewise reports `payment.amount` as T2. There is **no order-amount column** in `models/entities.py`, so nothing in the system records what was really authorised.

**Impact.** If T1 > T2 the buyer is **overcharged** relative to the terms they approved. If T1 < T2 the merchant is **undercharged** while stock is decremented and the trail records full payment. In both directions the audit trail states an amount that was never charged — which defeats the project's headline claim directly, not incidentally.

**Scope.** `sim` is unaffected (nothing is really charged). `saved` and `checkout` — the two real-money drivers — are affected. `.env` sets `PAYMENT_DRIVER=saved`.

**Not a hypothetical.** The live `data/merchant.db` contains a session sitting in exactly this window: `cs_3a24cd3c29`, `status=requires_payment`, `razorpay_order_id=order_TT5LO4V474QIpU`, total `230`. One `PUT` and one `complete` reproduce the divergence.

**Fair note on intent.** Order reuse is deliberate and tested — `tests/test_saved_driver.py:31-42` asserts that a retry reuses the open order and that exactly one `razorpay_order_created` event is written. That behaviour is *correct* when the basket has not changed; duplicate-order avoidance is a real concern and the team was right to address it. The defect is narrow and specific: the guard is keyed on `status == "requires_payment"` alone, with no amount or version binding. It is a missing condition, not a wrong idea.

### 3.3 Paths 2 and 3 — Merchant Agent completion (`agent_completed` mode)

| Checkpoint | Implementation | Verdict |
|---|---|---|
| **ENTRY POINT** | `POST /agent_sessions` (`api/agent.py:91-114`) or `POST /agent_sessions/{id}/decision` (`:122-139`) → `loop.run` / `loop.resume` → `tools._tool_request_completion:595-667` | OK |
| **AUTHORIZATION** | mode gate at `tools.py:596-600` — `request_completion` raises `ToolError` unless `state.execution_mode == "agent_completed"`; delegation held server-side only (`state.py:46`) and passed from state, never from the LLM | OK; asserted by `tests/test_merchant_agent.py:305` |
| **STATE / PRICE / STOCK / GATING** | delegates to `checkout_service.complete_checkout` — the identical ladder as 3.1 | OK — the agent cannot skip or weaken any check |
| **IDEMPOTENCY** | key generated server-side: `f"ma_{state.agent_session_id}_complete_{attempts}"` (`tools.py:605-612`) | OK, and not LLM-controlled |
| **RAZORPAY** | same `complete_checkout` code | Same guarantees, same C-1 exposure |
| **AUDIT** | `_audit:48-79` writes `agent_<tool>` per action plus `agent_outcome`; the underlying checkout events are written by the service | OK |
| **FINAL STATE** | agent session terminal status + `CheckoutSession` state | See below |

Two observations. First, the design here is right: the agent's most privileged tool is a *request*, and the authority stays in the service. `run_gating` is a read-only preview (`tools.py:485-525`) whose result the agent cannot substitute for the authoritative re-run at `complete_checkout:730-739`. The LLM cannot invent a price, a policy outcome, or an idempotency key.

Second, the **reported outcome is not verified** (H-7). `loop._apply_finish:687-701` coerces the LLM's `finish(outcome=...)` to one of three strings and, for `"completed"`, terminates the agent session as completed **without checking that any payment occurred**:

```
outcome = outcome if outcome in ("completed", "ready_for_completion", "declined") else "declined"
if outcome == "completed":
    self._terminate(state, "completed", reason_code, message)
```

The tool schema even advertises `"completed: paid/fulfilled."` (`agent/prompts.py:32-57`). A model that calls `finish(outcome="completed")` without ever calling `request_completion` yields an agent session reporting success while the `CheckoutSession` remains `requires_payment`. `buyer_service/agent.py:184-195` then maps that status to *"The order is PAID and placed. Tell the user it succeeded with the total."*

**No money moves and no state is corrupted** — this is a false *claim*, not a false *charge*, which is why it is HIGH rather than CRITICAL. But it is the exact failure mode a judge probing "can the LLM lie about payment?" would look for, and the answer today is yes. It is invisible to the test suite because `conftest.py:16` sets `MA_LLM_BRAIN=false`, so **no test exercises the LLM brain**; the deterministic brain never fabricates a completion, and `tests/test_merchant_agent.py:62-83` asserts a real `payment_id` and a DB `status == "completed"` on the fallback path.

### 3.4 Path 4 — `POST /checkout_sessions/{id}/payment_callback` (CRITICAL: C-2)

| Checkpoint | Implementation | Verdict |
|---|---|---|
| **ENTRY POINT** | `api/checkout.py:179-239`; **no credentials of any kind required** | — |
| **AUTHORIZATION** | no `buyer_agent_id`, no delegation, no ownership check | **ABSENT** |
| **STATE CHECK** | **none** — nothing compares `cs.status` against anything | **ABSENT** |
| **PRICE CHECK** | **none** | **ABSENT** |
| **STOCK CHECK** | **none** | **ABSENT** |
| **GATING** | **none** | **ABSENT** |
| **IDEMPOTENCY** | none; repeat callbacks re-enter `_finalize_payment` | **ABSENT** |
| **GUARDS ACTUALLY PRESENT** | exactly two: `cs.razorpay_order_id != order_id` → reject (`:188`); `verify_checkout_signature` → reject (`:193`) | Partial |
| **RAZORPAY** | `finalize_with_outcome:892` marks the session paid; then a best-effort token capture at `:209-236` | Reached with no gate |
| **AUDIT** | `payment_success` + `checkout_completed` via `_finalize_payment:909-929` | Written — but records a success that no gate approved |
| **FINAL STATE** | `completed`, stock decremented, reservations consumed | **Unsound** |

**Why this is reachable without forging anything.** The signature is produced by **Razorpay**, not by the attacker, and the order id is the session's own. So the scenario needs no cryptographic break:

1. A session parks for modal authorization (order O1, status `requires_payment`) — the normal `saved`/`checkout` flow.
2. Before the human finishes in the modal, the session goes **terminal** by any legitimate route: the buyer cancels (`cancel_checkout:489` → `cancelled`), or a retry declines on stock (`_fail:636` → `declined`). **Neither clears `razorpay_order_id`** (`:634-666`, `:488-497`) and both **release the reservation**.
3. The human completes the still-open modal. Razorpay charges and returns a valid signature.
4. The callback passes both guards → `_finalize_payment` → `cs.status = "completed"` (`:908`), stock decremented (`:906`), reservations "consumed".

**Result: a cancelled or declined session becomes `completed`.** This violates protocol guarantee #1 (*terminal sessions never execute payments*), requirement K11, and the project's core claim that every money action is gated. Because the reservation was already released, the stock consumed may have been sold to another buyer in between; `max(0, ...)` at `:906` then **silently absorbs the oversell** with no audit record of the clamp.

**Test coverage: none.** `payment_callback` is not exercised by any test in the suite. Its only appearance anywhere is `scripts/audit_bypasses.py:112`, which sends an *invalid* signature and is correctly rejected — so the script demonstrates the guard that exists while leaving untested the two that do not.

### 3.5 Path 5 — `POST /checkout_sessions/{id}/reconcile`

| Checkpoint | Implementation | Verdict |
|---|---|---|
| **ENTRY POINT** | `api/checkout.py:242-246` → `reconcile_session:981-993` | — |
| **AUTHORIZATION** | **none** — no credentials, no ownership | **ABSENT** |
| **STATE CHECK** | `cs.status != "payment_pending"` → 409 (`:984-989`) | Present — and it is what keeps this path from being critical |
| **PRICE / STOCK / GATING** | none (arguably correct: reconciliation reports what the gateway already did) | Acceptable |
| **RAZORPAY** | `reconcile_pending:109-143` — read-only order lookup, never retries | OK |
| **AUDIT** | via `_finalize_payment` | OK |
| **FINAL STATE** | `completed` / `declined` / unchanged | Sound, subject to C-3 |

The status guard means an attacker cannot use this to resurrect a terminal session. The defects are the missing authentication (any caller can drive any session id) and the shared C-3 root cause below. Not covered by any test.

### 3.6 C-3 — no transition guard on the payment finaliser

`_finalize_payment:896-978` is the only function that writes the three payment-outcome states, and it **never calls `_assert_transition`**:

- `cs.status = "completed"` at `:908`
- `cs.status = "payment_pending"` at `:931`
- `cs.status = "declined"` at `:942`

`complete_checkout` calls `_assert_transition` once at `:629`, before the ladder — so on Path 1 the machine is respected. But `finalize_with_outcome` (`:892`) is a **public wrapper** used by `payment_callback`, and `reconcile_session` (`:993`) calls the finaliser directly. Both enter with no transition check at all. `VALID_TRANSITIONS` declares terminal states as empty sets (`:47`), so the table already forbids these writes; the table is simply not consulted. This is the enabling defect beneath C-2, and it is one line of missing enforcement rather than a design flaw.

### 3.7 Bypass analysis — can any path reach Razorpay without the required gates?

| Question | Answer | Evidence |
|---|---|---|
| Can the **buyer** reach Razorpay directly? | **No** | No credentials, no client, no host reference anywhere under `buyer_service/` or `buyer_b/`. |
| Can an **LLM** cause a charge by tool call? | **No** | `complete_checkout` is not in either LLM tool registry in the shipped `buyer_completes` mode; `request_completion` is mode-gated (`tools.py:596-600`) and still routes through the full ladder. |
| Can a client inject a **price**? | **No** | Inexpressible — no DTO field; `_resolve_items:91` always re-reads the catalog. |
| Can a client skip **gating**? | **No** on paths 1–3 | Gating re-runs at `:730-739` before any gateway call. |
| Can a **declined** session be paid? | **YES — C-2** | `payment_callback` has no state check; `_finalize_payment` has no transition guard. |
| Can a **cancelled** session be paid? | **YES — C-2** | Same path; `cancel_checkout` retains `razorpay_order_id`. |
| Can the amount charged differ from the amount validated? | **YES — C-1** | `:759-763` reuses an order with no amount comparison; no order-amount is persisted anywhere. |
| Can a charge occur with **no credentials configured**? | **Partially — M8** | `create_order:52-60` fabricates `order_sim_*` and reports success rather than failing closed. |
| Can an unauthenticated caller mutate payment state? | **YES (bounded)** | `payment_callback` (needs a real signature) and `reconcile` (needs `payment_pending`). |
| Can the **audit trail** be erased? | **YES** | `POST /admin/demo/reset` deletes `AuditEvent` rows using a token hardcoded in shipped client JS, under `allow_origins=["*"]`. |

---

## PART 4 — AGENTICITY AUDIT

**Test applied:** an LLM in the request path is not an agent. Agency requires a goal, a bounded tool surface, a loop that observes results it did not predict, and adaptation. Judged against that, and against the opposite failure — theatre, where an LLM is inserted to *look* agentic — this project comes out well.

### 4.1 Is the Buyer Agent an agent?

**Yes.** `llm_loop:286-382` is a real tool-calling loop with a bounded 5-tool surface. Three distinct adaptations are implemented in code, not prompted: substitution after `STOCK_UNAVAILABLE` (`:532-559` + `_propose_substitutes:581-669`), re-confirmation after `PRICE_CHANGED` (`:515-530`), and reconciliation after `payment_pending`. It maintains state across turns (`conv.declined_packs`, `:96-116`) so it does not re-propose what was already refused.

**What keeps it honest:** the buyer cannot pay. `complete_checkout` is absent from `TOOL_SCHEMAS` (`prompts.py:46-147`), so payment is not expressible as a model action; it is reachable only from `_complete_flow:436-572` after a human approval that the loop parks on (`BILL_PAUSE`, `:17`). An agent that can shop but structurally cannot pay itself is the right shape for this problem.

### 4.2 Is the Merchant Agent an agent?

**Yes — and it is agentic by structure rather than by LLM presence.** This is the distinction the brief asks for, and it is the project's strongest architectural idea. `run:433-476` implements observe → reason → act → observe → adapt; `_post_action:590-685` genuinely re-enters the loop on recoverable outcomes (e.g. `STOCK_UNAVAILABLE` returns `False` to continue rather than terminating). Autonomy is bounded **in code**: 8 iterations, 10 tool calls (`config.py:27-28`), a 12-tool allowlist with privilege classes (`tools.py:32-41`), and a busy-lock returning 409 on concurrent drive (`api/agent.py:107-113`).

Decisively, the same behaviour runs with the LLM removed: `_fallback_decide:236-410` is a complete deterministic decider, and `_next_action:413-422` fails over to it automatically. The agent is therefore demonstrable with NIM offline — which also means the "agentic" claim does not depend on a model being up during judging.

**Where it is weaker than it appears:** the terminal outcome is the LLM's word (H-7, Part 3.3), and `_confirm_terms:551-574` reaches into `checkout_service` directly rather than going through the protocol, so internally the two agents are not as symmetrically separated as the architecture diagram implies.

### 4.3 Is anything agent-washed?

Audited for it; largely **no**.

- Discovery's LLM is scoped to parsing and says so (`discovery_service.py` docstring), with a deterministic fallback and deterministic ranking. Not dressed up as an agent.
- Gating, pricing, revalidation and payment contain **no** model calls. Nobody put an LLM in charge of authorization to make the merchant look more agentic — which is the right call and worth stating plainly, since the temptation in this track is strong.
- `run_gating` as an agent tool could have been agent-washing; it is implemented as a read-only preview that cannot alter the authoritative evaluation (`tools.py:485-525`). Correct.

One honest deduction: the `agent_completed` mode — the most "agentic" configuration — is **not** the shipped path. `buyer_service/tools.py:129` hardcodes `execution_mode="buyer_completes"`, so in the demo the merchant agent orchestrates and the human authorises. That is the *safer* choice and defensible, but any claim of "fully autonomous agent-to-agent payment" describes a code path the product does not use by default. See Part 9.

### 4.4 Where does authority actually sit?

Authority sits with deterministic server code, and the audit found no path where an LLM's output becomes a financial decision:

| Decision | Decided by | LLM able to influence? |
|---|---|---|
| Price of an item | `Pack.price` via `_resolve_items:91` | No — inexpressible |
| Order total | `_compute_totals:98-108` | No |
| Policy pass/fail | `gating_service.py:39-113` | No |
| Stock availability | `inventory_service.available_stock:46-48` + `:717` | No |
| Delegation validity | `delegation_service.py:15-31` | No |
| Whether to call Razorpay | `complete_checkout:730-763` after the ladder | No |
| Amount sent to Razorpay | `:756-757` — **but see C-1** | No (the defect is deterministic, not LLM-driven) |
| Which tool to call next | LLM or deterministic brain | Yes — bounded by allowlist and budgets |
| What the buyer is told happened | LLM prose over tool results | Yes — **and H-7 lets it be wrong** |

The last row is the only real leak, and it is a reporting leak rather than an authority leak.

---

## PART 5 — ORIGINAL TRACK COMPLIANCE

**Track:** AI Growth & Agentic Commerce. **Chosen direction:** make a merchant transactable by an AI buyer end to end. **The bar:** every money action explainable, bounded and gated; show the audit trail and one failure handled gracefully.

As with the PRD (0.3), the brief's 20 questions are not reproduced verbatim in the repository; the questions below are the operative track questions restated from the track statement and the bar. Answers are evidence-bound.

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | Is there a merchant that can actually be transacted with? | **YES** | 45-product catalog, 7-route protocol, working checkout lifecycle, real Razorpay Orders integration. |
| 2 | Is there an AI buyer that transacts, not just chats? | **YES** | `buyer_service/agent.py` LLM loop creates real checkout sessions and drives real completions. |
| 3 | Is the flow end to end (discover → basket → policy → pay → record)? | **YES**, with the last mile human-authorised | `/discover` → `/checkout_sessions` → gating → `/complete` → Razorpay → audit. |
| 4 | Does the buyer transact **through a protocol** rather than internals? | **YES** | `protocol/protocol_spec.md`; `buyer_service/tools.py` is pure HTTP; zero merchant imports. |
| 5 | Is every money action **gated**? | **MOSTLY — 2 gaps** | Paths 1–3 fully gated (Part 3.1/3.3). `payment_callback` ungated (C-2); reused-order amount unvalidated (C-1). |
| 6 | Is every money action **bounded**? | **YES** | Delegation caps (value, category, per-item quantity, expiry) enforced in code at create, update and pre-payment; agent iteration/tool budgets enforced server-side. |
| 7 | Is every money action **explainable**? | **PARTIALLY** | Hash-chained trail with actor, decision, rules and reasoning per event — but `/explain` misreports create-time declines (H-1) and the declined `final_validation` carries no check payload (H-6). |
| 8 | Is there an audit trail, and is it shown? | **YES** | 24+ event types, chain verified independently over 46 live rows; Ops view renders the timeline and a chain-verification button. |
| 9 | Is the audit trail tamper-evident? | **YES** | `verify_chain:81-90`; tampering test at `tests/test_audit_and_errors.py:33`; recomputed outside the app during this audit. |
| 10 | Is the audit trail append-only in practice? | **NO** | `/admin/demo/reset` deletes `AuditEvent` rows (R5). |
| 11 | Is at least one failure handled gracefully? | **YES — genuinely** | TOCTOU stock loss at completion: declines, releases the reservation, records *"Razorpay NOT called"*, and the buyer agent proposes a substitute. Asserted by `tests/test_inventory.py:71-90`. |
| 12 | Is more than one failure handled? | **YES** | Price change → re-confirmation; gateway network error → `payment_pending` + reconcile; policy decline at create; budget exhaustion; LLM outage → deterministic fallback. |
| 13 | Does the merchant retain pricing authority? | **YES** | `_resolve_items:91`; no price field in any DTO. |
| 14 | Does the merchant retain inventory authority? | **YES** | Reservations plus a strict dual stock check at `:717`. |
| 15 | Does the merchant retain authorization authority? | **YES** | Delegation validated at create, update and immediately before payment. |
| 16 | Does the merchant retain payment-execution authority? | **YES** | All Razorpay calls confined to `payment_service.py`, server-side only. |
| 17 | Is money-critical logic deterministic (not prompt-based)? | **YES** | Gating, pricing, revalidation, state machine, idempotency — all pure Python. Prompts add constraints but enforce nothing, and nothing relies on them. |
| 18 | Is Razorpay actually integrated (test mode)? | **CODE YES / LIVE NOT VERIFIABLE** | Orders API, Checkout.js modal, HMAC signature verification, saved-token charge; `rzp_test_*` keys in `.env`. No call could be made from this environment. |
| 19 | Is idempotency real (no double charge)? | **YES, with a scoping bug** | Snapshot replay verified by test and by the independent client; keys are not scoped to endpoint/session (N6). |
| 20 | Could a judge independently verify the claims? | **PARTIALLY** | Strong: "Razorpay invoked YES/NO" from the audit trail, chain verification in the UI, a second independent client. Weak: `/explain` misreports the most quotable decline, the amount displayed is not provably the amount charged, and the PRD the code cites is not in the repo. |

**Track verdict in one line:** the project does the hard, unglamorous parts of this track properly — deterministic authority, a real protocol, a real audit chain, a real failure — and then leaves two money-safety holes on the least-demoed paths.

---

## PART 6 — MOCK vs REAL MERCHANT FAIRNESS

Per the brief, the project is **not** penalised for lacking Zepto integration, real delivery, logistics, KYC or merchant onboarding — the track does not require them, and a mock grocery merchant is the stated design.

**Legitimately mocked, correctly out of scope:**

| Mocked | Fair? | Why |
|---|---|---|
| Grocery catalog (45 products, static JSON) | **Fair** | Realistic, categorised, priced; sufficient to exercise policy and inventory. |
| Delivery, fulfilment, logistics | **Fair** | Track is about the transaction, not the last mile. Nothing claims otherwise. |
| Merchant onboarding / KYC | **Fair** | Delegations are seeded; out of scope. |
| A single merchant | **Fair** | Multi-tenancy is not a track requirement. |
| In-memory agent sessions | **Fair for a demo** | Money state is in SQLite; only conversational state is volatile. |
| Simulated capture (`sim` driver) | **Fair, and clearly labelled** | Marks `simulated=True` in the payment outcome and the audit payload. Honest. |

**Not a fairness question — these are real gaps regardless of the mock boundary,** because they concern the transaction itself rather than the surrounding business:

| Gap | Why it is not excused by "it's a mock merchant" |
|---|---|
| C-1 amount drift | Razorpay is real. The amount authorised is a real amount. |
| C-2 ungated callback | The callback is a real gateway integration point, reachable in the demo. |
| C-3 no transition guard on the finaliser | The state machine is the product's own claim. |
| M8 fabricated orders when credentials are absent | Fail-open on a money path is a defect at any scale. |
| H-1 explain bug | Explainability is the track's bar, not a nice-to-have. |
| R5 deletable audit trail | Append-only is the project's own stated property. |

**One reverse-fairness point in the project's favour:** the mock merchant is used *harder* than a mock usually is. `POST /admin/packs/{id}/stock` lets an operator zero out stock mid-transaction, which is what makes the TOCTOU demo a real race rather than a scripted animation, and the reservation model deliberately does not shield actual stock (`:716-717`) so the race can actually be lost. That is a mock built to be tested against, and it deserves credit.

---

## PART 7 — CONSOLIDATED GAP TABLE

Status: PASS / PARTIAL / FAIL / NOT VERIFIABLE. Risk: CRITICAL / HIGH / MEDIUM / LOW.

| Area | Requirement | Actual Implementation | Status | Evidence | Risk | Required Fix |
|---|---|---|---|---|---|---|
| Payment amount integrity | The amount charged must equal the amount validated and approved | Open order reused with no amount or version binding; no order-amount persisted | **FAIL** | `checkout_service.py:759-763`; `update_checkout:435-439`; `confirm_checkout:574-577`; `entities.py` (no amount column) | **CRITICAL** | Persist `razorpay_order_amount_paise` + the version it was created for; reuse the order only if `order_amount == total_rupees*100` **and** `order_version == cs.version`; otherwise create a fresh order. Clear `razorpay_order_id` in `update_checkout` and `confirm_checkout`. |
| Terminal-state protection | Completed / declined / cancelled sessions must never be charged or re-charged | `payment_callback` has no status check; finaliser has no transition guard | **FAIL** | `api/checkout.py:179-239`; `checkout_service.py:896-978` | **CRITICAL** | Reject the callback unless `cs.status in {"requires_payment","payment_pending"}`; on a terminal session, record `payment_received_after_terminal` and initiate a refund path rather than completing. |
| State machine enforcement | No path may set a state without passing `VALID_TRANSITIONS` | `_finalize_payment` writes `completed`/`payment_pending`/`declined` with no guard | **FAIL** | `checkout_service.py:908`, `:931`, `:942` vs `:206-213` | **CRITICAL** | Call `_assert_transition(cs, target)` at the top of each branch of `_finalize_payment`. |
| Order creation safety | Missing credentials must fail closed | Fabricates `order_sim_<hex>` and reports success | **FAIL** | `payment_service.py:52-60` (contrast `:148-149`, which fails closed) | **HIGH** | Raise `ProtocolError(PAYMENT_FAILED)` when `has_credentials()` is false, unless `driver == "sim"` is explicitly configured. |
| Explainability | Declines must be explained by rule | Iterates a JSON **string** as a list; create-time declines render "All checks passed" + "still in progress" | **FAIL** | `explain_service.py:45`, `:54`, `:64` vs `audit_service.py:53`; correct decoding at `api/audit.py:22` | **HIGH** | `json.loads(ev.rules_fired)` once at the top of the loop. One-line fix, judge-facing impact. |
| Idempotency scoping | Keys must be scoped to endpoint and session | `db.get(IdempotencyKey, key)` ignores both persisted columns; replay also precedes ownership | **FAIL** | `checkout_service.py:219-225`; replay at `:609-611` **before** `_check_ownership` at `:614` | **HIGH** | Filter on `(key, endpoint, session_id)`; move the replay **after** ownership, or verify the requester owns the replayed session before returning it. |
| Reported vs actual amount | The API and UI must report the amount actually authorised | `payment.amount` = session total; audit line "Paid ₹{session total}" | **FAIL** | `serialize_session:150`; `_finalize_payment:916` | **HIGH** | Report the persisted order amount; keep the session total as a separate field. |
| Transaction boundary | A gateway call must not sit inside a rollback-able transaction | Razorpay called inside route-level `mutate()`; idempotency snapshot stored after | **PARTIAL** | `api/checkout.py:101-136`; `checkout_service.py:774`, `:815-829` | **HIGH** | Commit an `order_created` intent row **before** the gateway call; finalise in a second transaction. |
| Declined-path evidence | Every validation event must carry its checks | `_fail` writes `final_validation` with no payload and `details: {}` | **FAIL** | `checkout_service.py:638-646`, `:661` vs `:741-750`; consumer at `OpsView.jsx:159-160`, `:222` | **HIGH** | Pass `payload={"checks": [...]}` and populate `details` with the failing rule(s). Fixes the blank policy panel on the flagship demo. |
| Admin authorization | Privileged operations must be protected | Default token hardcoded in shipped client JS; `allow_origins=["*"]`; reset deletes audit rows | **FAIL** | `config.py:20`; `dashboard/src/api.js:13`; `main.py:36-41`; `api/admin.py:74-95` | **HIGH** | Move the token out of client code; require an explicit non-default value; restrict CORS; exclude `AuditEvent` from reset (or write a signed `demo_reset` marker and start a new chain). |
| LLM outcome trust | An LLM must not be able to assert that payment happened | `finish(outcome="completed")` accepted unverified; buyer reports "PAID" | **FAIL** | `agent/loop.py:687-701`; `agent/prompts.py:32-57`; `buyer_service/agent.py:184-195` | **HIGH** | Before terminating as `completed`, assert the `CheckoutSession` is `completed` **and** a `razorpay_payment_id` exists; otherwise downgrade to `unable_to_fulfill`. |
| Append-only audit | Audit rows must never be deleted | `/admin/demo/reset` deletes `AuditEvent` | **FAIL** | `api/admin.py:74-95`; docstring claim at `audit_service.py:5` | **HIGH** | See admin fix; or delete transactions only and keep the chain. |
| Discovery stock accuracy | Advertised availability must match gating's view | Discovery reports `stock_actual`, ignoring reservations | **FAIL** | `discovery_service.py:297` vs `inventory_service.available_stock:46-48` and `agent/tools.py:96`; displayed at `SubstituteCard.jsx:29` | **MEDIUM** | Use `inventory_service.available_stock` in discovery. |
| Gating on confirmation | Policy should hold whenever terms change | `confirm_checkout` re-prices and returns to `requires_payment` without re-gating | **PARTIAL** | `checkout_service.py:569-577` (no `_gate` call) | **MEDIUM** | Re-run `_gate` on acceptance; decline instead of returning to `requires_payment` if it fails. |
| Reservation auditability | Inventory movements must be explainable | Zero `append_event` calls in `inventory_service` | **PARTIAL** | `inventory_service.py:23-94` | **MEDIUM** | Emit `reservation_released` / `reservation_consumed` / `reservation_expired`. |
| Oversell detection | Selling unavailable stock must be recorded | `max(0, stock - qty)` silently clamps | **PARTIAL** | `checkout_service.py:906` | **MEDIUM** | Detect the clamp and write an `oversell_detected` audit event. |
| Read authorization | Session and audit reads should be scoped | `GET /checkout_sessions/{id}`, `/audit_logs`, `/audit_logs/verify`, `/explain/{id}`, `/reconcile` all unauthenticated | **FAIL** | `api/checkout.py:95-98`, `:242-246`; `api/audit.py:31`, `:44`, `:50` | **MEDIUM** | Require buyer credentials + ownership on session reads and reconcile; admin-gate audit reads (or accept publicly, but say so in the spec). |
| Protocol completeness | The spec must describe the implemented surface | `payment_callback` and `reconcile` absent; JSON Schemas published but never enforced | **PARTIAL** | `protocol/protocol_spec.md` endpoint table | **MEDIUM** | Document both routes with their guards; optionally validate responses against the schemas in tests. |
| Configuration honesty | Config must describe its own live values | `config.py:16` comment says `# sim \| checkout`; live value is `saved` | **PARTIAL** | `config.py:16` vs `.env` (README documents `saved` correctly) | **LOW** | Update the comment to `sim \| checkout \| saved`. |
| Agent session durability | Paused agent sessions should survive a restart | In-memory dict only | **PARTIAL** | `agent/state.py:18` | **LOW** | Acceptable for a demo; note it rather than fix it before submission. |
| Delegation revocation | Authority should be revocable | `status != "active"` is enforced, but nothing ever writes `revoked` | **PARTIAL** | `entities.py:46`; `delegation_service.py:15-31`; no writer found repo-wide | **LOW** | Add an admin route, or state that revocation is out of scope. |
| Test coverage of money paths | Tests must cover the paths that move money | No test touches `payment_callback` or `reconcile`; none covers amount drift; suite always runs credential-less | **FAIL** | `merchant_agent/tests/*`; `conftest.py:11-16` | **HIGH** | Add: callback on terminal session rejected; callback amount mismatch rejected; reuse-after-reprice creates a new order; cross-endpoint key rejected; explain on a spend-limit decline names the rule. |
| Documentation accuracy | Claims must match the code | README "terminal-state protection … never re-charge" and "append-only"; README "34 pytest cases" (actual 48); `AUDIT_REPORT.md` stale on tool count, file size and whether the merchant is an agent, and stops at Section H | **PARTIAL** | `README.md` safety-properties list; `AUDIT_REPORT.md:119`, `:56` | **MEDIUM** | See Part 9 for exact rewordings. |
| Submission integrity | The submitted repository must contain the system being demonstrated | `merchant_agent/agent/`, `api/agent.py`, `test_merchant_agent.py`, `MerchantAgentTrace.jsx` and 3 scripts are untracked; `main.py` mounting the agent router is uncommitted; 26 tracked files modified | **FAIL** | `git ls-files merchant_agent/agent/` (empty); `git status --short`; `git show HEAD:merchant_agent/main.py:14` | **CRITICAL** | `git add -A && git commit`, then verify a clean checkout still collects 48 tests and still serves `/agent_sessions`. |
| PRD traceability | Cited requirements must be locatable | Code cites `PRD §…`, `FR-MA-…`, `NFR-…`, `G…`; no PRD in the repo | **FAIL** | Repo root contains only README, AUDIT_REPORT, protocol spec | **MEDIUM** | Commit the PRD, or drop the citations. |

---

## PART 8 — CRITICAL ISSUES THAT COULD CAUSE JUDGE FAILURE

Only issues that can produce a wrong money outcome, a false claim in front of a judge, or a broken demo are listed here.

### C-1 — CRITICAL — A payment can be authorised for an amount the merchant never validated

`checkout_service.py:759-763` reuses an existing `razorpay_order_id` whenever the session is `requires_payment`, without comparing the order to the freshly revalidated total, and neither `update_checkout` nor `confirm_checkout` clears that order id when they change the totals. Full trace and reproduction in Part 3.2. In the configured `saved` driver the token charge is rejected for amount mismatch and then **deliberately falls back to the modal on the same stale order** (`:830-845`), so the human is charged the old amount while the merchant books and audits the new one (`_finalize_payment:916`). Nothing persists the authorised amount, so the divergence is undetectable after the fact.

**Judge exposure:** this is the exact question a payments-literate judge asks — *"show me that the amount charged is the amount you validated."* Today the answer cannot be produced from the system's own records. A live session in `data/merchant.db` (`cs_3a24cd3c29`, order `order_TT5LO4V474QIpU`, total 230) is already sitting in the vulnerable window.

**Mitigating context, stated fairly:** the reuse is intentional duplicate-order avoidance with a passing test for the unchanged-basket case (`tests/test_saved_driver.py:31-42`). The fix is a missing condition, not a redesign.

### C-2 — CRITICAL — A cancelled or declined session can be completed through the payment callback

`api/checkout.py:179-239` enforces exactly two things: the order id matches the session, and the Razorpay signature verifies. There is no session-status check, no amount check, no ownership check, no idempotency. `_finalize_payment` then marks the session `completed`, decrements stock and consumes reservations. Because Razorpay itself produces the signature, no forgery is required — a modal left open across a cancel or a stock decline is sufficient (Part 3.4). This directly violates protocol guarantee #1 and the README's *"Terminal-state protection — completed/declined/cancelled sessions never re-charge."* **No test exercises this endpoint at all.**

### C-3 — CRITICAL — The payment finaliser bypasses the state machine

`_finalize_payment:896-978` writes `completed` (`:908`), `payment_pending` (`:931`) and `declined` (`:942`) and never calls `_assert_transition`, though `VALID_TRANSITIONS:47` already forbids transitions out of terminal states. `complete_checkout` guards once at `:629`, but `finalize_with_outcome:892` (used by the callback) and `reconcile_session:993` enter the finaliser directly. This is the enabling defect beneath C-2 and is a one-line-per-branch fix.

### C-4 — CRITICAL (submission integrity) — The entire Merchant Agent is uncommitted

This was found by git verification, not by reading code, and it is the finding most likely to cost the team the submission outright.

`git ls-files merchant_agent/agent/` returns **nothing**. `git status --short` reports as untracked (`??`):

- `merchant_agent/agent/__init__.py`, `loop.py`, `prompts.py`, `state.py`, `tools.py` — the whole agent
- `merchant_agent/api/agent.py` — every `/agent_sessions` route
- `merchant_agent/tests/test_merchant_agent.py` — 12 of the 48 tests
- `dashboard/src/components/MerchantAgentTrace.jsx` — the trace UI
- `scripts/audit_bypasses.py`, `scripts/audit_smoke.py`, `scripts/demo_merchant_agent.py`

And `merchant_agent/main.py` is **modified but uncommitted**: the working-tree version imports and mounts `agent.router` at `:46`, while `git show HEAD:merchant_agent/main.py:14` imports only `admin, audit, checkout, discovery`. Twenty-six other tracked files, including `buyer_service/agent.py`, `dashboard/src/views/OpsView.jsx`, `config.py`, `models/dto.py` and seven of the nine test files, are likewise modified and uncommitted. The three commits in the repository are titled `wip: cart-centric chat build (pre-rollback snapshot, known broken)`, `restore: bill-centric baseline`, and `fix: token-charge rejection falls back to Checkout modal`.

**What this means concretely.** A judge who clones the repository at `HEAD` gets a competent deterministic commerce API with **no merchant agent, no agent-to-agent protocol, no agent trace UI and 12 fewer tests** — that is, none of the agentic layer the submission is built around. The single most differentiating part of this project exists only on one laptop's working tree. There is no branch, stash or tag holding it.

**Severity rationale.** No money moves incorrectly because of this, so it is not a money-safety gap in the sense of C-1 to C-3. It is listed as CRITICAL because the consequence — an unrecoverable loss of the flagship feature, or a judge evaluating a repository that does not contain the thing being demonstrated — is at least as damaging as any defect in this report, and because it is the cheapest of all findings to fix.

**Required fix (do this before anything else in this document):** `git add -A && git commit`. Then verify the claim rather than assume it: `git stash -u && git status --short` should be clean, `python -m pytest merchant_agent/tests -v` should still collect 48 tests from a clean checkout, and `python -m uvicorn merchant_agent.main:app` should still expose `/agent_sessions` and `GET /health/agent`. Confirm `.env` remains ignored and no key material enters the commit (`git diff --cached --stat`; secret scanning of tracked files is currently clean and must stay clean).

### H-7 — HIGH — The merchant LLM can claim a payment that never happened

`agent/loop.py:687-701` accepts `finish(outcome="completed")` from the model and terminates the agent session as completed with no verification that any payment occurred; the tool schema describes `completed` as *"paid/fulfilled."* `buyer_service/agent.py:184-195` then tells the buyer LLM *"The order is PAID and placed."* No money moves and no state is corrupted — but the user is told an order was paid when it was not, which is the most quotable possible failure for this track. Invisible to the suite because `conftest.py:16` disables the LLM brain, so **no test exercises the LLM decision path at all**.

### H-1 — HIGH — The explanation endpoint misreports the most demo-friendly declines

`explain_service.py:45`, `:54` and `:64` iterate `rules_fired` (a JSON **string**) as if it were a list. For **create-time** policy declines — spend limit, category blocked, quantity limit, expired delegation — the output is *"All deterministic policy checks passed."* with the headline *"This transaction is still in progress."* on a terminally declined session, plus *"The transaction was declined ([)"*. Completion-time declines escape only because of the `or ev.reasoning_summary` fallback at `:56`. The single explain test (`tests/test_audit_and_errors.py:48-55`) uses the completion-time case and asserts substrings, so it passes over corrupted output.

**Judge exposure:** the README's adversarial demo (`scripts/scenario_check.py`, a 50-unit over-budget rejection) is precisely a create-time spend-limit decline. If a judge clicks Explain on it, the dashboard says all policy checks passed and the transaction is in progress.

### H-6 — HIGH — The policy panel goes blank on the flagship failure demo

`_fail:638-646` writes `final_validation` with no payload, while the approved path writes the full check array (`:741-750`). `OpsView.jsx:159-160` selects the **last** `gating_evaluated`-or-`final_validation` event and renders `payload?.checks || []`, so on a completion-time decline the "Policy checks" grid renders empty — during step 4 of the README's own demo script. The error envelope is likewise `details: {}` (`:661`), so an API-only judge sees no machine-readable reason either.

### H-5 — HIGH — The audit trail can be erased from a browser tab

`config.py:20` defaults `admin_token` to `admin_demo_token`; `dashboard/src/api.js:13` hardcodes that exact value into shipped client JavaScript; `main.py:36-41` sets `allow_origins=["*"]`; `api/admin.py:74-95` deletes `AuditEvent` rows. Any page in any tab can therefore wipe the tamper-evident trail that the project's central claim rests on.

### H-4 — HIGH — A gateway call inside a rollback-able transaction

The Razorpay call happens inside the route's `db.mutate()` (`api/checkout.py:101-136`), and the completion idempotency snapshot is stored afterwards (`:113-114`). Any exception after the gateway call rolls back the order id and the session row while the charge stands — and the 500 envelope then tells the client *"transaction not executed"* (`main.py:87`). No such exception is currently reachable, which is why this is HIGH and not CRITICAL, but the pattern is the standard cause of lost payments.

### H-8 — HIGH — Orders are fabricated when credentials are missing

`payment_service.create_order:52-60` returns `order_sim_<hex>` with `_simulated_no_credentials: True` instead of failing. A misconfigured deployment reports successful orders that do not exist at Razorpay, and — because `conftest.py:13-14` blanks the keys — **this is the code path every single test runs through**. Contrast `verify_checkout_signature:148-149`, which correctly fails closed on the same condition.

---

## PART 9 — OVERCLAIM AUDIT

Each row states a claim made by the project's own documentation or UI, the code that bears on it, whether the claim survives, and wording that would be defensible.

### 9.1 — `README.md`, "Safety properties" list

| # | CLAIM | ACTUAL EVIDENCE | JUSTIFIED? | RECOMMENDED WORDING |
|---|---|---|---|---|
| 1 | "Terminal-state protection — completed/declined/cancelled sessions never re-charge" | `complete_checkout` does guard (`:629`), and `test_state_machine.py:18-39` proves replay and terminal rejection on **that** route. But `payment_callback` (`api/checkout.py:179-239`) has no status check, and `_finalize_payment:896-978` has no `_assert_transition`, so a declined or cancelled session **can** be flipped to `completed` (C-2, C-3). | **NO — this is the single most consequential overclaim in the repo.** | "The `/complete` endpoint rejects any attempt to charge a session that is already completed, declined or cancelled, and replays the original result for a repeated `Idempotency-Key`. The gateway callback path does not yet enforce the same check — see Known Limitations." |
| 2 | "Append-only audit trail with SHA-256 hash chaining" | Chain construction and verification are real and correct (`append_event:33-64`, `verify_chain:81-90`). But `/admin/demo/reset` deletes `AuditEvent` rows (`api/admin.py:74-95`) using a token hardcoded in shipped client JS (`api.js:13`), so the log is erasable by design. | **PARTIALLY.** The chain is genuine; "append-only" is not, in the shipped configuration. | "Hash-chained audit trail: every event stores `prev_hash` and its own SHA-256, and `/audit_logs/verify` detects any modification or deletion. A demo-reset endpoint intentionally clears the chain between runs; it is admin-token protected and is not part of the transaction path." |
| 3 | "Server-authoritative pricing — buyers can never submit prices" | `LineItem` in `models/dto.py` accepts only `pack_id` and `quantity`; `_resolve_items:76-95` reads `pack.price` from the DB with the comment *"authoritative price — never client-supplied"*; every total flows from `_compute_totals:98-108`. | **YES — fully supported.** | Keep as written. This is one of the strongest properties in the system. |
| 4 | "Final revalidation immediately before payment (TOCTOU-proof)" | The ladder in `complete_checkout:596-780` genuinely re-resolves prices, re-checks delegation, re-checks stock **twice** (reserved and actual) and re-runs the full policy engine before any gateway call, all inside a process-wide lock. `test_inventory.py:80-95` proves Razorpay is not reached when stock has vanished. | **YES for the `/complete` path**, and this is real engineering. **NO as an unqualified claim**, because the revalidated total is not bound to the Razorpay order that gets charged (C-1). | "Every `/complete` call re-prices, re-checks delegation, re-checks reserved and actual stock, and re-runs the full policy engine inside a single lock before the gateway is contacted; if any check fails, Razorpay is never called." (Drop "TOCTOU-proof" until C-1 is fixed.) |
| 5 | "34 pytest cases" | 48 test functions across 9 files. | **Understated** — the only claim in the repo that is *more* conservative than reality. | "48 tests across 9 files." |
| 6 | Implicit: the merchant is an agent | `merchant_agent/agent/` is real: an observe→reason→act→adapt loop (`loop.py`), a 12-tool allowlist with privilege classes (`tools.py`), iteration and tool-call budgets, and a working substitution/negotiation path. | **YES**, with the caveat in Part 4: it is a bounded tool-using agent, not an autonomous one, and its financial authority is delegated to deterministic code. | "A bounded merchant agent: it plans, substitutes and negotiates, but every money-affecting action is executed by deterministic services that can overrule it." |

### 9.2 — `AUDIT_REPORT.md` (the team's own self-audit)

This file deserves credit before criticism: it independently found the `_finalize_payment` guard gap, the two additional call sites, the spec's omission of `payment_callback` and `reconcile`, the discovery/reservation stock mismatch, and the idempotency-before-ownership ordering. Those findings are correct and this audit confirms all five.

| # | CLAIM | ACTUAL EVIDENCE | JUSTIFIED? | RECOMMENDED WORDING |
|---|---|---|---|---|
| 7 | "The Buyer Agent exposes exactly three tools" | `buyer_service/agent.py` registers five tool schemas; `buyer_service/tools.py` exposes twelve protocol methods. | **NO — stale.** | "five buyer tools over a twelve-method protocol client." |
| 8 | "`buyer_service/agent.py` (572 lines)" | 731 lines. | **NO — stale.** | Drop hard line counts, or regenerate them. |
| 9 | "`merchant_agent` is *not* an agent — it is a deterministic API" | The files exist in the working tree and are wired into the working-tree `main.py:46`. **But git verification exonerates this claim:** `merchant_agent/agent/` and `api/agent.py` are **untracked** (`git ls-files merchant_agent/agent/` returns nothing), and the committed `HEAD:merchant_agent/main.py:14` imports only `admin, audit, checkout, discovery`. Measured against the committed repository — which is what a judge clones — the statement was **accurate when written**. | **Correct for HEAD; stale only against uncommitted work.** No criticism is warranted. The real problem is C-4, not this sentence. | "As of the last commit the merchant is a deterministic transaction engine; the merchant-agent layer exists in the working tree and must be committed before it can be claimed." |
| 10 | Titled "FINAL TECHNICAL AUDIT" | The document stops after Section H; Sections I–V, the money-path trace, the gap table and the verdict are absent. | **NO — it is an in-progress audit, not a final one.** | Retitle "Interim technical audit (Sections A–H)" or complete it. Shipping an audit labelled *final* that stops a third of the way through is itself a judge risk. |

### 9.3 — Code-level claims that overstate

| # | CLAIM | ACTUAL EVIDENCE | JUSTIFIED? | RECOMMENDED WORDING |
|---|---|---|---|---|
| 11 | `audit_service.py:5` docstring: "append-only" | Same as row 2. | **PARTIALLY.** | "append-only within a run; cleared only by the explicit demo-reset admin route." |
| 12 | `_fail`'s audit line: `"{code}: Razorpay NOT called"` | Accurate on the `/complete` decline path — this is a well-chosen, judge-legible audit string and `test_inventory.py:86-87` proves it. | **YES.** | Keep. |
| 13 | `_finalize_payment:916`: `f"Paid ₹{json.loads(cs.totals)['total']}"` | Writes the **session** total, not the gateway's authorised amount. Under C-1 these differ, and the audit trail then records an amount that was never charged. | **NO.** | Record both: `authorized_paise` from the order and `session_total` from the checkout, and flag a mismatch. |
| 14 | `config.py:16` comment: `# sim \| checkout` | The live `.env` sets `PAYMENT_DRIVER=saved`, and the README documents `saved` as the default with a full driver table. The comment — not the README — is the stale artifact. | **NO (comment only).** | `# sim \| checkout \| saved` |
| 15 | Code comments citing `PRD §…`, `FR-MA-…`, `NFR-…`, `G…` throughout | No PRD, FR or NFR document exists in the repository. | **NOT VERIFIABLE.** The citations are self-consistent and specific, which suggests a real document exists outside the repo; but no reader — and no judge — can check a single one. | Commit the PRD to `docs/`, or reduce the citations to prose. |

---

## PART 10 — DEMO READINESS

Assessed against the four demonstrations the README and `scripts/scenario_check.py` set up. **Every judgment below is a code-path prediction, not an observed run** — see Part 0.2 and Appendix A.

### Demo 1 — Successful agent-to-agent purchase

**Path:** buyer chat → NIM tool loop → `/agent_sessions` → merchant agent loop → `/checkout_sessions` → human approval → `/complete` → Razorpay → `completed`.

**Expected to hold:** the ladder is correct and well ordered; the human-approval gate is deterministic and cannot be argued past by either LLM (`buyer_service/agent.py:391-411`, `resume_after_decision:412+`); the OpsView timeline, gating grid and "Razorpay invoked: YES" indicator all populate on the approved path because `_gate` writes `payload.checks` (`:741-750`).

**Risks, in order:**
1. **Two live NIM calls on the critical path** (buyer loop and `_llm_decide`) with `MA_LLM_BRAIN=true` in `.env`. A timeout or 429 mid-demo is the single most likely failure. Mitigation already exists — `_fallback_decide` — but the buyer side has no equivalent. **NOT VERIFIABLE** here (no network).
2. **H-7:** if the model calls `finish(outcome="completed")` early, the UI will claim "The order is PAID and placed" with no payment. Untested by design (`conftest.py:16`).
3. `payment_driver=saved` means the demo depends on a saved-token charge succeeding against Razorpay test mode, with a modal fallback (`:830-845`). The fallback works, but it is also the C-1 overcharge path.

**Verdict: LIKELY TO SUCCEED, with a live-LLM dependency that cannot be verified here.**

### Demo 2 — Policy rejection (over-budget basket)

**Path:** `scripts/scenario_check.py` builds a basket above `max_order_value` → `create_checkout` → `_gate` fails `spend_limit` → `declined`, Razorpay never called.

**Expected to hold:** the enforcement itself is solid, deterministic and well tested (`test_gating.py`, 9 cases). The decline is written with `decline_code`, `rules_fired` and a "Razorpay NOT called" audit line.

**Risks:**
1. **H-1 fires exactly here.** This is a *create-time* decline, so `/explain` will output "All deterministic policy checks passed." with the headline "This transaction is still in progress." and the string "declined ([)". If a judge clicks Explain on the flagship rejection demo, the system contradicts itself on screen. **This is the highest-value single fix in the report: one line.**
2. Create-time declines do carry `payload.checks` (`_gate` runs before `_fail`-style handling at `:343-344`), so the policy grid should render — but the explanation panel beside it will be wrong.

**Verdict: THE ENFORCEMENT WILL DEMO WELL; THE EXPLANATION WILL EMBARRASS.**

### Demo 3 — Graceful failure (stock vanishes between reservation and payment)

**Path:** create a session (stock reserved but not shielded) → drain `stock_actual` out of band → `/complete` → strict dual stock check fails → `declined`, `STOCK_UNAVAILABLE`, `retryable: true`, reservations released, Razorpay never called.

**Expected to hold:** this is the requirement the track names explicitly — *"one failure handled gracefully"* — and the implementation genuinely satisfies it. `test_inventory.py:80-95` asserts that `razorpay_order_created` and `payment_success` are **absent** and `final_validation` is **present**. That is real evidence, not a test named after a feature.

**Risks:**
1. **H-6:** the OpsView policy grid renders **blank** on this path, because `_fail` omits `payload.checks`. The single most important screen in the most important demo shows an empty panel.
2. The API error envelope is `details: {}` — an API-only judge sees a code but no per-rule reason.
3. The reproduction requires draining stock out of band; if the demo script relies on a second client racing, timing is not guaranteed.

**Verdict: CORRECT UNDERNEATH, WEAK ON SCREEN. Fix H-6 before demoing.**

### Demo 4 — Audit trail and integrity verification

**Path:** OpsView timeline + `/audit_logs/verify` → `{"valid": true, "count": n}` → IntegrityPanel.

**Expected to hold:** the chain is real. `verify_chain` recomputes every hash and every link (`audit_service.py:81-90`), and `test_audit_and_errors.py` covers both the valid chain and a tampered row. `api/audit.py:22` decodes `rules_fired` correctly, so the raw log renders properly even though `/explain` does not.

**Risks:**
1. If a judge asks *"can you delete an event?"*, the honest answer is yes — `/admin/demo/reset`, with a token visible in the shipped bundle (H-5).
2. Reservation lifecycle events are absent, so the timeline cannot show a release or an expiry.
3. Under C-1 the trail records an amount that may not be the amount charged (row 13 above) — a judge who cross-checks the Razorpay dashboard against the audit line could find them disagreeing.

**Verdict: STRONGEST OF THE FOUR. Prepare an honest answer on the reset endpoint rather than hoping the question is not asked.**

### Cross-cutting demo risks

| Risk | Evidence | Severity for the demo |
|---|---|---|
| Live NIM dependency on two paths, no offline buyer fallback | `.env` `MA_LLM_BRAIN=true`; `buyer_service/agent.py` NIM client | HIGH |
| `saved` driver depends on a valid saved token in Razorpay test mode | `payment_service.py:100-160` | MEDIUM |
| **The demoed merchant agent is not in any commit** — a laptop failure or a repo-URL submission loses it entirely | `git status --short`; `git ls-files merchant_agent/agent/` empty | **CRITICAL** |
| Existing DB already contains a session in the C-1 vulnerable window | `cs_3a24cd3c29`, `order_TT5LO4V474QIpU`, total 230 | MEDIUM — reset before demoing |
| `buyer_service_debug.log` grows unconditionally during demos (gitignored, so not shipped) | `agent.py:327-331` | LOW |
| Merchant-agent sessions are in-memory; a restart loses paused proposals | `agent/state.py:18` | LOW — do not restart mid-demo |

---

## PART 11 — SCORES

### 11.1 — PRD implementation score: **74 / 100**

| Area | Weight | Score | Rationale |
|---|---|---|---|
| A — Catalog | 5 | 5 | 45 products, 74 packs, 9 categories, real substitution data, server-authoritative prices. |
| B — Discovery | 6 | 4.5 | Works with an LLM parse plus a deterministic fallback; loses points for advertising `stock_actual` without subtracting reservations. |
| C — Buyer Agent | 10 | 8.5 | Real tool-using agent, protocol-only access, deterministic approval gate. Loses points for H-7's downstream "order is PAID" message. |
| D — Merchant Agent | 10 | 8 | Genuine loop, tool allowlist, privilege classes, budgets. Loses points for the unverified `finish(completed)` and in-memory state. |
| E — Protocol | 6 | 4.5 | Clean surface, versioned, typed errors. Spec omits two implemented routes; schemas published but unenforced. |
| F — Agent independence | 8 | 8 | **Fully clean.** No DB, Razorpay, gating or policy access from the buyer — verified by search, not assumed. |
| G — Delegated authority | 8 | 6.5 | Tokens are real, server-side and re-validated at completion. No revocation path; not bound to a specific basket or amount. |
| H — Deterministic gating | 10 | 9.5 | Five pure rules, no LLM anywhere near them, re-run before payment, 9 tests. The strongest area in the project. |
| I — Price integrity | 8 | 4 | Server-authoritative pricing is exemplary; **the authorised amount is not bound to the validated total (C-1)**, which is what this area exists to guarantee. |
| J — Inventory / reservations | 6 | 4.5 | TTL reservations, dual stock check, deliberate TOCTOU surface. Unaudited lifecycle; silent oversell clamp. |
| K — Checkout state machine | 8 | 4 | The table and `_assert_transition` are correct and tested — but the finaliser bypasses them entirely (C-3) and the callback ignores state (C-2). |
| L — Final revalidation | 6 | 5 | Ladder ordering is correct and provable. Docked for the amount-binding gap. |
| M — Razorpay | 6 | 3.5 | Three drivers, real signature verification, retry-safe order reuse. Fails open without credentials; no amount verification; no persisted order amount. |
| N — Idempotency | 5 | 3 | Full response-snapshot replay works and is tested; keys are unscoped and replay precedes authorization. |
| O — Failure handling | 6 | 4.5 | Typed errors, graceful stock decline, LLM fallback brain, modal fallback. Callback and reconcile untested. |
| P — Audit trail | 6 | 5 | Comprehensive, hash-chained, verifiable. Two omissions (`payload` on `_fail`, reservation events) and one misstated amount. |
| Q — Explainability | 5 | 2 | Endpoint and UI exist and read well on approvals; **broken on create-time declines (H-1)**, which are the declines a judge will look at. |
| R — Audit integrity | 5 | 3.5 | Chain and verifier are genuine; append-only is violated by an internet-reachable reset. |
| S — Dashboard | 5 | 4 | Two purposeful views, live polling, integrity panel. Shows an amount that may not be the amount charged; admin token in client JS. |
| T — Second client | 3 | 3 | `buyer_b/client.py` is a genuinely independent protocol client. |
| U — Security / boundary | 5 | 2.5 | Agent boundary is clean; HTTP boundary is not (CORS `*`, default admin token in the bundle, unauthenticated reads). |
| V — Test quality | 8 | 5.5 | 48 real tests with meaningful assertions and one honest adversarial script. Zero coverage of the callback, reconcile, amount drift, or the LLM brain; one tautological assertion. |

Weighted result: **74 / 100.**

### 11.2 — Track compliance score: **76 / 100**

The track's bar is *"Every money action explainable, bounded and gated. Show the audit trail and one failure handled gracefully."*

| Track criterion | Score | Rationale |
|---|---|---|
| Merchant transactable by an AI buyer end to end | 18 / 20 | Real, and the strongest thing about the submission: an AI buyer reaches a real payment through a real protocol with no human in the loop except one deliberate approval. |
| Every money action **gated** | 15 / 20 | The `/complete` gate is excellent. Two other paths into the finaliser are not gated (C-2, C-3), and the amount itself is ungated (C-1). |
| Every money action **bounded** | 17 / 20 | Delegation caps, category and quantity limits, agent iteration and tool-call budgets, a tool allowlist with privilege classes. Bounds are real and server-side. Docked because the bound is on the *validated* total, not the *charged* amount. |
| Every money action **explainable** | 12 / 20 | The data to explain every decision exists and is hash-chained. The endpoint that renders it is broken on the most explainable decisions. |
| Audit trail shown | 8 / 10 | Real chain, real verifier, real UI. Docked for erasability and the two omissions. |
| One failure handled gracefully | 6 / 10 | Substantively correct — and docked hard because the panel that displays it renders blank (H-6). |

Weighted result: **76 / 100.**

**Why track > PRD:** the track rewards demonstrated bounded agentic commerce, which this project genuinely achieves. The PRD score is dragged down by breadth requirements — idempotency scoping, state-machine universality, security posture, test coverage — that the track does not explicitly grade but a payments judge will still notice.

**What moves both scores fastest:** four small, local fixes — one line in `explain_service.py` (H-1), one `payload=` argument in `_fail` (H-6), three `_assert_transition` calls plus a status check on the callback (C-2/C-3), and an amount comparison at `:759-763` (C-1). None is a redesign. Together they would plausibly put the project in the high 80s on both axes.

---

## PART 12 — FINAL VERDICT

### 1. What Is Fully Correct

These items were verified in the production runtime path, not inferred from names, comments or schemas.

**Server-authoritative pricing.** The buyer cannot submit a price. `LineItem` accepts only `pack_id` and `quantity`; `_resolve_items:76-95` reads `pack.price` from the database; every total is computed by `_compute_totals:98-108`. There is no code path anywhere in the repository through which a client-supplied amount reaches a total. This is the cleanest property in the system.

**Deterministic gating with no LLM anywhere near it.** Five pure functions — `delegation_expiry`, `category_allowed`, `quantity_limit`, `spend_limit`, `stock_check` — take facts and return decisions. `gating_service.py` imports no LLM client. The rules are re-run immediately before payment, not only at basket creation. Nine tests cover them with real assertions. When the specification says *"a prompt telling the LLM to obey a budget does not count as a gate,"* this project passes that test unambiguously: the budget is enforced in Python, and the LLM has no ability to overrule it.

**Agent independence.** The Buyer Agent reaches the merchant only through HTTP. Verified by search rather than assumption: `buyer_service/` contains no SQLAlchemy import, no `merchant_agent` import, no `razorpay` import, no gating or policy import, and no Razorpay credentials. `buyer_service/tools.py` is a pure protocol client. The boundary the specification cares most about is genuinely intact.

**The completion ladder's ordering.** `complete_checkout:596-780` performs, in this order: idempotent replay, ownership, completed short-circuit, transition assertion, price re-resolution, delegation re-validation, strict dual stock check (reserved **and** actual), full policy re-run — and only then contacts Razorpay. When any check fails, `_fail` releases reservations, marks the session declined and writes an audit line stating that Razorpay was not called. `test_inventory.py:86-87` asserts the absence of `razorpay_order_created` and `payment_success` on that path. This is exactly the architecture the track asks for, and it is real.

**The hash-chained audit trail.** Every event stores `prev_hash` and its own SHA-256 over a canonical serialization; `verify_chain:81-90` recomputes both and detects modification or deletion. Both the valid and tampered cases are tested. The trail covers delegation validation, gating evaluation, order creation, payment success and failure, declines, confirmations and cancellations, with a machine-readable `rules_fired` array and a human-readable summary on each.

**Checkout state-machine design.** `VALID_TRANSITIONS:37-45` and `_assert_transition:206-213` are correct, terminal states are explicit, and replay of a completed session returns the original `payment_id` rather than re-charging (`test_state_machine.py:18-39`). The design is right; see section 3 for where it is not applied.

**Idempotent replay mechanics.** Full response snapshots are stored and replayed, so a repeated `Idempotency-Key` returns the original body rather than re-executing. Three tests cover it.

**A genuinely bounded merchant agent.** An observe→reason→act→adapt loop with two interchangeable brains, a 12-tool allowlist whose entries are classified read / write / financially_privileged, hard budgets of 8 iterations and 10 tool calls, and a busy-lock that returns 409 rather than allowing concurrent mutation. Twelve tests. It plans and substitutes; it does not hold financial authority.

**A second, independent client.** `buyer_b/client.py` speaks the protocol with no shared code with `buyer_service/`, which is what makes the protocol claim credible rather than decorative.

**Graceful failure on the stock path.** The reservation model deliberately does not shield actual stock, which creates a real TOCTOU window that the final revalidation then catches. Choosing to build the failure the track asks you to demonstrate — rather than engineering it away and then faking it — is the right call, and the decline is typed, retryable and audited.

### 2. What Is Partially Correct

**Final revalidation.** The checks are all there and correctly ordered, but the outcome of those checks is not bound to the payment instrument. The re-validated total is computed at `:756` and then never compared to the order that gets charged at `:759-763`.

**Terminal-state protection.** Enforced on `/complete`. Absent on the callback and inside the finaliser.

**Idempotency.** The replay works; the key is not scoped to endpoint or session, and replay happens before the ownership check, so a key holder can read a session they do not own.

**Explainability.** Every fact needed to explain any decision is captured and chained. The renderer mistypes one field and therefore misreports create-time declines.

**Audit completeness.** Comprehensive except for three things: `_fail` omits the check array, reservation lifecycle events are never written, and the payment audit line records the session total rather than the authorised amount.

**Audit immutability.** The chain is real and the verifier is real; the trail is deletable by an unauthenticated-in-practice admin route.

**Protocol specification.** Clean, versioned and typed, but it omits two implemented routes, and the JSON Schemas it publishes are never used to validate anything.

**Delegated authority.** Real, server-side, re-validated at completion — but not revocable through any endpoint and not bound to a particular basket or amount.

**Discovery.** Works well with a deterministic fallback, but advertises `stock_actual` while gating uses `stock_actual − reservations`, so the two disagree in the UI.

**Test suite.** 48 tests with mostly meaningful assertions and a real adversarial script — with zero coverage of the payment callback, reconcile, amount drift, or the LLM brain, and one assertion that cannot fail (`test_inventory.py:90`).

### 3. What Is Missing

- Any binding between the amount validated and the amount authorised: no persisted order amount, no comparison, no version check on order reuse.
- A status guard on `payment_callback`.
- `_assert_transition` calls inside `_finalize_payment`.
- Endpoint and session scoping on idempotency keys, and an ownership check before replay.
- Verification that a payment occurred before the merchant agent reports `completed`.
- Fail-closed behaviour in `create_order` when credentials are absent.
- Authentication on `GET /checkout_sessions/{id}`, `/reconcile`, `/audit_logs`, `/audit_logs/verify` and `/explain/{id}`.
- Reservation lifecycle audit events; an `oversell_detected` event.
- A delegation revocation endpoint.
- A commit containing the merchant agent, the agent routes, the agent tests and the agent trace UI (C-4).
- The PRD itself — dozens of code comments cite `PRD §`, `FR-MA-`, `NFR-` and `G` identifiers that no reader can resolve.
- Tests for every one of the above.

### 4. What Could Fail In Front Of Judges

Ranked by probability × visibility.

1. **A judge clones the repository and the merchant agent is not there.** `HEAD` contains no `merchant_agent/agent/`, no `/agent_sessions` routes and 12 fewer tests, because all of it is untracked (C-4). If the submission is a repo URL rather than a live laptop demo, the agentic layer — the entire premise — is absent. One `git commit` prevents this.
2. **A judge clicks "Explain" on the over-budget rejection.** The dashboard says *"All deterministic policy checks passed"* and *"This transaction is still in progress"* on a session that was terminally declined for spend limit. Highest probability, highest embarrassment, one-line fix (H-1).
3. **A judge watches the stock-failure demo and looks at the policy panel.** It is blank, because `_fail` writes no checks (H-6). This is the demo the track explicitly asks for.
4. **A judge asks "what stops the amount charged from differing from the amount you approved?"** There is currently no answer the system can substantiate from its own records, and one live session in the database is already in that window (C-1).
5. **A judge asks "can a cancelled order still be paid?"** It can, without forgery, through the callback (C-2).
6. **A NIM call times out mid-demo.** The merchant has a deterministic fallback brain; the buyer does not.
7. **A judge asks "can you delete an audit event?"** Yes — from any browser tab (H-5).
8. **A judge reads `AUDIT_REPORT.md`.** It is titled "FINAL TECHNICAL AUDIT", stops at Section H, and asserts that the merchant is *not* an agent — contradicting the README and the code in the same repository.
9. **A judge counts the tests against the README.** 48 versus 34; harmless, but it signals that the docs were not re-read.

### 5. Critical Fixes Before Submission

**P0 — do not submit without these.**

| ID | Fix | Location | Effort |
|---|---|---|---|
| **C-4** | **Commit the work.** `git add -A && git commit`, then confirm from a clean checkout that 48 tests collect and `/agent_sessions` still serves. Do this before touching any other fix, so the fixes themselves cannot be lost. | whole repo | 2 minutes |
| C-2 | Reject `payment_callback` unless `cs.status in {"requires_payment", "payment_pending"}`; on a terminal session, write a `payment_after_terminal` audit event and do not complete. | `api/checkout.py:179-239` | ~10 lines |
| C-3 | Call `_assert_transition(cs, target)` in each branch of `_finalize_payment`. | `checkout_service.py:908`, `:931`, `:942` | 3 lines |
| C-1 | Persist `razorpay_order_amount_paise` and `razorpay_order_version`; reuse the open order only when both match the revalidated total and current version; clear `razorpay_order_id` in `update_checkout` and `confirm_checkout`. | `entities.py`, `checkout_service.py:759-763`, `:435-439`, `:574-577` | ~25 lines + migration |
| H-1 | `rules = json.loads(ev.rules_fired)` once, then iterate `rules`. | `explain_service.py:45`, `:54`, `:64` | 1 line |
| H-6 | Pass `payload={"checks": [...]}` to `_fail`'s `final_validation` event and populate the error `details`. | `checkout_service.py:638-646`, `:661` | ~5 lines |

**P1 — fix if there is time before judging.**

| ID | Fix | Location |
|---|---|---|
| H-5 | Remove the default admin token from `dashboard/src/api.js`; refuse to start with `admin_demo_token` outside debug; restrict CORS; exclude `AuditEvent` from demo reset. | `api.js:13`, `config.py:20`, `main.py:36-41`, `api/admin.py:74-95` |
| H-7 | Verify `CheckoutSession.status == "completed"` and a `razorpay_payment_id` exists before accepting `finish(outcome="completed")`. | `agent/loop.py:687-701` |
| H-8 | Fail closed in `create_order` when credentials are missing and the driver is not `sim`. | `payment_service.py:52-60` |
| H-2 | Scope idempotency lookup to `(key, endpoint, session_id)`; move replay after `_check_ownership`. | `checkout_service.py:219-225`, `:609-614` |
| H-3 | Report the authorised amount, not the session total, in `payment.amount` and in the payment audit line. | `serialize_session:150`, `_finalize_payment:916` |
| — | Add the five missing tests: terminal-session callback rejected; callback amount mismatch rejected; reuse-after-reprice creates a new order; cross-endpoint key rejected; explain on a spend-limit decline names `spend_limit`. | `merchant_agent/tests/` |
| — | Correct the README claims in Part 9 rows 1, 2, 4 and 5. | `README.md` |
| — | Retitle or finish `AUDIT_REPORT.md`, and fix rows 7–9. | `AUDIT_REPORT.md` |

**P2 — after judging.**

Reservation lifecycle audit events; `oversell_detected` detection instead of the silent `max(0, …)` clamp; discovery to subtract reservations; re-run gating on `confirm` acceptance; authentication on session, audit, explain and reconcile reads; delegation revocation endpoint; persist merchant-agent sessions; move the Razorpay call outside the rollback-able transaction; document `payment_callback` and `reconcile` in the protocol spec and validate responses against the published schemas; commit the PRD; delete the tautological assertion at `test_inventory.py:90`; add asserts to `scripts/audit_bypasses.py`; update the `config.py:16` driver comment.

### 6. Track Alignment Verdict

**Does the project satisfy the track — "make a merchant transactable by an AI buyer end to end, with every money action explainable, bounded and gated, plus the audit trail and one failure handled gracefully"?**

**PARTIALLY — and closer to YES than the score suggests.**

*Transactable end to end by an AI buyer:* **YES.** An LLM buyer discovers, negotiates with a merchant agent, builds a basket and drives a real Razorpay test-mode payment through a versioned protocol, with the only human step being one deliberate approval gate. This is not simulated and it is not a wrapper around a form.

*Bounded:* **YES.** Delegation caps, category and quantity limits, agent iteration and tool-call budgets, and a tool allowlist with explicit privilege classes — all server-side, all enforced in Python, none of it delegated to a prompt.

*Gated:* **PARTIALLY.** The gate on `/complete` is genuinely strong. But two other paths reach the finaliser without passing it, and the amount itself passes through ungated. For a track judged on money safety, that gap is the difference between "partially" and "yes".

*Explainable:* **PARTIALLY.** The evidence is complete, chained and verifiable; the endpoint that presents it is broken on the very decisions a judge will inspect.

*Audit trail shown:* **YES**, with an erasability caveat.

*One failure handled gracefully:* **YES underneath, weakly on screen.**

The honest summary is that the architecture is right and the discipline is real — money-critical authorization is deterministic, the LLMs are confined to reasoning and discovery, and the boundary between buyer and merchant is clean. What is missing is not vision but closure: three unguarded doors into the payment finaliser and one mistyped variable in the explanation layer. Every P0 fix above is local and small. Fixed, this is a **YES**.

### 7. Recommended Final Product Description

> A mock grocery merchant that an AI buyer agent can transact with end to end over a versioned commerce protocol: the buyer discovers products, negotiates substitutions with a bounded merchant agent, and drives a real Razorpay test-mode payment, while every money-affecting decision is made by deterministic server-side policy code — spend, category, quantity, expiry and stock — that the LLMs can trigger but never overrule. Each decision is written to a SHA-256 hash-chained audit trail with the exact rules that fired, and the `/complete` path re-prices, re-checks delegation and re-checks stock immediately before contacting the gateway, declining without ever calling Razorpay when a check fails. Known limitations: the gateway callback path does not yet enforce the same terminal-state check as `/complete`, the authorised order amount is not yet bound to the re-validated total, and the explanation endpoint currently mislabels basket-creation-time policy declines.

---

## APPENDIX A — NOT VERIFIABLE ITEMS AND HOW TO CLOSE THEM

### A.1 — Why nothing could be executed

Three independently confirmed blockers, none of which is a defect in the project:

1. **The virtualenv is Windows-only.** `.venv/pyvenv.cfg` reads `home = C:\...\Python312`; the tree contains `_pydantic_core.cp312-win_amd64.pyd` and `Scripts/`, with no `bin/`. It cannot be invoked from the Linux side of this session.
2. **All outbound network is blocked.** Every request returns `X-Proxy-Error: blocked-by-allowlist`; PyPI is unreachable, so no Linux-native environment could be built.
3. **No offline wheel cache** exists in the repository.

Consequently **no test was run, no endpoint was called, and no request or response body in this document was executed.** Every finding above is derived from source inspection of the production runtime path, from the committed SQLite database, or from test assertions read literally. Nothing was fabricated; where a claim could not be established from code, it is marked NOT VERIFIABLE rather than assumed.

### A.2 — Items marked NOT VERIFIABLE

| Item | Why unverifiable | How to close it |
|---|---|---|
| Whether all 48 tests currently pass | Cannot execute | `.venv\Scripts\python -m pytest merchant_agent\tests -v` |
| Whether Razorpay test mode accepts the `saved`-driver token charge | No network, no credentials in the audit environment | Run one live checkout with `PAYMENT_DRIVER=saved` and record the response |
| Whether NIM returns valid tool calls reliably under demo conditions | No network | Run the buyer flow three times and record latencies and failures |
| Real end-to-end latency of `/agent_sessions` (two LLM round trips) | No network | Time ten runs |
| Whether the C-1 amount divergence reproduces against the live gateway | Requires a live order | Follow A.3 below |
| Whether `verify_chain` returns `valid: true` on the current committed database | Requires the app's own hashing over a live session | `GET /audit_logs/verify` after seeding |
| Whether the modal fallback at `:830-845` visually presents the stale amount | Requires a browser and the live gateway | Run A.3 step 6 with the network tab open |
| Whether the LLM brain (`MA_LLM_BRAIN=true`) ever emits `finish(outcome="completed")` prematurely | No test covers the LLM brain; `conftest.py:16` disables it | Add a recorded-transcript test, or run the loop 20 times with the brain enabled |
| Whether the PRD's Section A–V numbering matches this audit's interpretation | No PRD in the repository | Commit the PRD |

### A.3 — Exact commands to close the verification gap

Run from `D:\Hackathons\rz-agent` in PowerShell.

**Baseline**

```
.venv\Scripts\python -m scripts.seed_catalog
.venv\Scripts\python -m pytest merchant_agent\tests -v
.venv\Scripts\python -m uvicorn merchant_agent.main:app --port 8000
```

Then, in a second shell:

```
.venv\Scripts\python -m scripts.scenario_check
.venv\Scripts\python -m scripts.audit_bypasses
curl http://localhost:8000/audit_logs/verify
curl http://localhost:8000/health/agent
```

**Reproducing C-1 (the amount-binding gap) — seven steps**

1. `POST /checkout_sessions` with a single pack, quantity 1. Record `checkout_session_id` and `totals.total` as **T1**.
2. `POST /checkout_sessions/{id}/complete` with `PAYMENT_DRIVER=saved`. The session parks at `requires_payment` with `awaiting_authorization: true`. Record `razorpay_order_id` as **O1**.
3. Do **not** complete the payment. Leave the modal open or dismiss it.
4. `PUT /checkout_sessions/{id}` with quantity 5 (staying inside the delegation caps). The session re-prices to **T5**, `version` increments, status returns to `requires_payment`, and `razorpay_order_id` **remains O1**.
5. `POST /checkout_sessions/{id}/complete` again. Observe in the response that `payment.order_id == O1` while `totals.total == T5`.
6. `GET https://api.razorpay.com/v1/orders/O1` with the test key pair. **The assertion that fails is `amount == T5 * 100`; it will equal `T1 * 100`.** That single comparison is the whole of C-1.
7. `GET /audit_logs?checkout_session_id={id}` and confirm the `payment_success` summary reads `Paid ₹T5` while the gateway authorised `T1` — the divergence is unrecorded.

Equivalent variant via `confirm`: replace step 4 with a price change plus `POST /checkout_sessions/{id}/confirm` with `accept_new_terms: true`, which also re-prices, bumps the version and retains O1 — and additionally skips the gating re-run.

**Reproducing C-2 (terminal-state payment) — four steps**

1. `POST /checkout_sessions`, then `POST .../complete` to park at `requires_payment` with an open order **O1**.
2. `POST /checkout_sessions/{id}/cancel`. Status becomes `cancelled`; `razorpay_order_id` is **not** cleared.
3. Pay **O1** in the still-open Razorpay modal. Razorpay produces a genuine signature — no forgery is needed.
4. The browser posts to `/payment_callback`. Observe the session flip from `cancelled` to `completed`, stock decrement and reservations consume. The expected correct behaviour is a rejection plus a `payment_after_terminal` audit event.

**Reproducing H-1 (broken explanation) — two steps**

1. Create a basket whose total exceeds the delegation's `max_order_value`. The session is declined at creation with `decline_code: SPEND_LIMIT`.
2. `GET /explain/{checkout_session_id}`. Expect the incorrect output: `"All deterministic policy checks passed."`, headline `"This transaction is still in progress."`, and `"The transaction was declined ([)"`.

**Reproducing H-2 (cross-endpoint key reuse)**

Send the same `Idempotency-Key` to `POST /checkout_sessions` and then to `POST /checkout_sessions/{other_id}/complete` with a different `buyer_agent_id`. The lookup at `:219-225` matches on the key alone and the replay at `:609-611` precedes the ownership check at `:614`, so the second caller receives the first caller's session body.

### A.4 — Evidence conventions used in this document

`file.py:NN` denotes a line number verified by direct read in this session. Ranges denote a function's full extent. Where a code excerpt appears, it is a verbatim copy, not a paraphrase. Test citations name the test function and state what it actually asserts; no test was counted as evidence merely for existing or for being named after a feature. Claims about the live database refer to `data/merchant.db` as it exists in the working tree (`md5 01863f7aa0cb6c7d5fe5c7aba6a90a14`), read from a byte-identical copy because WAL mode requires write access to the shared-memory file. `data/` is gitignored, so this database is a local runtime artefact rather than part of the repository — correctly so.

---

*End of audit.*
