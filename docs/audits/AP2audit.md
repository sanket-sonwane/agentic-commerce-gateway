# CURRENT SYSTEM IMPLEMENTATION AUDIT

## 1. Executive Summary

The **Agentic Commerce Gateway** is a working, policy-gated commerce protocol that enables an **LLM Buyer Agent** to complete real **Razorpay test-mode payments** against an **LLM Merchant Agent**. It is implemented as two Python FastAPI services plus a React/Vite dashboard, an independent deterministic interop client, and a published protocol contract with JSON Schemas.

**Architecture (verified):**
- **Merchant Agent** (`merchant_agent/`, port 8000): FastAPI + SQLAlchemy + SQLite (WAL). Holds the deterministic control plane: 5-rule gating engine, checkout state machine, completion ladder, stock reservations, delegation authority, Razorpay integration, SHA-256 hash-chained audit trail. Also hosts an LLM "merchant brain" (Groq, `openai/gpt-oss-120b`) that reasons/plans but executes **only** through allowlisted deterministic tools.
- **Buyer Service** (`buyer_service/`, port 8001): Groq LLM chat agent (`openai/gpt-oss-20b`) with an OpenAI tool-loop and a deterministic **bill approval gate** (`BILL_PAUSE`) before any money movement.
- **Buyer B** (`buyer_b/client.py`): independent deterministic protocol client (no LLM) proving a third party can transact.
- **Dashboard** (`dashboard/`, port 5173): React + Vite; chat, merchant ops, audit timeline, money-story and explainability UI.
- **Protocol** (`protocol/`): `protocol_spec.md` + JSON Schemas (`product`, `error`, `checkout_session`, `basket_proposal`, `audit_event`).
- **Scripts** (`scripts/`): seed, reset, adversarial-bypass audit, TOCTOU/failure simulation, benchmarks.

**Money-safety posture:** The system is deliberately **fail-closed and structurally safe** — the LLM can *never* directly move money. Every payment path re-runs the deterministic ladder immediately before Razorpay invocation, uses HMAC-verified payment callbacks, idempotency, stock revalidation (TOCTOU protected), and an append-only SHA-256 hash chain. 53/53 pytest cases pass; prior audits score it **NEARLY READY (89/100)**.

**AP2-relevant gap:** The current model has a **strong deterministic control plane but no cryptographic authorization / mandate / proof layer**. Authority is a plaintext **delegation token** (`dlg_*`) looked up in a DB table — it is *not signed, not replay-protected, not cryptographically bound* to an intent, exact cart, or exact amount. The audit chain is *unkeyed* (suffix-rewrite undetectable). Razorpay order amount is **not stored** on the session, so payment cannot be provably bound to the authorized total. These are the precise insertion points for an AP2-inspired layer.

---

## 2. Actual Architecture

| Component | Technology | Key Files |
|---|---|---|
| Frontend | React 18 + Vite 6, JSX, lucide-react | `dashboard/src/App.jsx`, `views/ChatView.jsx`, `views/OpsView.jsx`, `components/AuditTimeline.jsx`, `MoneyStoryDrawer.jsx`, `RazorpayPayCard.jsx`, `PaymentCard.jsx`, `MerchantAgentTrace.jsx`, `utils/actionMeta.js`, `utils/events.js`, `api.js` |
| Merchant backend | FastAPI 0.115 + uvicorn, SQLAlchemy 2.0, pydantic 2.10, pydantic-settings | `merchant_agent/main.py`, `api/*.py`, `services/*.py`, `models/entities.py`, `models/dto.py`, `agent/*.py` |
| Buyer backend | FastAPI, SSE streaming, Groq OpenAI SDK | `buyer_service/main.py`, `agent.py`, `tools.py`, `prompts.py`, `conversations.py`, `config.py` |
| Interop client | Python (deterministic, no LLM) | `buyer_b/client.py` |
| LLM provider | Groq (OpenAI-compatible) | Merchant brain `openai/gpt-oss-120b`; Buyer chat `openai/gpt-oss-20b`; discovery parser `openai/gpt-oss-120b` |
| Database | SQLite WAL, process-wide `threading.RLock` mutation lock | `merchant_agent/db.py`, `data/merchant.db` |
| Catalog storage | SQLite `products`/`packs` tables, seeded from JSON | `merchant_agent/models/entities.py:10-34`, `merchant_agent/seed/catalog.json`, `scripts/seed_catalog.py` |
| Session storage | SQLite `checkout_sessions` + in-memory agent `SESSIONS` dict | `entities.py:49-68`, `agent/state.py:18` |
| Audit storage | SQLite `audit_events` + `IdempotencyKey` | `entities.py:84-108` |
| Razorpay integration | `razorpay==1.4.2` SDK + httpx for recurring charges | `merchant_agent/services/payment_service.py` |
| Auth/authorization | Delegation-token DB lookup + admin header token | `services/delegation_service.py`, `api/admin.py:23-25` |
| Cryptographic | SHA-256 hash chain + HMAC-SHA256 payment signature | `services/audit_service.py`, `services/payment_service.py:184-193` |
| Background jobs | None (sweep-on-read reservations only) | `services/inventory_service.py:23-33` |
| API gateway | None (Vite dev-proxy `api.js` → ports 8000/8001) | `dashboard/vite.config.js:8-11` |

### Component interaction matrix

**Merchant Agent** `merchant_agent/`
- **Purpose:** Authoritative commerce server: discovery, catalog, gating, checkout state machine, stock reservations, delegation enforcement, Razorpay money movement, audit.
- **Called by:** Buyer Service (HTTP), Buyer B (HTTP), Dashboard (via proxy), scripts.
- **Calls:** Razorpay API (`payment_service.py:294`), Groq LLM (`agent/loop.py`, `discovery_service.py`).
- **Owns:** packs/prices/stock, reservations, checkout sessions, delegations, payment methods, all audit SQLite rows.
- **Security/authority role:** The **sole financial authority** and the **sole owner of Razorpay credentials** and the deterministic policy engine.
- **Status:** Working; 53/53 tests green.

**Buyer Service** `buyer_service/`
- **Purpose:** Natural-language chat facade; LLM-driven intent, discovery, basket, negotiation with merchant agent; deterministic human approval gate.
- **Called by:** Dashboard chat UI, `scripts/test_buyer_flow.py`.
- **Calls:** Merchant via `MerchantClient` (`buyer_service/tools.py`) — the **only** merchant boundary; Groq LLM (`agent.py`).
- **Owns:** in-memory conversations/transcripts (`conversations.py`), no DB.
- **Security/authority role:** Holds `DELEGATION_TOKEN` (plaintext) and `BUYER_AGENT_ID` from `.env`; **no Razorpay creds, no merchant DB access**.
- **Status:** Working.

**Buyer B** `buyer_b/client.py`
- **Purpose:** Prove a fully independent deterministic client can transact via the public protocol.
- **Owns:** nothing server-side; uses `dlg_buyerb_001` delegation.
- **Status:** Working; known P1 bug at `client.py:109` (`err.get('code')` crashes when `"error": null`).

**Dashboard** `dashboard/src/`
- **Purpose:** Read/display UI only; no write logic beyond admin stock + payment modal.
- **Status:** Working. Note `api.js:12-14` **hardcodes `X-Admin-Token: 'admin_demo_token'`** in client JS.

---

## 3. End-to-End Transaction Flow

Traced one **successful `saved`-driver** transaction (the `.env` default):

**Step 1 — User natural-language request**
- Actor: Human. Via `dashboard/src/views/ChatView.jsx` → `POST /api/buyer/chat` (SSE) → `buyer_service/main.py:131-158` `chat()`.
- LLM: enters `BuyerAgent.llm_loop` (`buyer_service/agent.py:292-425`).

**Step 2 — Buyer Agent intent/discovery**
- LLM calls tool `discover_products` → `buyer_service/agent.py:81-122` `_execute_tool` → `MerchantClient.discover()` → **POST `/discover`** (`merchant_agent/api/discovery.py:12-28`) → `discovery_service.discover()` (`services/discovery_service.py:237-319`), optional `_llm_parse` / deterministic `_fallback_parse` + `_score_product`/`_pick_pack`.
- Deterministic: matching/ranking/pricing. LLM: parsing only. Money: No. Audited: `discovery_requested`, `discovery_result_generated` (`discovery_service.py:247,308`).

**Step 3 — Basket construction**
- LLM calls `create_checkout_session` (`agent.py:124-166`) → `MerchantClient.create_checkout()` (uuid Idempotency-Key) → **POST `/checkout_sessions`** (`api/checkout.py:27-46`) → `checkout_service.create_checkout()` (`services/checkout_service.py:260-370`).
- Deterministic: delegation validation, item resolution (authoritative price), totals, gating, reservations. Money: No. Audited: `checkout_create_requested`, `gating_evaluated`, `reservation_created`, `checkout_created`.
- Buyer service then deterministic **`present_bill` → `BILL_PAUSE`** (`agent.py:18`, `_complete_flow`) — parks loop awaiting human approval.

**Step 4 — Bill approval**
- Dashboard shows bill (`PaymentCard`), user approves → **POST `/conversations/{id}/decision`** (`buyer_service/main.py:161-172`) → `agent.resume_after_decision` (`agent.py:455-477`) → `_complete_flow` → `MerchantClient.complete_checkout()` (fresh uuid Idempotency-Key) → **POST `/checkout_sessions/{id}/complete`** (`api/checkout.py:101-136`).

**Step 5 — Completion ladder** `checkout_service.complete_checkout()` (`checkout_service.py:596-943`)
1. idempotent replay (`:609`), 2. ownership (`:614`), 3. already-completed (`:625`), 4. state transition (`:629`), 5. **price re-check** (`:669-694`), 6. **delegation revalidate** (`:698`), 7. **strict stock re-check** (`:703-727`), 8. **policy re-run** (`:730-739`), audited `final_validation[approved]` (`:741`).
- Deterministic: all of the above. LLM: No. Money: No (not yet). Audited: `complete_requested`, `final_validation`.

**Step 6 — Razorpay order creation** `checkout_service.py:752-843`
- `saved` driver → `payment_service.ensure_customer` (`:762-781`) + `create_order()` (`payment_service.py:62-108`) with E-Mandate block → Razorpay. Retry guard reuses open order (`:785`). Audited: `razorpay_order_created` (`:824`).
- Money: Yes (real test-mode Razorpay Order).

**Step 7 — First order: Checkout modal (saved-card absent)** `checkout_service.py:909-943`
- Session parked `awaiting_authorization`, audit `awaiting_authorization`. Dashboard `RazorpayPayCard.jsx` opens Checkout.js modal. Razorpay completes → **POST `/checkout_sessions/{id}/payment_callback`** (`api/checkout.py:179-248`) → order-id binding + **HMAC-SHA256 signature verify** (`payment_service.py:184-193`) → `finalize_with_outcome` → `_finalize_payment` (`checkout_service.py:963-1045`) → `completed`.
- Money: Yes. Audited: `payment_success`, `checkout_completed`, `payment_token_saved` (`:220`).

**Step 8 — Subsequent orders (headless)**: `charge_with_token` (`payment_service.py:269-386`) → `POST /v1/payments/create/recurring` (Razorpay). Audited: `razorpay_token_charge`.

**Step 9 — Final response**: SSE back to dashboard; user sees "Receipt: {checkout_session_id}" (`buyer_service/agent.py:497,519`).

**Failure/alternate path note:** At any gating/validation failure, `_fail` (`checkout_service.py:634-666`) releases reservations, sets `declined`, audits `final_validation[declined]` + `checkout_declined` (**"Razorpay NOT called"**), and no Razorpay call happens.

---

## 4. Merchant Agent — Is It Really an Agent?

**Classification: D/E — an agent runtime containing LLM reasoning + deterministic tools/policies (a tool-use loop), where the LLM's only power is choosing among deterministic, gated tools. It is *not* merely a REST API, and the LLM is *not* the decision-maker for money.**

Evidence:
- The `MerchantAgent` class (`merchant_agent/agent/loop.py:537-624` `run()`, `:626-675` `resume()`) executes an Observe→Reason→Act loop with two brains: `_llm_decide` (`:214-289`, Groq `chat.completions`) and `_fallback_decide` (`:337-511`, deterministic fallback), switching on transport failure (`:514-526`).
- Tools are registered in a strict allowlist (`agent/tools.py:32-44`): `READ_TOOLS` (6), `WRITE_TOOLS` (4), `FINANCIAL_TOOLS = {"run_gating", "request_completion"}`. `execute_tool` (`:819-895`) enforces the allowlist, tool-call budget (`ma_max_tool_calls`), signature filtering, and runs writes/financial tools under `mutate()`.

**Where merchant-side reasoning happens:** `agent/loop.py` `_llm_decide` for planning/choice; `agent/prompts.py` system prompt constraints.

**Where merchant-side decisions happen:** The **LLM** picks which tool to call (e.g., `find_alternative`, `request_confirmation`, `request_completion`) and proposes substitution terms. **Deterministic code** decides whether to allow them.

**Where deterministic enforcement happens:** `gating_service.evaluate` (5 rules), `checkout_service` state machine + completion ladder, `inventory_service` reservations, `delegation_service.validate_delegation`, `tools.py` anti-silent-substitution guard `_guard_no_silent_substitution` (`:408-446`).

**Can the LLM directly move money?** **No.** `request_completion` (`tools.py:724-797`) is **zero-parameter**, mode-guarded (`agent_completed` only), and merely delegates to the deterministic `checkout_service.complete_checkout` ladder. Verified: tests assert no `razorpay_order_created`/`payment_success` on decline paths.

**Can the LLM bypass policy?** **No.** Gating is re-run in the ladder (`checkout_service.py:730-739`) independent of any LLM claim; adversarial "ignore all limits" test → `DELEGATION_SCOPE_VIOLATION`, no payment.

**Can the LLM modify prices/stock?** **No.** `get_current_price` returns "authoritative merchant price"; totals computed server-side (`_compute_totals`); stock only via reservations/gating; LLM has no tool to write `Pack.stock_actual` or `price`.

**Can the LLM approve its own payment?** In `agent_completed` mode the LLM *triggers* the gated completion flow, but any `finish(outcome="completed")` claim without gateway-confirmed payment is **held then downgraded to `ready_for_completion`** (`loop.py:575-590`, `735-766`) — payment truth is verified against state, not trusted from the LLM. *Important nuance:* in `agent_completed` mode there is **no separate human sign-off at the money step** — the delegation token (held server-side) is the authorization and the LLM can trigger the charge. This is a key AP2 insertion point.

**Independent authority boundary from buyer:** Yes — the merchant owns prices/stock/gating/money; the buyer agent has none of these.

**Where "agent" is only a name/UI:** `buyer_service` calls itself an agent (it is a real tool-loop). The `merchant_agent` is a real agent loop too. There is **no** component where "agent" is purely cosmetic — though the merchant's *financial authority* is deterministic, not the LLM.

---

## 5. Buyer Agent Independence

**Verdict: The Buyer Agent is genuinely independent from and subordinate to the Merchant Agent, with an enforced protocol boundary. One documented boundary weakness exists.**

Verified:
- **Direct DB access?** **No.** `buyer_service/` has zero imports of merchant models/DB; it interacts only via `MerchantClient` HTTP (`buyer_service/tools.py:13-147`).
- **Razorpay credentials?** **No.** Credentials live only in `merchant_agent/services/payment_service.py`; never exposed to buyer context (`agent/prompts.py` design note: "never sees Razorpay credentials, delegation tokens, or DB internals"). Other buyer tools use only merchant-returned IDs.
- **Access to merchant policy internals?** **No.** Gating runs merchant-side; buyer only receives pass/fail + structured error codes.
- **Calls merchant only through protocol boundary?** **Yes.** All `MerchantClient` methods hit the 8000 endpoints.
- **Can buyer directly manipulate checkout state?** Only through the permitted state-transition endpoints (`update`/`confirm`/`cancel`/`complete`), each re-validating ownership/gating/state server-side.
- **Can buyer force payment?** **No.** `complete_checkout` ladder runs merchant-side; a buyer cannot make Razorpay fire without passing every gate.
- **Can buyer bypass merchant gating?** **No.** All line-item writes re-run gating; client-supplied prices ignored (`_resolve_items` authoritative).
- **LLM contexts separated?** **Yes.** Two separate services, separate models, separate prompts, no shared runtime context.
- **Can a second independent client use the protocol?** **Yes** — proven by `buyer_b/client.py` and seeded `dlg_buyerb_001`.

**Boundary weakness found:** `GET /checkout_sessions/{id}` (`api/checkout.py:95-98`) performs **no ownership check** — session IDs are guessable hex. An authenticated-but-wrong buyer (or any party) can read another's checkout state. This does not move money but leaks state and is an IDOR-class issue (see §15).

---

## 6. Current Authority / Delegation Model

**Authority is a plaintext DB-registry token, not a cryptographic mandate.**

1. **Where created:** `scripts/seed_catalog.py:31-76` `seed_delegations()` + `/admin/demo/reset` (`api/admin.py:74-96`) reseeds them.
2. **Who creates:** The operator/seed script, via direct DB insert; there is **no runtime issuance endpoint**. Delegations are static demo fixtures.
3. **What it authorizes:** Spending up to `max_order_value` (INR), only `allowed_categories`, per-item quantity ≤ `max_quantity_per_item`, valid until `expires_at`.
4. **How represented:** `Delegation` ORM row (`entities.py:37-46`): `delegation_id` (PK), `buyer_agent_id`, `max_order_value`, `allowed_categories` (JSON), `max_quantity_per_item`, `expires_at`, `status` (`active`/`revoked`).
5. **How transmitted:** As a plaintext string `delegation_token` in request bodies (e.g., `CreateCheckoutRequest.delegation_token`, `dto.py:107-110`; `CompleteCheckoutRequest`, `dto.py:119-122`).
6. **How validated:** `delegation_service.validate_delegation()` (`services/delegation_service.py:15-31`) — DB lookup by token; unknown/mismatched-buyer/inactive → `UNAUTHORIZED_AGENT` (401); expired → `DELEGATION_EXPIRED` (401). Also embedded in gating (expiry/category/quantity/spend rules, `gating_service.py:39-99`).
7. **Which component validates:** The Merchant Agent service layer (`checkout_service`, `gating_service`).
8. **Signed?** **No.** It is a database row keyed by a guessable plaintext string (`dlg_9f8e7d`). No signature/HMAC.
9. **Can it be modified?** Yes — by anyone with DB/admin access; no revoke API exists despite `status` field (FINAL_AUDIT P1/P2). `validate_delegation` checks `status != "active"` (`delegation_service.py:18`), but no endpoint flips it.
10. **Replay possible?** **Yes.** Ownership is re-checked on `complete` (`_check_ownership`, `checkout_service.py:197-203`), but a captured token is reusable until expiry; no nonce/counter.
11. **Expiry enforced?** **Yes** — both in `validate_delegation` and gating rule 4.
12. **Bound to buyer/session/transaction?** Bound to a **buyer_agent_id**, not to a specific session or transaction. One delegation authorizes unlimited independent transactions up to the caps.
13. **Sufficient to prove user authorization?** **No.** The token is a server-known constant; it proves "this caller knows the merchant's predefined token," not "the human user authorized *this* transaction." There is **no user identity, no per-transaction authorization, no user signature**.

**Difference vs AP2 mandate:** The existing delegation is a **static, unsigned, reusable capability string** bound to a buyer ID and broad limits (category/quantity/spend). It is *not* an AP2-style **mandate**: no user intent statement, no per-transaction object, no cryptographic signature, no binding to exact cart/amount/merchant, no nonce/replay protection, no evidence for third-party verification. The AP2 mandate must be a separate, signed, per-intent/per-transaction object layered on top.

---

## 7. Current Payment Authorization Flow

**Path from "buyer wants to pay" to "Razorpay is called":**

The gated path is `complete_checkout` (`checkout_service.py:596-943`). Gates, in order:

| # | Gate | Implementation | Input | Output | Failure | LLM influence? | LLM bypass? | Audited? | Crypto-verifiable? |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Idempotency | `_idempotent_replay` (`:609`) | idempotency_key | replay resp / None | — | No | — | No | No |
| 2 | Ownership | `_check_ownership` (`:614`, def `:197-203`) | buyer_agent_id + delegation_token | pass/403 | `UNAUTHORIZED_AGENT` 403 | No | No | No (only on success path later) | No (plaintext token) |
| 3 | State | `_assert_transition` (`:629`) | cs.status | pass/409 | `INVALID_STATE_TRANSITION` | No | No | No | No |
| 4 | Price re-check | `_resolve_items`+`_compute_totals` (`:669-694`) | live catalog | new total | → `requires_confirmation` + `PRICE_CHANGED` | No | No | Yes (`final_validation[hold]`) | No |
| 5 | Delegation | `validate_delegation` (`:698`) | token | delegation | `_fail(DELEGATION_EXPIRED/UNAUTHORIZED_AGENT)` | No | No | Yes (`final_validation[declined]`) | No |
| 6 | Stock (strict) | `checkout_service.py:703-727` | pack actual+available | pass/`_fail` | `STOCK_UNAVAILABLE` | No | No | Yes | No |
| 7 | Policy re-run | `_gate` (`:730-739`) | 5 rules | GatingResult | `_fail` with code | No | No | Yes (`final_validation[approved]/[declined]`) | No |

Money movement starts **only after gate 7** (`:752+`). Note the gate order differs slightly from the spec doc ("price→delegation→stock→policy"): the actual order is **price re-check → delegation → stock → policy**.

**Threshold gates before Razorpay order creation:** `ensure_customer` (`saved` driver, `:762-781`), open-order reuse guard (`:785`).

**Can the system answer "Why was this payment allowed?" from persisted evidence?**
- **Partially.** The audit trail records `final_validation[approved]` with the delegation info, plus `complete_requested`, `razorpay_order_created`, `payment_success`. So "which checks passed and which delegation allowed it" is reconstructable.
- **But not cryptographically/provable.** There is **no signed authorization object**. Nothing ties the user's intent to the payment cryptographically. The reason is *server-asserted* (audit rows written by the merchant itself), **not independently verifiable**. And the **amount actually sent to Razorpay is not stored** on the session/audit (see §13), so the exact authorized amount cannot be proved from evidence.

---

## 8. Current Explainability Implementation

**Explainability is deterministic, audit-fact-driven, template-based.** No LLM generates explanations.

- Backend: `services/explain_service.py` (90 lines) — `RULE_PHRASES` (`:9-15`), `ACTION_SENTENCES` (`:17-31`), `explain_session()` (`:34-83`) → `{"explanation": headline, "timeline_facts": [...]}`. Exposed at `GET /explain/{id}` (`api/audit.py:50-53`). Empty → "No recorded activity…" (`explain_service.py:36-37`).
- Structured evidence: every audit event carries `actor`, `action`, `decision`, `rules_fired`, `reasoning_summary`, `payload` (hash-chained). Agent trace `GET /agent_sessions/{id}/trace` (`api/agent.py:146-167`).
- UI: `MoneyStoryDrawer.jsx` (verdict stepper, "Every rupee" ladder, policy grid, agent actions, gateway proof), `AuditTimeline.jsx` (event timeline + filters + stages), `OpsView.jsx` (explanation + policy grid + "Razorpay invoked YES/NO"), `MerchantAgentTrace.jsx` (decision trace), `utils/actionMeta.js` (action→title mapping), `deriveStages`.

### A–H explainability matrix

| Aspect | Exists? | Evidence |
|---|---|---|
| A. What happened (factual timeline) | **Yes** | `AuditTimeline`, `/audit_logs`, `explain` timeline_facts |
| B. Why it happened | **Yes (rule-level)** | `gating_evaluated`/`final_validation` rules_fired; `RULE_PHRASES` |
| C. What rule allowed it | **Yes** | `rules_fired` on approved events; policy grid in UI |
| D. What authorization allowed it | **Partial** | `final_validation` payload references delegation, but no signed mandate object shown |
| E. What user intent authorized it | **Partial** | `reasoning_summary`/`payload` capture the buyer request string, but no structured, signed intent document |
| F. Exact transaction authorized | **Partial** | Session line_items + totals recorded; but **Razorpay order amount not recorded** (`entities.py` has no order-amount column) |
| G. Who/what signed the authorization | **No** | Nothing is cryptographically signed; only the plaintext delegation token identity |
| H. Independently verifiable evidence | **Partial** | Hash chain detects *local* tampering, but is **unkeyed** — cannot prove authenticity to a third party (no external anchor/signature) |

**Gap:** The system explains *what/why (rules)* well but cannot explain *who provably authorized the exact amount* or provide *third-party-verifiable* proof.

---

## 9. Current Cryptographic / Tamper-Evidence System

**Two distinct cryptographic mechanisms exist, and neither is authorization.**

**A. SHA-256 hash chain (audit)** — `services/audit_service.py`
- `_canonical()` (`:18-30`) canonicalizes exactly **9 fields** (`event_id, checkout_session_id, actor, action, decision, rules_fired, reasoning_summary, payload, timestamp`) via `json.dumps(sort_keys=True, separators=(",",":"))`. **`payload` uses `default=str`** (may serialize non-deterministically across object order — a subtle canonization caveat but `sort_keys` applies at each level).
- `append_event` (`:33-62`): `hash = SHA256(canonical + prev_hash)` (`:59`); links to last row by `seq desc` (`:44-45`); first entry `prev_hash=""`.
- `verify_chain` (`:81-90`): recomputes from `seq asc`; checks both `event.prev_hash == prev_hash` and `event.hash == expected`.
- **Who signs:** nobody — it's a plain SHA-256 over server-generated content. **Key:** none (unkeyed). **Stored:** in `audit_events` columns. **Verifies:** merchant's own `GET /audit_logs/verify`.
- **Covered:** the 9 business fields. **Mutable outside signed material:** `seq`, `prev_hash`, `hash` themselves are not in the hash input; a single-row edit that also recomputes the local row's hash and re-links still passes only if the attacker recomputes the whole suffix — but see the fatal limitation.
- **Actually append-only?** In application code **yes** (only `append_event` writes). **Undermined by:** `/admin/demo/reset` (`api/admin.py:80`) deletes all `AuditEvent` rows (admin-token-gated), and the test fixture `conftest.py:58` deletes rows.
- **Tampering detectable?** **Yes for in-place edits** (test `test_audit_and_errors.py:33-45`). **But:** because the chain is **unkeyed** and verification starts from `seq asc` with `prev_hash=""`, **suffix deletion+rewrite is undetectable**. No external anchor (signed head / off-box hash) exists. A DB administrator could rewrite history and `verify_chain` would still report `intact`.
- **Replay protection?** **None.**

**B. HMAC-SHA256 payment signature** — `services/payment_service.py:184-193` `verify_checkout_signature`
- `expected = HMAC(key_secret, "order_id|payment_id")`; compared with `hmac.compare_digest`. Fails **closed** when no creds. Used only in `payment_callback` (`api/checkout.py:193`).
- **This is the single cryptographic trust anchor in the whole system** — Razorpay's own secret. It authenticates that a Razorpay Checkout payment occurred for a given order, but covers **only the order_id/payment_id pair**, not the amount, not the authorization, not the mandate.

**Distinction (critical):** A hash chain proves *internal consistency* ("these rows link"), not *authorization* or *authenticity*. HMAC proves *Razorpay signed the payment*, not *that any party authorized the transaction*. Neither is an AP2-style mandate/signature. "Hash chain ≠ cryptographic authorization."

---

## 10. Current Audit Trail

**Schema:** `AuditEvent` (`entities.py:84-98`): `seq, event_id, checkout_session_id, actor, action, decision, rules_fired (JSON), reasoning_summary, payload (JSON), prev_hash, hash, timestamp`.

**Event types** (verified, from `checkout_service`, `discovery_service`, `loop.py`, `admin.py`, `checkout.py`): `discovery_requested`, `discovery_result_generated`, `checkout_create_requested`, `checkout_update_requested`, `gating_evaluated`, `reservation_created`, `checkout_created`, `checkout_updated`, `checkout_declined`, `checkout_completed`, `checkout_cancelled`, `confirmation_requested`, `confirmation_accepted`, `confirmation_rejected`, `complete_requested`, `final_validation`, `razorpay_order_creation`, `razorpay_order_created`, `razorpay_token_charge`, `payment_pending`, `awaiting_authorization`, `payment_success`, `payment_failure`, `payment_token_saved`, `token_needs_reauth`, `token_charge_fallback`, `inventory_adjusted`, `demo_reset`, plus agent events (`agent_{tool}` per iteration, `agent_reasoning_only`, `agent_outcome`, guard holds, `buyer_proposal_decision`, `buyer_terms_decision`).

**Actors:** `buyer_agent`, `merchant_agent`, `operator`, `razorpay` (enum in `protocol/schemas/audit_event.json`).

**Ordering:** monotonic `seq` PK autoincrement, single writer under `mutate()` lock.

**Correlation:** `checkout_session_id` indexed; agent events via `payload.agent_session_id`; payments via `payload.order_id`; delegation referenced in `reasoning_summary` (no dedicated column).

**Hash linkage/prevention:** `prev_hash`+`hash` per row (`audit_service.py`). Verification `GET /audit_logs/verify`.

**UI:** `AuditTimeline.jsx`, `OpsView.jsx`, `MerchantAgentTrace.jsx`, `MoneyStoryDrawer.jsx`, `actionMeta.js`. Filters: all/money/agent/system chips.

### Can the trail reconstruct the full chain?
| Link | Reconstructable? | Evidence |
|---|---|---|
| User intent | **Partial** (free-text in reasoning_summary/payload) | audit + buyer transcript |
| Authorization | **Partial** (delegation referenced, no signed mandate) | `final_validation` payload |
| Buyer decision | **Partial** (`buyer_proposal_decision`, chat transcript) | buyer_service separate (not hash-chained) |
| Merchant validation | **Yes** | `final_validation`, agent events |
| Policy decision | **Yes** | `gating_evaluated` rules_fired |
| Stock reservation | **Yes** | `reservation_created` (+ `inventory_adjusted`) |
| Payment authorization | **Partial** | `awaiting_authorization`/`razorpay_order_created` (no signed auth object) |
| Razorpay invocation | **Yes** | `razorpay_order_created`/`razorpay_token_charge` |
| Razorpay result | **Yes** | `payment_success`/`payment_failure`/`payment_pending` |
| Final outcome | **Yes** | `checkout_completed`/`checkout_declined` |

**Missing links:** No binding of user intent → authorization → *exact amount*; buyer_service transcript is **not** hash-chained (separate service, in-memory) so "what the buyer thought it was approving" is not tamper-evident. Razorpay **order amount not persisted** (see §13). No mandate/signature references.

---

## 11. Checkout State Machine

**Actual states:** `created`, `requires_payment`, `requires_confirmation`, `payment_pending`, `completed`, `declined`, `cancelled`.

**Transitions** (`checkout_service.py:37-47` `VALID_TRANSITIONS`; terminal set `:47`):
```
created                 -> {requires_payment, declined, cancelled}
requires_payment        -> {requires_confirmation, payment_pending, completed, declined, cancelled}
requires_confirmation   -> {requires_payment, declined, cancelled}
payment_pending         -> {completed, declined}
completed / declined / cancelled  (terminal, empty)
```

**Triggers/assertions:** `_assert_transition(cs.status, target)` (`:206-213`).

| State/Transition | Who can trigger | Deterministic? | Notes |
|---|---|---|---|
| `created→requires_payment` | `create_checkout` (`:312-341`) on gate pass | Yes | |
| `created→declined` | `create_checkout` gate fail (`:342-365`) | Yes | `_declined_at_create` (`api/checkout.py:49-72`) |
| `requires_payment→requires_confirmation` | `complete_checkout` price-change (`:671-694`) | Yes | `PRICE_CHANGED` |
| `requires_payment→payment_pending` | order-create error (`:801-820`) | Yes | retryable=False; reconcile required |
| `requires_payment→completed` | complete ladder + payment | Yes | only via `_finalize_payment` |
| `requires_payment→declined` | `_fail` on revalidation (`:634-666`) | Yes | |
| `requires_payment→cancelled` | `cancel_checkout` (`:467-499`) | Yes | |
| `requires_confirmation→requires_payment` | `confirm_checkout` accept (`:569-590`) | Yes | version-checked |
| `requires_confirmation→declined/cancelled` | confirm reject / stale (`:527-567`) | Yes | `CONFIRMATION_STALE` |
| `payment_pending→completed/declined` | `reconcile_session` (`:1048-1060`) | Yes | |

**Properties:**
- **Terminal immutable?** Yes — terminal states have empty transition sets; `complete_checkout` short-circuits already-completed (`:625`), cancel blocks terminal (`:479-486`).
- **Complete idempotent?** Yes — idempotency replay (`:609`) and already-completed short-circuit (`:625`); no double charge.
- **Cancellation idempotent?** Yes (`:477-478`).
- **`payment_pending` exists?** Yes.
- **`requires_confirmation` exists?** Yes.
- **Session expiry?** **None.** No session-level expiry field/state. Only *reservation* TTL (10 min) exists (`inventory_service`), and reservations are sweep-on-read. A session in `requires_payment` (or `requires_confirmation`) has **no timeout** — stale sessions persist indefinitely until acted upon. `SESSION_EXPIRED` is defined in the `ErrorCode` enum (`dto.py:9-25`) **but is not produced by any code path** (dead enum value).
- **Stale session handling:** Only via reservation TTL sweep; the session row itself is never expired.

**vs intended conceptual models:** The actual machine covers `created→requires_payment→completed` (main), `→declined` (gating), `requires_payment→requires_confirmation→requires_payment` (price change), `requires_payment→payment_pending→{completed,declined}` (headless/reconcile), and `→cancelled`. **Missing from intent:** any session-expiry/`SESSION_EXPIRED` path.

---

## 12. Pre-Payment Revalidation (TOCTOU)

**Immediately before money moves, the `complete_checkout` ladder re-checks (in order):** price (`:669-694`), delegation (`:698`), stock strict actual+available (`:703-727`), policy gate (`:730-739`). Then Razorpay order creation (`:752+`).

**Not re-checked at completion:**
- **Idempotency** — checked but preceding these (`:609`).
- **Category/quantity/spend** — covered inside `_gate` (`:730-739`), so effectively re-checked.
- **Session status** — via `_assert_transition` (`:629`).
- **Authorization binding to exact amount** — **no order-amount stored**, so the amount sent to Razorpay is *computed* at `:756-757` (`amount_paise = total_rupees * 100`) from the just-validated totals; there's no mis-match possible *within code*, but the authorized amount is **not persisted** anywhere for later proof (bug C-1).

**TOCTOU gap:** Between `final_validation[approved]` (`:741`) and Razorpay invocation there is effectively **no gap** — money movement runs in the same synchronous `mutate()` block immediately after the gates, under the process-wide DB mutation lock (`db.py:48-63`, WAL + `busy_timeout`). Stock/price/policy cannot change between the checks and the charge within a single serialized transaction. The residual TOCTOU is **external**: Razorpay-side amount changes are not re-verified against a persisted authorized amount (none exists). For AP2, this is exactly where a signed authorization snapshot (cart + amount + nonce) should be committed *before* the Razorpay call so the charged amount can be cryptographically re-verified.

---

## 13. Razorpay Integration

**SDK/API:** `razorpay==1.4.2` SDK (`payment_service._get_client`, `:41-53`) + raw `httpx` for recurring charges (`:294`). Two outbound HTTP targets only: Razorpay and Groq.

**Drivers:** `saved` (default; headless recurring after first order), `checkout` (Checkout.js modal every order), `sim` (pytest-only, hard-gated by `config.py:22-38` validator + `_sim_allowed()` `payment_service.py:25-31`; production refuses anything but `saved`/`checkout`).

- **Order creation:** `create_order(checkout_session_id, amount_rupees, mandate=None)` (`payment_service.py:62-108`): `amount = rupees*100`, `currency="INR"`, `receipt=checkout_session_id`, `payment_capture=1`, `notes={checkout_session_id}`; E-Mandate block (`customer_id, method=card, token{max_amount=1500000, expire_at, frequency=as_presented}`) for `saved`.
- **Capture/verification:** `verify_checkout_signature` (HMAC-SHA256, `:184-193`); `charge_with_token` (`:269-386`, recurring endpoint, one retry on `SERVER_ERROR`); capture via `payment.capture` (`:238-239`); `reconcile_pending` (`:147-181`, never retries).
- **Amount conversion:** `*100` rupees→paise in `create_order` (`:84`), completion (`checkout_service.py:756-757`), sim (`:84`), buyer `_complete_flow` (`agent.py:543`), dashboard modal.
- **Currency:** INR constant throughout; `const INR` in `protocol/schemas/product.json`.
- **Receipt:** `receipt=checkout_session_id`.
- **Idempotency:** retry guard reuses open `cs.razorpay_order_id` (`checkout_service.py:785-787`) — **never duplicates orders**; HTTP-level idempotency keys on create/complete.
- **Retry/timeout:** one 3s retry on transient `SERVER_ERROR` only (`payment_service.py:311-323`); network errors → `pending`, reconcile, no blind retry.
- **Duplicate-order protection:** open-order reuse + already-completed short-circuit + idempotency snapshot.
- **Reconciliation:** `GET /reconcile` (`api/checkout.py:251-255`) for `payment_pending`.
- **Error mapping:** `classify_token_rejection` (`:242-266`) → `auth_required`/`declined`; declined charge is non-terminal → modal fallback on same order (`checkout_service.py:892-907`).

**Credentials location & access:**
- `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` in `.env` → `config.py:29-30` → `payment_service.py` (lazy `razorpay.Client`). `has_credentials()` (`:56-59`), `get_key_id()` (`:196-197`).
- **Only `merchant_agent/services/payment_service.py` ever touches them.** The dashboard `RazorpayPayCard.jsx` receives the Razorpay **key_id** (public) via the session's `payment.key_id` for Checkout.js — that's a public publishable key, not the secret.
- **Can the Buyer Agent ever access Razorpay credentials directly?** **Verified: No.** Buyer service has no Razorpay import/credentials; it receives only the *public* key_id through merchant-owned session serialization (`serialize_session`). The secret never leaves `payment_service`.

---

## 14. Current Failure Handling

| Failure | Trigger | Current behavior | State | Audit event | User-facing explanation | Razorpay called? | Retry possible? | Potential bug |
|---|---|---|---|---|---|---|---|---|
| Stock unavailable | gating rule 2 / final stock recheck | `_fail(STOCK_UNAVAILABLE)`; releases reservations | `declined` | `final_validation[declined]`+`checkout_declined` "Razorpay NOT called" | "STOCK_UNAVAILABLE: Razorpay NOT called" (`checkout_declined` summary) | No | Yes (`retryable=True` only for STOCK) | — |
| Spend limit exceeded | gating rule 1 | `_fail` | `declined` | same | error code | No | No (non-retryable) | — |
| Category blocked | gating rule 3 | `_fail` | `declined` | same | `CATEGORY_BLOCKED` | No | No | — |
| Invalid delegation | `validate_delegation` | 401 | n/a (create) / `_fail` (complete) | `UNAUTHORIZED_AGENT` | message | No | No | — |
| Expired delegation | gating rule 4 / validate | 401 | n/a/declined | `DELEGATION_EXPIRED` | message | No | No | — |
| Expired checkout | **no session expiry** | n/a | stays open | — | — | — | — | No session TTL; `SESSION_EXPIRED` unused |
| Price changed | final price recheck `:669` | → `requires_confirmation`, `PRICE_CHANGED` | `requires_confirmation` | `final_validation[hold]` | "price changed ₹old→₹new; confirm" | No | Yes (`retryable=True`) | gating_result overwritten with empty pass (`:673`, P2) |
| Stock changed | strict recheck `:703` | `_fail` | `declined` | TOCTOU demo | STOCK_UNAVAILABLE | No | Yes | — |
| Razorpay rejection (token) | `charge_with_token` non-200 | decline→modal fallback | stays open / `needs_reauth` | `token_charge_fallback`/`token_needs_reauth` | modal reopened | Yes (attempted) | Yes (same order) | — |
| Razorpay timeout | network exc | `pending` | `payment_pending` | `payment_pending` | reconcile required | Yes | No blind retry; via `/reconcile` | — |
| Network failure | httpx exc | `pending` | `payment_pending` | `payment_pending` | reconcile required | Yes | No blind retry | — |
| Duplicate completion | idempotency replay / already-completed | returns existing snapshot | unchanged | no new event | replay | No | — | idempotency replay precedes ownership (`:609` vs `:614`) |
| Invalid state transition | `_assert_transition` | 409 | unchanged | (none in some) | code | No | No | — |
| Malformed request | Pydantic validation | 400 `INVALID_REQUEST` | n/a | none | details | No | Yes | — |
| Buyer declares declined | `finish(declined)` in buyer_completes | one-time hold then accept (`loop.py:753-758`) | audit | `agent_decline_hold` | — | No | — | — |
| Payment pending | order-create err / reconcile | `payment_pending` | `payment_pending` | `payment_pending` | reconcile | Yes | via `/reconcile` | — |

**Payment bypass/test mechanism:** `PAYMENT_DRIVER=sim` + `ACG_TEST_ALLOW_SIM=1` (pytest-only), recorded in `config.py` validator comment as the fix for a historical "stale env silently served simulated paid confirmations." It is **not treated as a bug** — it is double-locked (validator + `_sim_allowed`) and intentionally unreachable outside tests. It can be **accidentally enabled** only if someone sets `PAYMENT_DRIVER=sim` *and* `ACG_TEST_ALLOW_SIM=1` simultaneously (plus blank/none credentials route through sim in `create_order` `:82-89`). It **cannot** silently reach production with the default `.env` (`saved`). It bypasses **Razorpay** only, not the gating ladder. Recommendation: keep as development-only; it is correctly gated.

---

## 15. Security Assessment — Current State

| Finding | Class | Detail |
|---|---|---|
| Razorpay **order amount not persisted** | **HIGH** | `entities.py` lacks order-amount column; `payment_success` records session `totals`, not the gateway-charged amount (`checkout_service.py:983`); open-order reuse never compares total to order amount; update/confirm never clear `razorpay_order_id` (C-1/I6-I7). Prevents proving exact payment amount. |
| `payment_callback` lacks session-state guard | **CRITICAL/HIGH** | `api/checkout.py:179-248` checks only order-id match + HMAC. A `declined`/`cancelled` session with a valid *old* signature could be flipped to `completed` (C-2), because `finalize_with_outcome`→`_finalize_payment` has **no state/ownership/amount guard** (`checkout_service.py:963-1045`). |
| `_finalize_payment` itself unguarded | **HIGH** | Decrements stock/consumes reservations with no transition/ownership/idempotency check; the two unguarded callers are `payment_callback` and `reconcile_session`. |
| `GET /checkout_sessions/{id}` no ownership check | **MEDIUM** | `api/checkout.py:95-98`. IDOR; guessable hex IDs; leaks other buyers' state (no money movement). |
| `GET /audit_logs` unauthenticated + full payload | **MEDIUM** | `api/audit.py:31`. Leaks buyer_agent_id/reasoning_summary; enumeration primitive (C-5). |
| Delegation token plaintext/guessable | **HIGH** | `dlg_*` constant strings; no signature, no nonce; a leaked token is reusable until expiry; not bound to session/amount. |
| Admin token hardcoded in client JS | **HIGH (deployment)** | `dashboard/src/api.js:12-14` ships `admin_demo_token`; `config.py:42` default. Demo-acceptable; production must not. |
| Unauthenticated `/reconcile` | **MEDIUM** | `api/checkout.py:251-255`; scope-limited to `payment_pending` but no auth. |
| CORS `allow_origins=["*"]` | **LOW/MEDIUM** | `main.py:36-41`. |
| `.env` contains live test keys on disk (untracked) | **MEDIUM** | Identity tokens present; not git-tracked (verified) but a live NIM key sits in a deprecated comment. |
| Prompt injection / tool abuse | **LOW (mitigated)** | Architectural: control plane never reads request; tool allowlist + signature filter + `_guard_no_silent_substitution`; adversarial tests pass. No explicit jailbreak-text filtering — relies on architecture. |
| LLM overreach / money access | **LOW** | No LLM path to money/price/stock write; fail-closed. |
| Client-controlled prices/totals/policy | **LOW** | `_resolve_items` uses authoritative price; totals server-side; gating server-side. |
| Stock/checkout manipulation | **LOW** | mutation lock + revalidation + strict stock. |
| Audit tampering | **MEDIUM** | chain detects in-place edits; **suffix rewrite undetectable** (unkeyed); `/admin/demo/reset` deletes rows. |
| Unsafe logging / secret exposure | **LOW** | no printed secrets verified. |
| Rate limits missing | **MEDIUM** | no rate limiting anywhere. |
| Race conditions/TOCTOU | **LOW** (internal) | serialized by `mutate()` lock; external gap only (see §12). |
| Replay of payment callback | **LOW** | HMAC + order binding; but no idempotency/nonce on callback path itself beyond finalize short-circuit. |
| Webhook/payment verification | **LOW** | HMAC verified; no amount/customer re-verification against persisted auth (unpersisted amount). |
| `buyer_b/client.py:109` crash on `"error": null` | **LOW** | interop bug. |
| `razorpay_api_reachable` probe misleading | **LOW** | `admin.py:111` (P2). |
| In-memory agent `SESSIONS` unbounded/volatile | **LOW** | `state.py:18`; lost on restart, no eviction, single-process locks. |

---

## 16. Code Quality Assessment

**Strengths:** Excellent separation of concerns (api / services / models / agent); structured error envelopes everywhere (`ProtocolError`, `dto.py:28-60`); disciplined "raise after commit" pattern (`api/checkout.py` header); clear failure codes; thorough pytest suite (53 tests, six categories); hash-chain + explainability well-factored.

**Findings:**
- **Dead/weak code:** `SESSION_EXPIRED` enum value never raised (`dto.py`); `IdempotencyKey.endpoint`/`session_id` populated but ignored on replay (`_idempotent_replay` `checkout_service.py:219-225`); `IntegrityPanel.jsx` referenced in docs but removed (docs stale).
- **Duplicate logic:** amount `*100` conversion repeated across `payment_service.py`, `checkout_service.py`, `agent.py:543`, dashboard — single-source-of-truth opportunity for AP2.
- **Gaps:** `payment_callback` uses untyped `body: dict` (string-coerced); `reconcile` no body validation; `GET /conversations/{id}` unauthenticated.
- **Hardcoded:** admin token in JS; delegation tokens are constants.
- **Oversized:** `checkout_service.py:1060` lines (esp. `complete_checkout` 596-943) — the AP2 layer should wrap, not refactor.
- **Config:** central pydantic-settings good; the *intentional* `sim` bypass is documented and double-locked (correct).
- **Test gaps:** no test asserts the Razorpay order amount is stored/bound; no negative test on `payment_callback` flipping a terminal session; no test for session expiry (feature absent).
- **No circular deps, no unreachable branches observed** in the hot paths.

---

## 17. Scalability / Concurrency Assessment

- **Stock reservation:** `create_reservations`/`release`/`consume` with TTL (`inventory_service.py`); `available_stock` = actual − reserved (`:46-48`); over-reservation blocked by gating + strict final validation. Handles concurrent reservations correctly under the mutation lock.
- **Concurrent create/complete:** serialized by the process-wide `threading.RLock` `mutation_lock` (`db.py:48-63`) + SQLite WAL + `busy_timeout=5000`. **Single-process only** — not horizontally scalable, but correct for the hackathon scope.
- **Duplicate completion:** idempotency snapshot + already-completed short-circuit + open-order reuse. Sound.
- **Audit ordering / hash-chain concurrency:** single writer under the same lock → `prev_hash` linkage is race-free in-process.
- **Locks/race conditions:** no data races observed within the serialized model.
- **Reconciliation:** `/reconcile` only from `payment_pending`; no blind retry.
- **Limits:** reservations sweep is sweep-on-read (no background reaper); sessions never expire; agent `SESSIONS` in-memory single-process. **Conclusion:** architecture is **logically correct and mature for the hackathon scope**; not production/distributed-grade, and that is acceptable per the task. AP2 additions must preserve the single-writer serialization.

---

## 18. Protocol Surface (Merchant Agent Public Endpoints)

| Method | Path | Purpose | Auth | State change | Money? | Audit | Actor |
|---|---|---|---|---|---|---|---|
| GET | `/health` | liveness + driver | none | no | no | no | any |
| POST | `/discover` | NL discovery→proposal | none | no (audit only) | no | yes | buyer |
| POST | `/checkout_sessions` | create checkout | delegation token (service) | create session/reservations | no | yes | buyer |
| PUT | `/checkout_sessions/{id}` | update basket | ownership | re-gate/re-reserve | no | yes | buyer |
| GET | `/checkout_sessions/{id}` | read session | **none (IDOR)** | no | no | no | any |
| POST | `/checkout_sessions/{id}/complete` | complete→pay | ownership | charge, stock, complete | **yes** | yes | buyer |
| POST | `/checkout_sessions/{id}/confirm` | accept price change | ownership | →requires_payment | no | yes | buyer |
| POST | `/checkout_sessions/{id}/cancel` | cancel | ownership | cancel/release | no | yes | buyer |
| POST | `/checkout_sessions/{id}/payment_callback` | Checkout.js result | **HMAC** | complete (unguarded finalize) | **yes** | yes | razorpay |
| POST | `/checkout_sessions/{id}/reconcile` | resolve pending | none | finalize | **yes** | yes | buyer/op |
| GET | `/audit_logs` | list audit | none | no | no | — | any |
| GET | `/audit_logs/verify` | verify chain | none | no | no | — | any |
| GET | `/explain/{id}` | explain | none | no | no | — | any |
| POST | `/agent_sessions` | run merchant agent | delegation (in tools) | variable, may pay | **yes** | yes | buyer |
| GET | `/agent_sessions/{id}` | read agent session | none | no | no | — | any |
| POST | `/agent_sessions/{id}/decision` | buyer answers proposal | none (state-guarded) | re-run tools, may pay | **yes** | yes | buyer |
| GET | `/agent_sessions/{id}/trace` | decision trace | none | no | no | — | any |
| GET | `/health/agent` | agent status | none | no | no | — | any |
| GET/PATCH | `/admin/packs*` | stock mgmt | **X-Admin-Token** | stock mutate | no | yes | operator |
| POST | `/admin/demo/reset` | wipe+reseed | **X-Admin-Token** | destructive | no | yes | operator |
| GET | `/admin/ops/status` | ops status | **X-Admin-Token** | no | no | — | operator |

**Could a separate buyer transact using only these public contracts?** **Yes** — proven by `buyer_b/client.py`. The buyer needs: a valid `delegation_token`+`buyer_agent_id` pair, discovery, create→complete. Money flows merchant-side. The main external dependency is the pre-seeded delegation row (no issuance endpoint) — a real independent buyer needs a way to obtain a valid token.

---

## 19. AP2 Readiness Matrix

| # | Concept | Status | Why / what blocks |
|---|---|---|---|
| 1 | User intent / mandate | **PARTIALLY EXISTS** | Intent captured only as free-text `reasoning_summary`/prompt; no structured, signed intent document. |
| 2 | Intent constraints | **EXISTS (as delegation, not intent)** | category/quantity/spend/expiry in `Delegation` row. Not bound to a specific intent. |
| 3 | Specific transaction authorization | **MISSING** | No per-transaction auth object; delegation covers unlimited txns. |
| 4 | Closed/finalized transaction authorization | **MISSING** | Amount finalized in-ladder but never captured as a signed authorization snapshot. |
| 5 | Binding to exact cart | **MISSING** | Line items in session, not signed; on update/confirm old `razorpay_order_id` retained. |
| 6 | Binding to exact merchant | **PARTIALLY EXISTS** | Authorization inherently merchant-side (buyer holds token for that merchant), but not cryptographically bound in a mandate. |
| 7 | Binding to exact amount | **MISSING** | Order amount **not persisted** (C-1). |
| 8 | Binding to currency | **EXISTS (implicit)** | INR constant throughout, `const INR` in schema. Not signed. |
| 9 | Expiry | **EXISTS (delegation-only)** | `expires_at` enforced; no session/mandate-level expiry. |
| 10 | Nonce/replay protection | **MISSING** | No nonce on authority; idempotency keys exist per request but not for auth. |
| 11 | Cryptographic signing | **MISSING** (only HMAC payment sig + unkeyed hash) | No mandate signature. |
| 12 | Signature verification | **MISSING** (only payment-sig verify) | No mandate verification; chain unkeyed. |
| 13 | Evidence chain | **PARTIALLY EXISTS** | Audit chain + explain, but unkeyed and no signed auth object in evidence. |
| 14 | Authorization→transaction linkage | **MISSING** | Session stores `delegation_id`, no mandate_id linking auth to txn; amount unpersisted. |
| 15 | Intent→authorization linkage | **MISSING** | No intent object; only `reasoning_summary`. |
| 16 | Authorization→Razorpay payment linkage | **MISSING** | `receipt=checkout_session_id` links payment↔session; **no signed auth↔payment binding**, amount not re-verified. |
| 17 | Verifiable audit trail | **PARTIALLY EXISTS** | Chain detects local tamper; suffix-rewrite undetectable; not third-party verifiable. |
| 18 | Dispute reconstruction | **PARTIALLY EXISTS** | Timeline/explain good; buyer transcript not chained; exact amount unproveable; no signed auth. |
| 19 | Agent accountability | **PARTIALLY EXISTS** | every agent step audited with reasoning; but no signed decision receipt. |
| 20 | Merchant accountability | **PARTIALLY EXISTS** | merchant enforces deterministically; no cryptographic proof. |
| 21 | Separation of user intent from agent reasoning | **EXISTS (deployment)** | two services/contexts; but buyer intent not persisted as a signed artifact in the shared chain. |

---

## 20. AP2-Inspired Integration Architecture (DESIGN ONLY — no code)

Conceptual layering to add on top of the existing system, wrapping it rather than replacing it:

```
User Intent
   │  (captured + structured as an Intent Mandate: what, caps, expiry)
   ▼
Intent Mandate  ← SIGNED (merchant/authority key); nonce; bound to user+buyer
   ▼
Buyer Agent  (existing tool-loop; unchanged)
   ▼
Merchant Discovery  (existing /discover; unchanged)
   ▼
Basket  (existing)
   ▼
Checkout Session  (existing state machine; unchanged)
   ▼
Transaction/Execution Mandate  ← new, DERIVED at completion: binds {session, exact line
   │                               items, exact total, currency, merchant, buyer, nonce,
   │                               prev intents}; SIGNED; PERSISTED to a new table
   ▼
Merchant Validation  (existing completion ladder, gates 1-7; unchanged)
   ▼
Deterministic Policy Gates  (existing gating engine; unchanged)
   ▼
Authorization Verification  ← NEW: verify execution-mandate signature + nonce/amount
   │                           BEFORE Razorpay; store the E-mandate hash in the
   │                           checkout session and audit event
   ▼
Razorpay Test Payment  (existing payment_service; unchanged call)
   │                        + persist order_id AND authorized amount (store amount!)
   ▼
Signed Decision Receipt  ← NEW: sign {outcome, payment_id, order_id, final total,
                             session_hash} with merchant key; append to chain
   ▼
Tamper-Evident Audit Trail  (existing hash chain; anchor head with signed receipt so
                             suffix rewrite becomes detectable)
```

**Layer ownership/proof matrix:**

| Layer | Owns | Proves | Must NOT trust | Created by | Signed by | Verified by | Persisted | Shown to judge |
|---|---|---|---|---|---|---|---|---|
| Intent Mandate | user intent + caps | "user authorized this scope" | LLM claims | user/human | authority key | merchant | mandate table | signed mandate + intent text |
| Execution Mandate | exact cart+amount+currency+merchant+nonce | "this exact transaction authorized" | client/LLM prices | merchant (at completion) | merchant key | merchant (pre-Razorpay) + verifier | mandate table + session link | signed E-mandate + line items + total |
| Decision Receipt | outcome + payment linkage | "payment X occurred for authorization Y" | merchant's own later edits | merchant | merchant key | independent verifier | chain + receipt table | signed receipt (chain-anchored) |
| Authorization Verification | gate before Razorpay | "money only after valid signed authorization" | — | merchant | — | merchant | audit event | verify_passed audit row |

**Key insertion points (no refactor of existing logic):**
1. **New middleware/interceptor** in `complete_checkout` *after* gate 7 (`final_validation[approved]`, `checkout_service.py:741`) and *before* money movement (`:752`): build + sign the Execution Mandate, verify, then proceed. This preserves the existing ladder.
2. **Persist the authorized amount** — store `razorpay_order_id` + the authorized `total` (paise) on the new mandate table and (minimally) surface it, addressing C-1.
3. **Wrap the audit chain** with a periodic signed "head anchor" (e.g., a signed receipt each completion) so a suffix rewrite becomes detectable — closing the unkeyed-chain gap.
4. **Sign the Intent Mandate** at the buyer/user boundary (dashboard or a new issuance endpoint), reusing the existing merchant key + adding a user/authority key.
5. **Reuse existing**: policy engine, checkout state machine, Razorpay integration, audit trail/hash chain, stock reservation, buyer agent, dashboard — all untouched as integration targets (see §21).

---

## 21. What NOT to Rebuild

These components are **functioning and correct** — the AP2 layer should **wrap/integrate**, never replace:

| Existing | Status | AP2 role |
|---|---|---|
| Deterministic policy/gating engine | Works, fail-closed, well-tested | **Keep as the enforcement oracle**; AP2 signs/proves its outcomes, doesn't re-implement them |
| Checkout state machine (`checkout_service.py:37-47`) | Correct transitions, idempotent complete/cancel | **Keep as the lifecycle**; AP2 adds a mandate verification step at the completion boundary |
| Completion ladder (price→delegation→stock→policy) | Sound TOCTOU protection | **Keep; insert** Authorization Verification after gate 7 |
| Razorpay integration (`payment_service.py`) | Credentials isolated, HMAC callback, drivers | **Keep; add** amount persistence + mandate linkage around the order-creation call |
| Audit trail + hash chain (`audit_service.py`) | Append-only in-app, tamper detection | **Keep; add** signed head anchors + signed decision receipts to make it verifiable |
| Stock reservation (`inventory_service.py`) | TTL, correct accounting | **Keep as-is**; the execution mandate references the reserved/consumed quantities |
| Buyer Agent (`buyer_service/`) | Genuine tool-loop, approval gate | **Keep; emit** a structured intent for the mandate at bill-approval time |
| Merchant Agent runtime (`agent/`) | Real LLM+tool agent | **Keep; unchanged** — the mandate sits in the deterministic layer it already calls |
| Dashboard (`dashboard/`) | Read/display UI | **Keep; add** a "Signed authorization & proof" panel reusing the existing Ops/MoneyStory views |

---

## 22. Critical Gaps

1. **No signed authorization (mandate) anywhere.** All authority is unsigned plaintext delegation tokens / server-asserted audit rows — cannot prove user authorization or transaction binding to a third party. *(Foundational AP2 gap.)*
2. **Razorpay order amount is never persisted / bound.** `entities.py` has no order-amount column; `payment_success` records session totals not the charged amount; open-order reuse never compares to order amount (C-1). Blocks proving the exact authorized/charged amount. *(Critical to fix before/with AP2.)*
3. **`payment_callback` (and the underlying `_finalize_payment`) can flip a session to `completed` with only order-id+HMAC — no state/ownership/amount guard** (C-2). The new Authorization Verification layer must cover this path too.
4. **Unkeyed hash chain → suffix rewrite undetectable.** No external signed anchor; `/admin/demo/reset` deletes audit rows.
5. **No intent object and no intent→authorization→payment linkage** persisted; buyer transcript is untrusted/in-memory and un-chained.
6. **No nonce/replay protection on authorization.**
7. **No session expiry** — `SESSION_EXPIRED` exists but is never raised.
8. **IDOR/read leaks** (`GET /checkout_sessions/{id}`, `GET /audit_logs` unauthenticated) weaken the confidentiality of the evidence that a judge would rely on.

---

## 23. Recommended Implementation Order

1. **P0 — Persist & bind the authorized amount** (store `razorpay_order_id` + authorized total on a mandate/session record at completion). Unlocks everything else.
2. **P0 — Add state/ownership guard on `payment_callback`** (prevent terminal-session flip) — close C-2 before layering sign on.
3. **P0 — Design the Execution Mandate schema** (session, exact line items, exact total paise, currency, merchant, buyer, nonce, expiry, prev-intent-ref) + generation at gate 7 + **signature** (merchant key).
4. **P1 — Authorization Verification step** immediately before Razorpay invocation in `complete_checkout`.
5. **P1 — Intent Mandate** at buyer bill-approval boundary (capture + sign structured intent).
6. **P1 — Signed Decision Receipts + audit-head anchors** to make the chain externally verifiable (via append_event'ing signed anchors).
7. **P2 — Nonce/replay protection** on both mandates.
8. **P2 — Session expiry** (wire up the dormant `SESSION_EXPIRED`).
9. **P2 — Close read-path IDOR** and gate `/reconcile`; move admin token out of client JS for non-demo deployments.
10. **P2 — New dashboard "Signed Authorization & Proof" panel** reusing OpsView/MoneyStory primitives, plus judge-facing export.

---

## Gap Summary Table

| Area | Current Status | AP2-Relevant Gap | Risk | Priority |
|---|---|---|---|---|
| Intent mandate | Free-text reasoning only; unsigned | No structured signed intent; no linkage to auth | HIGH | P0/P1 |
| Execution authorization | Unsigned delegation + server gate | No per-transaction signed auth; no amount binding | HIGH | P0 |
| Signing | HMAC payment sig + unkeyed SHA chain | No mandate/authority signature | HIGH | P0/P1 |
| Verification | HMAC callback only; chain internal | No mandate verification; chain not third-party verifiable | HIGH | P0/P1 |
| Audit | Append-only SHA chain, tamper-detect (in-place) | Suffix rewrite undetectable; no signed anchor | MEDIUM | P1 |
| Razorpay binding | `receipt=session_id` links pay↔session; HMAC callback | **Order amount not persisted**; no auth→payment binding | HIGH | P0 |
| Replay protection | Idempotency keys; no nonce on auth | No auth nonce; token reusable | MEDIUM | P1/P2 |
| Explainability | Strong what/why-rules; weak who-authorized/exact-amount | A–H: D/E/F/G/H partial or missing | MEDIUM | P1 |

---

**Verification caveat:** All findings above are traced to actual code with file/line references. Items traced across previous audit markdowns (`AUDIT.md`, `FINAL_AUDIT.md`, etc.) were cross-checked against source where feasible; anything not confirmable from code is marked "NOT VERIFIED" above. The single genuinely unverifiable-by-static-reading risk is live behavior of the Groq models and real Razorpay test-mode responses (not exercised in this read-only pass) — the test suite simulates them.

This is a **factual snapshot only** — no code was modified, and the AP2 layer is left as design recommendations in §19–§23 for the next (implementation) step.
