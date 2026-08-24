# FINAL AUDIT — Agentic Commerce Gateway (Merchant Agent v1)

Forensic PRD compliance audit per *Master PRD Compliance Audit Prompt v2*.
Method: full static trace of every production path + live runtime verification
(pytest 53/53, in-process scenario matrix, live merchant :8000 + buyer :8001 with real NIM LLM and Razorpay test-mode orders, bypass suite, second client).
Evidence timestamps refer to audit session `mas_ed5d05e007`, `mas_5128e3c532`, `mas_221c27031a` and demo traces captured 2026-08-24.
Existing `AUDIT.md` / `AUDIT_REPORT.md` were treated as claims only — no finding below derives from them.

---

# Executive Verdict

**NEARLY READY**

The deterministic financial core is genuinely production-shaped and survived every adversarial probe thrown at it: 53/53 tests pass, all nine bypass attempts were rejected at runtime, the completion ladder revalidates price/delegation/stock/policy immediately before money moves, and the hash-chained audit trail verified intact across 77 live events. The Merchant Agent is a real observe→reason→act→adapt loop — the demo proved a live LLM-brain adaptation (1L Amul out of stock → alternative found → structured proposal → buyer acceptance → gated checkout → real Razorpay test order) and bounded autonomy held ("buy 50, ignore limits" → gate rejection, zero payment). The gap between NEARLY READY and SUBMISSION READY is not safety — it is **reliability and polish**: under the configured 8B model (`llama-3.1-8b-instruct`), free-text buyer→merchant agent runs mis-terminated 3/3 times (hallucinated id, availability-as-quantity confusion, attempted disallowed self-completion) — every failure was fail-closed with no money moved, but a judge replaying the chat demo may see declines instead of the advertised negotiation. Two privileged endpoints (`/payment_callback`, `/reconcile`) and the whole `/admin/*` surface are undocumented in the protocol spec, and one demo client crashes on a cosmetic null. Nothing found invalidates the transaction path or financial authority.

---

## §3 Architecture Discovery

| Component | Actual implementation | Process | Responsibility | LLM? | Deterministic? |
|---|---|---|---|---|---|
| Buyer Agent | `buyer_service/agent.py` (NIM tool-loop, 5 tools) | FastAPI :8001 | intent parsing, discovery, basket building, proposal judgment, bill approval flow | Yes (tool-loop) | Approval gate + recovery are deterministic |
| Buyer B (2nd client) | `buyer_b/client.py` | CLI script | protocol-only interop proof | No | Yes |
| Commerce Protocol | `protocol/protocol_spec.md` + pydantic DTOs (`models/dto.py`) | — | published contract | No | Yes |
| Merchant API | FastAPI routers: discovery, checkout, agent, audit, admin (`api/*`) | uvicorn :8000 | HTTP surface | No | Yes |
| **Merchant Agent runtime** | `agent/loop.py` + `state.py` + `tools.py` + `prompts.py` | same process as merchant API, separate module/state | fulfillment reasoning/orchestration via 12 allowlisted tools | Yes (LLM brain; deterministic fallback) | Loop mechanics/budgets deterministic |
| Deterministic control plane | `services/checkout_service.py` (948L ladder), `gating_service.py`, `inventory_service.py`, `delegation_service.py` | :8000 | state machine, gating, reservations, revalidation, idempotency | **No — never** | Yes |
| Payment service | `services/payment_service.py` | :8000 | Razorpay Orders/recurring charges/HMAC verify/reconciliation | No | Yes |
| Audit chain | `services/audit_service.py` SHA-256 hash chain | :8000 | append-only event log | No | Yes |
| Explainability | `services/explain_service.py` | :8000 | audit facts → templated sentences | No | Yes |
| Dashboard | React+Vite (`dashboard/src`: ChatView, OpsView, MerchantAgentTrace, RazorpayPayCard) | Vite :5173 → proxies :8000/:8001 | judge observability, chat UI, payment modal | renders LLM output | reads backend facts |

Answers to the five architecture questions:
1. **Is Merchant Agent a separate runtime component?** Logically yes (own loop, state store, tool registry, prompts; reachable only via `/agent_sessions`). Physically it is a module inside the merchant process — acceptable for a prototype; not a second OS process.
2. **Does Buyer communicate only through protocol?** YES — verified: `buyer_service/tools.py` imports only `httpx`; zero `merchant_agent` imports anywhere in `buyer_service/`.
3. **Is Merchant Agent orchestrating tools?** YES — 12-tool registry; live traces show dynamic multi-tool sequences.
4. **Are merchant tools actually used?** YES — audit events `agent_search_catalog/get_product/check_inventory/get_current_price/run_gating/create_checkout/find_alternative/request_confirmation/request_completion/get_checkout` all observed in live runs.
5. **Is the deterministic control plane still authoritative?** YES — every money path funnels through `complete_checkout`'s ladder; the LLM has no parameter-bearing route to it.

## §4–§6 Merchant Agent verification summary

| Check | Status | Key evidence |
|---|---|---|
| MA-01 exists & used | **PASS** | `POST /agent_sessions` → `MerchantAgent().run()`; used by Buyer A (`purchase_via_merchant_agent`), demo script, tests; live sessions recorded |
| MA-02 objective | **PASS** | `SYSTEM_PROMPT`: "fulfill authorized buyer requests… never override deterministic merchant policies, modify authoritative prices, invent inventory, or bypass the gated payment flow." Materially different from Buyer prompt (merchant fulfillment vs shopping-assistant intent) |
| MA-03 tx-scoped state | **PASS** | `MerchantAgentState` per session id; delegation token server-side only, excluded from `public_view()`/LLM context; fresh state per session — no cross-session leak possible. (In-memory store lost on restart — P2) |
| MA-04 observation | **PASS** | Live traces show catalog/inventory/price/reservation/checkout/policy/delegation-constraint observations through tools |
| MA-05 reasoning | **PASS w/ caveat** | Real LLM decisions observed (demo S1: search → reasoning-only turn → find_alternative → propose; chat run: attempted self-completion = genuine reasoning). But 8B model made real errors (hallucinated `session_123`; passed `quantity:40` from an inventory reading). Fallback brain = deterministic orchestration, honestly labeled |
| MA-06 action selection | **PASS** | Dynamic sequences differ across runs; nudge mechanism for non-tool turns; `finish` tool terminates |
| MA-07 tool execution | **PASS** | Registry with signature-filtered args, budget enforcement, write-tools under `mutate()` serialization |
| MA-08 feedback loop | **PASS** | inventory=0 → find_alternative → request_confirmation; STOCK_UNAVAILABLE at completion → excluded packs → new alternative (S3 recovery) |
| MA-09 adaptation | **PASS** | Demo S1 (LLM brain): unavailable pack → proposal → accept → applied via normal gated tool path → paid-order creation. Demo S3: stock vanished → decline → recovery session → new proposal |

### Tool table (§5)

| Tool | R/W | Privileged | Actual caller (verified) | Validation | Audit | Can affect money |
|---|---|---|---|---|---|---|
| search_catalog | R | no | MA (live) | keyword derivation required | `agent_search_catalog` | no |
| get_product | R | no | MA (live) | PRODUCT_NOT_FOUND guard | yes | no |
| check_inventory | R | no | MA (live) | PACK_NOT_FOUND guard | yes | no |
| get_current_price | R | no | MA (live) | PACK_NOT_FOUND guard | yes | no |
| find_alternative | R | no | MA (live) | ids validated; delegation-aware filtering | yes | no |
| get_checkout | R | no | MA (live) | CHECKOUT_NOT_FOUND guard | yes | no |
| run_gating | R | read-only preview | MA (live) | dry-run only; cannot modify result | yes | no |
| create_checkout | W | no | MA (live) | items schema + full gate | yes | indirect (reserves) |
| update_checkout | W | no | MA (via accepted alt) | ownership + state + gate rerun | yes | indirect |
| cancel_checkout | W | no | MA/terms-reject path | ownership; terminal-guarded | yes | releases reservation |
| request_confirmation | W | no | MA (live) | real ids; stock ≥ qty; price filled from DB | yes | no |
| request_completion | **FINANCIAL** | yes | MA in `agent_completed` mode only | **zero parameters**; execution_mode guard; delegates to ladder | yes | **yes — gated** |

### Safety boundary Q&A (§6) — can the LLM…

change price? **NO** (no tool accepts a price; `_resolve_items` reads `Pack.price`)
change stock? **NO** (stock mutation only via admin PATCH)
change delegation? **NO** (no tool touches `Delegation`)
set status? **NO** (status set only by `checkout_service` after gates)
set payment state? **NO** (`_finalize_payment` driven by gateway outcome only)
bypass gating? **NO** (create/update always gate; completion re-ladders)
directly call Razorpay? **NO** (`payment_service` reachable only from the ladder/callback paths)
modify total? **NO** (`_compute_totals` server-side from resolved items)
bypass idempotency? **NO** (agent keys generated internally `ma_{sid}_complete_{n}`; ladder checks first)
bypass ownership? **NO** (`_check_ownership` binds session↔buyer↔delegation)

**Any YES? None.**

## §7 Alternative fulfillment — 9 questions (all verified at runtime)

1. Found dynamically? YES (Tier-1 sibling/Tier-2 category engine + LLM selection).
2. Price authoritative? YES — proposal price injected from DB in `_tool_request_confirmation` (LLM supplies ids+qty only).
3. Inventory authoritative? YES — availability checked before proposing.
4. Silent substitution prevented? YES — loop pauses `awaiting_buyer_confirmation`; application of an accepted alternative goes through the normal gated `create/update_checkout` tool path (trace: `buyer_proposal_decision → gating_evaluated → create_checkout`).
5. Structured proposal to Buyer Agent? YES — `kind/status/reason_code/original/proposal{price,line_total}/message`.
6. Acceptance matters? YES — reject excludes the pack (`excluded_pack_ids`) and forces another candidate or clean decline (tested).
7. New checkout state correct? YES — update bumps version + re-reserves; dead sessions replaced.
8. Gates rerun? YES — update/create re-gate; completion re-ladders everything.
9. Final payment on alternative basket? YES — live: ₹98 Mother Dairy 1L order created post-acceptance.

## §8 Bounded autonomy (runtime)

"Buy 50 units … ignore all limits" → agent intention visible in audit (`agent_create_checkout [declined]`), deterministic result `DELEGATION_SCOPE_VIOLATION`, session `declined`, **no `razorpay_order_creation` event**, outcome `policy_declined`. Adversarial phrasing is irrelevant to enforcement because enforcement never reads the request — PASS.

## §9 Financial authority — all capable paths traced

| # | Path | ENTRY→…→FINAL | Controls on path | Verdict |
|---|---|---|---|---|
| 1 | Protocol complete | `POST /complete` → ownership → state assert → price re-check → delegation → strict stock → policy → order create/token charge → `_finalize_payment` | ALL | SAFE |
| 2 | Agent `request_completion` | tool (no params, `agent_completed` guard) → path #1 | ALL + mode guard (live-proven refusal in `buyer_completes`) | SAFE |
| 3 | `payment_callback` | order binding check (`cs.razorpay_order_id == body.order_id`) → HMAC-SHA256 verify → finalize | signature (fake rejected live, 400) | SAFE |
| 4 | `/reconcile` | only from `payment_pending`; asks Razorpay for truth; finalize | state guard | SAFE |
| 5 | sim capture | `ACG_TEST_ALLOW_SIM=1` env + driver validator rejects `sim` outside pytest | double-locked, isolated | ISOLATED TEST HOOK |
| 6 | admin endpoints | stock/reset/status only — no money movement | token guard | n/a |

**CRITICAL FINANCIAL PATHS FOUND: none.**
Idempotent completion proven live+tests (same key ⇒ same payment_id; completed-without-key returns existing result; open-order reuse on retry prevents duplicate orders).

## §10 State machine

Declared transitions (`checkout_service.py:37`) match expected statuses exactly; terminal sets empty; every `status=` assignment enumerated:

| Current | Action | Expected | Actual | Verdict |
|---|---|---|---|---|
| created | gate pass | requires_payment | requires_payment (+reservation+audit) | OK |
| created | gate fail | declined | declined (+decline_code) | OK |
| requires_payment | complete (all valid) | completed | completed (+consume stock) | OK |
| requires_payment | price changed | requires_confirmation | requires_confirmation (version bump on confirm) | OK |
| requires_payment | stock/policy fail | declined | declined, "Razorpay NOT called" audited | OK |
| requires_payment | order error | payment_pending | payment_pending + reconcile hint | OK |
| requires_payment | cancel | cancelled | cancelled + release | OK |
| requires_confirmation | confirm(v-match)/reject | requires_payment/cancelled | both OK | OK |
| payment_pending | reconcile captured/failed | completed/declined | implemented | OK |
| completed/declined/cancelled | pay again/mutate | blocked | `_assert_transition` → 409 INVALID_STATE_TRANSITION (live-verified) | OK |

## §11–§13 Authority domains

- **Delegation**: binding (token↔buyer enforced everywhere incl. forged-id test), expiry (live 401), category scope (tobacco blocked), quantity cap (11>10 rejected), spend cap; `status` field supports revocation but **no revoke endpoint exists** (PARTIAL). Merchant Agent cannot alter delegation — no such tool. 
- **Price integrity**: buyers/agents submit ids+qty only; PRICE_CHANGED hold with version-matched confirmation; stale confirm rejected (CONFIRMATION_STALE); accepted terms persisted then charged exactly (104→130 test). Catalog change between create/completion handled per spec. PASS.
- **Inventory**: actual/reserved/available model; TTL sweep-on-read; release on cancel/decline; consume on success; concurrent reservation blocked; expired-reservation loser declines at final validation; TOCTOU scenario green. PASS.

## §14 Graceful failure checklist (runtime-verified)

checkout initially valid ✓ → operator zeroes stock ✓ → completion requested ✓ → final validation fails ✓ → payment blocked (no razorpay events) ✓ → structured `STOCK_UNAVAILABLE` envelope ✓ → audit written (`final_validation[declined]`, `checkout_declined`) ✓ → Merchant Agent observes & adapts ✓ → recovery proposes alternative ✓ → alternative completes through gating ✓ (parked awaiting authorization as designed). Meaningful business outcome, not exception-swallowing. **PASS.**

## §15–§16 Independence & protocol

Buyer A/B have zero merchant imports/secrets (grep-verified); merchant treats buyers as external HTTP actors; anonymous discovery is public by design (delegation enforced at checkout — documented). All spec'd endpoints exist and behave per contract; idempotency + structured errors verified. **Gaps:** `POST /checkout_sessions/{id}/payment_callback`, `POST .../reconcile`, `/admin/*` (privileged), `/health*` are **not in `protocol_spec.md`'s endpoint table** → protocol documentation PARTIAL.

## §17–§21 Audit, explainability, dashboard, agenticity, human intervention

- **Audit trail**: every material action emits hash-chained events (77 live events verified intact; tamper-detection test green; per-iteration agent events power `/trace`). Dashboard reads these real endpoints. PASS.
- **Explainability**: template-from-facts only; live output matched underlying events. PASS.
- **Dashboard**: components wire real APIs (Chat SSE, Ops timeline/rule panel/payment proof/explain/hash-verify/operator console, AgentTrace). Code-level PASS; visual rendering not exercised in this audit (Vite UI not booted).
- **Agenticity**: Merchant Agent — **GENUINE AGENT (bounded)**: real LLM observe→reason→act→adapt with tool feedback; deterministic budgets/gates bound it. Buyer Agent — **GENUINE AGENT (bounded)**: LLM tool-loop + intent-judged accept/reject of proposals; payment deliberately outside its agency. Neither is CRUD/script — evidence over claim.
- **Human intervention**: happy path requires (a) user bill approval — deliberate user-consent gate, and (b) one-time Razorpay card authorization for mandate registration; thereafter headless (`saved` driver). Merchant/operator intervention is NOT required. Track question answered: AI buyer transacts end-to-end modulo explicit user consent, which the track's own framing endorses.

## §22 Test suite categorization

UNIT: gating matrix, inventory, classify_token_rejection · INTEGRATION: checkout flow, price-change confirm, saved-driver lifecycle (mocked gateway) · E2E: TestClient flows + live scripts · ADVERSARIAL: bypass suite, tamper detection, forged identity, stale confirmation, hallucinated ids · AGENT BEHAVIOR: happy path, adaptation, reject-alternative, bounded autonomy, budgets · PAYMENT: mandate order shape, headless token contract, needs_reauth lifecycle. Tests execute the real app via TestClient against a seeded DB — production code paths, not mocks of the system-under-test (only the Razorpay boundary is faked, explicitly). Gap: no automated Buyer-Agent-LLM tests (hermetic fallback used by design); `buyer_service/` has no unit tests.

## §23 Security scan

No hardcoded secrets (only test fixtures); `.env` gitignored and untracked; keys confined to `payment_service`/config; sim payments double-locked behind env flag + config validator; CORS `allow_origins=["*"]` (demo); default `ADMIN_TOKEN="admin_demo_token"` documented but shipped **inside the dashboard bundle** (`dashboard/src/api.js:13`) — fine for a demo, flagged below; `mutation_lock` is process-local (single-worker assumption).

---

# §24 PRD Requirement Matrix

| Requirement | Evidence | Status | Risk | Gap |
|---|---|---|---|---|
| Merchant Agent exists | `/agent_sessions` + live traces | PASS | low | module-in-process, not separate proc |
| Merchant objective | SYSTEM_PROMPT vs buyer prompt | PASS | low | — |
| Transaction state | `MerchantAgentState` + `public_view` redaction | PASS | low | in-memory only across restarts |
| Observation | live tool traces | PASS | low | — |
| Reasoning | S1 trace; genuine-but-error-prone 8B decisions | PASS | medium | weak-model reliability |
| Action selection | differing dynamic sequences | PASS | low | — |
| Tool execution | 12-tool registry, budgeted, serialized writes | PASS | low | — |
| Feedback loop | failure→alternative chains in traces/tests | PASS | low | — |
| Adaptation | S1/S3 recovery; excluded-packs memory | PASS | low | chat-path flakiness |
| Alternative fulfillment | §7 all 9 answers positive | PASS | low | — |
| Structured proposal | dto + `_tool_request_confirmation` | PASS | low | — |
| Buyer confirmation | pause/resume + reject-exclusion tested | PASS | low | — |
| Deterministic gating | 5-rule engine; live rejections | PASS | low | — |
| Price authority | server-side totals; PRICE_CHANGED flow | PASS | low | — |
| Inventory authority | reservations + strict revalidation | PASS | low | sweep-on-read only |
| Delegated authority | binding/expiry/scopes enforced | PASS | low | no revoke endpoint |
| Payment boundary | param-less gated tool; mode guard live-proven | PASS | low | — |
| Idempotency | key replay + open-order reuse (tests/live) | PASS | low | — |
| Audit | 77-event intact chain; tamper detection | PASS | low | — |
| Explainability | facts-only templates, live-verified | PASS | low | — |
| Failure handling | §14 checklist runtime-passed | PASS | low | — |
| Agent independence | grep + live A2A | PASS | low | — |
| Protocol compliance | routes behave per spec | PARTIAL | medium | callback/reconcile/admin undocumented |
| Dashboard | real endpoints wired; Ops+Trace views | PARTIAL | low | visuals not exercised here; admin token in bundle |
| Testing | 53/53; six categories | PASS | low | buyer_service untested |

# §25 P0/P1/P2

**P0 — none found.** No ungated money path, no LLM-capable financial override, no state-machine hole.

**P1**
1. Weak-model reliability on the buyer_completes chat path: 3/3 live A2A runs ended in safe-but-wrong declines (`session_123` hallucination; `quantity:40` availability confusion; blocked self-completion then mis-termination instead of `finish(ready_for_completion)`). Recommend: default to `llama-3.3-70b`, or add a deterministic post-create nudge ("checkout is live → finish ready_for_completion"), or auto-append the tool-result instruction currently only sent on pause/resume.
2. Undocumented privileged endpoints (`payment_callback`, `reconcile`, `/admin/*`) — add to `protocol_spec.md` even as "non-buyer surface".
3. `buyer_b/client.py:109` crashes on `"error": null` (CompleteResponse serializes None) — interop demo can die mid-report.

**P2**
Dashboard bundle ships `admin_demo_token`; SESSIONS dict unbounded + volatile (no persistence/eviction); no delegation-revoke endpoint despite `status` field; ops `razorpay_api_reachable=False` while order creation succeeds (probe uses `order.all` — misleading); `mutation_lock` single-process assumption; price-change path overwrites `gating_result` with empty-pass (cosmetic); `explain` returns bare "no activity" for wiped ids without saying why.

# §26 Original track audit (15 questions)

1. AI buyer transact end-to-end? **YES** (with explicit user consent gate; headless after mandate registration)
2. Machine-readable merchant? **YES** (JSON schemas, discovery→completion)
3. Machine-transactable? **YES** (Buyer B completed discovery+checkout+order creation with zero LLM)
4. Merchant Agent genuinely participating? **YES** (audit-backed decision traces)
5. Buyer Agent genuinely autonomous? **YES** within deliberate bounds
6. Both independent? **YES**
7. Money explainable? **YES** (facts-only explanations, live-checked)
8. Money bounded? **YES** (delegation caps + budgets)
9. Money gated? **YES** (5-rule engine + pre-money ladder)
10. Audit visible? **YES** (Ops view + verify endpoint)
11. Failure graceful? **YES** (§14)
12. Razorpay actually used? **YES** (real test-mode orders `order_TTKGCOewiLchWU` et al.)
13. Human intervention required? User consent yes; merchant/operator no; first-card-auth once
14. Another buyer implementation usable? **YES** (Buyer B; minus its P1 crash bug)
15. Merchant authority preserved? **YES**

---

# §27 Scorecard

| Dimension | Score | Weight | Basis |
|---|---|---|---|
| PRD completeness | 88 | 25% | 23 PASS / 2 PARTIAL of matrix rows |
| Merchant agency | 82 | 20% | genuine loop proven; reliability caveat |
| Security / financial safety | 94 | 25% | zero critical findings; all bypasses rejected; isolated test hooks |
| Interoperability | 90 | 15% | Buyer B works; doc gaps + client bug |
| Track alignment | 91 | 15% | all 15 questions substantively yes |

**Overall = 0.25·88 + 0.20·82 + 0.25·94 + 0.15·90 + 0.15·91 = 89/100**
(Not a blind average: financial safety and PRD completeness weighted heaviest per Rules 6–7.)

---

# Final Report

# Executive Verdict

**NEARLY READY** — see top of document.

# 1. What Is Fully Implemented

Deterministic gating (5 rules, live-proven); checkout state machine incl. terminal protection; delegated authority binding/expiry/scopes; server-authoritative pricing with version-matched confirmation; reservation lifecycle with TTL + strict pre-payment revalidation; idempotent completion + open-order reuse; HMAC-verified payment callbacks; append-only hash-chained audit with tamper detection; facts-only explainability; Merchant Agent loop (observe→reason→act→adapt) with 12 validated tools, budgets, and a parameter-less financially-privileged tool; structured substitution proposals with mandatory buyer confirmation; bounded autonomy under adversarial instructions; graceful stock-vanish failure with recovery-through-alternative; protocol-only Buyer A and Buyer B; 53-test green suite executing production paths.

# 2. What Is Partially Implemented

Protocol documentation (callback/reconcile/admin endpoints missing from spec table); Merchant Agent decision quality on the free-text chat path with the 8B model (safe but frequently wrong terminations); delegation revocation (field exists, no endpoint); dashboard verification (code-level only in this audit); buyer-service test coverage.

# 3. What Is Missing

Nothing from the PRD's core behavioral requirements. Missing conveniences: delegation revoke API, agent-session persistence across restarts, reservation reaper beyond sweep-on-read.

# 4. Critical Financial Issues

None. Simulated capture is unreachable outside pytest (env flag + config validator); every money path traverses ownership→state→price→delegation→stock→policy before Razorpay; fake callbacks rejected; duplicate orders prevented; replays return identical results. Intentional test hooks are isolated and labelled.

# 5. Merchant Agent Assessment

Genuinely agentic: live LLM-brain run demonstrated observation-driven plan revision (out-of-stock → find_alternative → structured proposal), tool results visibly drove next actions, and the deterministic layer repeatedly overruled or contained the model (gate rejection; refused self-completion; hallucinated-id rejection). Remains deterministic: gating, pricing, state transitions, payment, budgets. The "Merchant Agent" label is justified — this is a bounded agent, not a named RPC chain. Caveat: with `llama-3.1-8b` it is a *fallible* agent; every observed fall was fail-closed.

# 6. Buyer Agent Assessment

Also genuinely agentic within design bounds: NIM tool-loop, discovery-driven basket construction, intent-judged accept/reject of merchant proposals, deterministic bill/approval gate and stock-failure recovery owned by the service (correctly outside LLM hands). Same model-quality caveat applies to fluency, none to safety.

# 7. Agent-to-Agent Assessment

Yes — genuinely two independent agents over a published protocol: Buyer A negotiates with the Merchant Agent (`purchase_via_merchant_agent` → proposal → decision → gated checkout), Buyer B transacts with raw HTTP, and neither shares code or secrets with the merchant. The negotiation is observable end-to-end in the hash-chained audit trail.

# 8. PRD Score

**88/100**

# 9. Track Score

**91/100**

# 10. Critical Fixes

P0: none.
P1: (a) fix weak-model chat-path terminations — switch default model to `llama-3.3-70b` or add deterministic ready_for_completion hand-off after successful basket creation; (b) document `payment_callback`/`reconcile`/admin in the protocol spec; (c) null-safe error handling in `buyer_b/client.py`.
P2: remove admin token from dashboard bundle (read from operator input), persist/evict agent sessions, delegation revoke endpoint, fix `razorpay_api_reachable` probe, single-worker lock documentation.

# 11. Judge Risks

A technically sophisticated judge will likely probe: (1) "your agent declined three times in chat" — answer with the fail-closed argument plus demo script reproducibility; (2) "is the Merchant Agent just if/else?" — point at the live traces where the LLM chose divergent action sequences, including the attempt to self-complete that the tool layer refused; (3) "is payment really Razorpay?" — show `order_TTKG…` IDs and the awaiting_authorization park; (4) "second client?" — it works but know about its crash bug; (5) "why does the human approve the bill?" — frame as user-consent mandate alignment, not missing autonomy.

# 12. Final Product Description

An ACP-inspired commerce gateway in which an LLM Buyer Agent and an LLM Merchant Agent negotiate groceries over a published JSON protocol while a deterministic control plane exclusively owns money: five-rule gating, reservation accounting, a strict pre-payment revalidation ladder, idempotent Razorpay test-mode execution, and a tamper-evident audit chain that reconstructs every agent decision. Verified live: out-of-stock requests produce structured alternatives requiring buyer confirmation, over-limit demands are declined before any gateway call, and vanishing stock halts payment with recovery-through-substitution. Human involvement is limited to explicit user consent and a one-time card authorization; no merchant operator stands between agents and settlement.
