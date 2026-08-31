# C-2 / C-3 FIX REPORT

**Project:** Agentic Commerce Gateway (ACG) — Razorpy AI Buildathon
**Phase:** 2 — Critical Payment-State Security Fixes
**Defects:** C-2 (callback terminal-state bypass), C-3 (finalizer state-machine bypass)
**Status:** ✅ CLOSED and verified (automated suite + live integration)

---

## 1. Root Cause — C-2

The payment callback (`merchant/api/checkout.py::payment_callback`) reached
`_finalize_payment()` with **no independent guarantee** that the checkout's own
state machine still permitted finalization. A valid Razorpay HMAC signature
proves something about the *payment event*, not that the *checkout session* is
still legally eligible (a previously valid session may have been cancelled,
declined, or already completed).

Inspection showed a real-time state guard already existed at
`api/checkout.py:201` (`cs.status != "requires_payment"` → reject) from prior
AP2 hardening (commit `c01bb96d`), so the API layer rejected
cancelled/declined/completed sessions. However:
- There was **no defense-in-depth**: the guard lived only in the caller, and
  `_finalize_payment()` itself (the money-critical sink) did not independently
  validate state.
- A direct call, a replay through another entry point, or a future caller that
  forgot the guard could still drive `cancelled/declined/completed -> completed`,
  which would re-consume stock, re-issue a receipt, and re-mark `completed`
  (duplicate charge / stock corruption).

## 2. Root Cause — C-3

`_finalize_payment()` (`merchant/domain/checkout_service.py`) branches purely on
`outcome.status` and **never asserted `cs.status`**. It unconditionally:
- for a `completed` outcome → consume stock, mark `completed`, emit
  `payment_success`, issue a Decision Receipt;
- for a `pending` outcome → mark `payment_pending`;
- else → mark `declined`.

Terminal states had **no protection at all** inside the finalizer. Relying
exclusively on callers behaving correctly violates defense-in-depth: the outer
callers (complete/callback/reconcile) each had their own check, but the
finalizer itself was the single unprotected choke point for money-critical
effects.

## 3. Files Changed

| File | Change |
|---|---|
| `merchant/domain/checkout_service.py` | Added `_assert_finalizable_state()` helper (C-3): independent terminal-state + `VALID_TRANSITIONS` check. `_finalize_payment()` now calls it first, before any mutation. |
| `merchant/tests/test_c2_c3_state_security.py` | **New** — 26 tests (C-2, C-3, reconcile, idempotency, mismatch, adversarial sequences). |

Localized. No changes to `api/checkout.py`, the state-machine definition,
`VALID_TRANSITIONS`, AP2, agents, pricing, stock algorithm, crypto, protocol,
or directory structure. C-1 amount binding untouched.

## 4. State Machine Before

```python
VALID_TRANSITIONS = {
  "created":              {"requires_payment","declined","cancelled"},
  "requires_payment":     {"requires_confirmation","payment_pending","completed","declined","cancelled"},
  "requires_confirmation":{"requires_payment","declined","cancelled"},
  "payment_pending":      {"completed","declined"},
  "completed": set(), "declined": set(), "cancelled": set(),
}
TERMINAL_STATES = {"completed","declined","cancelled"}
```
The machine was correct but the finalizer did not enforce it internally.

## 5. State Machine After

The `VALID_TRANSITIONS` definition is **unchanged**. Enforcement is now layered:

- `_finalize_payment()` maps `outcome.status -> target state`:
  `completed -> completed`, `pending -> payment_pending`, `declined -> declined`.
- If `cs.status in TERMINAL_STATES` → reject (`INVALID_STATE_TRANSITION`).
- If the target is not reachable from `cs.status` per `VALID_TRANSITIONS` → reject.
- Special case preserved: `payment_pending -> payment_pending` (a still-unresolved
  reconciliation no-op) is allowed — it does not mutate a terminal state.

## 6. Callback Flow Before

```
POST /payment_callback
  order match? -> reject on mismatch
  state == requires_payment? -> reject otherwise        (API guard)
  signature valid? -> reject otherwise
  C-1 amount binding (settle_probe) -> PAYMENT_BLOCKED on mismatch
  finalize_with_outcome -> _finalize_payment()           (NO internal state check)
```
A cancelled/declined/completed session was rejected only because the API guard
happened to be present; the finalizer itself would not have stopped a bypass.

## 7. Callback Flow After

```
POST /payment_callback
  order match? -> reject on mismatch            (order/session binding)
  state == requires_payment? -> reject otherwise (API guard)
  signature valid? -> reject otherwise           (gateway auth)
  C-1 amount binding -> PAYMENT_BLOCKED on mismatch
  finalize_with_outcome -> _finalize_payment()
      _assert_finalizable_state(cs, outcome)     (C-3: LAST line of defense)
```

## 8. Finalizer Protection (C-3)

`_finalize_payment()` now starts with `_assert_finalizable_state(db, cs, outcome)`:

- reuses the existing, authoritative `_assert_transition`/`VALID_TRANSITIONS`
  (no second competing state machine);
- rejects **terminal** sessions outright (no stock/reservation/status change,
  no receipt, no success audit — the irreversible steps are all downstream);
- validates `outcome.status` maps to a known target (rejects unknown outcomes);
- preserves the legitimate `payment_pending -> payment_pending` reconciliation
  no-op;
- per Step 7, the guard runs **before** any irreversible operation, so invalid
  finalization never consumes stock, corrupts reservations, writes receipts, or
  emits a success audit.

## 9. C-1 Interaction

C-1 (amount binding) is fully preserved. The guard only checks **state**; the
C-1 `authorized == order == verified` check inside `_finalize_payment` is
unchanged and still runs after the state guard. Test `test_c3_requires_payment_amount_mismatch_still_fails_closed`
proves a mismatched amount on a legal-state direct call still declines
(`PAYMENT_BLOCKED`). The complete security chain remains:

```
STATE VALID (C-2/C-3)
   -> AMOUNT VALID (C-1)
   -> PAYMENT VALID
   -> FINALIZE
```

## 10. Security Impact

- `cancelled -> completed`, `declined -> completed`, `completed -> completed`
  (again), and `created -> completed` are all now impossible, both via callback
  and via any direct call into the finalizer.
- A Razorpay signature alone is **not** sufficient authority: the checkout state
  machine must also permit the transition (verified by `test_c2_5_valid_signature_but_wrong_state_is_rejected`).
- No duplicate charge: a completed session can never be finalized again
  (no second order/payment/stock/receipt).
- No stock/reservation corruption: the guard runs before stock and reservation
  mutation, so an invalid finalization leaves stock and reservations intact.
- Order/session binding: a callback for a foreign order is rejected before
  finalization.

## 11. Tests Added

`merchant/tests/test_c2_c3_state_security.py` (26 tests):

**C-2 (callback, via API):**
- `test_c2_1_cancelled_session_callback_never_completes`
- `test_c2_2_declined_session_callback_never_completes`
- `test_c2_3_completed_session_duplicate_callback_is_safe`
- `test_c2_4_invalid_non_completable_state_callback_rejected`
- `test_c2_5_valid_signature_but_wrong_state_is_rejected`
- `test_c2_order_and_session_mismatch_rejected`

**C-3 (direct finalizer calls, bypassing /complete + callback):**
- valid `requires_payment -> completed`
- `requires_payment` amount mismatch → still `PAYMENT_BLOCKED` (C-1 preserved)
- terminal states (`cancelled`/`declined`/`completed`) → rejected, no stock/receipt/success audit
- non-completable states (`created`, `requires_confirmation`) → rejected
- `payment_pending -> completed` (legit reconcile) succeeds
- `payment_pending -> payment_pending` (reconcile no-op) preserved

**Reconciliation (`/reconcile`):**
- `payment_pending -> completed` via API succeeds
- `cancelled -> reconcile` stays cancelled
- `declined -> reconcile` stays declined

**Idempotency:** terminal session + new idempotency key → no new payment/order.

**Adversarial sequences (Step 15 A–F):** documented and asserted below (§13).

## 12. Test Results

```
merchant/tests/test_c2_c3_state_security.py : 26 passed
merchant/tests                            : 143 passed  (117 baseline + 26 new)
scripts.scenario_check                    : EXIT=0      (all scenario groups pass)
```
No regressions. C-1 tests (8), AP2 tests, idempotency, checkout, state-machine,
saved-driver, inventory, e2e, agent tests all still green.

## 13. Adversarial Callback Results (Step 15)

| Seq | Flow | FINAL | RAZORPAY | STOCK | RESERVATION | AUDIT |
|---|---|---|---|---|---|---|
| A | requires_payment → callback → callback again | completed | 1 settle | consumed once | consumed once | 1× payment_success, 2nd rejected |
| B | requires_payment → cancel → callback | cancelled | none (order parked) | unchanged | released | no payment_success |
| C | requires_payment → callback → cancel → callback | completed | 1 settle | consumed once | consumed once | cancel rejected (terminal), dup callback rejected; 1× payment_success |
| D | requires_payment → payment_pending → cancel → reconcile | cancelled | n/a | unchanged | released | reconcile rejected; stays cancelled |
| E | declined → callback | declined | none | unchanged | released | no payment_success |
| F | completed → callback | completed | none (no 2nd charge) | unchanged (no 2nd use) | consumed once | dup callback rejected; 1× payment_success |

Verified by `test_seq_A..F`. Live integration on the real DB additionally
confirmed Scenario B (cancelled stays cancelled, callback→409) and Scenario C /
duplicate-callback (completed stays completed, duplicate→409).

## 14. Reconciliation Results

- `payment_pending -> completed` reconcile: allowed (state machine permits it);
  verified direct + via `/reconcile`.
- `payment_pending -> payment_pending` (gateway still unresolved): preserved
  no-op, returns `PAYMENT_PENDING`.
- `cancelled -> reconcile` (even a "captured" payment): **must not** become
  completed — verified → stays `cancelled`.
- `declined -> reconcile`: stays `declined`.
- No back door: `/reconcile` respects the same authoritative transition rules.

## 15. Regression Results

- Baseline: **117 passing**.
- After fix: **143 passing** (117 + 26 new), `scenario_check` exit 0.
- Full suite includes prior C-1 (8), C-2/C-3 (26), AP2 provenance, AP2 fixes,
  state machine, checkout flow, saved driver, idempotency, inventory, e2e, gating,
  merchant agent, agent handoff, discovery.

## 16. Remaining Known Issues

- **C-2/C-3 advisory on audit evidence:** Step 3 suggests emitting structured
  denial audit evidence. The architecture's `mutate()` context **rolls back on
  exception** (`db.py:59-60`), and the callback/complete wrappers `raise` the
  `INVALID_STATE_TRANSITION` while still inside `with mutate()`. Appending a
  "denied" audit before raising would be rolled back, so none is persisted. This
  is correct fail-closed behavior (no *successful* audit/receipt is written), but
  a persistent denied-event would require a separate committed transaction — not
  added to keep the change minimal and avoid inventing a second state machine.
- **H-1, H-2, H-5, H-6, H-7, H-8** and any other defects found were **not**
  fixed; they are out of scope per the task.

## 17. Confirmation That No Unrelated Bugs Were Fixed

Only C-2 and C-3 were addressed. The sole production-code change is the C-3
`_assert_finalizable_state()` guard + its call at the top of `_finalize_payment()`
in `merchant/domain/checkout_service.py`. C-2 relied on the existing caller-side
guard (verified) plus the new finalizer defense-in-depth. No changes were made
to: Buyer Agent, Merchant Agent reasoning, AP2 mandate design, cryptographic
algorithms, directory structure, catalog, pricing, inventory reservation
algorithm, UI, protocol schemas, or the C-1 amount-binding implementation.
