"""Multi-process audit-chain concurrency regressions.

These spawn real subprocess workers (each with its own engine, connection pool,
and mutation_lock) that append audit events to a shared SQLite file — the shape
that previously produced the stale-head break (live chain: ``seq 28`` carrying
the hash of ``seq 19`` while another process had committed ``seqs 20-27`` in
between). After ``mutate()`` opens every transaction with ``BEGIN IMMEDIATE``,
the SQLite writer (RESERVED) lock is held from the head-read through the insert,
so no other writer can interleave.

Each test uses an isolated database under ``tmp_path`` with a SEPARATE
verification engine in the parent process; the app-wide engine/env is never
touched here.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from merchant.persistence.db import Base
from merchant.persistence.models import entities as models  # noqa: F401  (register metadata on Base)
from merchant.persistence.models.entities import AuditEvent
from merchant.evidence import audit_service

ROOT = Path(__file__).resolve().parents[2]
WORKER = "merchant.tests._audit_mp_worker"


def _make_engine(db_path: Path):
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


@pytest.fixture()
def concurrency_db(tmp_path):
    db_path = tmp_path / "merchant.db"
    engine = _make_engine(db_path)
    Base.metadata.create_all(engine)
    yield db_path, engine
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


def _worker_env(db_path: Path) -> dict:
    env = os.environ.copy()
    env["MERCHANT_DB_PATH"] = str(db_path)
    env["PAYMENT_DRIVER"] = "sim"
    env["ACG_TEST_ALLOW_SIM"] = "1"
    env["RAZORPAY_KEY_ID"] = ""
    env["RAZORPAY_KEY_SECRET"] = ""
    env["MA_LLM_BRAIN"] = "false"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _launch(db_path, *, events, actor, sleep_ms=0, hold_ms=0, busy_timeout=0, barrier=None, hold_signal_file=None):
    cmd = [
        sys.executable,
        "-m",
        WORKER,
        "--db",
        str(db_path),
        "--events",
        str(events),
        "--actor-prefix",
        actor,
        "--sleep-ms",
        str(sleep_ms),
        "--hold-ms",
        str(hold_ms),
        "--busy-timeout",
        str(busy_timeout),
    ]
    if barrier is not None:
        cmd += ["--barrier", str(barrier)]
    if hold_signal_file is not None:
        cmd += ["--hold-signal-file", str(hold_signal_file)]
    return subprocess.Popen(
        cmd,
        env=_worker_env(db_path),
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish(proc, timeout=90):
    out, err = proc.communicate(timeout=timeout)
    return proc.returncode, out, err


def _wait_for_file(path, timeout=30.0):
    """Block until `path` exists or `timeout` elapses (returns bool)."""
    p = Path(path)
    deadline = time.time() + timeout
    while not p.exists():
        if time.time() > deadline:
            return False
        time.sleep(0.01)
    return True


def _chain_snapshot(engine):
    with Session(engine) as s:
        intact, checked, first_broken = audit_service.verify_chain(s)
        events = list(s.execute(select(AuditEvent).order_by(AuditEvent.seq.asc())).scalars())
    links_ok = all(b.prev_hash == a.hash for a, b in zip(events, events[1:]))
    seqs = sorted(e.seq for e in events)
    return intact, checked, first_broken, links_ok, seqs


def test_cross_process_writers_form_an_intact_chain(concurrency_db):
    db_path, engine = concurrency_db
    go = db_path.parent / "go.flag"
    procs = [_launch(db_path, events=12, actor=f"worker-{n}", barrier=go) for n in (1, 2)]
    time.sleep(0.5)
    go.write_text("go")
    for p in procs:
        rc, out, err = _finish(p)
        assert rc == 0, f"worker failed rc={rc}\n{out}\n{err}"

    intact, checked, first_broken, links_ok, seqs = _chain_snapshot(engine)
    assert len(seqs) == 24
    assert seqs == list(range(1, 25))
    assert intact is True
    assert links_ok is True
    assert first_broken is None


def test_high_contention_writers_never_break_the_chain(concurrency_db):
    db_path, engine = concurrency_db
    go = db_path.parent / "go.flag"
    workers = 4
    events_each = 20
    procs = [
        _launch(db_path, events=events_each, actor=f"worker-{n}", sleep_ms=0, barrier=go)
        for n in range(workers)
    ]
    time.sleep(0.5)
    go.write_text("go")
    for p in procs:
        rc, out, err = _finish(p)
        assert rc == 0, f"worker failed rc={rc}\n{out}\n{err}"

    total = workers * events_each
    intact, checked, first_broken, links_ok, seqs = _chain_snapshot(engine)
    assert len(seqs) == total
    assert seqs == list(range(1, total + 1))
    assert intact is True
    assert links_ok is True
    assert first_broken is None


def test_writer_waiting_on_lock_succeeds_without_loss(concurrency_db):
    db_path, engine = concurrency_db
    go = db_path.parent / "go.flag"
    holder = _launch(db_path, events=1, actor="holder", hold_ms=1200, barrier=go)
    contender = _launch(db_path, events=1, actor="contender", barrier=go)
    time.sleep(0.5)
    go.write_text("go")

    rc_h, out_h, err_h = _finish(holder)
    rc_c, out_c, err_c = _finish(contender)
    assert rc_h == 0, f"holder failed rc={rc_h}\n{out_h}\n{err_h}"
    assert rc_c == 0, f"contender failed rc={rc_c}\n{out_c}\n{err_c}"

    intact, checked, first_broken, links_ok, seqs = _chain_snapshot(engine)
    assert len(seqs) == 3
    assert seqs == [1, 2, 3]
    assert intact is True
    assert links_ok is True
    assert first_broken is None


def test_writer_that_exceeds_busy_budget_fails_explicitly_not_silently(concurrency_db):
    """Deterministic lock-contention timeout.

    The holder is started first and signals, via an IPC file written INSIDE its
    BEGIN IMMEDIATE transaction, that it actually owns the SQLite writer lock.
    Only once that signal is observed does the contender launch. Because the
    holder provably holds the RESERVED lock before the contender exists, the
    contender deterministically blocks on BEGIN IMMEDIATE and must time out when
    its busy_timeout budget is intentionally too small. This never relies on
    process launch order, sleep duration, or "holder starts first".
    """
    db_path, engine = concurrency_db
    held_flag = db_path.parent / "holder.held"
    holder = _launch(
        db_path,
        events=1,
        actor="holder",
        hold_ms=1500,
        hold_signal_file=held_flag,
    )
    # 1. Proof the holder owns the lock before the contender is even started.
    assert _wait_for_file(held_flag), "holder never signalled that it owns the write lock"

    contender = _launch(db_path, events=1, actor="contender", busy_timeout=200)

    rc_h, out_h, err_h = _finish(holder)
    rc_c, out_c, err_c = _finish(contender, timeout=30)

    # 3/4. Contender fails EXPLICITLY (budget exceeded); holder commits.
    assert rc_h == 0, f"holder failed rc={rc_h}\n{out_h}\n{err_h}"
    assert rc_c != 0, f"contender unexpectedly succeeded:\n{out_c}\n{err_c}"
    assert "database is locked" in (out_c + err_c).lower()

    # 5/6. Chain stays intact; no audit event silently lost (both holder events
    # committed, contender added none — its transaction rolled back).
    intact, checked, first_broken, links_ok, seqs = _chain_snapshot(engine)
    assert len(seqs) == 2
    assert seqs == [1, 2]
    assert intact is True
    assert links_ok is True
    assert first_broken is None


def test_append_outside_serialized_write_transaction_is_rejected():
    from merchant.persistence.db import SessionLocal

    with SessionLocal() as db:
        with pytest.raises(RuntimeError, match="serialized write transaction"):
            audit_service.append_event(db, actor="rogue", action="should_not_slip_in")


def test_nested_mutate_reuses_the_outer_transaction():
    from merchant.persistence.db import SessionLocal, mutate

    with mutate() as outer:
        with mutate() as inner:
            assert inner is outer
        audit_service.append_event(outer, actor="nested", action="outer_only")

    with SessionLocal() as db:
        events = list(db.execute(select(AuditEvent).order_by(AuditEvent.seq.asc())).scalars())
        intact, checked, first_broken = audit_service.verify_chain(db)
    assert len(events) == 1
    assert intact is True
    assert first_broken is None
    assert checked == 1