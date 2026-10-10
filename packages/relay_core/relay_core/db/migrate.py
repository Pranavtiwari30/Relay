"""Programmatic Alembic entry point (``relay-migrate``).

Locates the migration scripts relative to this package so it works from an
installed wheel or container, not only from the repository root.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(database_url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if database_url:
        config.attributes["database_url"] = database_url
    return config


def upgrade(database_url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(database_url: str | None = None, revision: str = "base") -> None:
    command.downgrade(alembic_config(database_url), revision)


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Relay database migrations")
    parser.add_argument(
        "action", choices=["upgrade", "downgrade", "current"], nargs="?", default="upgrade"
    )
    parser.add_argument("revision", nargs="?")
    args = parser.parse_args()
    if args.action == "upgrade":
        upgrade(revision=args.revision or "head")
    elif args.action == "downgrade":
        if not args.revision:
            parser.error("downgrade requires an explicit revision (for example '-1' or 'base')")
        downgrade(revision=args.revision)
    else:
        command.current(alembic_config(), verbose=True)


if __name__ == "__main__":
    main()
