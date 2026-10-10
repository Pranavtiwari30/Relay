"""Idempotent demo-workspace seeding (used by ``scripts/seed.py`` and the end-to-end tests).

Creates configuration only: a workspace flagged ``is_demo``, user accounts, two
connections and one integration. It never inserts fabricated events or metrics —
every number the UI shows comes from events actually sent through the pipeline.
Passwords are supplied by the caller (from the environment); none are committed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from relay_core.adapters.registry import AdapterRegistry
from relay_core.db.models import Connection, Integration, User, Workspace, WorkspaceMembership
from relay_core.domain.roles import Role
from relay_core.errors import ValidationFailedError
from relay_core.services import connections as connection_service
from relay_core.services import integrations as integration_service
from relay_core.services.auth import hash_password, normalise_email
from relay_core.services.demo import DEMO_TOKEN_REF
from relay_core.services.event_state import SYSTEM
from relay_core.services.secrets import SecretResolver
from relay_core.simulator import DEMO_INTEGRATION_CONFIG

DEMO_SLUG = "demo"
DEMO_WEBHOOK_SECRET_REF = "env:RELAY_SECRET_DEMO_WEBHOOK"  # noqa: S105 - a reference, not a value
SOURCE_NAME = "Acme storefront"
DESTINATION_NAME = "Demo fulfilment API"
INTEGRATION_NAME = "Storefront orders to fulfilment"


@dataclass
class SeedAccount:
    email: str
    password: str
    role: Role
    display_name: str


@dataclass
class SeedResult:
    workspace_id: uuid.UUID
    source_connection_id: uuid.UUID
    destination_connection_id: uuid.UUID
    integration_id: uuid.UUID
    integration_status: str
    notes: list[str] = field(default_factory=list)


def _ensure_user(session: Session, account: SeedAccount, notes: list[str]) -> User:
    email = normalise_email(account.email)
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        user = User(
            id=uuid.uuid4(),
            email=email,
            display_name=account.display_name,
            password_hash=hash_password(account.password),
        )
        session.add(user)
        session.flush()
        notes.append(f"created user {email} ({account.role.value})")
    return user


def seed_demo(
    session: Session,
    *,
    accounts: list[SeedAccount],
    registry: AdapterRegistry,
    secrets: SecretResolver,
    now: datetime,
) -> SeedResult:
    """Create the demo workspace if missing. Safe to run repeatedly; the caller commits."""
    notes: list[str] = []
    workspace = session.execute(
        select(Workspace).where(Workspace.slug == DEMO_SLUG)
    ).scalar_one_or_none()
    if workspace is None:
        workspace = Workspace(id=uuid.uuid4(), name="Demo workspace", slug=DEMO_SLUG, is_demo=True)
        session.add(workspace)
        session.flush()
        notes.append("created demo workspace")

    for account in accounts:
        user = _ensure_user(session, account, notes)
        membership = session.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace.id,
                WorkspaceMembership.user_id == user.id,
            )
        ).scalar_one_or_none()
        if membership is None:
            session.add(
                WorkspaceMembership(
                    id=uuid.uuid4(),
                    workspace_id=workspace.id,
                    user_id=user.id,
                    role=account.role.value,
                )
            )

    def connection(name: str, direction: str, adapter_type: str, secret_ref: str) -> Connection:
        existing = session.execute(
            select(Connection).where(
                Connection.workspace_id == workspace.id, Connection.name == name
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        created, _ = connection_service.create_connection(
            session, actor=SYSTEM, workspace_id=workspace.id, registry=registry,
            name=name, direction=direction, adapter_type=adapter_type, config={}, secret_ref=secret_ref,
        )  # fmt: skip
        notes.append(f"created {direction} connection '{name}'")
        return created

    source = connection(SOURCE_NAME, "source", "webhook", DEMO_WEBHOOK_SECRET_REF)
    destination = connection(DESTINATION_NAME, "destination", "demo", DEMO_TOKEN_REF)

    integration = session.execute(
        select(Integration).where(
            Integration.workspace_id == workspace.id, Integration.name == INTEGRATION_NAME
        )
    ).scalar_one_or_none()
    if integration is None:
        integration = integration_service.create_integration(
            session,
            actor=SYSTEM,
            workspace_id=workspace.id,
            name=INTEGRATION_NAME,
            description="Demo: maps a storefront's order.created webhook to a canonical order and "
            "delivers it to the built-in demo fulfilment API.",
            source_connection_id=source.id,
            destination_connection_id=destination.id,
            config=DEMO_INTEGRATION_CONFIG,
            change_note="Seeded demo configuration",
        )
        notes.append(f"created integration '{INTEGRATION_NAME}'")
    if integration.status == "draft":
        try:
            integration_service.change_status(
                session, actor=SYSTEM, workspace_id=workspace.id, integration_id=integration.id,
                action="activate", registry=registry, secrets=secrets, now=now,
            )  # fmt: skip
            notes.append("activated the demo integration")
        except ValidationFailedError as exc:
            problems = "; ".join(f"{d.get('path')}: {d.get('message')}" for d in exc.details)
            notes.append(f"integration left as draft — {problems}")
    session.flush()
    return SeedResult(
        workspace.id, source.id, destination.id, integration.id, integration.status, notes
    )
