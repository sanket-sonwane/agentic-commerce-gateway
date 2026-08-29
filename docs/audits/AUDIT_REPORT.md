# FINAL TECHNICAL AUDIT — Agentic Commerce Gateway

**Auditor:** adversarial code review, execution-path tracing, live-artifact forensics
**Date:** 2026-08-22
**Repo audited:** `D:\Hackathons\rz-agent` (7,201 LOC of project code, excluding `.venv`, `node_modules`, `dashboard/dist`)
**Track bar being tested against:** *"Every money action explainable, bounded and gated. Show the audit trail and one failure handled gracefully."*

---

## AUDIT METHOD AND ITS LIMITS (read this first)

What I did:

1. Read every production source file in `merchant_agent/`, `buyer_service/`, `buyer_b/`, `scripts/`, `protocol/`, `dashboard/src/` — plus the built `dashboard/dist` bundle.
2. Traced every path that can reach Razorpay by following call sites, not filenames.
3. Enumerated **every** `cs.status =` assignment and **every** caller of `_finalize_payment`.
4. **Executed the real gating engine source** (`gating_service.evaluate`, `first_failure_code`) against stubbed ORM objects to test rule boundaries behaviourally, rather than reading them.
5. **Performed forensics on `data/merchant.db`** — a real database left behind by actual runs on 2026-08-22 containing 4 checkout sessions, 60 audit events, 8 reservations, 4 idempotency keys and 1 saved payment method. This is genuine runtime evidence, not inference.
6. **Independently re-verified the audit hash chain** with a clean stdlib reimplementation of `_canonical()` over those 60 real rows.
7. Read `buyer_service_debug.log` (183 lines of real LLM tool calls) and `.pytest_cache` (the last recorded pytest run).
8. Compared `.env` / `.env.example` / `config.py` for configuration drift (secret **values** were never printed — only compared and length-checked).

### What is NOT VERIFIABLE in this environment, and why

| Item | Reason |
|---|---|
| **`pytest merchant_agent/tests` result** | The audit sandbox is Linux with no PyPI access (`403 Forbidden` on the proxy). The project's `.venv` is a **Windows** venv containing 17 `.pyd` binaries (`pydantic_core`, SQLAlchemy C-ext, `greenlet`, `jiter`) that cannot load on Linux. `fastapi`, `pydantic`, `sqlalchemy`, `pytest`, `httpx`, `razorpay` are all absent. The suite could not be run. |
| **Live endpoint exercise** (adversarial probes: injected `unit_price`, cross-buyer delegation, `payment_callback` on a cancelled session, stale-order reuse, idempotency replay) | Same reason — the FastAPI app cannot be imported, so `TestClient` could not be constructed. All findings below about these paths are derived from **exhaustive call-site tracing**, and where possible corroborated by the runtime DB. Each is labelled with the evidence class it rests on. |
| **Live Razorpay API behaviour** | Deliberately not exercised. No network calls were made with the live test-mode credentials found in `.env`. |

Everything else in this report is backed by either (a) source at a cited line, (b) a real row in `data/merchant.db`, or (c) behavioural execution of the real source under stubs. I say so explicitly at each finding.

---

# PART 1 — REPOSITORY AND ARCHITECTURE DISCOVERY

## 1.1 Actual shape of the system

Three processes plus one script client:

| Component | Port | Nature |
|---|---|---|
| `merchant_agent` (FastAPI) | 8000 | **Deterministic transaction engine.** Owns catalog, delegations, checkout sessions, reservations, audit chain, Razorpay. |
| `buyer_service` (FastAPI + SSE) | 8001 | **Genuine LLM agent.** NVIDIA NIM `llama-3.3-70b-instruct` tool loop, max 12 iterations. |
| `dashboard` (React/Vite) | 5173 | Two views: consumer chat ("Shop") and judge-facing "Merchant Ops". |
| `buyer_b/client.py` | — | Independent deterministic second client, ~150 LOC, no shared code with `buyer_service`. |

Data: single SQLite file (`data/merchant.db`) with WAL, `foreign_keys=ON`, `busy_timeout=5000`. Money-critical writes are serialised by a **process-wide `threading.RLock`** exposed as the `mutate()` context manager (`merchant_agent/db.py`). Reads use a separate `read_only()` dependency.

## 1.2 What is an agent, and what is just an API

This distinction matters for the track, so I am being precise.

**`buyer_service` is a real agent.** Evidence, not inference: `buyer_service_debug.log` records the model choosing its own tool sequence across turns — e.g. one conversation issued `discover_products("milk")`, then `discover_products("bread")`, then `discover_products("butter")` as three separate autonomous calls, then composed a two-item basket. Another conversation shows the model **inventing product IDs** (`"pasta_candidate_id"`, `"ingredients_candidate_pack_id"`), being rejected, and then retrying with a smaller basket three times. That is unmistakable model-driven control flow, including model-driven error recovery.

**`merchant_agent` is *not* an agent.** It is a deterministic transaction engine. The only LLM use on the merchant side is `discovery_service._llm_parse()` — natural-language → `{keywords, budget, quantities}` JSON at `temperature=0.0`, 10-second timeout, returning `None` on any failure, with a full deterministic keyword/synonym fallback (`_fallback_parse`). Matching, ranking, pack selection, pricing, gating, state transitions, reservations and payment are **100% deterministic code with zero LLM involvement**. The project should describe this component as a *deterministic merchant transaction engine with an LLM query parser*, not a "merchant agent". Calling it an agent is the single most misleading naming choice in the repo — and, importantly, the underlying design (LLM nowhere near payment authorisation) is the *correct* one. The name undersells the engineering by mislabelling it.

## 1.3 Where state lives

| State | Location | Durable? |
|---|---|---|
| Catalog, delegations, sessions, reservations, audit, idempotency keys, saved cards | SQLite `data/merchant.db` | Yes |
| Buyer conversation state (`CONVERSATIONS` dict), pending bill, declined packs | **Python process memory in `buyer_service/main.py`** | **No — lost on restart** |
| Dashboard UI state | React component state | No |
| Theme | `localStorage` | Yes |

Consequence: restarting the buyer service mid-demo orphans any conversation awaiting approval. The merchant-side session survives (correctly), but the buyer can no longer resume it, because the delegation/approval context is gone.

## 1.4 Where financial authority actually lives

Financial authority is concentrated in exactly one file: `merchant_agent/services/checkout_service.py` (978 lines). Specifically:

- `complete_checkout()` — the guarded ladder. **Enforces** ownership → state → price → delegation → stock → gating, and only then calls Razorpay.
- `_finalize_payment()` — the actual *mutation of money state*: decrements `Pack.stock_actual`, consumes reservations, sets `status = "completed"`, writes `payment_success` + `checkout_completed`.

**The critical architectural fact of this entire audit:** those two functions are separable. `_finalize_payment()` contains **no** guards of its own — no `_assert_transition`, no state check, no ownership check, no re-validation, no idempotency. Everything that makes completion safe lives in `complete_checkout()`, and there are **two other call sites** that reach `_finalize_payment()` without passing through it (`payment_callback` and `reconcile_session`). Section L and PART 3 develop this.

## 1.5 Deterministic vs LLM — the boundary

| Deterministic (no LLM) | LLM-driven |
|---|---|
| 5-rule gating engine | Buyer intent → basket construction |
| Price resolution and totals | Merchant-side NL query parsing (with deterministic fallback) |
| State machine and terminal states | Buyer-facing conversational copy |
| Reservations and TTL | — |
| Final revalidation ladder | — |
| Substitute search (`_propose_substitutes`) | — |
| Explanations (`explain_service` templating) | — |
| Audit hashing | — |

The boundary is drawn in the right place. No LLM can authorise, price, or approve a payment.

---

# PART 2 — REQUIREMENT-BY-REQUIREMENT VERIFICATION

## SECTION A — Catalog realism · **IMPLEMENTED**

`merchant_agent/seed/catalog.json`: **45 products / 74 packs / 9 categories**, verified by parsing the file. Categories: produce 10, staples 10, dairy 8, meat_eggs 5, beverages 4, bakery 3, snacks 2, household 2, **tobacco 1**. Units span `g, kg, l, ml, pcs`. 29 distinct brands. Prices ₹10–₹2,600 (`pack_saffron_5g` provides a high-value case for spend-limit demos). Multi-pack products are real (74 packs across 45 products), which is what makes `_pick_pack` budget selection meaningful.

Deliberate demo affordances that are correctly present: `tobacco` exists in the catalog but is **excluded** from `ALLOWED_CATEGORIES` in `scripts/seed_catalog.py`, giving a genuine category-block case; `pack_chicken_500` is seeded at `stock=1`, giving a genuine scarcity case.

Weaknesses (real but minor): `pack_chicken_500` is the **only** pack with stock < 40 — there is no zero-stock item, so the only way to demo stock exhaustion is the admin endpoint. `catalog.json` contains no `delegations` block; delegations are seeded in code (`seed_catalog.seed_delegations`), which is fine but means the file is not a complete description of the demo fixture.

## SECTION B — Discovery · **IMPLEMENTED (with one accuracy defect)**

`POST /discover` → `discovery_service.discover()`. Verified path: `_llm_parse()` (NIM, `temperature=0.0`, JSON-only prompt, 10s timeout, `None` on any exception) → `_fallback_parse()` (regex budget/quantity extraction, stopword filtering, synonym map including Hindi terms such as *tel*, *anda*, *paav*). Matching and ranking (`_score_product`) and pack selection (`_pick_pack`, cheapest pack fitting budget) are deterministic. The response reports which parser ran (`parser: "llm" | "keyword-fallback"`), which is honest and judge-friendly.

Runtime evidence the LLM parser really runs: audit events 2–9 in `data/merchant.db` all carry `(parser=llm)`.

**Defect — reservations are invisible to discovery.** `discovery_service` reports `available_stock = max(0, pack.stock_actual)`. It does **not** subtract active reservations, unlike `inventory_service.available_stock()` which gating uses. Consequence: an item fully reserved by another in-flight session is advertised as available, the buyer builds a basket around it, and gating then rejects it at create time. The rejection is safe, but the advertised number is wrong — and it is the number the substitution card shows the user (`"{available_stock} in stock"` in `SubstituteCard`). **PARTIAL on stock accuracy.**

Second weakness: `excluded_products.add(product.product_id)` restricts results to one product per keyword, so "milk" returns exactly one milk product rather than a comparison set.

Third: `POST /discover` requires **no delegation token** — it accepts any `buyer_agent_id` string and writes an audit event attributing the query to it. See Section U.

## SECTION C — Buyer agent · **IMPLEMENTED**

`buyer_service/agent.py` (572 lines). Real NIM tool loop capped at `max_agent_iterations=12`. Exactly three tools are exposed (`buyer_service/prompts.py`): `discover_products`, `create_checkout_session`, `cancel_checkout_session`. **There is deliberately no `complete_checkout` tool** — completion is driven by service code *after* human approval. That is a strong bounded-autonomy design decision and it is real, not aspirational: `MerchantClient.complete_checkout` exists in `tools.py` but is never wired into the LLM tool schema.

Verified guard: before creating a checkout, the agent validates that every `pack_id` the model chose appeared in the **last discovery result**, and returns an `INVALID_REQUEST` feedback message for unknown IDs. **Runtime proof this guard fires:** `buyer_service_debug.log` shows the model submitting `"pasta_candidate_pack_id"`, `"dinner_candidate_pack_id"`, `"ingredients_candidate_pack_id"` — pure hallucinations — across three successive attempts, none of which produced a checkout session (confirmed: no such session exists in the DB, and no `checkout_create_requested` audit event corresponds to them). The hallucination never reached the merchant.

Human gate: `present_bill()` → `BILL_PAUSE`, resumed only by `POST /conversations/{id}/decision`. Verified in `main.py` and mirrored in the dashboard (`BillCard` with Approve/Cancel; the input box is **disabled** with "Answer the bill above to continue" until the bill is resolved).

## SECTION D — Merchant agent · **PARTIAL (as an "agent")**

As an orchestrator it is complete and correct: 4 API routers, per-request `mutate()` transaction, structured error envelope on every failure path (`main.py` registers handlers for `ProtocolError`, `RequestValidationError`, and bare `Exception`).

As an *agent* it does not qualify, and should not claim to. See §1.2. **The engineering is good; the label is wrong.**

## SECTION E — Protocol endpoints · **IMPLEMENTED (with undocumented surface)**

Implemented and reachable: `POST /discover`, `POST /checkout_sessions` (201; 200 on idempotent replay), `PUT /checkout_sessions/{id}`, `GET /checkout_sessions/{id}`, `POST …/complete`, `POST …/confirm`, `POST …/cancel`, `GET /audit_logs`, `GET /audit_logs/verify`, `GET /explain/{id}`, `GET /health`, plus admin routes.

`protocol/protocol_spec.md` publishes the endpoint table, the lifecycle, the completion ladder, 16 error codes, and 7 guarantees. Five JSON Schemas exist and are well-formed draft-07 with sensible `required` sets (`checkout_session.json` requires `gating_result` and `reservation` — good).

**Two production endpoints are absent from the published spec:** `POST /checkout_sessions/{id}/payment_callback` and `POST /checkout_sessions/{id}/reconcile`. The first is the highest-privilege endpoint in the system (it can mark a session paid) and the second can also mutate money state. A protocol document that omits its two most dangerous endpoints is incomplete. Note also that the schemas are **published but never enforced** — nothing in the runtime validates responses against them.

## SECTION F — Agent independence · **IMPLEMENTED (strong)**

`buyer_service/tools.py` contains a `MerchantClient` that speaks **only** HTTP/JSON via `httpx`. Verified by import inspection: `buyer_service` imports nothing from `merchant_agent` — no shared DB session, no shared models, no in-process shortcut. Separate processes, separate ports.

`buyer_b/client.py` is a genuinely independent second client: distinct identity (`buyer_agent_b_deterministic`), distinct delegation (`dlg_buyerb_001`, ₹2,000 cap — confirmed present in the live DB), no shared code with `buyer_service`, and no LLM. This is the strongest possible evidence for interoperability: the protocol is consumable by a non-LLM client.

## SECTION G — Delegated authority · **PARTIAL**

What exists and works: a `Delegation` entity with `max_order_value`, `allowed_categories` (JSON), `max_quantity_per_item`, `expires_at`, `status`; `delegation_service.validate_delegation()` raising `UNAUTHORIZED_AGENT` (401) for unknown/mismatched/inactive and `DELEGATION_EXPIRED` (401) for `expires_at <= utcnow()`; four seeded delegations covering the demo matrix (normal ₹5,000; restricted ₹500 with `max_quantity_per_item=50` so the *spend* rule is the one that fires; buyer-B ₹2,000; and an already-expired one). All four confirmed present in the live DB with the expected values.

Ownership binding is enforced: `_check_ownership()` compares both `cs.buyer_agent_id` and `cs.delegation_id` and raises `UNAUTHORIZED_AGENT` (403). Called from `update_checkout`, `cancel_checkout`, `confirm_checkout` and `complete_checkout`.

Why this is PARTIAL, not IMPLEMENTED:

1. **The token is the primary key.** `Delegation.delegation_id` *is* the bearer token (`dlg_9f8e7d`). There is no signature, no HMAC, no expiry inside the token, no issuer. Possession of a 12-character string is total authority within its limits. The protocol spec is honest about this ("a prototype representation, not production authentication") — credit for the disclosure — but it remains a prototype-grade authority model.
2. **No revocation path.** `status` exists on the entity and is checked, but no endpoint or script can set it to anything other than `active`. Revocation is unreachable at runtime.
3. **Idempotency replay precedes authorisation.** In `complete_checkout` (line 609) `_idempotent_replay()` runs and can `return` **before** `get_session()`/`_check_ownership()` at lines 613–614. Anyone presenting a known `Idempotency-Key` receives the full serialised checkout session of whoever created it, with no ownership check. Same ordering in `create_checkout` (line 269). Evidence class: code order, verified by reading. See Section N and U.
4. `POST /discover` requires no delegation at all.

## SECTION H — Deterministic gating · **IMPLEMENTED (verified behaviourally)**

`gating_service.evaluate()` is a pure function: no LLM, no randomness, no clock beyond `utcnow()` for expiry. Five rules, each producing `GatingCheck{rule, result, detail}` with a human-readable detail string. `GatingResult.passed = all(c.result == "pass")`. `first_failure_code()` maps the first failing rule to a protocol error code.

I did not take this on trust. I extracted the real `evaluate()` source, executed it against stubbed delegation/item objects, and probed the boundaries:

| Probe | Result | Verdict |
|---|---|---|
| total ₹500 vs cap ₹500 | `spend_limit:pass` — "₹500 <= ₹500" | inclusive, correct |
| total ₹501 vs cap ₹500 | fail → `SPEND_LIMIT_EXCEEDED` | correct |
| quantity 10 vs max 10 | pass | inclusive, correct |
| quantity 11 vs max 10 | fail → `DELEGATION_SCOPE_VIOLATION` | correct |
| category `tobacco` | fail → `CATEGORY_BLOCKED`, detail "blocked categories: tobacco" | correct |
| `expires_at` 1s in the past | fail → `DELEGATION_EXPIRED` | correct |
| **`expires_at` exactly == now** | **fail → `DELEGATION_EXPIRED`** | correct — fails closed at the boundary |
| stock 3, reserved 3, want 1 | fail → `STOCK_UNAVAILABLE`, "available=0" | reservations correctly reduce availability |
| quantity **and** spend both violated | both reported as `fail`; `first_failure_code` returns `DELEGATION_SCOPE_VIOLATION` (check order) | deterministic and stable |

Note the last row: **all** failures are always reported in `checks`, and only the *error code* picks the first. Nothing is hidden from the audit record. That is the right design.

Callers verified — gating runs at **three** points, all inside `mutate()`: `create_checkout` (line ~287), `update_checkout` (line ~407), and `complete_checkout` step 4 (line ~730). Runtime corroboration: the live DB contains `gating_evaluated` events for all 4 sessions and `final_validation` events for all 4 completions, each with the full five-check payload.
