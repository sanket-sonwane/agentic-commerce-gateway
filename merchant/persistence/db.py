"""SQLite engine/session setup (WAL) plus a process-wide mutation lock.

The lock serializes money-critical checkout mutations (create/update/complete/
cancel/payment callbacks). This is a deliberate MVP simplification: SQLite WAL
gives safe concurrent reads, and the lock gives serialized writes so reservation
accounting and idempotency stay correct during the demo.
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


@contextmanager
def mutate():
    """Serialize money-critical sections."""
    with mutation_lock:
        session: Session = SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


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
            ("execution_mandate_id", "VARCHAR NULL DEFAULT NULL"),
        ],
        "execution_mandates": [("consumed_at", "DATETIME NULL DEFAULT NULL")],
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
