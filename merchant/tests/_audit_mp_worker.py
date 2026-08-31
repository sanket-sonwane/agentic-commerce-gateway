"""Subprocess audit-chain writer for the concurrency regression suite.

Each invocation is a fresh Python process with its own SQLAlchemy engine,
connection pool, and mutation_lock writing to a shared SQLite file — exactly
the deployment shape that previously raced (one mutation_lock per uvicorn
process, so a stale head-read could produce a broken audit chain).

``MERCHANT_DB_PATH`` is taken from ``--db`` and exported BEFORE merchant is
imported so the engine binds to the target file. Merchant is never imported at
module scope (package ``__init__`` files are empty), which keeps
``python -m merchant.tests._audit_mp_worker`` import-time safe; only ``main()``
imports it, after the environment is set.
"""
import argparse
import os
import random
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="absolute path to the shared merchant.db")
    parser.add_argument("--events", type=int, default=1, help="number of audit appends")
    parser.add_argument("--actor-prefix", required=True, help="audit actor prefix for this process")
    parser.add_argument("--sleep-ms", type=int, default=0, help="max random jitter between appends (ms)")
    parser.add_argument("--hold-ms", type=int, default=0, help="hold the write lock this long after the last append (ms)")
    parser.add_argument("--busy-timeout", type=int, default=0, help="override PRAGMA busy_timeout (ms); 0 keeps the default")
    parser.add_argument("--barrier", default=None, help="file to wait for before starting")
    parser.add_argument(
        "--hold-signal-file",
        default=None,
        help="optional path: after acquiring a serialized write lock, write this "
        "file (proving BEGIN IMMEDIATE is held) then sleep --hold-ms before commit",
    )
    args = parser.parse_args()

    os.environ["MERCHANT_DB_PATH"] = str(Path(args.db).resolve())

    from merchant.persistence import db as dbmod
    from merchant.evidence import audit_service

    if args.busy_timeout:
        from sqlalchemy import event

        @event.listens_for(dbmod.engine, "connect")
        def _override_busy_timeout(dbapi_connection, connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute(f"PRAGMA busy_timeout={int(args.busy_timeout)}")
            cursor.close()

    if args.barrier:
        barrier = Path(args.barrier)
        deadline = time.time() + 30.0
        while not barrier.exists():
            if time.time() > deadline:
                print("worker timed out waiting for barrier", file=sys.stderr)
                return 2
            time.sleep(0.01)

    count = 0
    for i in range(args.events):
        count = i + 1
        with dbmod.mutate() as db:
            audit_service.append_event(
                db,
                actor=args.actor_prefix,
                action="worker_append",
                payload={"worker": args.actor_prefix, "n": count},
            )
        if args.sleep_ms:
            time.sleep(random.random() * args.sleep_ms / 1000.0)

    if args.hold_ms:
        with dbmod.mutate() as db:
            if args.hold_signal_file:
                # Deterministic lock-owned signal: the BEGIN IMMEDIATE writer lock
                # (RESERVED) is held from the moment this mutate() block opens.
                # Writing the signal file here proves the holder owns the lock
                # before the caller releases the contender, so the contender is
                # guaranteed to wait on (or time out acquiring) the SAME lock.
                Path(args.hold_signal_file).write_text("held", encoding="utf-8")
            audit_service.append_event(
                db,
                actor=args.actor_prefix,
                action="worker_hold",
                payload={"worker": args.actor_prefix, "n": "hold"},
            )
            time.sleep(args.hold_ms / 1000.0)

    print(f"worker {args.actor_prefix}: appended {count} events", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())