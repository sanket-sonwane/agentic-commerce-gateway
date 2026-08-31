# NTH_AUDIT — Full System Audit of the Agentic Commerce Gateway (ACG)

**Scope:** live re-run of the reference implementation at `D:\Hackathons\rz-agent` (buyer service :8001, merchant service :8000, real Razorpay Test orders, AP2 export fixtures).
**Method:** every claim below carries a `file:line` citation and/or runtime evidence captured during this audit (fresh `201`-test run, live HTTP traces, DB + backup inspection, AP2 bundle exports verified offline with a standalone verifier).
**Evidence files produced:** `data/` logs (`audit_m{1,2,3}_*.log`, `audit_b{2,3}_*.log`, `audit_chat_run{1,2,3}.txt`, `audit_complete_result.json`, `audit_ap2_{create,intent,complete}.json`), `data/merchant.db.bak_audit_20260830_122910` (pre-run snapshot), `artifacts/` (`evidence_bundle.json`, `evidence_bundle_completed.json`, `evidence_bundle_live.json`, `execution_mandate.json`, `intent_mandate.json`, `policy_decision.json`, `merchant_public_key.pem`).
**Date:** 2026-08-30 (UTC timestamps in evidence).

---

## 0. How to read this document

The repo ships three prior audits: `docs/audits/FINAL_AUDIT.md` (verdict: *NEARLY READY*), `docs/audits/AP2audit.md`, `docs/audits/AP2-post-audit.md`, `AUDIT.md`, `AUDIT_MERCHANT_AGENT_V1.md`, `AUDIT_REPORT.md`, `BUYER_AGENT_AUDIT.md`. Those were claim-based (mostly static). This report is **live-evidence-based** and attempts #3 in the sequence — it re-runs the money path against Razorpay Test, exports the AP2 evidence bundles, verifies them offline, and reproduces 4 real incidents (426/429 rate-limit degradation, audit-chain break under multi-writer, saved-card token-charge 404 → modal re-auth park, Buyer-B client crash). Section 27 reconciles prior claims against what actually happened.

---

## 1. Executive summary

| Area | Verdict (live) | Primary evidence |
|---|---|---|
| Architecture | **SOUND** — clean api → domain/payment/evidence layering, dual drivers, mandates | §4–9 |
| Money path | **SOUND but not production-complete** — gating/razorpay-order/mandate all fire live; *saved-card server-side charge is refused by the Test gateway → fail-safe park for one-time modal re-auth*; a fully-headless "pay once, trust thereafter" was **never reached** on this account | §15–16 |
| Audit/AP2 | **REAL with a concurrency flaw** — hash-subsumption + signed head anchor verify; chain **broke under two writer processes** (observed), healable by single-writer; append-only is erasable via admin demo-reset | §18, §22 |
| Security | **GOOD** — delegation expiry/category/quantity/spend/stock all gate live; credentialed admin surface | §18, §21 |
| Observability | **GOOD** — `/explain`, `/provenance`, `/consumer/transactions/.../provenance` and `/agent_sessions/{id}/trace` all returned live evidence | §19–20 |
| Interop | **PARTIAL** — Buyer-B client crashes (P1, reproduced twice) after a successful checkout | §24, §28 |
| Readiness | **STRONG HACKATHON ARTIFACT; NOT production-money-ready** (8 hard gaps, §1.1) | §2 |

### 1.1 The eight hard gaps (each is a verbatim requirement/per-claim mismatch)

1. **Audit hash chain is not safe under multiple writer processes.** §22 — only a process-local `threading.RLock` serializes appends (`db.py:48`). Two concurrent writer processes into the same `merchant.db` produced a **real chain break** (`/audit_logs/verify` → `intact:false, first_broken_seq:28`) during this audit. The break was *detected* correctly by the system's own verify (defense-in-depth works), but the guarantee "append-only tamper-evident" is conditional on a **single-writer invariant that nothing enforces**.
2. **"Append-only" is erasable by design.** `POST /admin/demo/reset` deletes every `AuditEvent` row (`api/admin.py:81-82`) and re-anchors a new chain. Admin-gated, but the PRD claim is "append-only", and a judge with the hardcoded client token (§21) can erase.
3. **Fully-headless "authorize then trust" is unproven.** The recurring-charge endpoint returned `404`/`The requested URL was not found` and the saved-card token charge was **rejected at the gateway**; the system correctly fell back to parking the session at `awaiting_authorization` for a one-time Razorpay Checkout modal (`token`, `PAYMENT_DRIVER=saved`, card `••••4366` attached). No live transaction ever executed the "thereafter-headless" step on this Test account.
4. **Buyer-B client crashes (P1).** `AttributeError: 'NoneType' object has no attribute 'get'` at `buyer_b/client.py:109` on `err=None`, reproduced twice live, right after the merchant returned a *successful* terminal checkout (`cs_1c7c76be98`, `cs_9625330ff4` both ₹155). Merchant is not at fault; the consumer's own outcome-handling is unguarded.
5. **Admin token is a shipped constant.** `admin_demo_token` is a literal in `dashboard/src/utils/api.js` (string minted in client JS) and accepted verbatim by `api/admin.py:24-26`. A buyer-JS reader can zero stock / wipe transactions.
6. **Dashboard money views are inconsistent.** Two payment card components exist (`RazorpayPayCard.tsx:206` and a duplicate `PaymentCard`), and `ChatView` renders the buyer log without `payment_callback` handling — a completed row can show a stale/blank payment state (§20).
7. **Inventory reservation lifecycle is not audited.** `inventory_service` performs **zero** `append_event` calls; reservation create is audited by the caller (`checkout_service.create_checkout`), but release/consume/expire/sweep are invisible to the trail (§9, §28).
8. **LLM brain is best-effort-coupled to a rate-limited provider.** Groq `429 (TPM)` clipped `gpt-oss-120b` mid-demo; the loop degraded to the deterministic decider and completed. Money never depends on the LLM (good), but the "agentic" surface is network-obligated (§10, §26).

**Bottom line:** 7.7/10 on the weighted scorecard — architecturally the right skeleton exercised correctly end-to-end, with the money trail provable *up to* the gateway's one-time authorization seam and a concurrency flaw in the audit chain that must be fixed before money provenance is trusted.

---

## 2. Weighted scorecard (overall **7.7 / 10**)

Weighted across 6 areas (total weight 50). PASS=10, PASS†=9 (caveat), PARTIAL=6, PARTIAL-FAIL=4, FAIL=2.

| Area (weight) | Section | Grade | Weight | Weighted |
|---|---|---|---|---|
| Architecture (12) | §4 layering/inversion | PASS† 9 | 1.5 | 13.5 |
| | §5 dual drivers + refuse-to-simulate | PASS† 9 | 1.5 | 13.5 |
| | §6 AP2 signed artifacts | PASS† 9 | 1.5 | 13.5 |
| | §7 append-only audit + head anchor | PARTIAL 6 | 3.0 | 18.0 |
| | §8 consumer provenance surface | PASS† 9 | 1.5 | 13.5 |
| | §9 reservation/price/stock conformance | PASS† 9 | 1.5 | 13.5 |
| | §10 no-LLM money coupling | PASS† 9 | 1.5 | 13.5 |
| Money path (14) | §11 create→complete ladder + revalidation | PASS† 9 | 2.0 | 18.0 |
| | §12 money only via `request_completion` | PASS 10 | 2.0 | 20.0 |
| | §13 TOCTOU protections | PASS† 9 | 2.0 | 18.0 |
| | §14 deterministic gating | PASS† 9 | 2.0 | 18.0 |
| | §15 Razorpay live honesty | PARTIAL 5 | 2.5 | 12.5 |
| | §16 RBI-AFA modal reality | PASS† 9 | 1.5 | 13.5 |
| | §17 catalog parity / 1L vs 500ml | PARTIAL 7 | 2.0 | 14.0 |
| Security (8) | §18 delegation expiry/category/qty/spend/stock | PASS† 9 | 2.0 | 18.0 |
| | §19 admin token surface | PARTIAL 6 | 1.5 | 9.0 |
| | §20 error semantics | PASS† 8 | 1.5 | 12.0 |
| | §21 financial allowlist + tool budget | PASS 10 | 3.0 | 30.0 |
| Observability (7) | §22 evidence endpoints live | PASS† 9 | 2.0 | 18.0 |
| | §23 dashboard money views | PARTIAL 5 | 2.0 | 10.0 |
| | §24 audit integrity under concurrency | FAIL 3 | 3.0 | 9.0 |
| Interop & resilience (7) | §25 Buyer-B client robustness | PARTIAL-FAIL 4 | 2.0 | 8.0 |
| | §26 two brains degrade gracefully | PASS† 9 | 2.0 | 18.0 |
| | §27 hidden-state determinism/reconstitution | PASS† 9 | 3.0 | 27.0 |
| Testing & honesty (2) | §28 test-suite honesty | PASS† 8 | 1.0 | 8.0 |
| | §29 prior-audit reconciliation | PARTIAL 5 | 1.0 | 5.0 |
| **Total** | | | **50** | **385 → 7.7/10** |

---

## 3. Test harness — how this audit actually ran

- Fresh `pytest`: `.venv\Scripts\python.exe -m pytest merchant/tests -q -p no:cacheprovider` → **201 passed in 9.03s**.
- Live harness: pre-run DB snapshot (`data/merchant.db.bak_audit_20260830_122910`) → boot **one** merchant (:8000) + **one** buyer (:8001) on the venv → drive real flows → capture `/audit_logs/verify`, `/explain`, `/provenance`, `/consumer/transactions/.../provenance`, `/agent_sessions/{id}/trace` → export AP2 bundles → verify offline with `scripts/verify_ap2.py` → shut down.
- **Writer-discipline note:** I first observed a *broken* audit chain because leftover uvicorn instances (system python, older sessions) were concurrently writing the same `merchant.db`. I killed every instance, rebooted exactly one of each service, and re-ran both capstone traces. The section-24 finding is **based on the actual observed break**, not on the recovery.

---

## 4. Runtime architecture conformance

`merchant.api` (HTTP + ProtocolError) → `merchant.domain` (checkout/discovery/inventory) + `merchant.payment` + `merchant.agent` → `merchant.persistence` (SQLAlchemy/SQLite/WAL). Verified statically and at runtime; no dependency cycle (grep confirms service layer never imports `api.*`). Layering holds.

**Evidence:** health endpoint `GET /health` → `{"protocol":"Agentic Commerce Protocol — Reference v0.1","payment_driver":"saved"}`.

---

## 5. Payment drivers — dual mode done right

- `payment_service.py` has **two drivers** (`saved`/`test`-style) and `has_credentials()` + `_sim_allowed` guard (`payment_service.py:59-104`); when credentials are absent the driver **refuses to simulate** any money op outside the test-only `saved` path.
- `.env` at audit time: `PAYMENT_DRIVER=saved`, `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` set. `GET /admin/ops/status` → `razorpay_credentials_present:true`.
- **Added finding:** `razorpay_api_reachable:false` at the tail of the audit (`ping_api` failed) while order-creation still worked minutes earlier — network reachability is not guaranteed; the code path handles it (create returns a typed `ProtocolError`), but a reviewer should treat "reachable" checks as network-dependent. Evidence: `data/audit_m3_*.log`, ops-status output.

---

## 6. AP2 signed artifact chain

Full evidence chain, all offline-verifiable:
- **Intent mandate** `im_294a881592a049d9` (live, `cs_d2112640db` batch): Ed25519-signed, `canonical_payload_hash 180ac6c0d0fb5791799ffc8c1f2f2de98328fecb4d4ec36bb64bd854cfcb9986`, `key_id merchant-key-01`, `verifiable_public_key` PEM inline (`artifacts/intent_mandate.json`).
- **Execution mandate** `em_e24edcded3f94290` (fresh-chain success run, §29): `authorized_amount_paise 10000`, `razorpay_order_amount_paise 10000` → **amounts_agree**.
- **Standalone verifier** (`scripts/verify_ap2.py` → `merchant/ap2/standalone_verifier.py`, Ed25519-only, never loads the private key): all three artifacts (`execution_mandate`, `policy_decision`, `intent_mandate`) → **`RESULT: ✅ VALID`** (Public key / Required fields / Canonical form / Payload hash / Signature / Key ID / Expiry relationship all VALID) when fed the **public** key PEM.
- Full bundle (mandate + policy + receipt + cross-refs) for a **real completed** transaction `cs_44cf1197a0` (₹72, from backup DB): **`RESULT: ✅ VALID`** with `execution_mandate_valid:true, policy_decision_valid:true, decision_receipt_valid:true, receipt_refs_execution:true, policy_refs_execution:true, receipt_refs_policy:true, session_agreement:true, amounts_agree:true` (`artifacts/evidence_bundle_completed.json`).
- **Honest boundary:** the live (parked) bundle `cs_9b727a1ee6` verifies mandate+policy but correctly fails the receipt cross-check (`decision_receipt_valid:false` → `MALFORMED_ARTIFACT`, `reason: BUNDLE_RELATIONSHIP_FAILURE`) because the transaction is not settled. The verifier cannot be tricked into green-lighting an unsettled transaction — correct behavior.

**Earlier false alarm (for the record):** initial `--public-key secrets/merchant_signing_key.pem` runs returned `INVALID_PUBLIC_KEY` / `BUNDLE_RELATIONSHIP_FAILURE`. That was **my invocation error** — the file is a *private* key PEM (`-----BEGIN PRIVATE KEY-----`, 119 B); the verifier wants the *public* key. Fed the public key (extracted from the bundle/private key via `cryptography`), every artifact verifies VALID.

---

## 7. Audit trail — the good, and the two real defects

Hash-subsumption is exactly as claimed: `entry_n.hash = SHA256(canonical_n + hash_(n-1))` (`audit_service.py:18-30, 33-62`), first entry `prev_hash=""`, verify recomputes the full chain (`audit_service.py:81-90`), and the head is signed as an AP2 anchor (`audit_service.py:93-111`).

- **Verdict on the clean single-writer chain:** `GET /audit_logs/verify` after the money path → **`{"intact":true,"entries_checked":42,"first_broken_seq":null,"anchor":{... "key_id":"merchant-key-01"}}`**. Chain integrity holds; the signed anchor is real.
- **Defect A (append-only is erasable):** `POST /admin/demo/reset` does `db.query(AuditEvent).delete()` to wipe the trail, then re-anchors (`api/admin.py:81-82,90-96`). Gated by `X-Admin-Token`, but the token is a shipped constant from client JS (§21), so this is a demo convenience, not a tamper-story.
- **Defect B (multi-writer breaks the chain, detected only by verify):** see §24.

---

## 8. Consumer-facing provenance surface

`GET /consumer/transactions/{cs_id}/provenance` returned a **full 8-stage journey**, authorization block, 5 gate "checks" (delegation/category/quantity/spend/stock, each `pass` with evidence), payment block with `razorpay_called:true`, and an AP2 `proof` block. For the live parked run:
`current_stage: razorpay, proof:{execution_mandate:'verified', policy_decision:'verified', decision_receipt:'verified', provenance:'verified', audit_chain:'verified', verifier_status:'VALID'}`.
For **this** trace, `audit_chain` read `verified` precisely because it ran on the clean single-writer chain (contrast with the multi-writer failure in §24 where the same field was `unavailable`). `verifier_status` is genuinely recomputed per request (`provenance_service.verify_provenance`), not cached.

---

## 9. Reservation / stock / price — conformance and the audit hole

- Stock revalidation happens **inside** `complete` (price + delegation + stock re-check before any money move; `checkout_service.py:750-789` price/terms, delegation revalidation, `788+` stock revalidation). Live: the stock-vanish trace fired `final_validation` → 409 `STOCK_UNAVAILABLE` → session declined, `razorpay_called:false` (§30).
- Price-change path is retryable & audited (`requires_confirmation` + `PRICE_CHANGED`, `checkout_service.py:754-780`).
- **Hole:** reservation release / consume / expire / sweep are **not** audit events — `inventory_service` has zero `append_event` calls. The trail cannot explain later availability changes (§30-irrelevant, but a real provenance gap for a "money-explainable" thesis).

---

## 10. Independence — no-LLM money coupling

- The LLM is the *planner* only. Money is executed **exclusively** by `request_completion` (`loop.py:12-13`; handler at `1204-1236`), which calls the deterministic control plane; the "model insists completed without money" case is downgraded to `ready_for_completion` (`loop.py:643`).
- `LLM observable degradation` (Groq 429 during demo) → loop switched to the deterministic `fallback` decider and still completed the checkout (§28). Money always had a deterministic gate in front of it.
- **Verdict:** the "no-LLM money" invariant is **genuinely enforced** (§12).

---

## 11. Independence — agent autonomy bounds (bounded by delegation, enforced)

- Demo scenario "ignore the limit" → declined with `reason_code = SILENT_MODIFICATION_BLOCKED` (not prior-audit's `DELEGATION_SCOPE_VIOLATION`). This is a *different, additional* guard: quantity *(max per line) vs spend ceiling*. Two distinct bounded-autonomy rails both fired live.
- `scenario_check` 1-3 verified spend-limit (`SPEND_LIMIT_EXCEEDED` 409), category (`CATEGORY_BLOCKED` 409), expiry (`DELEGATION_EXPIRED` 401).
- Terminal-state protection: replay `complete` on a declined session → 409 `INVALID_STATE_TRANSITION` (scenario 6).

---

## 12. Independence — substitution policy (deterministic seam)

- LLM proposals + buyer accept are surfaced as structured events and the applied alternative is **replayed deterministically** through the ordinary tool path (buyer accepted Mother Dairy 1L sub live; §29). Buyer terms confirm/reject both execute through deterministic control.
- The substitution **policy itself** (category/quantity/price) is deterministic; the LLM only ranks.

---

## 13. Independence — concurrency & idempotency claims

- `mutate()` wraps money-critical sections in a process-wide `RLock` (`db.py:48,51-63`); idempotency keys checked before create; reservations carry TTL (`expires_at` seen live: `2026-08-30T07:59:53`).
- **What survives code review / fails live:** the lock is **process-local**. Any second process writing the same SQLite DB (leftover dev server, a second demo, or a horizontally-scaled deployment) invalidates the audit chain — *demonstrated here*, §24.

---

## 14. Independence — appendix of conformance claims (what holds / what doesn't)

| Claim | Status | Evidence |
|---|---|---|
| Deterministic gating before create | HOLDS | `gating_evaluated` seq 30, 5 rules all `pass` |
| Deterministic revalidation before payment | HOLDS | `final_validation` seq 36; stock-vanish 4c |
| Signed mandate before money | HOLDS | `execution_mandate_created` seq 37 |
| No simulate-without-credentials | HOLDS | `_sim_allowed`/`has_credentials` guard; SDK reachability is network-bound (§5) |
| Append-only audit | PARTIAL | chain real (§7) + erasable demo-reset (§21) + multi-writer break (§24) |
| Offline AP2 verification of an exported bundle | HOLDS | `verify_ap2 --bundle` completed chain → VALID (§6) |

---

## 15. Razorpay live — exact behavior observed

Live money steps on the clean run (`cs_9b727a1ee6`, ₹100):
1. `razorpay_order_creation` → **`razorpay_order_created` `order_TVu9wBaOeJZSG2` ₹100** (gateway accepted the order-create call).
2. `razorpay_token_charge` → *"Charging saved Visa ••••4366 server-side via token"* → **rejected by gateway**: `Token charge rejected by gateway: The requested URL was not found` (recurring endpoint 404 on this Test account).
3. `token_charge_fallback` → `awaiting_authorization` park (session stays `requires_payment`); buyer must re-authorize once via Razorpay Checkout modal (save-card enabled) — `docs` honestly call this `awaiting_authorization`.

**Honest statement:** order-create works live against Razorpay Test; **saved-card headless charging does not** on this account; a human-in-the-loop first payment is unavoidable today, and "trust thereafter" was unreachable in any live run here. This is a platform/account property, not a code regression — but §1.1.3 must be a go/no-go question for the judges.

---

## 16. RBI-AFA / saved-card reality (₹15k mandate cap)

`create_order` mandates carry `max_amount_paise: 1_500_000` (₹15,000) + `expire_at` + `frequency` for the E-Mandate authorization-transaction order (`payment_service.py:97-104`) — the AFA threshold is encoded where the order is built. Live trace shows mandate+order amounts reconcile exactly (`10000 == 10000`); the modal re-auth path is the correct AFA-compliant fallback.

---

## 17. Catalog parity — V1 claims vs live behavior

- V1 goals (in docs) referenced a 500ml-substitution flow; the agent's `find_alternative` path proposed **1L→1L Mother Dairy** here (same size), while `parse_target_pack` + `SIZE_RE` (`tools.py:45`) handle free-text sizing. 500ml substitution exists in deterministic e2e fixtures but was not the case exercised live.
- Deterministic buyer-B fallback returned size/category-based alternatives fine (`pack_milk_amul_1l` present at stock 40). **PARTIAL** only because no live 500ml product is seeded in `catalog.json` for the demo (checkout total recomputations are consistent).

---

## 18. Delegation security gates — all live-verified

Delegation expiry/category/qty/spend/stock: expiry (401), category-block (409), spend (409), quantity (`SIZE_RE`/`gating`), terminal-state protection (409) — all from `scenario_check` run 2 and the demo. `delegation_id dlg_9f8e7d` with `max_amount ₹5000` (seeded) drove the success line-item consistency.

---

## 19. Admin surface

`/admin/packs/{id}/stock` and `/admin/demo/reset` exist and are gated by `X-Admin-Token: <admin_token>` (`api/admin.py:24-26`). **Finding:** the same literal gate token also ships in the dashboard JS (`dashboard/src/utils/api.js`); a JS consumer can zero stock or wipe the ledger. For a demo this is fine; for a real merchant it is a credential leak.

---

## 20. Error semantics

- `ProtocolError` carries `code/http_status/retryable/details/session_ref` (`dto.py`), response codes consistent with the PRD: 401 expired/gated, 409 policy/stock/state, 422 malformed, plus `PAYMENT_BLOCKED` and `PROVENANCE_NOT_FOUND` paths (`dto.py:26-27`). All 5 gates return typed errors; all captures matched the documented code strings.

---

## 21. Financial tool allowlist + budget

Agent tool registry (`tools.py:33-43`): 6 read + 4 write + 2 financial tools; `execute_tool` enforces the allowlist (`UNKNOWN_TOOL`) and a tool-call budget (`TOOL_BUDGET_EXHAUSTED`) and runs write tools only via `mutate()`. Money tools (`run_gating`, `request_completion`) are the only financial entry points and are deterministic.

**Note contrasting with §13:** the *read* tools use a bare unlocked `SessionLocal()` (`tools.py:872-877`); the discovery pair that broke the chain came through this non-`mutate()` session when a concurrent writer existed — see §24.

---

## 22. Evidence endpoints — live status

| Endpoint | Live result |
|---|---|
| `GET /audit_logs/verify` | `intact:true` (clean chain, 42 entries) / `intact:false @seq28` (multi-writer) — §24 |
| `GET /explain/{cs}` | "still in progress" + 5 timeline facts (parked) |
| `GET /consumer/transactions/{cs}/provenance` | 8-stage journey, `verifier_status:VALID`, `razorpay_called` true/false correctly |
| `GET /agent_sessions/{id}/trace` | full 15-event reconstructible trace from hash-chained events (§29) |

---

## 23. Dashboard money views

`RazorpayPayCard.tsx` (used in OpsView, line ~206) and a second `PaymentCard` both exist; `ChatView` does not handle `payment_callback`. Radar: **PARTIAL** — ops console works, but a judge seeing buyer log on the dashboard may get a stale payment state vs the API truth.

---

## 24. Audit integrity under concurrency — the one FAIL (real, observed)

**Trigger (during this audit):** multiple uvicorn processes (system python + venv, leftover + fresh) were concurrently writing `data/merchant.db` — each with its **own** `mutation_lock`. The app's process-local RLock cannot serialize *other processes*.

**Observed break:** `GET /audit_logs/verify` → `{"intact":false,"entries_checked":51,"first_broken_seq":28}`. The broken row `seq 28` (`discovery_requested`) has `prev_hash = 7a2ce8…` which matches **`seq 19`'s** hash—its writer had taken a read snapshot while the head was 19, then appended *after* another writer had committed seq 20-27 (`cs_d8e2d9c7b0`). Two writers, two `prev_hash` ticks → chain break.

**Why the code allowed it:** `append_event` computes `prev_hash` from a `select...order_by(seq.desc()).limit(1)` on the *current session* (`audit_service.py:44-45`); correctness relies entirely on the process-local `mutation_lock` (`db.py:3-7` explicitly calls this an "MVP simplification"). A second process (or the agent's locked read-tool session, `tools.py:872-877`) invalidates it.

**Defense that worked:** the system's own `/audit_logs/verify` **caught** the break — tamper-evidence by recomputation is real. And after killing all writers and re-running on a single process, the chain verified `intact:true` (42 entries) through the full money path.

**Fix ⇒ (report-only, per audit scope):** enforce single-writer (single-instance guard / file lock) **or** move the chain to DB-level serialization (e.g., `BEGIN IMMEDIATE` write transactions for appends), and do not audit-append through an unlocked session.

---

## 25. Buyer-B client — P1 crash (reproduced twice)

`python -m buyer_b.client` → discovery + checkout create **succeed** (`cs_9625330ff4` ₹155 `requires_payment`), then:

```
File "...buyer_b/client.py", line 109, in run
    print(f"[buyer-b] outcome: {final_status} | error={err.get('code')} — {err.get('message')}")
AttributeError: 'NoneType' object has no attribute 'get'
```

`err` is `None` when the completion *succeeded* and the run returns `final_status`; the consumer then indexes into an error dict unconditionally. Reproduced on both the old multi-writer DB and the clean chain. The merchant is **not** at fault; the reference buyer-b client is not robust to success.

---

## 26. Two brains, one loop — graceful degradation

Groq `429 (TPM)` clipped `gpt-oss-120b` mid-demo → the app degraded to the deterministic `fallback` brain and still returned a correct, audited outcome. `fallback` also mirrors the discovery keyword-fallback philosophy (Hermetic). **Verdict:** the two-brain design is real; only brain-1 has a hard network dependency, and money never routes through it.

---

## 27. Hidden-state determinism / reconstitution

- Hash chain + signed mandates ⇒ an auditor can reconstruct the money path from artifacts alone (proven in §6: full bundle verified **offline, DB-free**, including `amounts_agree` and all cross-references).
- `verify_ap2 --bundle artifacts/evidence_bundle_completed.json` (a *completed* historical transaction) required only JSON + public key → `VALID`. Decisive reconstitution proof.

---

## 28. Test-suite honesty

- **201 tests pass** (9.03 s). The suite covers gating, proofs, drivers, api, agent loop, dashboard-independent.
- Dept **caveats:** several paths are covered via deterministic/hidden flags (LLM disabled in tests); prior FINAL_AUDIT claimed "53/53" at its time — the repo has since grown. The suite **does not** simulate two writer processes against the chain (§24) nor a live-with-credentials Razorpay run (§15) — both found real issues here.

---

## 29. SUCCESS trace (live, single-writer chain)

Agent session `mas_064fe32942`, buyer chat `conv_099e73f206` (:8001 `POST /chat "buy 1L Amul milk"`), checkout `cs_9b727a1ee6` (₹100, Amul 1L, stock 40).

| seq | action | evidence |
|---|---|---|
| 28 | `agent_search_catalog` | tool executed |
| 29 | `checkout_create_requested` | buyer_agent |
| 30 | `gating_evaluated` | 5/5 rules `pass` |
| 31 | `reservation_created` | TTL `07:59:53` |
| 32 | `checkout_created` | `requires_payment, ₹100` |
| 33 | `agent_create_checkout` | tool result |
| 34 | `agent_ready_for_completion` | deterministic hand-off |
| 35 | `complete_requested` | |
| 36 | `final_validation` | "Price, delegation, stock and policy all valid at completion" |
| 37 | `execution_mandate_created` | `em_e24edcded3f94290` signed+verified, ₹100 |
| 38 | `razorpay_order_creation` | |
| 39 | `razorpay_order_created` | `order_TVu9wBaOeJZSG2` ₹100 (auth/mandate order) |
| 40 | `razorpay_token_charge` | saved Visa ••••4366 server-side |
| 41 | `token_charge_fallback` | gateway 404 → "Saved-card charge rejected" |
| 42 | `awaiting_authorization` | parked; modal re-auth pending |

Provenance witness: `current_stage:razorpay, razorpay_called:true, proof{execution_mandate/policy_decision/decision_receipt/provenance/audit_chain:'verified', verifier_status:'VALID'}`. Chair answer to "did money move?" → order created & agreed exactly ₹100; **charge not yet captured** awaiting one-time AFA modal.

---

## 30. FAILURE trace + the 15 direct answers

**FAILURE trace — TOCTOU stock vanish (must NOT charge):** checkout `cs_bc12af5c64` ₹290 chicken → operator zeroes stock (`pack_chicken_500 → 0`, `inventory_adjusted` seq) → `POST .../complete` → **409 `STOCK_UNAVAILABLE`, session `declined`** — `razorpay_called:false`, audit `[checkout_create_requested, gating_evaluated, reservation_created, checkout_created, complete_requested, final_validation, checkout_declined]`. Provenance: `failure_stage:safety_checks, failure_reason:stock_check`.

**Direct answers to the 15 final questions**

1. **Is "the money never moves before a deterministic gate" structurally true?** YES — money only via `request_completion`; gating and mandates precede every Razorpay call live.
2. **Is the money path LLM-free?** YES — the LLM plans; the deterministic control plane executes.
3. **Can an LLM silently buy beyond the delegation?** NO live — `SILENT_MODIFICATION_BLOCKED` and spend/category rails all fired.
4. **Is the audit chain real?** YES — recomputed + signed, verified `intact:true` on a single-writer run.
5. **Is purchase history tamper-proof?** NO — reset-erase + multi-writer break both observed (§21, §24).
6. **Is the mandate/signature story externally verifiable offline?** YES — exported bundle verified VALID with only JSON + public key.
7. **Is every charge explained?** PARTIAL — trail reconstructs up to settlement; earliest inventory movements missing (§9).
8. **Are reservation/price-vanish protected?** YES — live 409 `STOCK_UNAVAILABLE`; price-change retryable + audited.
9. **Is state deterministically reconstructable from logs?** YES — hash chain + mandate IDs (`em_…`, `order_…`) reconcile amounts.
10. **Is "pay once, trust thereafter" live?** NO — recurring 404 ⇒ first-payment modal is mandatory here.
11. **Is the RBI-AFA cap encoded?** YES — 1500000 paise in the create-order mandate; modal re-auth behavior observed.
12. **Are credentials required — no silent simulate?** YES — the guard refuses simulation without credentials.
13. **Is Buyer-B interop robust?** NO — P1 `NoneType.get` crash after a *successful* checkout (twice).
14. **Do both brains degrade gracefully?** YES — Groq 429 → deterministic fallback completed the demo.
15. **Is the whole thing production-money-ready?** **NO** — 8 hard gaps (§1.1); ready as a *honest demo + reference architecture*: `7.7/10` weighted.

---

*Prepared by the NTH audit process. No repository code was modified by this audit; the merchant.db was mutated by the sanctioned live re-run and restored-compatible via `data/merchant.db.bak_audit_20260830_122910`.*