from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from relay_core.db import migrate
from relay_core.db.models import Base
from relay_core.db.session import SessionFactory, build_engine, build_session_factory
from tests.support import Harness, StubDestination

TEST_DATABASE_URL = os.environ.get(
    "RELAY_TEST_DATABASE_URL", "postgresql+psycopg://relay@127.0.0.1:54329/relay_test"
)

DB_DIRS = ("integration", "e2e", "contract")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        parts = item.nodeid.split("/")
        if len(parts) > 1 and parts[1] in ("integration", "e2e", "contract", "security"):
            item.add_marker(getattr(pytest.mark, parts[1]))


@pytest.fixture(scope="session")
def database_url() -> str:
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    """A freshly migrated database. Migrations — not ``create_all`` — build the schema,
    so every database-backed test also exercises the real migration scripts."""
    if "test" not in database_url.rsplit("/", 1)[-1]:
        pytest.exit(
            f"refusing to run destructive tests against '{database_url}': database name must contain 'test'"
        )
    engine = build_engine(database_url, pool_size=10)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    except Exception as exc:
        pytest.exit(
            f"cannot reach the test database at {database_url} ({type(exc).__name__}). "
            "Start it with 'make db-up' or set RELAY_TEST_DATABASE_URL.",
            returncode=2,
        )
    migrate.upgrade(database_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def session_factory_session(engine: Engine) -> SessionFactory:
    return build_session_factory(engine)


@pytest.fixture
def session_factory(engine: Engine, session_factory_session: SessionFactory) -> SessionFactory:
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return session_factory_session


@pytest.fixture(scope="session")
def destination_server() -> Iterator[StubDestination]:
    server = StubDestination()
    yield server
    server.close()


@pytest.fixture
def destination(destination_server: StubDestination) -> StubDestination:
    destination_server.reset()
    return destination_server


@pytest.fixture
def harness(
    session_factory: SessionFactory, database_url: str, destination: StubDestination
) -> Iterator[Harness]:
    h = Harness(session_factory, database_url, destination)
    h.seed()
    yield h
    h.http_adapter.close()
