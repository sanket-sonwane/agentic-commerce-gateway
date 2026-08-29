# AP2-Inspired Layer — Post-Implementation Audit Report

**Audited system:** Agentic Commerce Gateway (ACG) — `merchant_agent/`, `buyer_service/`, `buyer_b/`, `dashboard/`
**Audit basis:** AP2-inspired authorization & provenance layer as **currently implemented** (i.e. including the RE-3 / RE-1 / RE-4 coordinator fixes landed in the prior session). This is a *post-implementation*, not a *pre-implementation*, audit.
**Evidence gathered:** code reading, live TestClient probes against a seeded SQLite DB, and the 102-test suite. Live probe outputs are quoted inline.
**Date:** 2026-08-28

---

## 1. Executive Verdict

**PARTIALLY.**

The AP2 layer adds a genuine, working **tamper-evident recording** stack: canonical-JSON Ed25519 signatures over Intent Mandates, Execution Mandates, Policy Decisions and Decision Receipts; a SHA-256 append-only audit hash chain with a signed head anchor; and an enforcement gate that now actually connects *"what the buyer authorized"* (Intent Mandate) to *"what was executed"* (Execution Mandate), failing closed before money movement.

However, the layer still has structural limits a skeptical auditor must understand:

- The **amounting source of authority is the merchant's server-side catalog+gating**, not an external signer. AP2 "verification" is a **server-side self-check** over data the same process produced; the signatures prove *the merchant signed this content*, which is meaningful for tamper-evidence but is **not an independent authorization vote** by the buyer/user.
- Mandate verification and the artifacts live in the **same SQLite database** being attested, so a live attacker with DB write access can rewrite both the data *and* the signature/hash columns. Signatures only prove content was signed *at signing time*, not that the live DB row is untampered — this is a property of all DB-resident signing schemes without an offline/anchor export.
- The intent→execution chain is now connected and enforced (RE-3 fixed), the amount cross-check is no longer a pure tautology (RE-1 fixed), and mandates are single-use (RE-4 fixed) — but only when the caller supplies an `intent_mandate_id`; a checkout completed without one still gets an unlinked Execution Mandate (backward-compatible).

**Strongest single piece of evidence that the layer works:** an inline tamper of the persisted session total is *not* caught by the old `amount_matches` check (true tautology) but **is** caught by the new independent `amount_matches_persisted` cross-check; and a budget-violating Intent Mandate blocks completion with `409 PAYMENT_BLOCKED` with **no Razorpay order created** and the session parked `declined`.

**Residual highest-risk gap:** the merchant Ed25519 **private key was not actually protected by `.gitignore`** (a trailing-comment syntax bug made the `secrets/` pattern inert). This audit found and fixed it — see §19 (P0).

---

## 2. Actual Architecture Discovered

**What is actually in the code (not the PRD):**

| Component | Files | Key classes/functions | Purpose | Data owned | Called by | Agent or deterministic? |
|---|---|---|---|---|---|---|
| Buyer Agent (protocol client) | `buyer_service/tools.py`, `agent.py`, `conversations.py` | `MerchantClient` (HTTP-only), `Conversation`, agent loop | Turns natural language into Merchant-Protocol HTTP calls | In-memory conversation; **no merchant DB, no secrets** | CLI/UI | LLM-driven tool selection; deterministic HTTP client |
| Buyer B (deterministic interop) | `buyer_b/client.py` | CLI HTTP client | Independent second buyer implementation | none | CLI | Deterministic |
| Merchant API | `merchant_agent/api/checkout.py`, `provenance.py`, `audit.py`, `admin.py` | routers + Pydantic DTOs | Published protocol surface | none (stateless) | external clients | Deterministic handlers |
| Checkout service | `merchant_agent/services/checkout_service.py` | `create_checkout`, `update_checkout`, `complete_checkout`, `_finalize_payment`, `_issue_receipt_for_outcome`, `_assert_transition`, `computes_cart_hash`, `settle_probe` | Session lifecycle, gating, mandate building, payment orchestration, state machine | CheckoutSession, Reservation, IdempotencyKey | API + merchant-agent tools | Deterministic |
| Gating engine | `merchant_agent/services/gating_service.py` | `evaluate` (5 rules), `first_failure_code` | Deterministic policy: spend/stock/category/expiry/quantity | none (pure) | checkout_service | Deterministic |
| Delegation registry | `merchant_agent/services/delegation_service.py` | `validate_delegation`, `allowed_categories` | Buyer authority bounds | Delegation | gating, agent tools | Deterministic |
| Authorization service | `merchant_agent/services/authorization_service.py` | `build_execution_mandate`, `verify_execution_mandate`, `build_intent_mandate`, `verify_intent_scope`, `sign_policy_decision`, `issue_decision_receipt`, `mark_mandate_consumed` | Build+sign/verify the 4 AP2 artifacts; intent-scope enforcement | Intent/Execution Mandate, PolicyDecision, DecisionReceipt | checkout_service, provenance, API | Deterministic |
| Crypto service | `merchant_agent/services/crypto_service.py` | `canonical_json`, `sha256_hex`, `object_hash`, `sign`, `verify`, key mgmt | Ed25519 + canonical JSON + SHA-256 | merchant Ed25519 key (file, gitignored) | authorization, audit, provenance | Deterministic |
| Provenance service | `merchant_agent/services/provenance_service.py` | `verify_provenance` | Reconstruct + verify the per-transaction chain | none (reads) | `/provenance/{id}` | Deterministic |
| Audit service | `merchant_agent/services/audit_service.py` | `append_event`, `verify_chain`, `signed_head_anchor` | Append-only SHA-256 hash chain | AuditEvent | everywhere | Deterministic |
| Explain service | `merchant_agent/services/explain_service.py` | `explain_session` | Templated human sentences from audit facts | none | `/explain/{id}` | Deterministic (no LLM) |
| Payment service | `merchant_agent/services/payment_service.py` | `create_order`, `execute_sim_capture`, `verify_checkout_signature`, `charge_with_token`, `reconcile_pending` | Razorpay boundary + sim/test stub | Razorpay creds (config) | checkout_service, callback | Deterministic |
| Merchant Agent loop | `merchant_agent/agent/loop.py`, `tools.py` | agent loop + `complete_checkout` tool | Optional LLM-driven negotiation that ends in the **same** gated service | AgentSession | `/agent_sessions` | LLM orchestration, deterministic gates |

**Key architectural fact:** There is **no LLM in the money path**. The lowest-risk operation `complete_checkout` (and every payment step) is pure deterministic Python (gating + mandate + Razorpay client). LLM output can only *produce candidate baskets and call the same HTTP endpoints*; it cannot bypass `complete_checkout`.

---

## 3. AP2-Inspired Features Implemented

The four AP2 artifacts exist and are populated:

### 3.1 Intent Mandate (`intent_mandates`, `IntentMandate`)
Built by `build_intent_mandate` (`authorization_service.py:100`) via `POST /mandates/intents` (`api/provenance.py:23`). Signed payload (`intent_mandate_payload`, `:82`): `mandate_id, type, merchant_id, buyer_agent_id, intent, constraints, iss_ts, exp, nonce, version`. Constraints are derived from the buyer's delegation and an optional `constraints_override` that can only **tighten, never loosen** (`api/provenance.py:30-49`).

### 3.2 Execution Mandate (`execution_mandates`, `ExecutionMandate`)
Built in `complete_checkout` (`checkout_service.py:806+`) via `build_execution_mandate` (`:141`). Signed payload (`execution_mandate_payload`, `:37`): `mandate_id, type, intent_mandate_id, merchant_id, buyer_agent_id, checkout_session_id, items, total_paise, currency, iss_ts, exp, nonce`. Live row confirms presence of all binding fields:

```json
"bindings_fields": {
  "buyer_agent_id": "buyer_agent_llm_01",
  "checkout_session_id": "cs_e7b25dd660",
  "currency": "INR",
  "key_id": "merchant-key-01",
  "merchant_id": "merchant_grocery_01",
  "nonce_present": true, "has_signature": true, "has_hash": true
}
```

### 3.3 Policy Decision (`policy_decisions`, `PolicyDecision`)
Signed by `sign_policy_decision` (`:236`) — wraps the **deterministic gating result**, not LLM output. Payload includes `decision`, `checks` (all 5 rule outcomes), `authorized_amount_paise`, `execution_mandate_id`.

### 3.4 Decision Receipt (`decision_receipts`, `DecisionReceipt`)
Signed by `issue_decision_receipt` (`:267`) issued in `_issue_receipt_for_outcome` (`checkout_service.py:1185+`) after payment settles. Binds `intent_mandate_id`, `execution_mandate_id`, `decision_id`, `checkout_session_id`, `authorized_amount_paise`, `razorpay_order_id`, `razorpay_payment_id`, `payment_status`, `outcome`.

**Canonical serialization:** all signing/hashing go through `crypto_service.canonical_json` (key-sorted, minimal separators, ISO-8601 datetimes) — verified by unit test `test_canonical_json_is_key_sorted`. **Nonce/freshness:** every mandate gets `new_nonce() = uuid4().hex`; Execution Mandates expire after `settings.mandate_ttl_minutes` (10).
**Key scheme:** Ed25519; private key `secrets/merchant_signing_key.pem` (load-or-generate at `crypto_service.py:90`); public key exposed via `public_key_pem()` / `verifiable_public_key` in the intent API; **private key never leaves the server**.

---

## 4. Cryptographic Correctness

### Signatures
Ed25519 (`crypto_service.py`). `sign` = Ed25519 over `canonical_json(payload).encode()`; `verify` re-canonicalizes. **Tamper detection confirmed live:** zeroing the stored signature makes the provenance check fail:

```json
"tamper_signature": [{ "name": "execution_mandate_signature", "ok": false, "detail": "Mandate em_... signature over canonical payload" }]
```

### Hashes
SHA-256 (`sha256_hex`). Domain separation: `object_hash` **excludes the `signature` field** but includes all semantic fields (`:66-70`), so a signature change doesn't break the content hash while any content change does. Unit test `test_object_hash_excludes_signature` covers this.

### Key management
- Private key: `secrets/merchant_signing_key.pem`, load-or-generate, PEM PKCS8 no-encryption (`:98-105`). Not served to buyers/dashboard/logs.
- **FINDING (P0, now fixed):** the `.gitignore` line `secrets/            # AP2 merchant Ed25519 signing key` was inert because gitignore does not honor trailing inline `#` comments — the whole line became a literal pattern that matches nothing. `git check-ignore secrets/merchant_signing_key.pem` returned **exit 1 (NOT ignored)**. This audit corrected the file (comment on its own line); it now returns **exit 0 (ignored)**. Until committed the key file was still on disk and at risk of a real `git add .`.

### Nonce/Freshness / replay
Covered in §9 (RE-4): a consumed mandate is rejected (`not_consumed=false`).

---

## 5. Authorization Enforcement

**The real money-guard is `complete_checkout` calling `verify_execution_mandate` and, when an intent is supplied, `verify_intent_scope`, failing closed before any Razorpay call.** Live evidence of enforcement:

- **Budget-violating intent** → `409 PAYMENT_BLOCKED`, session `declined`, **no order, no mandate**:
```json
"intent_budget_blocked": { "status": 409, "error": "PAYMENT_BLOCKED" }
"intent_budget_session": { "status": "declined", "order": null, "em": null }
```
- **Unknown intent id** → `409 PAYMENT_BLOCKED`:
```json
"unknown_intent_blocked": { "status": 409, "error": "PAYMENT_BLOCKED" }
```
- The pre-existing fail-closed test `test_execution_mandate_verify_failure_blocks_payment` (monkeypatched verification) asserts session→`declined`, `razorpay_order_id is None`.

**Enforcement is fail-closed**: the only path to a `PAYMENT_BLOCKED`/verification-failure is a return before `create_order` (`checkout_service.py:820-827`).

---

## 6. Amount / Merchant / Session Binding

Four independent bindings are checked in `verify_execution_mandate` (`:207`): `amount_matches` (vs `authorized_amount_paise`), **`amount_matches_persisted` (vs the authoritative `cs.totals` snapshot — RE-1)**, `merchant_id`, and `session_id`; plus `buyer_id` and `currency`.

Live anti-tautology evidence (the core of RE-1): tampering only the persisted totals is **missed** by the old `amount_matches` (true) but **caught** by the new cross-check (false):
```json
"tamper_persisted_totals": { "amount_matches": true, "amount_matches_persisted": false }
```
Happy-path bindings all agree:
```json
"happy_db": { "authorized_amount_paise": 10400, "mandate_total_paise": 10400, "razorpay_order_amount": 10400 }
```

**Amount origin is authoritative:** the amount in `complete_checkout` is `new_totals["total"] * 100` computed from the catalog (`_compute_totals`), **never from client/LLM-supplied prices**. The Razorpay order is created with that same amount (`create_order`, `checkout_service.py:851,946-947`), and `cs.razorpay_order_amount` is persisted.

---

## 7. Replay Protection (RE-4)

A **single-use `consumed_at`** column now exists on `ExecutionMandate` (entity + idempotent SQLite migration in `db.init_db()`). `_issue_receipt_for_outcome` calls `mark_mandate_consumed` after payment settles. `verify_execution_mandate` adds `not_consumed`.
Live evidence — after completion the mandate is both consumed and nonce-referenced:
```json
"consumed_replay_after_complete": { "nonce_unused": false, "not_consumed": false }
```
The new test `test_consumed_mandate_cannot_authorize_again` asserts both flags. **Scope note:** this single-use flag primarily defends the *same* Execution Mandate against a second authorization; replay of a *whole session* is separately blocked by the `completed` terminal-state guard (`checkout_service.py:655-657`) and the payment-callback state guard (`api/checkout.py:199`).

---

## 8. Explainability

`/explain/{checkout_session_id}` (`api/audit.py`, `explain_service.py`) produces templated, deterministic sentences from audit facts — **no LLM invents reasons**. Live output for a successful paid order:

```json
"explanation": "This order was completed successfully.",
"timeline_facts": [
  "All deterministic policy checks passed.",
  "A checkout session was created and stock reserved.",
  "Final re-validation passed immediately before payment.",
  "The exact transaction was frozen in a signed Execution Mandate before payment.",
  "A Razorpay order was created.",
  "The payment succeeded.",
  "The order was completed.",
  "A signed Decision Receipt bound the mandate, policy and payment."
]
```

**Explainability matrix** (question → whether the system evidences it):

| Question | System provides it? | Evidence endpoint/artifact |
|---|---|---|
| What happened? | **Yes** | `/explain` timeline_facts; audit chain |
| Why allowed? | **Yes** | gating `checks` in `/audit_logs` + `/provenance` `policy_approved` |
| Which mandate authorized it? | **Yes** | `/provenance` `execution_mandate` + `intent_mandate_id` |
| Was it within the buyer's intent? | **Yes (when intent linked)** | `/provenance` `intent_scope` check (live: `ok:true`) |
| What was the signed decision? | **Yes** | `policy_decision` artifact + `decision_receipt` |
| Payment identifiers? | **Yes** | receipt holds `razorpay_order_id`/`razorpay_payment_id` |
| Exact cart? | **Yes** | `cart_binding` + `items` in mandate payload |
| *Why did the LLM pick these items?* | **No** (out of scope) | not recorded |

---

## 9. Decision Receipts

Issued only when an Execution Mandate exists (legacy transactions get `PROVENANCE: NOT_AVAILABLE` — `provenance_service.py:41-50`). Binds mandate+policy+payment and is Ed25519-signed. Provenance includes a `decision_receipt` check (signature + amount + session match). Live check-list shows the receipt check is part of the verified set:

```
['execution_mandate_signature','execution_mandate_integrity','amount_binding','cart_binding','policy_approved','order_amount_binding','decision_receipt']
```

---

## 10. Audit Trail & Tamper Evidence

The audit trail is a **SHA-256 append-only hash chain** (`audit_service.py`): `entry_n.hash = SHA256(canonical_content_plus_prev_hash)`. `verify_chain` recomputes it; **modifying any old row breaks the chain** (guarded by `test_audit_and_errors`/`verify_audit_logs`). A signed head anchor is appended by `signed_head_anchor` and exposed at `/audit_logs/verify`.

Live chain output (12 entries, intact, signed anchor present):
```json
"audit_chain": { "intact": true, "entries": 12, "anchor_sig": true }
```

Actual chained rows show sequential `prev_hash→hash` linkage and the full reconstruction path:

```
seq  action                    prev_hash      hash
1    checkout_create_requested  (empty)        84b12c14de21
2    gating_evaluated           84b12c14de21   f58590954b8e
3    reservation_created        f58590954b8e   b4e6e3b590e7
4    checkout_created           b4e6e3b590e7   2c4a8ef0b77a
5    complete_requested         2c4a8ef0b77a   587dd32ebcc1
6    final_validation           587dd32ebcc1   dbc1ca0da51f
7    execution_mandate_created  dbc1ca0da51f   2dc1c6667b44
8    razorpay_order_creation    2dc1c6667b44   8a2aac46c551
9    razorpay_order_created     8a2aac46c551   79806c425d46
10   payment_success            79806c425d46   316152114c5e
11   checkout_completed         316152114c5e   fb730a47ab7e
12   decision_receipt_issued    fb730a47ab7e   19693cac60b5
```

This row 7→12 reconstruction = **Intent/Execution → Policy → Razorpay order → payment outcome → Receipt**, satisfying the "reconstruct a completed transaction" requirement. The mandate→receipt cross-links are by `execution_mandate_id` (mandates, decisions, receipts) and `intent_mandate_id` (execution↔receipt).

**Caveat (documented, not a code bug):** the signed anchor signs only the *head hash* at a point in time (`:93-111`) — it proves a head was witnessed, not that the whole history is immutable (a mid-chain rewrite followed by re-signing could pass `verify_chain`). An exportable/offline anchor set would be stronger (see §27).

---

## 11. Merchant Agent Boundary

- The Merchant Agent loop (`agent/loop.py`, `tools.py`) is **driven by LLM tool-selection but all its effectful actions funnel into the same deterministic service functions**. Its `complete_checkout` tool (`tools.py:736`) calls `checkout_service.complete_checkout` — the identical gated path a buyer uses. The LLM process has **no path to call `create_order`/Razorpay/mark-payment-success directly** (only `checkout_service.complete_checkout` and its tools are exposed).
- Enforcement modes: `buyer_completes` forces the buyer to trigger payment; `agent_completed` still routes through the same `complete_checkout` gate (`tools.py:725-730`).

---

## 12. Buyer Agent Boundary

- `buyer_service/tools.py` is a **pure HTTP client** (httpx) exposing only the Merchant Protocol endpoints. It never imports merchant internals, never opens the merchant DB, never touches `secrets/`, `razorpay_key_secret`, price lists or signing keys (**NFR-07/G2** satisfied).
- A second independent buyer (`buyer_b/client.py`) exists, proving the protocol is consumable without the LLM.
- **All public endpoints** enumerated (METHOD PATH; auth noted):

```
POST   /discover
POST   /checkout_sessions
PUT    /checkout_sessions/{id}
GET    /checkout_sessions/{id}
POST   /checkout_sessions/{id}/complete
POST   /checkout_sessions/{id}/confirm
POST   /checkout_sessions/{id}/cancel
POST   /checkout_sessions/{id}/payment_callback
POST   /checkout_sessions/{id}/reconcile
GET    /audit_logs
GET    /audit_logs/verify
GET    /explain/{id}
GET    /provenance/{id}
POST   /mandates/intents
POST   /agent_sessions
GET    /agent_sessions/{id}
POST   /agent_sessions/{id}/decision
GET    /agent_sessions/{id}/trace
GET    /health, /health/agent
GET    /admin/packs            (X-Admin-Token)
PATCH  /admin/packs/{id}/stock (X-Admin-Token)
POST   /admin/demo/reset       (X-Admin-Token)
GET    /admin/ops/status       (X-Admin-Token)
```
Admin endpoints use a **static default token** `admin_demo_token` (`config.py:42`, `api/admin.py:23`) — demo-scope only, flagged in §19/§26.

---

## 13. Gating vs AP2 Authorization

| Property | Gating Engine | AP2 Layer | Both | Missing |
|---|:--:|:--:|:--:|:--:|
| User Intent Validity | — | ✅ (intent scope check) | — | Only when intent linked |
| Delegation Scope | ✅ (category/qty/expiry) | ✅ (re-derives from intent) | ✅ | — |
| Stock Available | ✅ | — | — | — |
| Exact Cart Contents | — | ✅ (cart_hash) | — | — |
| Authorized Amount | ✅ (spend_limit) | ✅ (amount bindings) | ✅ | — |
| Merchant Consistency | — | ✅ (merchant_id) | — | — |
| Nonce/Freshness | — | ✅ | — | — |
| Signature Validity | — | ✅ (Ed25519) | — | — |
| Session/Ownership | ✅ (`_check_ownership`) | ✅ (session_id) | ✅ | — |

Gating is the **authority** (decides pass/fail). AP2 **proves and binds** the decision; it never manufactures one (`authorization_service.py:5-6`).

---

## 14. Razorpay Integration

- **Order amount is persisted-authoritative:** `create_order(... amount_rupees=new_totals["total"])`; `cs.razorpay_order_amount = int(order.get("amount") or amount_paise)` (`checkout_service.py:851,947`). No client-supplied price is used at this point.
- **Callback HMAC:** `payment_callback` (`api/checkout.py:179`) enforces (a) order match, (b) `status == requires_payment`, (c) `verify_checkout_signature(order_id, payment_id, signature)` (HMAC-SHA256 over `order_id|payment_id` with the webhook **key_secret** — `payment_service.py:184`), and (d) **AP2 amount binding** via `settle_probe` comparing settled vs `authorized_amount_paise` → `PAYMENT_BLOCKED` on mismatch.
- **Double-capture/state:** a session that is already `completed` cannot be re-finalized (`api/checkout.py:199` state guard); the callback on a completed session is blocked (`test_duplicate_callback_on_completed_is_blocked`).
- **Idempotency:** create/complete accept an `Idempotency-Key` header storing a byte-for-byte response snapshot (`checkout_service.py:249-284`), so network retries do not double-create orders.

**FINDING (correct, by design):** In the lt**test environment** the API key is invalid/empty and `ACG_TEST_ALLOW_SIM=1` short-circuits `create_order`/`execute_sim_capture`/`charge_with_token` to synthetic `order_sim_*`/`pay_sim_*` results (`payment_service.py:76-89,111-136`). **That is exactly why the tests pass with an invalid merchant API key** — the tests never call Razorpay. Production without that flag raises a `RuntimeError` instead of silently simulating (`:77-81,284-289`). This is a deliberate dev-only switch (kept out of production), but see §26.

---

## 15. State Machine

`VALID_TRANSITIONS` (`checkout_service.py:37-45`):

```python
created:                → requires_payment, declined, cancelled
requires_payment:       → requires_confirmation, payment_pending, completed, declined, cancelled
requires_confirmation:  → requires_payment, declined, cancelled
payment_pending:        → completed, declined
completed / declined / cancelled: → (terminal, no out-transitions)
```

`_assert_transition` enforces legality; tests cover double completion, terminal-declined, price-change → `requires_confirmation`, and stale confirmation (`test_state_machine.py`, `test_checkout_flow.py`). All failure paths release reservations and land the session in a terminal `declined` state — verified live for the blocked-intent case (`status: declined`, no order, no mandate).

---

## 16. Failure Testing

Failure-injection evidence (live + suite):
- **Intent out-of-budget** → `409 PAYMENT_BLOCKED`, session `declined`, no order (live, §5).
- **Unknown intent** → `409 PAYMENT_BLOCKED` (live, §5).
- **Signature corrupt** → provenance `execution_mandate_signature ok=false` (live, §4).
- **Persisted-total tamper** → `amount_matches_persisted=false` (live, §6).
- **Consumed-mandate replay** → `not_consumed=false` (live, §7).
- **Stock vanishes at completion** → `STOCK_UNAVAILABLE`, no payment (`test_inventory.py`, `test_merchant_agent.py:209`).
- **Verification monkeypatched to fail** → `PAYMENT_BLOCKED`, no order (`test_ap2_provenance.py`).
- **Duplicate callback after complete** → `PAYMENT_FAILED`/`INVALID_STATE_TRANSITION`, no state change (suite).

---

## 17. Security Findings

| ID | Severity | Finding | Evidence | Status |
|---|---|---|---|---|
| S-1 | **Critical** | Merchant Ed25519 **private key not actually gitignored** (trailing-comment syntax made `secrets/` inert) | `git check-ignore` exit 1 before fix | **Fixed this audit** |
| S-2 | High | No *offline/exportable* evidence anchor; artifacts + verification live in the same DB, so live DB-write attacker can rewrite signed columns | architecture; `signed_head_anchor` signs only head | Open (P1) |
| S-3 | Medium | Static default admin token `admin_demo_token` in config; no rotation/secret store | `config.py:42` | Open (P1) |
| S-4 | Medium | Intent-scope enforcement is **opt-in** (only if caller passes `intent_mandate_id`); unlinked completions still allowed (backward-compat) | `checkout_service.py` gating on `if intent_mandate_id` | Open (P1) |
| S-5 | Medium | Signatures are server self-signatures over server values; no buyer-behalf signature → identity of "who authorized" is merchant-internal | `crypto_service`/`authorization_service` | Intentional design (P2) |
| S-6 | Low | Private key PEM written unencrypted (PKCS8 no-encryption) | `crypto_service.py:98-105` — file perms defend | Open (P3) |
| S-7 | Low/Med | Prompt-injection surface: LLM text is not placed into signed objects (intent text is only a description string), so signing scope is protected — but intent `description` is free text logged to audit | `api/provenance.py:55`, audit payload | Low |
| S-8 | Medium | Race: process-wide `mutation_lock` serializes money-critical writes (SQLite), so two parallel completions cannot both create a payment for one session; but the lock is a single-process simplification | `db.py:48-63` | By design (demo); see §21 |

**IDOR:** `_check_ownership` verifies `buyer_agent_id`+delegation match the session owner (`checkout_service.py:225-233`, `UNAUTHORIZED_AGENT`/403); admin endpoints are token-guarded. No IDOR observed.

---

## 18. Observability

Every money step is audit-logged with cross-references: `execution_mandate_created` logs `mandate_id`, `decision_id`, `authorized_amount_paise`, `canonical_payload_hash`, `checks` (`checkout_service.py:828-845`); `razorpay_order_created` logs `order_id`/driver; `payment_success` logs order+payment+`simulated`; `decision_receipt_issued` logs receipt+mandate. Audit payloads avoid secrets (private key/Razorpay secret never logged).

---

## 19. Code Quality / Dead Code

- `authorization_service.py` docstring says the layer "only proves and binds the decision — it never manufactures one" — this is accurate post-fix.
- `PROVENANCE_NOT_FOUND` DTO code unused (per plan).
- `verify_with_key` (`crypto_service.py:165`) appears unused (only `verify` is referenced) — minor dead code.
- `settle_probe` returns `None` in sim when no credentials → amount check skipped with fallback to persisted order amount (`checkout_service.py:1058-1074`); in real mode it uses the live fetched payment amount. Fine but the fallback weakens sim coverage of the amount check.
- The `.gitignore` fix (S-1) is the notable quality correction.

---

## 20. Scalability / Concurrency

- **Atomicity:** stock decrement + reservation consumption occur within the serialized `mutate()` transaction (`checkout_service.py:1082-1088`); SQLite WAL + `mutation_lock` serialize money-critical mutations (`db.py`).
- **In-memory singletons:** `_PRIVATE_KEY`, `_client`, and a process-wide `RLock`. The `RLock` serializes all writes → **throughput bottleneck** in multi-process/multi-node deployments; fine for single-process demo, not horizontally scalable as-is.
- **Hash chain under concurrency:** writes are serialized, so `append_event`'s `select(...order_by(seq.desc()).limit(1))` is race-free within the lock; a multi-process deployment would need a DB-level serialization/locking story for the chain.
- **Multi-merchant/multi-buyer:** `merchant_id`, `buyer_agent_id`, `key_id` are modeled and bound, so multiple buyers work; truly *multiple merchants on one deployment* would reuse a single signing key (key is process-global) — a limitation.

---

## 21. Test Coverage

- **102 tests pass** (`python -m pytest merchant_agent/tests -q` → `102 passed`).
- New AP2 coverage: `test_ap2_fixes.py` (8 tests: intent linkage, budget/category/unknown-intent block, persisted-total tamper, consumed-replay, intent signature-in-scope, crypto sanity); `test_ap2_provenance.py` (13 tests: crypto unit, mandate-on-completion, provenance checks, amount/signature tamper, fail-closed, replay, intent creation, audit anchor).
- **Gaps:** (a) no live webhook-HMAC path test with real creds (never exercised in CI since creds empty); (b) `settle_probe` sim fallback means the callback amount-binding branch is only lightly covered; (c) no multi-process/concurrency stress test; (d) no test forcing the `verify_execution_mandate` `intent_scope_ok` flag *through* `complete_checkout` with a linked-but-violating intent via the API (unit-level via `verify_intent_scope` is covered).
- **Non-determinism:** none observed (all timing/math deterministic; sim payment uses uuid nonces but outcomes are deterministic).

---

## 22. Protocol Interoperability

Contracts are explicit Pydantic DTOs (`models/dto.py`) and auto-documented via `/openapi.json`, `/docs`. The Merchant Protocol is transport-plain JSON over HTTP and is **not coupled to a buyer LLM**: `buyer_b` (a deterministic CLI client) consumes it directly. Any custom client can replay `POST /checkout_sessions`, `/complete`, `/mandates/intents`, given the documented DTO shapes.

---

## 23. Protocol Interoperability (with AP2 canonical framing)

AP2's intent/execution separation (§14-15) is now expressible: client creates an Intent Mandate (scope) → completes with `intent_mandate_id` → Execution Mandate is linked and verified within scope → compliance/provenance is machine-checkable via `/provenance`. A custom replaying client can create both mandates and read a signed evidence chain, satisfying "different buyer implementations".

---

## 24. Critical Findings

1. **`secrets/` gitignore was inert** (S-1) — merchant private key at risk of being committed. **Now fixed.**
2. **Verification tautology (pre-fix)** is substantially closed by `amount_matches_persisted` (RE-1) but the mandate is still built and verified in the *same request* over *same-source* data; independence is improved, not absolute.
3. **Intent-scope enforcement is opt-in**: a checkout completed without `intent_mandate_id` produces an unlinked Execution Mandate (backward-compat) — so the strongest guarantee holds only when callers supply an intent.

---

## 25. P0 Fixes

- [x] **P0-S-1 — Protect the merchant private key from being committed.** Fixed `.gitignore` so `secrets/` is actually ignored (verified `git check-ignore` exit 0). *Also check whether `secrets/` was ever staged by a prior commit and `git rm --cached` if so.*

---

## 26. P1 Fixes

1. **Require (or strongly encourage) intent linkage for all new checkouts.** Consider making `intent_mandate_id` mandatory in `complete_checkout` once all callers are migrated (removes S-4).
2. **Seed a real admin token + rotation** instead of static `admin_demo_token` (S-3); move to env/secret store.
3. **Emit an exported/offline evidence bundle** (mandate + decision + receipt + audit head) signed and retrievable independent of the live DB, and let `/provenance` verify against it (S-2).
4. **Mark `verify_with_key` dead code for removal** or wire it into a HIERARCHY-of-trust verification (buyer/delegation-signed intent) to move from self-signed to multi-party authorization (S-5).

---

## 27. P2/P3 Improvements

- P2: move from single-process `RLock` to a transaction-safe concurrency story for SQLite or migrate to PostgreSQL (scalability, §20).
- P2: add a live webhook-HMAC integration test toggle (with test credentials) to cover the real callback path that CI currently can't.
- P2: make callback amount-binding exercise the probe path more strongly in sim (avoid `None` fallback masking).
- P3: encrypt the private key at rest (or rely on OS file ACLs) and document key rotation (`crypto_service.py:98`).
- P3: add a concurrency/stress test to lock in reservation/idempotency guarantees.

---

## 28. What Is Already Strong

- **Deterministic money path with no LLM in the loop** — gating + mandate + Razorpay are all code.
- **Canonical-JSON Ed25519 signing/hashing over the 4 artifacts** with correct domain separation (proven by unit + live tamper tests).
- **Append-only SHA-256 audit chain + signed head anchor** (live `intact:true`, 12 chained rows reconstructing the full transaction).
- **Fail-closed enforcement** (any verification failure → `PAYMENT_BLOCKED`, no Razorpay call).
- **Intent→Execution chain now connected + enforced** (RE-3) and **mandates single-use** (RE-4).
- **Clean buyer/merchant boundary** + two independent buyer clients (protocol genuinely interoperable).
- **Auto-documented Pydantic contract** (`/openapi.json`).

---

## 29. What Must NOT Be Claimed

- ❌ Do **not** claim the system performs **multi-party cryptographic authorization** — signatures are merchant-self-signed over merchant-derived values; there is no buyer/delegation signature in the mandate chain (S-5).
- ❌ Do **not** claim **/provenance detects all tampering** — it can only verify what it re-reads *in the same DB*; a live DB-write attacker with the merchant key can rewrite signed columns (S-2). Offline/export anchors are not implemented.
- ❌ Do **not** claim **test suite exercised real Razorpay** — the API key is empty and `ACG_TEST_ALLOW_SIM` short-circuits to simulated orders; the HMAC webhook path is untested in CI.
- ❌ Do **not** claim **intent enforcement for every transaction** — it applies only when a valid `intent_mandate_id` is supplied at completion (S-4).
- ❌ Do **not** claim **horizontal scalability** — single-process `mutation_lock` + in-memory key/crypto singletons are a demo simplification.

---

## 30. Final Scorecard

| Dimension | Grade | Notes |
|---|:-:|---|
| Authorization enforcement | B+ | Real gates; intent-scope opt-in gap |
| Cryptographic integrity | A− | Canonical Ed25519/SHA-256; key-management P0 fixed |
| Tamper evidence | B+ | Hash chain + signed anchor, but same-DB self-attestation |
| Explainability | B+ | Deterministic facts + intent-scope sentence |
| Replay protection | B+ | Single-use mandates + state guards |
| Agent boundaries | A− | Clean separation; LLM never in money path |
| Observability | A− | Rich cross-referenced audit |
| Scalability | C | Single-process lock limitation |
| Test coverage | B+ | 102 passing; webhook path + concurrency gaps |

---

## 31. Final YES / PARTIALLY / NO Verdict

**PARTIALLY.**

*"If a skeptical auditor inspects one transaction, can they verify using system evidence that it was correctly authorized and executed?"*

- **PARTIALLY — YES for: correctness of authorization** iff an intent mandate was linked: an auditor can call `/provenance/{id}` and get machine-verifiable checks (`execution_mandate_signature`, `policy_approved`, `amount_binding`, **`intent_scope`**, `order_amount_binding`, `decision_receipt`) over Ed25519-signed payloads, plus a SHA-256 chained audit log whose 12 rows reconstruct the entire lifecycle, plus a signed head anchor.
- **NO for: proof of *independent authority* and *DB-immutability*** — the signatures are merchant-self-signed and live in the same DB they attest, and there is no offline exportable anchor. The auditor can prove "the merchant signed this content and the audit chain is internally consistent," but **cannot cryptographically prove** "*an outside party with authority approved this exact amount*" or "*this DB row was never altered since signing.*" That requires a different trust architecture (external signer / ledger / offline anchor).

Strongest evidence for the PARTIALLY: the budget-violating intent → `409 PAYMENT_BLOCKED`, session `declined`, **no order created** (real enforcement), combined with the inline tamper where `amount_matches` (old tautology) says `true` but the independent `amount_matches_persisted` says `false` — proving the new cross-check adds real, non-tautological detection.

---

## 32. Recommended Fix Order

1. **P0 (done):** land the `.gitignore` fix; run `git rm --cached secrets/ -r` if anything was staged; rotate the signing key afterward.
2. **P1:** make intent linkage required for all new checkouts (or at least surface a loud warning/report for unlinked completions) → closes S-4.
3. **P1:** implement an exported/offline signed evidence bundle and verify provenance against it → closes S-2 (the single biggest audit-credibility gap).
4. **P1:** move to a real admin secret + rotation → closes S-3.
5. **P1:** add a live webhook-HMAC integration test (test-mode credentials) so the callback amount-binding branch is actually exercised.
6. **P2/P3:** concurrency story (remove single-process lock or move to PostgreSQL), encrypt-at-rest guidance + key rotation docs, remove `verify_with_key` dead code, add concurrency stress test.

---

### Appendix — Mermaid diagrams

#### A. End-to-end sequence (post-implementation, with AP2 artifacts)

```mermaid
sequenceDiagram
    participant U as User/Buyer LLM
    participant BA as Buyer Agent (tools.py)
    participant C as Merchant API
    participant AU as authorization_service
    participant G as gating_service
    participant CR as crypto_service
    participant P as payment_service (Razorpay)
    participant DB as SQLite + audit chain

    U->>BA: "buy milk x2"
    BA->>C: POST /mandates/intents (buyer, delegation)   [optional scope]
    C->>AU: build_intent_mandate
    AU->>CR: canonical_json + Ed25519.sign(intent)
    CR-->>C: Intent Mandate (im_...)
    C-->>BA: im_id
    BA->>C: POST /checkout_sessions (line_items)
    C->>G: evaluate (5 deterministic rules)
    C->>DB: reserve stock; AUDIT row 1-4
    C-->>BA: CheckoutSession (requires_payment)
    BA->>C: POST /…/complete (im_id)
    C->>G: re-evaluate + recheck price/stock
    C->>AU: verify_intent_scope(im_id, items, total)  fail->PAYMENT_BLOCKED
    C->>AU: build_execution_mandate (total, cart_hash, im_id)
    AU->>CR: sign; store cs.authorized_amount_paise
    C->>AU: verify_execution_mandate (amount_persisted, intent_scope, not_consumed)
    C->>DB: AUDIT row 5-7 (execution_mandate_created)
    C->>P: create_order(amount=authorized)  [sim in tests]
    P-->>C: order + razorpay_order_amount
    C->>DB: AUDIT row 8-9 (order created)
    P->>DB: payment outcome (sim capture)
    C->>DB: AUDIT row 10-11 (payment_success/completed)
    C->>AU: issue_decision_receipt; mark mandate consumed
    C->>DB: AUDIT row 12 (decision_receipt_issued); signed head anchor
    C-->>BA: CompleteResponse (completed)
```

#### B. Entity-relationship / artifact linkage

```mermaid
flowchart LR
    IM[IntentMandate im_*]<-- intent_mandate_id -->EM[ExecutionMandate em_*]
    EM -->|execution_mandate_id| PD[PolicyDecision dec_*]
    EM -->|execution_mandate_id| RC[DecisionReceipt rcpt_*]
    IM -->|intent_mandate_id| RC
    CS[CheckoutSession] -->|authorized_amount_paise / execution_mandate_id| EM
    CS -->|razorpay_order_id| RZ[Razorpay Order]
    RZ -->|order_id / payment_id| RC
    PD -.checks.-> G[GatingResult 5 rules]
    EM -.cart_hash.-> IT[line_items]
    ALL[Artifacts] -->|object_hash / signature| K[Merchant Ed25519 key / SHA-256]
    AUDIT[AuditEvent chain] -.prev_hash-> AUDIT
    AUDIT -.head-> ANCHOR["signed_head_anchor (Ed25519)"]
```
