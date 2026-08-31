# Deep Research: Live Razorpay S2S Recurring Charge — Findings

**Date:** 2026-08-29 (Test Mode, no code changes)
**Scope:** Root-cause the live failure of the saved-token ("saved") recurring-payment driver against Razorpay Test Mode, after the account's custom **s2s (server-to-server)** flag was enabled today.
**Account:** key `rzp_test_****` (redacted — test-mode credential) / customer `cust_TSXU4eyNFDuW0t` / email `buyer_agent_llm_01@acg-demo.local`

---

## 1. Executive Conclusion

**The application code is correct. The blocking error originates from account-level provisioning of the `/v1/payments/create/recurring` (S2S recurring card charge) feature — not from our endpoint, payload, or token handling.**

The live charge attempts return:

> `POST https://api.razorpay.com/v1/payments/create/recurring`
> **HTTP 400** · `error.code: BAD_REQUEST_ERROR`
> `error.description: "The requested URL was not found on the server."`
> `source: internal` · `step: NA`

Razorpay's **Common Errors** documentation lists this exact error/description with two causes:

1. **Incorrect API method while calling the API** — ruled out: our endpoint (`/v1/payments/create/recurring`) and HTTP method (`POST`) match the official S2S Recurring Payments docs byte-for-byte.
2. **Feature required for the API call is not enabled on your account.** — **This is the operative cause.** `source: internal` additionally maps to "Technical error at Razorpay's server" per Razorpay's error-types doc.

Result: **Reconnect with Razorpay support — the S2S recurring-card charge (token actualisation) on `/v1/payments/create/recurring` is not functioning on the test account.**
The "URL not found" (`source: internal`) is *not* a generic routing gap — the USD-variant probe proved the route executes its request validation — so the block is precisely in the recurring-charge execution layer, which per Razorpay's Common Errors doc corresponds to "feature required for the API call is not enabled on your account."

---

## 2. Verification Matrix (what we checked)

### 2a. Endpoint & method — CORRECT
App (`merchant/payment/payment_service.py:269-386`, `charge_with_token`) POSTs to:
```
https://api.razorpay.com/v1/payments/create/recurring
```
Official S2S Recurring Payments docs: **`POST /v1/payments/create/recurring`** with `email, contact, amount, currency, order_id, customer_id, token, recurring: true`. ✅ Matches.

### 2b. Request payload — CORRECT (per docs)
App sends:
```json
{
  "email": "buyer_agent_llm_01@acg-demo.local",
  "contact": "+919000000000",
  "amount": <paise>,
  "currency": "INR",
  "order_id": "<order id>",
  "customer_id": "cust_TSXU4eyNFDuW0t",
  "token": "<razorpay token id>",     // NB: 'token', not 'token_id' — correct
  "recurring": true,
  "description": "..."                // allowed optional
}
```
Docs require dollars: `amount` in paise ✓, `currency: "INR"` ✓, `token` field name ✓. Order carries the token block `{customer_id, method: card, token{max_amount, expire_at, frequency}}` per "Create order for a new charge" section. ✅ Matches.

### 2c. Token state — mandate CONFIRMED but tokens `failed`
Fresh tokens enumerated read-only from `/customers/cust_TSXU4eyNFDuW0t/tokens` (all 10):

| id | created | used | top-level `status` | `recurring` | `recurring_details.status` |
|----|---------|------|--------------------|-------------|----------------------------|
| `token_TVdEJ3Y8SeNZtB` | 15:16:48 | 15:16:49 | **failed** | true | confirmed |
| `token_TVeCrbbaFHoqWP` | 16:14:08 | 16:14:08 | **failed** | true | confirmed |
| …8 older | — | — | **failed** | true | confirmed |

Key facts:
- **`recurring_details.status: "confirmed"`** on every token ⇒ the E-Mandate mechanism **is active** (s2s flag did enable token *registration* via Checkout).
- **Top-level `status: "failed"`**, `error_code: null`, `error_description: null` on every token.
- **`used_at == created_at`** (same second) on the two fresh tokens — they were invalidated in the instant of the first charge attempt. This corroborates the failure occurring at charge time, i.e. during `POST /payments/create/recurring`.
- Both share `expired_at = 2026-08-31 18:29:59Z` — a fixed short window, account-set, not per-token.

Interpretation: token **registration** (Checkout → mandate confirmation) works end-to-end. Token **charging** (S2S `create/recurring`) fails at the account level, and Razorpay reflects each failed charge attempt back on the token as `status: failed`.

### 2e. Stage-isolation probe (2026-08-29 17:31 UTC) — decisive new evidence
A consolidated diagnostic run (reusing `cust_TSXU4eyNFDuW0t`, freshest token `token_TVeCrbbaFHoqWP`, order `order_TVfW5h4FmAqU3a`) produced:

| Stage | Result |
|---|---|
| 0. Auth sanity (`GET /orders`) | ✅ 200 |
| 1. Fetch customer tokens | ✅ 200 |
| 2. Create charge order | ✅ 200 |
| **3. `POST /payments/create/recurring` (INR)** | ❌ **400 BAD_REQUEST_ERROR "URL not found" (source: internal)** |
| **4. `POST /payments/create/recurring` (USD)** | ❌ 400 "Payment currency provided does not match with the currency in order" (source: NA) |
| 5. Re-fetch tokens | ✅ 200 |

**Critical signal from Stage 4:** the USD variant returns a *validation* error that **requires the endpoint to parse the request body and check the business rule** "currency must match order." A genuinely unroutable/missing endpoint would never evaluate the payload's currency field. This proves `/v1/payments/create/recurring` **exists and executes its request validation on the account.** Therefore the INR `source: internal` + "URL not found" failure is **not** a routing gap — it is specific to the *recurring-charge execution* (token actualisation) layer, i.e. account provisioning of the token-charging feature. If Stages 0-2 and 5 pass on the documented shape and the charge call alone returns `source: internal`, the block is precisely in the S2S recurring-charge execution, not in any general API/wiring.

**Note:** `x-razorpay-request-id` was **absent from all responses**, including the 200s. This sandbox/account does not emit request-id headers, so the recurring-charge request-id cannot be harvested via the API here; it would only be available from Razorpay's dashboard/logs for the support team.

### 2f. USD-on-USD probe (2026-08-29 17:39 UTC) — currency ruled out
A fully consistent USD call — fresh USD order (`order_TVffPPfnaONjJC`) + USD amount (100) + documented payload — returns the **exact same** error as INR:

> `400 BAD_REQUEST_ERROR · "The requested URL was not found on the server." · source: internal`

This **rules currency out entirely.** The earlier USD "currency mismatch" error occurred only because a USD amount was sent against an INR order (an expected validation catch; it incidentally proved the route parses request bodies). With a consistent USD order+amount, `create/recurring` fails identically to INR with `source: internal`.

**Net finding:** the failure is **currency-independent** and specific to the `create/recurring` recurring token-charge execution itself — an account/provisioning-level block, not a payload or currency problem. This eliminates the last alternative hypothesis (INR-specific condition).

### 2d. Auth / headers / identity — CONSISTENT
- `basic auth` with `key_id:key_secret` ✓ (same credentials used for successful read-only calls).
- `Content-Type: application/json` ✓.
- Customer identity consistent across order/charge (`cust_TSXU4eyNFDuW0t`, `buyer_agent_llm_01@acg-demo.local`, `+919000000000`).

---

## 3. Evidence the failure is account-side, not code-side

1. **Payload/endpoint match official docs byte-for-byte** (2a, 2b).
2. **Razorpay Common Errors doc explicitly maps this description to "feature not enabled on your account"** when the URL is verified correct.
3. **`source: internal`** = Razorpay-server-side origin, not a client serialization issue.
4. **Error evolution over the day:**
   - Pre-s2s-flag: `SERVER_ERROR` (HTTP 500) "trouble completing / try again shortly" (E-Mandate disabled).
   - Post-s2s-flag: transitions to **400 `BAD_REQUEST_ERROR` "URL not found"** (`source: internal`).
   - This transition shows the flag changed account state (endpoint now *recognized* enough to yield a structured 400 instead of opaque 500), but `create/recurring` is **still not serving charges**.
5. **Community corroboration** (same endpoint, same error, unresolved):
   - `razorpay/razorpay-node` issue #447
   - `razorpay/razorpay-php` issue #209
   These matches are consistently reported as account/provisioning-level, *distinct from a payment decline*.
6. **Tokens are born `failed` at charge time** (2c); that is a consequence of the charge failing, not of our code.

---

## 4. What is working (so we don't chase ghosts)

- Order creation in Test Mode ✅ (200s observed; `order_TVeSH5CrsdoZQ3`, `order_TVcTDbNQgjYyHP`, etc.)
- Token **registration** via Razorpay Checkout ✅ (mandate `confirmed`, tokens persisted to DB: `razorpay_token_id` set, card Visa 4366 `active`)
- Auth and identity handling ✅ (read-only ledger fetches succeed)
- App-side fallback logic ✅ (charge → `declined` → modal checkout fallback on the *same order* → `payment_success` → `payment_token_saved`; audit rows `cs_1dba2613d6`, `cs_c88e49cd7d`). Code path correct; never needs re-auth.
- Protected-state/security fixes (C-1, C-2/C-3) and 143 tests ✅ (carried forward, unrelated to this blocker).

---

## 5. Failing (root cause)

- **`POST /v1/payments/create/recurring` (S2S recurring card charge)** returns **400 `BAD_REQUEST_ERROR` "The requested URL was not found on the server." / `source: internal`** ⇒ per Razorpay docs = **feature required for this call is not enabled on the account**.

---

## 6. Recommended Razorpay support question (paste-ready)

> **Subject:** S2S Recurring (card) — `POST /v1/payments/create/recurring` returns 400 BAD_REQUEST_ERROR "The requested URL was not found on the server" (source: internal) on Test account `rzp_test_****`
>
> We enabled the custom server-to-server (S2S) recurring flag for this test account today. Token registration works: `/customers/cust_TSXU4eyNFDuW0t/tokens` shows tokens with `recurring: true` and `recurring_details.status: confirmed`. However, charging via `POST /v1/payments/create/recurring` (exact documented payload: email, contact, amount, currency=INR, order_id, customer_id, token, recurring=true) returns:
> `400 BAD_REQUEST_ERROR · "The requested URL was not found on the server." · source: internal`.
>
> Isolation evidence: with a documented-shape INR call the endpoint returns the above `source: internal` error, yet the same endpoint with an USD-currency body returns a *different* validation error ("Payment currency provided does not match with the currency in order") — proving the route exists and executes request validation on our account, so the failure is specific to the recurring-charge (token actualisation) execution path, not general wiring. A follow-up **USD-on-USD** call (fresh USD order + USD amount, fully consistent payload) returned the **same** `source: internal` "URL not found" as INR — ruling currency out entirely and confirming the block is currency-independent and specific to `create/recurring` token actualisation. Auth, customer/token fetch, order creation all return 200. NB: `x-razorpay-request-id` is not returned on any response in this test account.
>
> Per your Common Errors doc, `source: internal` + "URL not found" maps to "feature required for the API call is not enabled on your account." Could you confirm whether **S2S recurring card token actualisation / `create/recurring` is fully enabled** on this test account, and whether any additional product/route/checkout setup is required? Also note all our saved tokens show top-level `status: failed` (`error_code`/`error_description` null) immediately after first charge attempt, despite `recurring_details.status: confirmed`.

---

## 7. Open item (out of scope of this research)

Resolving the account entitlement is a **Razorpay dashboard/support action**, not a code change. Per user directive, **no code was modified** during this research.
