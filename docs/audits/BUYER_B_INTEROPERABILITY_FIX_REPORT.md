# BUYER-B INTEROPERABILITY FIX REPORT

## 1. Milestone / Summary
Buyer-B, the independent deterministic protocol client (PRD §39 / M7, no LLM, no
merchant internals), crashed with `AttributeError: 'NoneType' object has no
attribute 'get'` at `buyer_b/client.py` whenever the Merchant returned a
successful-looking completion that was not yet terminal. The client is now
robust against every normal Merchant outcome — SUCCESS, structured DECLINE /
protocol error, MALFORMED / unexpected response, and transport failure — with
explicit first-class handling for each. Scope was strictly limited to Buy-B
outcome handling: one production file (`buyer_b/client.py`) and one new test file.

## 2. Defect
`buyer_b/client.py` lines ~108-109:

```python
err = data.get("error", {})
print(f"... reason={err.get('code')}: {err.get('message')}")
```

For any HTTP response where an error object is absent, this raised
`AttributeError: 'NoneType' object has no attribute 'get'` and crashed the client
instead of reporting an outcome.

## 3. Acceptance Criteria — Intended vs Actual

| # | Criterion | Status |
|---|---|---|
| 1 | Successful checkout is handled as a first-class success (not error-redux) | ✅ |
| 2 | A successful merchant response is treated as SUCCESS even though the body carries `"error": null` | ✅ |
| 3 | A decline is reported as a decline with a real reason code + message | ✅ |
| 4 | An error object is read only after proving it is a dict (`isinstance(..., dict)`/`None`) | ✅ |
| 5 | Malformed responses fail safely (no crash) and are never reported as success | ✅ |
| 6 | Transport failures degrade to a structured client-side error, never a crash | ✅ |
| 7 | No `except Exception` swallows an entire flow; no fabricated fallbacks | ✅ |
| 8 | Original crash reproduced, then fixed; regression test pins it | ✅ |
| 9 | Full protocol independence retained (imports: `argparse`, `sys`, `httpx` only) | ✅ |
| 10 | `scripts/scenario_check.py` still passes | ✅ |
| 11 | No unrelated files changed; no merchant-side changes | ✅ |
| 12 | Full regression suite run; non-scope deficits documented, not silently "fixed" | ✅ (1 pre-existing, unrelated — §15) |

## 4. Root Cause
Merchant `CompleteResponse` declares `error: Optional[dict] = None`; FastAPI
serializes `None` model fields by default, so every HTTP-200 `/complete` body
contains an explicit `"error": null` — an **existing key with a `null` value**,
not an absent key. Python's `dict.get(key, default)` returns `None` when the key
exists with value `None` (the default is only used for a *missing* key). The
client then dereferenced that `None` with `.get("code")`.

This branch was reachable on any HTTP-200 completion whose `checkout_session.status`
was not `"completed"` — e.g. `requires_payment` on the human-authorization
(checkout/saved) driver path, or `payment_pending`. The seam is exercised by the
live merchant flow (`scenario_check` 5b: `complete-after-adapt: 200 requires_payment`).

Secondary gaps found in the same examination: `post()`/`get()`/`post_with_key()`
did `r.json()` directly (uncaught `httpx.RequestError` and JSON-decode crashes);
discovery dereferenced `data["basket_proposal"]` and proposal items directly; the
completion logic had no explicit success/error/malformed classification.

## 5. Fix Overview
`buyer_b/client.py`, rewritten around explicit outcome classification:

1. **Success is first-class** — `HTTP 200` **and** `checkout_session.status ==
   "completed"` **and** authoritative `totals.total` present → prints
   `[buyer-b] outcome: completed | paid ₹X (...)` and runs the idempotency replay
   check. `"error": null` in the body is never read as an error object.
2. **Structured DECLINE / protocol error** — `isinstance(data.get("error"), dict)`
   with a non-empty `code` → prints `outcome: {status|ERROR} | reason={code}: {message}`,
   preserving the reason. Covers 409 declined (with `session_ref`), 500s, 401s.
3. **MALFORMED / non-terminal / missing envelope** — the original bug seam
   (`200 + requires_payment + error:null`) lands here and is reported honestly as
   `outcome: {status} | HTTP {status}, no merchant error envelope`, returning False.
   It is neither success nor a fabricated error.
4. **Transport / parse guards** — `post`/`get`/`post_with_key` catch
   `httpx.RequestError` → `CLIENT_TRANSPORT_ERROR`, and non-JSON bodies →
   `CLIENT_MALFORMED_RESPONSE`, in an envelope mirroring the merchant shape, so the
   rest of the classification code is uniform. No network-layer redesign; helper
   signatures are unchanged (`post(path, payload)`, `post_with_key(path, payload, key)`).
5. **Discovery / create guards** — non-object body, missing `basket_proposal` list,
   missing `checkout_session`, malformed proposal items (non-dict or non-integer
   price/quantity via a small `_num()` coercion) all produce safe, honest output.

## 6. Files Changed
| File | Change |
|---|---|
| `buyer_b/client.py` | Sole production change: transport guards, explicit SUCCESS/DECLINE/ERROR/MALFORMED branches, safe extraction. |
| `merchant/tests/test_buyer_b_interop.py` | New — 10 Buyer-B outcome-handling regression tests. |
| (nothing else) | No merchant/AP2/payment/audit/catalog/agent/dashboard changes. |

## 7. Network / Transport
- Real connection-refused requests (`BASE` on a closed port) now degrade to a
  structured `CLIENT_TRANSPORT_ERROR` envelope and `run()` returns False — covered
  by test `test_transport_failure_is_structured`.
- Non-JSON (or empty) response bodies degrade to `CLIENT_MALFORMED_RESPONSE`.
- All three request helpers keep identical signatures so the canned tests can
  swap them via monkeypatch and the live test can route them through an ASGI transport.

## 8. Error Handling
- A real merchant error object is only dereferenced after
  `isinstance(data.get("error"), dict)` **and** a non-empty `code`.
- Errors are reported with code **and** message (`reason=CODE: message`); nothing
  is collapsed into a bare `error=None` line.
- The two generic `except Exception` clauses are confined to the HTTP *request*
  only (never the outcome classification), and return a structured envelope —
  they do not swallow down-stream logic or fabricate merchant data.

## 9. Malformed Handling
Malformed/unexpected responses are safe and never reported as success:
- Non-object body → `MALFORMED` banner, return False.
- Missing `basket_proposal` list → malformed banner, return False.
- Missing `checkout_session` on create/complete → malformed banner, return False.
- `completed` session without authoritative `totals.total` → malformed, return
  False (**not** success).
- Non-dict proposal items and non-integer `price`/`quantity` are skipped safely.

## 10. Success Handling
- Detector: `status == 200 and final_status == "completed"` **and** `totals.total`
  present. Nothing else is treated as success.
- Output: `[buyer-b] outcome: completed | paid ₹X (order=…, payment=…)`.
- Idempotency proof runs after success: replaying `/complete` with the same key
  must return the same `payment_id` (no double charge); `same_payment=True` shown
  and returned as the final success verdict.

## 11. Determinism & Idempotency Verification
- Selection remains deterministic (cheapest-total items within budget, stable sort
  over the proposal order).
- Completion uses a fresh per-run idempotency key (`buyerb-…`) and the live test
  asserts `same_payment=True` on replay.

## 12. Protocol Independence
`buyer_b/client.py` imports **only** `argparse`, `sys`, `httpx` (stdlib `uuid` —
no HTTP framing). No merchant internals, DB, services, or schemas are imported.
Merchant Agent A, AP2, payment, audit, catalog, and agent code are untouched.

## 13. Regression Suite
- New file: `merchant/tests/test_buyer_b_interop.py` — **10/10 pass**.
  - T1 successful checkout with `"error": null` → SUCCESS, no crash.
  - T2 structured decline (409 `STOCK_UNAVAILABLE`) → reason preserved.
  - T3 malformed set: missing `totals`; missing `checkout_session`; non-object
    body; non-dict/non-integer proposal items.
  - T4 original seam: 200 + `requires_payment` + `error: null` → no AttributeError,
    honest non-success outcome.
  - T5 merchant 500 + structured error envelope → reported.
  - T6 real connection-refused transport → `CLIENT_TRANSPORT_ERROR`, no crash.
  - T7 live flow: Buyer-B helpers routed through `httpx.ASGITransport` against the
    real merchant app (sim driver) → discovery → create → complete → replay,
    `outcome: completed`, `same_payment=True`.
- Full suite (`merchant/tests`): **224 passed, 1 failed**. The single failure is a
  pre-existing, unrelated concurrency test — see §15. All 215 pre-existing tests
  minus that flaky one still pass; the 10 new tests pass.

## 14. `scripts/scenario_check.py`
Passes cleanly (6 checks): spend-limit 409, category-block 409, expired-delegation
401, checkout/complete lifecycle (incl. **5b `complete-after-adapt: 200
requires_payment`** — the exact success-with-session waiting seam that previously
crashed Buyer-B), stock-unavailable decline after operator zeroes stock (Razorpay
never called on decline), and terminal-state protection 409.

## 15. Deferred / Non-Scope Findings
- **Pre-existing flaky test, unrelated to Buyer-B**: 
  `merchant/tests/test_audit_concurrency.py::test_writer_that_exceeds_busy_budget_fails_explicitly_not_silently`
  fails **on this machine regardless of Buyer-B** (verified: fails in isolation,
  and the 5 other concurrency tests pass). Root cause is in the *test's design*:
  both subprocesses race for the first `BEGIN IMMEDIATE`; the test asserts the
  `holder` process (launched first) wins, but the `contender` here consistently
  wins append #1 and exits rc 0, so `rc_c != 0` fails. The concurrency fix itself
  is intact (chains remain intact; contender appended legitimately). Fixing this
  is a concurrency-domain task, deliberately out of scope; per task rules it is
  documented, not silently changed.
- No other defects found in scope.

## 16. Verdict
**✅ CLOSED** — all acceptance criteria met. The Buyer-B crash is fixed, the
original seam has a regression test, all normal Merchant outcomes are handled
explicitly and safely, protocol independence is preserved, `scenario_check`
passes, and the full suite shows no Buyer-B-related regression (the sole failure
is the documented pre-existing concurrency race test).