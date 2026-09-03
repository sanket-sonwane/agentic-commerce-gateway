# ACG UI/UX RESTRUCTURE REPORT

> Branch: `ui-restructure` — Frontend-only presentation restructure.
> **Show meaning first. Show machinery second.** No backend, payment, API, or audit logic changed.

---

## 1. Objective
Restructure the Agentic Commerce Gateway (ACG) dashboard so every surface leads with user-facing meaning (outcome, amount, status, short actionable statement) and preserves all technical/proof evidence behind secondary, progressive disclosure.

## 2. Scope
- Frontend presentation only (`dashboard/src`).
- Backend semantics preserved verbatim: `AP2`, `UserIntentAssertion`, `Execution Mandate`, `audit`, payment state, `Merchant Agent`, `Buyer Agent`.
- Frontend continues to consume authoritative backend state; it invents no business truth.

## 3. Absolute Rules Obeyed
- ✅ No changes to backend logic, payment logic, or API contracts (diff shows dashboard-only for this work; backend test suite is green).
- ✅ Technical evidence never deleted — moved behind expanders / secondary views.
- ✅ No walls of text on primary surfaces — short labels, indicators, compact summaries, amounts.
- ✅ No JSX-text `\uXXXX` escapes left (would render literally) — all converted to real UTF-8 or kept only inside JS strings/templates.
- ✅ No dead UI / dead exports left behind.

## 4. Guiding Principle Applied
`Show meaning first, show machinery second.` The merchant/buyer agent UI expresses *what happened* in plain terms; the evidence (hashes, signatures, rule IDs, payment `state` codes) is reachable via "Why this total?", "Full trail", "View technical evidence", and the standalone verifier.

## 5. Terminology Layer (NEW)
- **`dashboard/src/utils/terminology.js` (NEW)** — single source of truth for user-facing labels:
  - `RULE_LABELS` — `spend_limit`→"Your spending limit", `stock_check`→"Item availability", `category_allowed`→"Purchase category", `delegation_expiry`→"Agent permission", `quantity_limit`→"Quantity per item".
  - `CHAT_STATUS` + `humanStatus()` — plain chat captions ("Order placed", "Order cancelled — nothing was charged", "Ready for your payment"…).
  - `PAYMENT_STATUS` — "Payment confirmed and verified", "Payment was declined", "Payment is being processed", etc.
  - `PROOF_LABELS`/`PROOF_STATUS` — "Transaction authorization", "Safety verification", "Payment binding", "Full provenance", "Audit trail"; statuses "Verified/Failed/Not available".
  - `AGENT_STATUS` — "Completed", "Waiting for buyer", "Ready for payment", "Payment pending", "Stopped", "Could not fulfil".
  - `INTEGRITY` — "Audit verified" / "Audit integrity issue".
- **`dashboard/src/utils/statusDisplay.js` was created per plan, then REMOVED** — it was entirely unconsumed (dead code). `terminology.js` is the single canonical mapping actually used, keeping the codebase DRY per Phase 17 (one mapping, not two).

## 6. Audit / Event Language (actionMeta.js + events.js)
- `actionMeta.js` rewritten to use shared `RULE_LABELS` and friendly audit titles:
  - "Safety checks" → "Final safety check" → "Order stopped" → "Payment sent to Razorpay" → "Payment confirmed" → "Payment declined" → "Thinking…".
- `events.js` now re-exports `humanStatus` from `terminology.js` (single implementation, no duplicate `STATUS_COPY`).

## 7. App Shell (App.jsx)
- Brand subtitle: **"Agentic Commerce Gateway · test mode"** (jargon-light).
- Integrity badge in topbar: **"Audit verified · N events"** or **"Audit integrity issue @seq"** (meaning first; raw sequence is compact).

## 8. Chat Surface (ChatView.jsx)
- Hero copy: **"Shop with your AI agent… You approve before money moves."**
- `OrderReady` card now includes a **`.safety-summary`** block: "Within your spending limit · Items in stock · Your agent is authorized" — a compact trust summary over the authoritative gated flow.
- Success/Caption paths use shared `humanStatus` (e.g., "Order placed", "nothing was charged").
- INR/mojibake fully fixed (`₹`, `·`, `×`, `→`, `↑`, `…`, `⚠`, `✓` render correctly).

## 9. Payment Card (PaymentCard.jsx)
- New **`SafetySummary`** panel on the payment surface.
- Richer success block: **`.pay-proof-summary`** with proof checks.
- Friendly failures: **"Payment could not be completed. Nothing was charged."** / **"Payment cancelled — nothing was charged. You can retry."** — removed the raw `(reason: ...)` technical display.
- Auto-pay fallback copy humanized.

## 10. Money Story Drawer (MoneyStoryDrawer.jsx)
- Sections renamed: "Deterministic checks" → **"Why this was allowed"**; "What the merchant agent did" → **"How your agent handled this"**; "Gateway proof" → **"Payment verification"**; "five deterministic checks" → **"all safety checks passed"**.
- Read `RULE_LABELS` from shared terminology.
- Fixed a stray `</div>` that previously broke the build.

## 11. Consumer Provenance Drawer (ConsumerProvenanceDrawer.jsx)
- Proof list humanized using shared `PROOF_LABELS`: "Transaction authorization", "Safety verification", "Payment binding", "Full provenance", "Audit trail"; status via `PROOF_STATUS`/`PAYMENT_STATUS`.
- "View technical evidence" expander retained (machinery preserved behind disclosure).

## 12. Merchant Agent Trace (MerchantAgentTrace.jsx)
- Agent status via shared `AGENT_STATUS`.
- "No deterministic evaluation yet" → **"No safety checks yet"**.
- "Razorpay invoked YES/NO" → **"Payment sent Yes/No"**.
- "Paid via gated completion ✓" → **"Payment completed ✓"**.
- "awaiting authorization / pending" → **"Payment waiting for authorization"**.

## 13. Ops / Policy View (OpsView.jsx)
- "Policy checks" → **"Safety checks"**.
- "Razorpay invoked YES/NO" → **"Payment sent Yes/No"**.
- "Signed Authorization & Proof" → **"Authorization & Proof"**.
- `₹` / `·` mojibake fixed.

## 14. Styles (styles.css)
- Added `.caption.red-line` (failure styling), `.safety-summary` / `.safety-item`, `.pay-proof-summary` / `.proof-check` — compact card styles for the new meaning-first surfaces.

## 15. Unchanged / Already Clean
`MessageBubble.jsx`, `QuickReplyChips.jsx`, `WorkingShimmer.jsx`, `CategoryIcon.jsx`, `tokens.css`, `api.js`, `lib/userAssertion.js`, `utils/paymentState.js` (authoritative payment mapping is correct and untouched), `AuditTimeline.jsx` (reads friendly titles from `actionMeta.deriveStages`).

## 16. Encoding Progress
All mojibake (`â‚¹`, `â†’`, `âˆ†`, `Ã—`, `âœ“`, `âš`, `â€¦` → `₹`, `→`, `↑`, `×`, `✓`, `⚠`, `…`) corrected. No `â` sequences remain. All JSX-text `\uXXXX` converted to real UTF-8; only JS-string/template/expression-brace escapes remain (they render correctly in the built bundle).

## 17. Code Hygiene
- Removed the dead `statusDisplay.js` and its unused import in `OpsView.jsx` (kept a single canonical mapping in `terminology.js`).
- Removed dead exports (`STATUS_DISPLAY`, `STAGE_LABELS`, `humanError`, `paymentCaption`) that no surface consumed.
- Wired `ConsumerProvenanceDrawer` to consume `PROOF_LABELS` instead of hardcoding duplicate strings.

## 18. Files Changed
1. `dashboard/src/utils/terminology.js` — **NEW**, canonical terminology.
2. `dashboard/src/App.jsx`
3. `dashboard/src/views/ChatView.jsx`
4. `dashboard/src/views/OpsView.jsx`
5. `dashboard/src/components/PaymentCard.jsx`
6. `dashboard/src/components/MoneyStoryDrawer.jsx`
7. `dashboard/src/components/ConsumerProvenanceDrawer.jsx`
8. `dashboard/src/components/MerchantAgentTrace.jsx`
9. `dashboard/src/utils/actionMeta.js`
10. `dashboard/src/utils/events.js`
11. `dashboard/src/styles.css`

## 19. Files Deleted
- None of the 14 planned files were deleted.
- `dashboard/src/utils/statusDisplay.js` was created then removed as dead code (net zero; not part of the committed set).
- `dist/` is a gitignored build artifact, not part of the commit.

## 20. Build Results
- `npm run build` (Vite) — **success** (1837 modules). Ran repeatedly after each change; final run green.
- Built bundle verified to contain recovered characters and friendly strings (`Payment completed — ₹`, `Payment completed ✓`, `Why this was allowed`, `How your agent handled this`, `Payment verification`, `nothing was charged`, `Transaction authorization`, `Audit trail`, `Safety checks`, `SECURE PAYMENT`, …).

## 21. Test Results
- Frontend: no test files exist (`dashboard/**/*.test.*`, `*.spec.*` = none). Build is the verification gate.
- Backend: `python -m pytest merchant/tests -q` → **305 passed, 0 failures** (3 non-blocking deprecation warnings). Confirms zero semantic regression — expected, since no backend file was changed by this work.

## 22. Remaining UI Issues / Caveats
- The `OrderReady` `.safety-summary` block ("Within your spending limit · Items in stock · Your agent is authorized") is shown because the merchant only presents an order-ready bill after its server-side gating passed. It is a safe summary of backend-confirmed state, not invented truth; the authoritative per-check detail remains behind "Why this total?".
- Backend files shown as modified in the working tree (`merchant/…`, `scripts/…`) and a set of untracked files (`artifacts/…`, `merchant/domain/reconcile_worker.py`, `merchant/tests/test_async_settlement.py`, `scripts/…`) are **pre-existing** working-tree state, not part of this restructure; they have not been staged.

## 23. Final Verdict
**PASS.** The dashboard now leads with meaning (outcomes, amounts, statuses, short statements) while keeping all technical and proof evidence reachable via progressive disclosure. The single canonical terminology layer removes duplicated label maps. INR/unicode rendering is correct. Build is green and the backend suite confirms no semantic change. **Note:** not yet committed — awaiting explicit commit confirmation.
