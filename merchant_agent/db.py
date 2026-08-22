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

from merchant_agent.config import settings


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
    from merchant_agent.models import entities  # noqa: F401

    Base.metadata.create_all(engine)
