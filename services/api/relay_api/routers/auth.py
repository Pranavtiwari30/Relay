from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from relay_api import schemas
from relay_api.deps import (
    Authenticated,
    Container,
    Context,
    check_origin,
    client_ip,
    get_authenticated,
    get_container,
    get_context,
    get_db,
    require,
)
from relay_core.db.models import User, WorkspaceMembership
from relay_core.domain.roles import ROLE_PERMISSIONS, Permission, Role
from relay_core.services import auth as auth_service
from relay_core.services import workspaces as workspace_service

router = APIRouter(prefix="/api/v1", tags=["auth"])


def _session_out(
    db: Session, authed: Authenticated, active: uuid.UUID | None = None
) -> dict[str, object]:
    memberships = workspace_service.list_for_user(db, authed.user.id)
    workspaces = [
        {"id": w.id, "name": w.name, "slug": w.slug, "is_demo": w.is_demo, "role": m.role}
        for w, m in memberships
    ]
    chosen = next(
        (w for w in workspaces if w["id"] == active), workspaces[0] if workspaces else None
    )
    role = Role(str(chosen["role"])) if chosen else None
    return {
        "user": {
            "id": authed.user.id,
            "email": authed.user.email,
            "display_name": authed.user.display_name,
        },
        "workspaces": workspaces,
        "active_workspace_id": chosen["id"] if chosen else None,
        "role": role.value if role else None,
        "permissions": sorted(p.value for p in ROLE_PERMISSIONS[role]) if role else [],
        "csrf_token": authed.session.csrf_token,
        "expires_at": authed.session.expires_at,
    }


@router.post(
    "/auth/login", response_model=schemas.SessionOut, summary="Sign in and start a session"
)
def login(
    body: schemas.LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    settings = container.settings
    check_origin(request, settings)
    now = container.clock()
    result = auth_service.login(
        container.session_factory,
        settings,
        email=body.email,
        password=body.password,
        user_agent=request.headers.get("user-agent"),
        client_ip=client_ip(request),
        now=now,
    )
    response.set_cookie(
        settings.session_cookie_name,
        result.session_token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,  # not readable from JavaScript
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )
    resolved = auth_service.resolve_session(db, result.session_token, now=now)
    assert resolved is not None
    return _session_out(db, Authenticated(user=resolved[1], session=resolved[0]))


@router.post("/auth/logout", status_code=204, summary="End the current session")
def logout(
    response: Response,
    authed: Authenticated = Depends(get_authenticated),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> None:
    auth_service.logout(db, authed.session, now=container.clock())
    db.commit()
    response.delete_cookie(container.settings.session_cookie_name, path="/")


@router.get(
    "/auth/me",
    response_model=schemas.SessionOut,
    summary="Current user, workspaces and permissions",
)
def me(context: Context = Depends(get_context)) -> dict[str, object]:
    return _session_out(
        context.db, Authenticated(user=context.user, session=context.session), context.workspace_id
    )


@router.post("/auth/password", status_code=204, summary="Change your password")
def change_password(
    body: schemas.PasswordChangeRequest,
    authed: Authenticated = Depends(get_authenticated),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> None:
    auth_service.change_password(
        db,
        user=authed.user,
        current_session=authed.session,
        current_password=body.current_password,
        new_password=body.new_password,
        now=container.clock(),
    )
    db.commit()


@router.get(
    "/workspaces",
    response_model=list[schemas.WorkspaceOut],
    tags=["workspaces"],
    summary="Workspaces you belong to",
)
def list_workspaces(
    authed: Authenticated = Depends(get_authenticated), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    return [
        {"id": w.id, "name": w.name, "slug": w.slug, "is_demo": w.is_demo, "role": m.role}
        for w, m in workspace_service.list_for_user(db, authed.user.id)
    ]


def _member(user: User, membership: WorkspaceMembership) -> dict[str, object]:
    return {
        "user_id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": membership.role,
        "is_active": user.is_active,
        "joined_at": membership.created_at,
    }


@router.get(
    "/members",
    response_model=list[schemas.MemberOut],
    tags=["workspaces"],
    summary="Members of the active workspace",
)
def list_members(
    context: Context = Depends(require(Permission.MEMBER_READ)),
) -> list[dict[str, object]]:
    return [
        _member(u, m) for u, m in workspace_service.list_members(context.db, context.workspace_id)
    ]


@router.post(
    "/members",
    response_model=schemas.MemberOut,
    status_code=201,
    tags=["workspaces"],
    summary="Add a member (administrators)",
)
def add_member(
    body: schemas.MemberCreate, context: Context = Depends(require(Permission.MEMBER_MANAGE))
) -> dict[str, object]:
    user, membership = workspace_service.add_member(
        context.db,
        actor=context.actor,
        workspace_id=context.workspace_id,
        email=body.email,
        display_name=body.display_name,
        role=Role(body.role),
        password=body.password,
    )
    context.db.commit()
    return _member(user, membership)


@router.patch(
    "/members/{user_id}",
    response_model=schemas.MemberOut,
    tags=["workspaces"],
    summary="Change a member's role (administrators)",
)
def update_member(
    user_id: uuid.UUID,
    body: schemas.MemberUpdate,
    context: Context = Depends(require(Permission.MEMBER_MANAGE)),
) -> dict[str, object]:
    workspace_service.change_role(
        context.db,
        actor=context.actor,
        workspace_id=context.workspace_id,
        user_id=user_id,
        role=Role(body.role),
    )
    context.db.commit()
    for user, membership in workspace_service.list_members(context.db, context.workspace_id):
        if user.id == user_id:
            return _member(user, membership)
    raise AssertionError("member vanished after update")


@router.delete(
    "/members/{user_id}",
    status_code=204,
    tags=["workspaces"],
    summary="Remove a member (administrators)",
)
def remove_member(
    user_id: uuid.UUID, context: Context = Depends(require(Permission.MEMBER_MANAGE))
) -> None:
    workspace_service.remove_member(
        context.db, actor=context.actor, workspace_id=context.workspace_id, user_id=user_id
    )
    context.db.commit()
