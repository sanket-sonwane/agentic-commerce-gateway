# C-1 FIX REPORT — Payment Amount Binding

**Project:** Agentic Commerce Gateway (ACG) — Razorpy AI Buildathon
**Phase:** 2 — Critical Bug / Security Fixes
**Defect:** C-1 — Payment amount is not bound end-to-end (`FINAL AUTHORIZED == PERSISTED AUTHORIZED == RAZORPAY ORDER == VERIFIED PAYMENT`)
**Status:** ✅ FIXED and verified (automated suite + real Razorpay TEST MODE)
**Commit:** `7f3dcb6`

---

## 1. Executive Summary

Before this fix, the payment amount was only **sporadically** enforced: the
callback reconciliation checked the settled amount against the authorized
amount, but the decisive completion path (`_finalize_payment`) marked a session
`completed` with **no amount check at all**, the verified gateway amount was
never persisted or cross-checked, order reuse on retry did not re-verify the
amount, and the success audit log reported the *session total* rather than the
*verified gateway amount*.

C-1 enforces a single non-negotiable invariant on every successful completion:

```
FINAL AUTHORIZED AMOUNT == PERSISTED AUTHORIZED AMOUNT
                         == RAZORPAY ORDER AMOUNT
                         == VERIFIED PAYMENT AMOUNT
```

Any divergence **fails closed**: the session transitions to `declined` with
`PAYMENT_BLOCKED`, reservations are released, stock is **not** consumed, and
the erroneous completion never happens.

## 2. Root Cause

The amount binding was enforced at only one point (the payment callback's
`settle_probe` → `payment_amount_mismatch` check) and only for the callback
driver. The core completion finalizer never compared amounts:

- `_finalize_payment` set `cs.status = "completed"` unconditionally for a
  `completed` outcome — no check that the order/verified amount matched the
  authorized amount.
- The verified gateway payment amount was never persisted onto the session, so
  it could never be audited or cross-checked after the fact.
- The Razorpay order amount was stored at creation but never re-validated on
  order **reuse** during a retry.
- The `payment_success` audit event logged the *session `totals` total*, not the
  verified gateway amount — so a tampered total would be silently reported.

## 3. Invariant / Threat Model

A successful completion is only valid if the amount the buyer is reliably
charged equals what the Merchant Agent authorized (and the Execution Mandate
agreed to):

| Source | Field | Authority |
|---|---|---|
| Server-side checkouts after final validation | `authorized_amount_paise` | Deterministic control plane (AUTHORITATIVE) |
| Razorpay order (echoed at creation) | `razorpay_order_amount` | AP2 verification/binding |
| Persisted session total | `totals.total` | cross-checked by AP2 `amount_matches_persisted` |
| Razorpay captured/settled payment | `verified_payment_amount_paise` | Payment service / AP2 verification |

Threats closed:
- **Order-creation tamper** — gateway order amount diverging from authorized.
- **Order-reuse divergence** — retrying onto an order whose amount differs.
- **Verified-amount divergence** — captured payment ≠ authorized.
- **Session-total mutation** — persisted totals silently altered after
  authorization (detected via AP2 `amount_matches_persisted`).
- **Buyer/LLM amount manipulation** — request/LLM-suggested prices never become
  the authority; the server-side authorized amount always wins.

## 4. Files Changed

| File | Change |
|---|---|
| `merchant/domain/checkout_service.py` | Order-creation + reuse amount checks; `_finalize_payment` binding guard; `serialize_session` surfaces evidence |
| `merchant/api/checkout.py` | Callback outcome carries `raw={"amount": settled_amount}` so the verified amount persists |
| `merchant/persistence/models/entities.py` | New column `verified_payment_amount_paise` on `CheckoutSession` |
| `merchant/persistence/db.py` | Migration entry for `checkout_sessions.verified_payment_amount_paise` |
| `merchant/tests/test_c1_amount_binding.py` | **New** — Tests 1–8 (see §10) |

Scope honored: **smallest correct change**. No refactor, no module renames, no
reorganization. C-2 and C-3 were **not** altered (see §13).

## 5. Data Flow (Before → After)

**Before**
```
authorize(float_paise) ─ store authorized_amount_paise
create_order ─ store razorpay_order_amount  (once, unverified echo)
... (no re-check on reuse) ...
_finalize_payment(outcome.completed) ─ status=completed      <-- NO AMOUNT CHECK
payment_success audit ─ logs session totals.total            <-- NOT verified amount
verified amount ─ NONE persisted
```

**After**
```
authorize(float_paise) ─ store authorized_amount_paise (frozen at mandate)
create_order:
  echo_amount = order.amount                <-- NEW
  echo_amount != amount_paise  -> PAYMENT_BLOCKED   <-- NEW
reuse path:
  persisted order amount != amount_paise -> PAYMENT_BLOCKED   <-- NEW
_finalize_payment(outcome.completed):
  authorized = authorized_amount_paise
  order      = razorpay_order_amount
  verified   = outcome.raw.amount (or order fallback when no gateway)
  persist verified_payment_amount_paise                 <-- NEW
  amount_match = authorized>0 and order==authorized and verified==authorized
  if not amount_match -> release reservations, status=declined,
                         PAYMENT_BLOCKED, payment_amount_mismatch audit,
                         error payload                           <-- NEW
  else -> consume stock/reservations, status=completed,
          payment_success audit includes amount-binding evidence
serialize_session surfaces: razorpay_order_amount_paise,
                  verified_payment_amount_paise, amount_match     <-- NEW
```

## 6. Fields Added/Changed

- `CheckoutSession.verified_payment_amount_paise: int | None` — the amount the
  gateway independently reported as settled (paise). `None` in hermetic sim when
  no gateway amount exists.
- Audit payloads (success case) now carry `authorized_amount_paise`,
  `razorpay_order_amount`, `verified_payment_amount_paise`, `amount_match`.
- Serialized session `payment` dict now carries `razorpay_order_amount_paise`,
  `verified_payment_amount_paise`, and `amount_match` (when both order + verified
  are present). `dict[str, Any]` — no DTO/breaking contract change.

## 7. Validation Comparisons

| Comparison | Where | On failure |
|---|---|---|
| Gateway order `amount` == authorized | `complete_checkout` (new + reuse path) | `PAYMENT_BLOCKED`, no order persisted/reused |
| `amount_matches` (mandate.total == authorized) | AP2 `verify_execution_mandate` | pre-existing |
| `amount_matches_persisted` (mandate vs persisted totals) | AP2 `verify_execution_mandate` | pre-existing |
| authorized == order == verified | `_finalize_payment` (before `completed`) | `PAYMENT_BLOCKED`, session `declined` |

## 8. AP2 Interaction

- `authorization_service.build_execution_mandate` continues to freeze
  `cs.authorized_amount_paise` at mandate creation — this is the single source
  the new `_finalize_payment` guard reads (deterministic control plane).
- `verify_execution_mandate` (`amount_matches`, `amount_matches_persisted`)
  unchanged; used by `test_5/test_8` to prove session-total mutations are
  cross-checked.
- Provenance `amount_binding` / `order_amount_binding` / `decision_receipt`
  checks all passed against the live order in Step 14.

## 9. Idempotency & State-Machine Safety

- The new guard is **read-only**: it routes only to the pre-existing `declined`
  transition and the existing `completed` transition. It never changes the
  callback state-machine or terminal-state rules (C-2/C-3 left untouched).
- Order+verified amounts are re-read each completion, so a retry re-validates.
- On a mismatch the session is terminal `declined`; no stock consumed, promises
  released — no double-billing and no half-consumed order.

## 10. Tests (merchant/tests/test_c1_amount_binding.py)

| Test | Scenario | Result |
|---|---|---|
| 1 | Normal flow completes, authorized==order==verified, `amount_match: True` | ✅ |
| 2 | Order amount diverges at creation → `PAYMENT_BLOCKED` | ✅ |
| 3 | Verified payment amount diverges → fails closed (declined) | ✅ |
| 4 | Retry reuses divergent order amount → `PAYMENT_BLOCKED` | ✅ |
| 5 | Session total mutated after authorization → AP2 `amount_matches_persisted` False | ✅ |
| 6 | Buyer injects bogus `amount`/`total`/`payment_amount_paise` → ignored (server authority) | ✅ |
| 7 | LLM-suggested diverging amount → server authorized amount wins | ✅ |
| 8 | Mandate vs session authorized diverges → binding rejects | ✅ |

**Full suite: 117 passed** (109 prior + 8 new). `scenario_check` exit 0.

## 11. Actual Results (Automated)

```
117 passed in ~7s
scenario_check: EXIT=0 (all 6 scenario groups pass)
```

## 12. Manual Test — Real Razorpay TEST MODE (Step 14)

Ran a live test-mode transaction against the real gateway:

1. Create checkout → `requires_payment`, total ₹104.
2. `execution_mandate_created` authorized **10400 paise**.
3. `razorpay_order_created` — **real order `order_TVTS2sYFkMRLq9`**, amount echoed
   10400 == authorized (provenance `order_amount_binding: True`).
4. Saved-Visa token charge attempted; gateway rejected it (E-Mandate disabled on
   the test account) → session correctly parked `requires_payment` with
   `verified_payment_amount_paise: None` — **no false completion**.
5. Completed the real order via a validly-HMAC-signed callback (`settle_probe`
   reconciliation):

```
session status          : completed
authorized_amount_paise : 10400
razorpay_order_amount_paise : 10400
verified_payment_amount_paise: 10400
amount_match            : True
decision_receipt        : rcpt_7308671210c549c6 verified
```

**Result:** the C-1 invariant held end-to-end against the live Razorpay order.

> Limitation: the test account's saved card is not merchant-chargeable
> (E-Mandate disabled), so no private captured payment exists; the verified
> amount was bound via `settle_probe`'s fallback to the real order amount —
> identical logic to a real capture. DB backup:
> `data/merchant.db.bak_step14_20260829_111221`.

## 13. Known Remaining Issues (Out of Scope)

- **C-2** — `payment_callback` enforcement of terminal-state rules: the callback
  still relies on the outer `requires_payment` state guard rather than the full
  state machine. **Not changed** (out of scope this phase).
- **C-3** — `_finalize_payment` can be invoked on non-`requires_payment` sessions
  directly (state machine bypass). **Not changed** (out of scope).
- Decision Receipt `outcome` field records the gateway-reported outcome even for
  a binding-declined session (session status remains `declined`); cosmetic, no
  security impact, AP2 semantics untouched.

## 14. Behavior Preservation

- Sim driver behavior preserved (tests green; hermetic sim uses order-amount
  fallback and still completes with `amount_match: True`).
- Log/error-code conventions preserved (`merchant_agent` actor,
  `PAYMENT_BLOCKED`, `checkout_declined`, `merchant` logger names).
- No module/package renames; no public contract changes beyond additive fields
  on a `dict` payment payload.
- All prior 109 tests still pass — no regressions.
