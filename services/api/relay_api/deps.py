"""Request-scoped dependencies: database session, authentication and authorisation.

Authorisation lives here, on the server. Every tenant-scoped route depends on
``require(Permission.X)``, which resolves the caller's workspace membership and
role before the handler runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from relay_core.adapters.registry import AdapterRegistry
from relay_core.config import Settings
from relay_core.db.models import User, UserSession, Workspace
from relay_core.db.session import SessionFactory
from relay_core.domain.roles import Permission, Role, has_permission
from relay_core.errors import NotFoundError, PermissionDeniedError, UnauthenticatedError
from relay_core.messaging.base import Publisher
from relay_core.services import auth as auth_service
from relay_core.services import workspaces as workspace_service
from relay_core.services.event_state import Actor
from relay_core.services.ingestion import IngestionService
from relay_core.services.outbox import OutboxDispatcher
from relay_core.services.secrets import SecretResolver

if TYPE_CHECKING:
    from relay_core.ai.service import InvestigationService

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "x-csrf-token"
WORKSPACE_HEADER = "x-workspace-id"


@dataclass
class Container:
    """Process-wide collaborators, built once at startup (or by a test)."""

    settings: Settings
    session_factory: SessionFactory
    registry: AdapterRegistry
    secrets: SecretResolver
    publisher: Publisher
    dispatcher: OutboxDispatcher
    ingestion: IngestionService
    investigations: InvestigationService
    clock: Callable[[], datetime]


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def get_db(container: Container = Depends(get_container)) -> Iterator[Session]:
    """A session per request. Handlers commit explicitly; anything left is rolled back."""
    session = container.session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def check_origin(request: Request, settings: Settings) -> None:
    """Reject cross-site state-changing requests from origins that are not allowlisted."""
    origin = request.headers.get("origin")
    if origin is not None and origin not in settings.cors_allowed_origins:
        raise PermissionDeniedError("request origin is not allowed", code="origin_not_allowed")


@dataclass
class Authenticated:
    user: User
    session: UserSession


def get_authenticated(
    request: Request,
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> Authenticated:
    settings = container.settings
    token = request.cookies.get(settings.session_cookie_name)
    resolved = auth_service.resolve_session(db, token, now=container.clock()) if token else None
    if resolved is None:
        raise UnauthenticatedError("sign in to continue")
    user_session, user = resolved
    if request.method not in SAFE_METHODS:
        # The session cookie is sent automatically by browsers, so a
        # state-changing request must also prove it came from our own UI.
        check_origin(request, settings)
        if not auth_service.csrf_matches(user_session, request.headers.get(CSRF_HEADER)):
            raise PermissionDeniedError("missing or invalid CSRF token", code="csrf_failed")
    auth_service.touch_session(container.session_factory, user_session, now=container.clock())
    return Authenticated(user=user, session=user_session)


@dataclass
class Context:
    """Who is calling, in which workspace, with which role."""

    user: User
    session: UserSession
    workspace: Workspace
    role: Role
    db: Session
    container: Container

    @property
    def actor(self) -> Actor:
        return Actor.user(self.user.id)

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.workspace.id

    def now(self) -> datetime:
        return self.container.clock()


def get_context(
    request: Request,
    authed: Authenticated = Depends(get_authenticated),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> Context:
    memberships = workspace_service.list_for_user(db, authed.user.id)
    requested = request.headers.get(WORKSPACE_HEADER)
    chosen: tuple[Workspace, Role] | None = None
    if requested:
        try:
            wanted = uuid.UUID(requested)
        except ValueError:
            wanted = None
        for workspace, membership in memberships:
            if workspace.id == wanted:
                chosen = (workspace, Role(membership.role))
        if chosen is None:
            # Same answer whether the workspace does not exist or the caller is
            # not a member: do not reveal which.
            raise NotFoundError("workspace not found")
    elif memberships:
        chosen = (memberships[0][0], Role(memberships[0][1].role))
    if chosen is None:
        raise PermissionDeniedError("you are not a member of any workspace", code="no_workspace")
    # Picked up by the request log. (A contextvar bound here would not survive:
    # dependencies and handlers run in separate thread contexts.)
    request.state.workspace_id = str(chosen[0].id)
    request.state.user_id = str(authed.user.id)
    return Context(
        user=authed.user,
        session=authed.session,
        workspace=chosen[0],
        role=chosen[1],
        db=db,
        container=container,
    )


def require(permission: Permission) -> Callable[[Context], Context]:
    def dependency(context: Context = Depends(get_context)) -> Context:
        if not has_permission(context.role, permission):
            raise PermissionDeniedError(
                f"your role ({context.role.value}) does not allow this action",
                details=[{"required_permission": permission.value}],
            )
        return context

    return dependency
