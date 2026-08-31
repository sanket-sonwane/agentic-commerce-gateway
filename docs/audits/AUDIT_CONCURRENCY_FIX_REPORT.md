# AUDIT CONCURRENCY FIX REPORT

**Date:** 2026-08-30
**Scope:** One live audit finding — audit hash-chain concurrency integrity.
**Verdict:** ✅ **CLOSED**

---

## 1. Inspection of the full audit-append path

All `append_event` call sites were enumerated (`grep -rn "append_event(" `1).

- **Production (45 call sites):** `checkout_service.py` (37), `api/checkout.py` (2), `discovery_service.py` (2), `api/admin.py` (2), `api/provenance.py` (1), `agent/loop.py` (1).
- Every production call site runs inside `with mutate()` — none on a bare `SessionLocal()`, none on the `read_only()` dependency, none in a background thread/MQ/BackgroundTask.
- Read-tools (`merchant/agent/tools.py:870`) open their own `SessionLocal()` but never append audit events — confirmed safe, left unchanged.
- The **only unsafe primitive** was therefore `mutate()` itself: it opened a *deferred* transaction.

Root cause (confirmed live and re-derived): `mutate()` used `SessionLocal()` under the default pysqlite legacy isolation, so the SQLite BEGIN was emitted lazily by the driver *at the first DML* — i.e. **after** `append_event`'s head `SELECT`. In WAL mode another writer could COMMIT between the head-read and the INSERT, so `prev_hash` could reference a head that was no longer the last committed event. Observed broken chain: `/audit_logs/verify` → `intact:false, first_broken_seq:28`; `seq 28` carried the hash of `seq 19` while another process had committed `seqs 20–27` in between (leftover uvicorn workers, each with its own process-local `mutate` RLock).

## 2. Transaction model

- Engine config unchanged: `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `check_same_thread=False` (`merchant/persistence/db.py`).
- Before: `mutate()` → `SessionLocal()` → deferred BEGIN (driver-issued at first write) → head-read + INSERT in the same txn, but the writer lock was taken only at the INSERT.
- After: `mutate()` creates its **own** connection (`engine.connect()`), sets `execution_options(isolation_level="AUTOCOMMIT")` (disables pysqlite's implicit deferred BEGIN on that one connection), then opens the transaction with `exec_driver_sql("BEGIN IMMEDIATE")` **before any read**.

## 3. Serialization point

The serialization point is the **database's own single-writer lock**, taken up front by `BEGIN IMMEDIATE` (RESERVED lock acquired before the transaction's first statement). This is DB-enforced and therefore process-independent: the `threading.RLock` (`db.py`) still fast-paths serialization within a process but is **no longer the correctness mechanism across processes**.

Choice rationale:
- `isolation_level="SERIALIZABLE"` was **rejected** — verified in the installed SQLAlchemy 2.0.36 that the sqlite dialect maps SERIALIZABLE to `PRAGMA read_uncommitted=0` + legacy `isolation_level=""` (`pysqlite.py:537-542`, `base.py:2054-2058`); it does **not** emit `BEGIN IMMEDIATE`.
- The documented pysqlite recipe (dialect docstring `pysqlite.py:362-399`) is to disable the driver's implicit BEGIN and emit BEGIN yourself. Scoping that to `mutate()` only (not engine-wide) avoids changing read-path / seed / migration behavior.

## 4. Atomic append implementation

`mutate()` (`merchant/persistence/db.py`):

```
with mutation_lock:
    with engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.exec_driver_sql("BEGIN IMMEDIATE")     # writer lock held from here
        session = Session(bind=conn, ...)
        session.info["_begin_immediate"] = True       # guard marker
        yield session
        session.commit()          # SA-level flush/state; DBAPI no-op in autocommit mode
        conn.exec_driver_sql("COMMIT")                # releases the writer lock
  on error: exec_driver_sql("ROLLBACK") (guarded) + session.rollback(), re-raise
```

Because the RESERVED lock is held from the head-read through the INSERT/COMMIT, **no other writer (thread or process) can commit between the head-read and the append** — `prev_hash` always equals the hash of the last committed predecessor. Verified empirically (smoke probe, temp DB): uncommitted rows invisible to other connections, `session.commit()` is a no-op at the DBAPI in autocommit mode (hence the explicit `COMMIT`), rollback discards, pool return restores legacy isolation (no state leak).

## 5. Hash-chain semantics preserved

Unchanged: event canonicalization (`_canonical`), `SHA256(canonical + prev_hash)`, sequence assignment, `event_id`, timestamps, `verify_chain`, `signed_head_anchor`, AP2 signing. Only transaction framing changed. `verify_chain` and the anchor API are byte-identical.

## 6. Unsafe append paths removed (one mechanism, enforced)

- `append_event` now opens with a hard guard (`merchant/evidence/audit_service.py`): raises `RuntimeError` unless `db.info["_begin_immediate"]` is set (i.e. the session came from a serialized `mutate()`). A future non-serialized append fails loudly instead of silently racing.
- Nested `mutate()` calls reuse the outer serialized session (thread-local guard) instead of deadlocking on a second `BEGIN IMMEDIATE`.
- During regression the guard caught one real non-conformant test call (`test_discovery_fallback.py:25` called `discover()` with a raw `SessionLocal()`); it now runs inside `with mutate()` and the redundant raw-session wrapper was removed. The two direct appends in `test_consumer_projection.py` were verified to already run inside `with mutate()`.

## 7. Multi-process regression test

`merchant/tests/test_audit_concurrency.py::test_cross_process_writers_form_an_intact_chain` — spawns 2 real subprocess workers (`python -m merchant.tests._audit_mp_worker`, each with its own engine/pool/RLock, `MERCHANT_DB_PATH` set before import), synchronized start (`go` barrier), 12 appends each. Asserts 24 events, sequential seqs 1–24, `verify_chain` intact, `first_broken_seq is None`, and an explicit per-link `prev_hash == previous hash` walk. **Passing.**

## 8. High-contention test

`test_high_contention_writers_never_break_the_chain` — 4 workers × 20 appends, zero jitter. Asserts 80 events, seqs 1–80, intact chain, links valid. **Passing.**

## 9. Lock / failure handling

- Second writer while the lock is held **blocks** at its `BEGIN IMMEDIATE`, honored by `PRAGMA busy_timeout` (default 5 s).
- If the wait exceeds the budget, SQLite raises `OperationalError: database is locked`, which propagates through `mutate()` as an explicit, structured failure (never a silent retry or drop). Money transactions are never blindly retried (side-effect risk).
- `test_writer_waiting_on_lock_succeeds_without_loss` (holder holds 1.2 s; contender waits then succeeds; 3 events, chain intact) and `test_writer_that_exceeds_busy_budget_fails_explicitly_not_silently` (contender with a 200 ms busy budget fails clearly with `database is locked`, holder commits, 2 events, chain intact) both **passing**.
- Note (documented, accepted): because `mutate()` uses autocommit-isolated connections, an uncommitted `BEGIN IMMEDIATE` is a no-op at the DBAPI `rollback()` on pool return; `mutate()` therefore always balances BEGIN with an explicit `COMMIT`/`ROLLBACK`, and the nested-reuse guard prevents a second BEGIN from ever opening on a new connection inside an active `mutate()`.

## 10. Single-writer regression

- `test_append_outside_serialized_write_transaction_is_rejected` (raw session → `RuntimeError`) and `test_nested_mutate_reuses_the_outer_transaction` (single event, intact) **passing**.
- The full single-process suite (207 tests, audit + AP2 + provenance + checkout + agents) passes, exercising `mutate()` in every non-read path.

## 11. Money-path regression

All C-1 (`test_c1_amount_binding`), C-2/C-3 (`test_c2_c3_state_security`), checkout/saved-driver/idempotency/state-machine/inventory, gating, discovery-fallback, H1–H3, e2e, merchant-agent, decision-loop, and agent-handoff suites pass unchanged (**105 + 95 targeted + 207 full**, overlapping). No money-path semantics changed: single-writer serialization is identical, just acquired earlier, from DB state that cannot be torn.

## 12. Admin reset / append-only — DEFERRED (not solved here)

`POST /admin/demo/reset` (`merchant/api/admin.py:81-82`) deletes every `AuditEvent` row and re-anchors the chain. **Deliberately out of scope** for this fix (next planned change). No claim of append-only or reset-immutability is made in docs. This report records it as the outstanding integrity finding.

> **RESOLVED** — now dated, see `docs/audits/AUDIT_RESET_APPEND_ONLY_FIX_REPORT.md`: reset no longer deletes `AuditEvent` rows; it records `demo_reset_requested`/`demo_reset_completed` inside the serialized transaction and preserves the chain.

## 13. Minimal documentation

- `merchant/persistence/db.py` module docstring rewritten: DB-enforced writer serialization (`BEGIN IMMEDIATE`) across threads and processes; RLock kept as in-process fast path.
- `merchant/evidence/audit_service.py` module docstring + `append_event` now state appends must run inside `with mutate()`.
- No README/user-doc claims updated — intentionally deferred until this report closes the fix.

## 14. Code quality

- 2 production files changed (`db.py`, `audit_service.py`) + 2 new subprocess/test files + 1 test-normalization edit (`test_discovery_fallback.py`). No generic concurrency utility, no broad refactor.
- `mutate()` unchanged in call signature and yield semantics; no API surface change.
- New tests are hermetic (per-test temp DB, no contact with the shared app engine/DB), deterministic, bounded (~28 s worst case), and clean up their files.

## 15. Security principle

The fix **prevents** the race at the database layer rather than hiding it: concurrent writers cannot produce an invalid chain, and any path that could bypass serialization now fails loudly at `append_event`. No reduction in money-flow guarantees: all checkout/complete/payment mutations still run in the same serialized transactions as before this change. Standing findings recorded previously (Buyer-B `client.py:109` crash, dashboard duplicates/missing `payment_callback`, reservation lifecycle, recurring-charge 404, admin reset) are out of scope and unchanged.

## 16. Acceptance criteria

- [x] Every production `append_event` runs in a transaction opened with `BEGIN IMMEDIATE` (writer lock held before any read).
- [x] Cross-process writers (2 × 12) form an intact chain — no `first_broken`; per-link prev_hash valid.
- [x] High-contention writers (4 × 20) form an intact chain — no loss, seq 1..80.
- [x] Lock waiting is bounded and succeeds within budget; exceeding budget fails with an explicit `OperationalError`, no silent drop, committed chain intact.
- [x] All non-serialized append paths removed or rejected by the hard guard.
- [x] Hash formula / seq / verify / anchor semantics preserved (byte-identical).
- [x] Single-writer, C-1, C-2/C-3, AP2, provenance, checkout, agent regressions green.
- [x] Full suite green: **207 passed** (201 prior + 6 new).
- [x] Admin reset documented as DEFERRED; no docs claim reset-immutability.
- [x] No unrelated modules or behaviors changed.

**Verdict: ✅ CLOSED** — the audit hash-chain is now serialized at the database layer and the previously observed multi-process break cannot reproduce.