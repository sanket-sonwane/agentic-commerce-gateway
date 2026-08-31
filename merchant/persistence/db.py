"""SQLite engine/session setup (WAL) plus DB-enforced write serialization.

Every mutation runs inside a transaction opened with ``BEGIN IMMEDIATE``, so
SQLite's single-writer (RESERVED) lock is acquired *before* the transaction's
first read instead of lazily at the first write. Concurrent writers in other
threads OR other processes therefore cannot commit between a head-read and its
follow-up write, which is what keeps audit hash-chaining (and money-critical
checkout mutations: create/update/complete/cancel/payment callbacks) correct.
The in-process ``mutation_lock`` remains as a fast-path within a process.
"""
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from merchant.config import settings


class Base(DeclarativeBase):
    pass


def _db_url() -> str:
    db_path = Path(settings.merchant_db_path)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path.as_posix()}"


engine = create_engine(
    _db_url(),
    connect_args={"check_same_thread": False},
    future=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

mutation_lock = threading.RLock()

# Thread-local state for the active mutate() session so a nested/recursive
# mutate() reuses the outer serialized transaction instead of deadlocking on a
# second BEGIN IMMEDIATE.
_current_mutate = threading.local()


def _begin_write(conn, session) -> None:  # noqa: ANN001
    """Open a serialized SQLite write transaction bound to ``conn``.

    SQLAlchemy AUTOCOMMIT isolation disables pysqlite's implicit deferred BEGIN
    on this connection (verified: ``dbapi_connection.isolation_level = None``),
    letting us frame the transaction ourselves. ``BEGIN IMMEDIATE`` acquires the
    single SQLite writer (RESERVED) lock up front and holds it until the matched
    COMMIT/ROLLBACK, so the head-read + write cannot interleave with any other
    writer, in this process or in another process sharing the database file.
    """
    conn.execution_options(isolation_level="AUTOCOMMIT")
    conn.exec_driver_sql("BEGIN IMMEDIATE")
    session.info["_begin_immediate"] = True


@contextmanager
def mutate():
    """Serialize money-critical sections across threads AND processes.

    Yields a session bound to a dedicated connection whose transaction was
    opened with ``BEGIN IMMEDIATE``. The writer lock is released only when this
    block commits (or rolls back), so a concurrent writer wanting the same lock
    waits (bounded by ``PRAGMA busy_timeout``) or fails with an explicit
    ``OperationalError`` -- never by racing a stale read. Nested mutate() calls
    reuse the outer session.
    """
    current = getattr(_current_mutate, "session", None)
    if current is not None:
        _current_mutate.depth += 1
        try:
            yield current
        finally:
            _current_mutate.depth -= 1
        return

    with mutation_lock:
        with engine.connect() as conn:
            session: Session = Session(
                bind=conn, autoflush=False, expire_on_commit=False, future=True
            )
            _begin_write(conn, session)
            _current_mutate.session = session
            _current_mutate.depth = 1
            try:
                yield session
                session.commit()
                conn.exec_driver_sql("COMMIT")
            except Exception:
                try:
                    conn.exec_driver_sql("ROLLBACK")
                except Exception:
                    pass
                session.rollback()
                raise
            finally:
                session.close()
                _current_mutate.session = None
                _current_mutate.depth = 0


def read_only():
    """FastAPI generator dependency yielding a read-only-ish session."""
    session: Session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def init_db() -> None:
    from merchant.persistence.models import entities  # noqa: F401

    Base.metadata.create_all(engine)
    _apply_migrations(engine)


def _apply_migrations(engine) -> None:  # noqa: ANN001
    """Idempotent column additions for pre-existing SQLite databases.

    create_all() only creates missing tables; it never alters existing ones, so
    new optional columns are added here with guarded ADD COLUMN statements.
    """
    import sqlalchemy as sa

    cols = {
        "checkout_sessions": [
            # AP2 columns added after the original live DB was created.
            ("authorized_amount_paise", "INTEGER NULL DEFAULT NULL"),
            ("razorpay_order_amount", "INTEGER NULL DEFAULT NULL"),
            ("verified_payment_amount_paise", "INTEGER NULL DEFAULT NULL"),
            ("execution_mandate_id", "VARCHAR NULL DEFAULT NULL"),
        ],
        "execution_mandates": [
            ("consumed_at", "DATETIME NULL DEFAULT NULL"),
            # Workstream E: optional reference to the user-signed intent assertion.
            ("user_assertion_id", "VARCHAR NULL DEFAULT NULL"),
            ("user_assertion_hash", "VARCHAR NULL DEFAULT NULL"),
        ],
    }
    with engine.begin() as conn:
        inspector = sa.inspect(conn)
        for table, additions in cols.items():
            if not inspector.has_table(table):
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in additions:
                if name not in existing:
                    conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
