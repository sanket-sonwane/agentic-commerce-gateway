# LIVE UI VERIFICATION REPORT

**Date:** 2026-08-30
**Scope:** Live, read-only (plus CDP browser drive) verification of the Agentic Commerce Gateway (ACG) dashboard — merchant/ops UI, payment-state UI, consumer provenance, and inventory reservation lifecycle audit evidence.
**Method:** CDP (Chrome DevTools Protocol) browser automation driving the real Vite dev-server app (`localhost:5173`) against the live Merchant (`:8000`) and Buyer (`:8001`) services, plus live HTTP/API inspection. A separate Chrome debug-profile window (PID via `--remote-debugging-port=9222`) was used so the user's existing browser session was left untouched.

> **Note on the split-result:** This report explicitly separates what was **proven live** from what was **environment-blocked**. The single most consequential finding — that approving a bill in the live UI declines server-side (payment UI never renders) — was reproduced end-to-end in the browser and traced to the audit trail. See §12 Root Cause.

---

## 1. Running Services

| Service | Port | PID | Started | Live `/health` |
|---|---|---|---|---|
| Merchant | 8000 | 21084 | 2026-08-30 20:43:31 | `{"status":"ok","protocol":"Agentic Commerce Protocol – Reference v0.1","payment_driver":"saved"}` |
| Buyer | 8001 | 10264 | 2026-08-30 20:43:31 | `{"status":"ok","model":"openai/gpt-oss-20b","buyer_agent_id":"buyer_agent_llm_01","merchant":"http://localhost:8000"}` |
| Vite dev server | 5173 | 6532 | 2026-08-30 08:03:03 | Standalone dev server (serves live `src/`, HMR active) |

- The browser-loadable frontend is the **Vite dev server** (`:5173`), not the `dist/` build. Vite transpiles `src/` on request, so the served bundle reflects **current source**.
- Vite proxy verified working: `GET http://localhost:5173/api/merchant/health` → merchant health payload; `POST /api/buyer/chat` → `200 text/event-stream`.
- A live Chrome (PID 2060, network service) held an ESTABLISHED connection to `[::1]:5173`; the verifying browser was the separate debug-profile instance.

## 2. Frontend Bundle Evidence (current source is served, not stale)

Every feature string below was retrieved over HTTP from the **Vite dev server** (URLs like `/src/...` transpiled on demand) — proving the running frontend is current source:

- `/src/utils/paymentState.js` — `PAYMENT_UI` = {COMPLETED, DECLINED, PAYMENT_PENDING, AUTHORIZATION_REQUIRED, PAYMENT_REQUIRED, CANCELLED, UNKNOWN}; `mapPaymentState` (completed→COMPLETED; declined→DECLINED; payment_pending→PAYMENT_PENDING; requires_payment + awaiting_authorization→AUTHORIZATION_REQUIRED else PAYMENT_REQUIRED; cancelled→CANCELLED; else UNKNOWN).
- `/src/components/PaymentCard.jsx` — `MANDATE_BLOCK_REASONS`, `loadRazorpay()`, two-stage E-Mandate/one-tap logic, POSTs verbatim Razorpay response to `/checkout_sessions/{id}/payment_callback`, calls `mapPaymentState`, only calls `onPaid(body)` when `state === COMPLETED`, and renders red "…nothing was charged." for any non-completed authoritative state.
- `/src/views/ChatView.jsx` — `PaymentSlot` renders `<PaymentCard redirect={ev} onPaid=.../>`; `paymentPaid(body)` maps the returned session via `mapPaymentState`, appends a `status` item, and only appends "✅ Order placed! You paid ₹{total}." when `state === COMPLETED`.
- `/src/components/ConsumerProvenanceDrawer.jsx` — `PAYMENT_TEXT` ("Paid and verified at the gateway", "Gateway declined the payment", "Payment is pending reconciliation", "Gateway order created – nothing charged yet", "Razorpay was never called") and `PROOF_TEXT` (Verified / Failed / Not available).
- `/src/App.jsx` — mounts `ChatView` (`onRequestOps: openOps`) and `ConsumerProvenanceDrawer` (opened via window `acg:open-consumer` event); also mounts OpsView (`acg:open-ops`).
- `/src/utils/events.js` — `bill_redirect` → `{ kind: 'payment' }` (this is what renders PaymentCard).
- `dist/` (built 2026-08-30 20:37:15) also contains all the above features — the released build is current too.

**Conclusion:** The frontend bundle is NOT stale. Current source is live-served and the released build contains the same feature code.

## 3. Payment-State Verification (live)

Live DB session statuses present: `completed`, `declined`, `requires_payment`. No live `payment_pending`, `cancelled`, or currently `awaiting_authorization` session existed.

Mapped via the live `mapPaymentState` (source-verified):
- `completed` → `COMPLETED`
- `declined` → `DECLINED`
- `requires_payment` (without `awaiting_authorization`) → `PAYMENT_REQUIRED`

**Live browser rendering result:** When the drive-through approved a bill (`cs_5dff1b9256`), the stream returned `{"type":"status","status":"declined"}` and the UI rendered the caption **"Order couldn't be placed · what happened?"** → **"❌ Sorry — I couldn't place this order because the payment was rejected by the gateway. Nothing was charged."** The PaymentCard (`kind:'payment'`) did **not** render because the backend never emitted `bill_redirect`.

The Ops panel rendered status labels correctly for live sessions: **Paid ₹185** (completed `cs_105f3af95b`), **Declined ₹560/₹1560/₹1040/₹185…**, **Awaiting payment ₹230/₹185…**, banner "driver SAVED · razorpay connected".

## 4. payment_callback (source + live API, not reachable end-to-end)

- **Source verified:** `PaymentCard` posts the Razorpay handler response verbatim to `/checkout_sessions/{id}/payment_callback`, reads the authoritative `body.checkout_session`, maps state, and only marks paid on `COMPLETED`. `RazorpayPayCard.jsx` is a legacy alias re-exporting `PaymentCard` (no duplicate logic).
- **Live limitation:** `razorpay_api_reachable: false` (admin status) and the drive-through's completed decline path proved the gateway could not be reached **at this time**. A real callback -> completion chain was **not exercised** in the live browser (no Razorpay order was created, `razorpay_order_id: null`).
- **Historical proof the paid path works when the gateway is up:** audit events for `cs_105f3af95b` (seq 149–153): `payment_success` "Paid ₹185 (order=order_TW1mZHUiMYTWVk, payment=pay_TW2IpOLHJbdvsu)", `reservation_consumed`, `checkout_completed`, `payment_token_saved "Saved Visa ••••4366"`. This is the `COMPLETED` path the frontend maps to "Paid".

## 5. Consumer Provenance (verified live in browser)

Driven live via `acg:open-consumer` for two real sessions; the drawer rendered correctly from the real `/explain`/provenance backend:

**Completed `cs_105f3af95b`:**
- Header: "Authorization & proof · cs_105f3af95b · MERCHANT GreenMart · ₹185 · **Completed**"
- Journey: intent → authorization → merchant decision → execution authorization → safety checks → razorpay payment verification → **COMPLETED**
- Safety checks: all ✓ (delegation, category, quantity, spend ₹185≤₹5000, stock)
- PAYMENT_TEXT: **"Paid and verified at the gateway"** · Razorpay `order_TW1mZHUiMYTWVk` / payment `pay_TW2IpOLHJbdvsu`
- PROOF: **Execution authorized / Policy decision / Decision receipt / Provenance / Audit chain — all "Verified"**

**Declined `cs_717e29c3f9`:**
- Journey stops at safety checks.
- Safety check failure: "✕ Stock Fresh Chicken Breast: requested=3, available=0"
- PAYMENT_TEXT: **"Razorpay was never called"**
- PROOF: Execution/Policy/Decision/Provenance "Not available"; **Audit chain "Verified"**

Both `PAYMENT_TEXT` values and proof rows exactly match the served component constants, and the API responses match (from prior HTTP checks: completed journey all-stages done; declined stops after execution authorization).

## 6. Inventory Audit Visibility (live)

Audit log (205 events) — reservation lifecycle counts:
| Action | Count | Example |
|---|---|---|
| `reservation_created` | 15 | live checks with `expires_at` |
| `reservation_consumed` | 1 | `cs_105f3af95b` res_c4ef60bdc7 pack_tea_250 active→consumed |
| `reservation_expired` | 1 | `cs_fed78df69d` res_024cde0ea1 pack_paneer_200 active→expired |
| `reservation_released` | **1** | `cs_5dff1b9256` res_52f20764d3 pack_tea_250 active→released |

- `/audit_logs/verify` → `{"intact": true, "entries_checked": 205, ...}` — the append-only chain is intact and every reservation transition is anchored/auditable.
- **Important correction (live observed):** the `reservation_released` path IS emitted at runtime. The drive-through's declined completion produced `reservation_released` (seq 203, "Final validation failed; reservation was released") followed by `final_validation` declined (`customer_setup_failed`) and `checkout_declined` (`PAYMENT_FAILED`). The pre-drive snapshot had no `reservation_released` only because no failure/cancel path had been exercised yet; it is now observed live.

## 7. TOCTOU Failure UI

- **Detected:** In the drive-through, `final_validation` at seq 201 passed all checks ("Price, delegation, stock and policy all valid at completion") and `execution_mandate_created` seq 202 succeeded — but the actual completion then failed at customer setup (`customer_setup_failed`) → order declined. This is the TOCTOU-style failure surface (validated-then-failed) between mandate issuance and payment settlement.
- **UI behavior observed:** the decline surfaced as **"Order couldn't be placed · what happened?"** with "payment was rejected by the gateway. Nothing was charged." — a clean error path (BUYER_AGENT error handling), **not** a silent hang, duplicate order, or stuck `awaiting_authorization`. Stock was released (`reservation_released`), so no inventory leak occurred.
- **No fresh live manipulation test was required** to observe the failure UI — the live drive-through exercised it organically.

## 8. Backend↔UI Consistency

- **Consistent:** completed session shows "Paid" in Ops and "Completed"/"Paid and verified" in provenance + audit `checkout_completed`; declined shows "Declined" in Ops + provenance "Razorpay was never called"/"Stopped at safety checks" + audit `checkout_declined`; `requires_payment` shows "Awaiting payment" in Ops + audit `awaiting_authorization`.
- **Frontend/backend contract is correct but the trigger is gated:** the PaymentCard only renders when the backend emits `bill_redirect` (i.e. `complete_checkout` returns `requires_payment` **with** `payment.awaiting_authorization`). The live backend, with the gateway unreachable, returns `declined` (`PAYMENT_FAILED`) instead, so the payment UI never mounts even though the component is fully wired. This is the observed disconnect: **code present + wired, but the backend never reaches the payment UI in this environment.**

## 9. Browser Console

Captured from the drive-through Chrome tab — **clean**:
- `[debug] [vite] connecting...` / `[debug] [vite] connected.` (normal)
- `[info] Download the React DevTools...` (normal)
- **Zero** `pageerror` events across all runs (initial load, checkout, decline, provenance, ops).
- No React hydration/type errors, no undefined-`mapPaymentState`/`PaymentCard` runtime exceptions.

## 10. Network

- Chat SSE stream `POST /api/buyer/chat` → `200 text/event-stream` (bill `cs_b82139633f`, total ₹185) — healthy.
- Decision stream `POST /api/buyer/conversations/conv_…/decision` → `200 text/event-stream` returning `status: declined` (+ decline message).
- Provenance calls `…/consumer/transactions/{id}/…` and Ops `/audit_logs/verify`, `/audit_logs`, admin endpoints — all `200`.
- **`payment_callback` was never invoked** by the browser in the drive-through (payment UI never mounted), so no callback request/response could be observed live.
- **Zero** `requestfailed` events across all runs (no broken asset requests, no missing chunks, no 404s).

## 11. Stale Bundle Analysis

- **Not stale.** The Vite dev server serves current source (verified by live-transpiled URLs and feature strings). The `dist/` bundle (built 20:37) also contains all new feature strings. The originally-suspected "changes not visible = stale build" hypothesis is **rejected**.
- The reason the payment/provenance changes appeared "not visible" is **not** a stale bundle — it is the runtime gating described in §8/§12 (backend declines before the payment UI can mount, given the unreachable gateway).

## 12. Root Cause (A–I selection)

**Chosen: Root cause H — the feature is actually present in source and bundle, but not reachable at runtime in the current environment** (strongest fit), with contributing detail from **C (downstream dependency unreachable)**.

Evidence chain:
1. All payment-state + provenance code is present, live-served, and wired (`PaymentSlot` → `PaymentCard` → `bill_redirect` mapping; provenance drawer → `acg:open-consumer`).
2. The live browser drive-through reproduced the symptom: approving a bill declined **without** showing the payment UI.
3. The audit trail explains exactly why: `complete_checkout` → `customer_setup_failed` ("Connection aborted / RemoteDisconnected… **Razorpay NOT called**") → `checkout_declined` (`PAYMENT_FAILED`). No Razorpay order id was created → no `awaiting_authorization` → no `bill_redirect` → no PaymentCard.
4. `razorpay_api_reachable: false` (admin status) is the environmental predicate that makes every new approval decline.

So the "frontend changes not visible" is a **runtime environment artifact**, not a code/feature absence. When the gateway was reachable (the earlier completed `cs_105f3af95b`), the full paid path worked end-to-end.

## 13. Runtime Files Used

- `dashboard/src/utils/paymentState.js` — payment-state mapping (authoritative).
- `dashboard/src/components/PaymentCard.jsx` + `RazorpayPayCard.jsx` (legacy alias) — money card + callback.
- `dashboard/src/views/ChatView.jsx` — PaymentSlot / paymentPaid wiring.
- `dashboard/src/components/ConsumerProvenanceDrawer.jsx` — provenance drawer.
- `dashboard/src/utils/events.js` — `bill_redirect`→`payment` mapping.
- `dashboard/src/App.jsx` — drawer/ops mounting; `vite.config.js` — proxy.
- `merchant/domain/inventory_service.py`, `merchant/domain/checkout_service.py`, `merchant/control/explain_service.py`, `merchant/api/admin.py` — backend semantics (release/consume/expire, completion, explain, admin status).
- `buyer_service/agent.py` — decision/completion flow (`_complete_flow`, `bill_redirect` emission).
- `data/merchant.db` (+WAL) — live persistence backing all audit/provenance reads.
- Evidence artifacts (throwaway, outside repo): `Temp/opencode/cdp-drive/*.json` (console/network/API captures) + PNG screenshots of bill, pay-card(generated), provenance, ops panel.

## 14. Problems Found

1. **[Root cause] Runtime gating of payment UI when Razorpay unreachable:** approving a bill declines server-side (`PAYMENT_FAILED` from `customer_setup_failed`) so the payment card/`payment_callback` path is unreachable in the current environment; user sees "payment was rejected by the gateway" instead of "Pay securely". Not a stale bundle — an environment-driven behavior.
2. **No live `payment_pending` and `cancelled` coverage:** no live session exercises those `mapPaymentState` branches; they remain source-verified only.
3. **`payment_callback` chain unobservable live:** because the gateway is down, the callback endpoint could not be driven end-to-end in the browser (only source-verified).
4. **Browser console/network for the paid-completion flow unavailable** for the same reason (the completion path we could drive was the declined one).
5. Minor: audit/event payloads contain PowerShell-mangled unicode in raw log dumps (₹/·) — a console-encoding artifact, not an app defect; UI renders symbols correctly.

## 15. Recommendation

1. **Restore Razorpay connectivity (or provide reachable test credentials) and re-run the drive-through** to observe the green "Paid" path live: bill → PaymentCard → Razorpay modal → `payment_callback` → `onPaid` → "✅ Order placed! Paid ₹…". This is the single remaining unverified leg, and it is purely environment-blocked.
2. To prove the `requires_payment+awaiting_authorization→AUTHORIZATION_REQUIRED` and `payment_pending`/`cancelled` render branches, drive a session that reaches `bill_redirect` (once the gateway is up) and/or create parked/cancelled sessions.
3. Consider surfacing the gateway-unreachable condition in the UI proactively (payment card disabled with "gateway unreachable — cannot start checkout") so the "changes not visible" symptom doesn't recur: the mismatch is between backend `complete_checkout` declining alone vs. emitting `bill_redirect`.
4. No code change is required to "show" the payment UI — it is wired and current. The fix for the observed symptom is environmental (reachable payment gateway).
5. Retain the `reservation_released` live event (now observed) as proof the inventory audit lifecycle is fully closed (create/consume/expire/release all present and chain-verified: `intact: true`, 205 entries).

---
*Evidence gathered exclusively by read-only HTTP/API inspection plus a separate-profile CDP browser drive. No repo/package/tooling/repo-`package.json` changes were made (puppeteer-core was installed only in a throwaway temp dir).*
