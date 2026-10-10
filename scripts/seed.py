"""Seed the demo workspace: `make seed`.

Idempotent. Creates configuration only (workspace, accounts, two connections,
one integration). It does not insert events: send some with the simulator.
Account passwords come from the environment and are never printed.
"""

from __future__ import annotations

import os
import sys

from relay_core.adapters.registry import build_default_registry
from relay_core.config import get_settings
from relay_core.db.session import get_session_factory, session_scope, utcnow
from relay_core.domain.roles import Role
from relay_core.seed import SeedAccount, seed_demo
from relay_core.services.secrets import EnvSecretResolver


def accounts() -> list[SeedAccount]:
    found = []
    for role, label in (
        (Role.ADMIN, "ADMIN"),
        (Role.OPERATOR, "OPERATOR"),
        (Role.VIEWER, "VIEWER"),
    ):
        email = os.environ.get(f"RELAY_SEED_{label}_EMAIL")
        password = os.environ.get(f"RELAY_SEED_{label}_PASSWORD")
        if email and password:
            found.append(SeedAccount(email, password, role, f"Demo {label.title()}"))
    return found


def main() -> int:
    seed_accounts = accounts()
    if not any(account.role is Role.ADMIN for account in seed_accounts):
        print(
            "RELAY_SEED_ADMIN_EMAIL and RELAY_SEED_ADMIN_PASSWORD must be set. Run `make setup` to create .env.",
            file=sys.stderr,
        )
        return 1
    settings = get_settings()
    with session_scope(get_session_factory()) as session:
        result = seed_demo(
            session,
            accounts=seed_accounts,
            registry=build_default_registry(settings),
            secrets=EnvSecretResolver(),
            now=utcnow(),
        )
    for note in result.notes or ["nothing to do: demo workspace already seeded"]:
        print(f"  - {note}")
    print(f"Demo workspace:  {result.workspace_id}")
    print(f"Integration:     {result.integration_id} ({result.integration_status})")
    print(f"Webhook path:    /webhooks/{result.source_connection_id}")
    print(
        "Sign in with:    "
        + ", ".join(a.email for a in seed_accounts)
        + "  (passwords are in .env)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
