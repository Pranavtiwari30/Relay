"""Workspaces and memberships. Every tenant-scoped query filters on ``workspace_id``."""

from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from relay_core.db.models import User, Workspace, WorkspaceMembership
from relay_core.domain.roles import Role
from relay_core.errors import ConflictError, NotFoundError, ValidationFailedError
from relay_core.services.audit import record_audit
from relay_core.services.auth import hash_password, normalise_email
from relay_core.services.event_state import Actor

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


def list_for_user(
    session: Session, user_id: uuid.UUID
) -> list[tuple[Workspace, WorkspaceMembership]]:
    rows = session.execute(
        select(Workspace, WorkspaceMembership)
        .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
        .where(WorkspaceMembership.user_id == user_id)
        .order_by(Workspace.name)
    ).all()
    return [(row[0], row[1]) for row in rows]


def get_membership(
    session: Session, user_id: uuid.UUID, workspace_id: uuid.UUID
) -> WorkspaceMembership | None:
    return session.execute(
        select(WorkspaceMembership).where(
            WorkspaceMembership.user_id == user_id, WorkspaceMembership.workspace_id == workspace_id
        )
    ).scalar_one_or_none()


def list_members(
    session: Session, workspace_id: uuid.UUID
) -> list[tuple[User, WorkspaceMembership]]:
    rows = session.execute(
        select(User, WorkspaceMembership)
        .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
        .where(WorkspaceMembership.workspace_id == workspace_id)
        .order_by(User.email)
    ).all()
    return [(row[0], row[1]) for row in rows]


def _admin_count(session: Session, workspace_id: uuid.UUID) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.role == Role.ADMIN.value,
            )
        ).scalar_one()
    )


def _lock_workspace(session: Session, workspace_id: uuid.UUID) -> None:
    # Serialises membership changes so "is this the last admin?" cannot race.
    session.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())


def add_member(
    session: Session,
    *,
    actor: Actor,
    workspace_id: uuid.UUID,
    email: str,
    display_name: str,
    role: Role,
    password: str | None,
) -> tuple[User, WorkspaceMembership]:
    email = normalise_email(email)
    if not _EMAIL_RE.fullmatch(email):
        raise ValidationFailedError("email address is not valid", code="invalid_email")
    _lock_workspace(session, workspace_id)
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    created_user = False
    if user is None:
        if not password:
            raise ValidationFailedError(
                "an initial password is required when adding a new user", code="password_required"
            )
        user = User(
            id=uuid.uuid4(),
            email=email,
            display_name=display_name.strip() or email,
            password_hash=hash_password(password),
        )
        session.add(user)
        session.flush()
        created_user = True
    if get_membership(session, user.id, workspace_id) is not None:
        raise ConflictError("this user is already a member of the workspace", code="already_member")
    membership = WorkspaceMembership(
        id=uuid.uuid4(), workspace_id=workspace_id, user_id=user.id, role=role.value
    )
    session.add(membership)
    session.flush()
    record_audit(
        session,
        workspace_id=workspace_id,
        actor=actor,
        action="member.add",
        entity_type="user",
        entity_id=user.id,
        details={"email": email, "role": role.value, "created_user": created_user},
    )
    return user, membership


def change_role(
    session: Session, *, actor: Actor, workspace_id: uuid.UUID, user_id: uuid.UUID, role: Role
) -> WorkspaceMembership:
    _lock_workspace(session, workspace_id)
    membership = get_membership(session, user_id, workspace_id)
    if membership is None:
        raise NotFoundError("member not found")
    previous = membership.role
    if previous == role.value:
        return membership
    if previous == Role.ADMIN.value and _admin_count(session, workspace_id) <= 1:
        raise ConflictError("a workspace must keep at least one administrator", code="last_admin")
    membership.role = role.value
    session.flush()
    record_audit(
        session,
        workspace_id=workspace_id,
        actor=actor,
        action="member.role_change",
        entity_type="user",
        entity_id=user_id,
        details={"from_role": previous, "to_role": role.value},
    )
    return membership


def remove_member(
    session: Session, *, actor: Actor, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    _lock_workspace(session, workspace_id)
    membership = get_membership(session, user_id, workspace_id)
    if membership is None:
        raise NotFoundError("member not found")
    if membership.role == Role.ADMIN.value and _admin_count(session, workspace_id) <= 1:
        raise ConflictError("a workspace must keep at least one administrator", code="last_admin")
    record_audit(
        session,
        workspace_id=workspace_id,
        actor=actor,
        action="member.remove",
        entity_type="user",
        entity_id=user_id,
        details={"role": membership.role},
    )
    session.delete(membership)
