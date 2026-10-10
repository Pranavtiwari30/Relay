"""Workspace roles and the permissions they grant.

Authorisation is enforced in the backend against this table; the UI only uses
it to hide controls the server would reject anyway.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class Permission(StrEnum):
    WORKSPACE_READ = "workspace:read"
    MEMBER_READ = "member:read"
    MEMBER_MANAGE = "member:manage"
    CONNECTION_READ = "connection:read"
    CONNECTION_WRITE = "connection:write"
    INTEGRATION_READ = "integration:read"
    INTEGRATION_WRITE = "integration:write"
    INTEGRATION_OPERATE = "integration:operate"
    EVENT_READ = "event:read"
    EVENT_REPLAY = "event:replay"
    INVESTIGATION_READ = "investigation:read"
    INVESTIGATION_RUN = "investigation:run"
    RECOMMENDATION_DECIDE = "recommendation:decide"
    AUDIT_READ = "audit:read"
    DEMO_USE = "demo:use"


_VIEWER = frozenset(
    {
        Permission.WORKSPACE_READ,
        Permission.MEMBER_READ,
        Permission.CONNECTION_READ,
        Permission.INTEGRATION_READ,
        Permission.EVENT_READ,
        Permission.INVESTIGATION_READ,
        Permission.AUDIT_READ,
    }
)
_OPERATOR = _VIEWER | frozenset(
    {
        Permission.INTEGRATION_OPERATE,
        Permission.EVENT_REPLAY,
        Permission.INVESTIGATION_RUN,
        Permission.RECOMMENDATION_DECIDE,
        Permission.DEMO_USE,
    }
)
_ADMIN = _OPERATOR | frozenset(
    {
        Permission.MEMBER_MANAGE,
        Permission.CONNECTION_WRITE,
        Permission.INTEGRATION_WRITE,
    }
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    Role.OPERATOR: _OPERATOR,
    Role.ADMIN: _ADMIN,
}


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]
