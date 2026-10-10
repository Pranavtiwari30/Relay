"""Engine and session factory.

Sessions never autocommit: application services decide where a transaction
ends, because those boundaries are what the reliability guarantees rest on.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from relay_core.config import get_settings

SessionFactory = sessionmaker[Session]


def utcnow() -> datetime:
    return datetime.now(UTC)


def build_engine(database_url: str, *, pool_size: int = 5) -> Engine:
    return create_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=5,
        # Serverless Postgres (and Cloud Run idling) drops idle connections.
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={"connect_timeout": 10, "application_name": "relay"},
    )


def build_session_factory(engine: Engine) -> SessionFactory:
    return sessionmaker(engine, expire_on_commit=False, autoflush=False)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return build_engine(settings.database_url, pool_size=settings.db_pool_size)


@lru_cache(maxsize=1)
def get_session_factory() -> SessionFactory:
    return build_session_factory(get_engine())


def reset_engine_cache() -> None:
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@contextmanager
def session_scope(factory: SessionFactory) -> Iterator[Session]:
    """One transaction: commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def database_is_reachable(factory: SessionFactory) -> bool:
    try:
        with factory() as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
