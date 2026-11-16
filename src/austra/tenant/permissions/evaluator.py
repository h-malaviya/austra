"""Default deny decisions over a freshly loaded authenticated context."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from austra.tenant.permissions.keys import (
    ADMIN_ONLY_PERMISSIONS,
    DEPARTMENT_PERMISSIONS,
    SUPPORT_READ_PERMISSIONS,
    Permission,
)

Role = Literal["super_admin", "org_admin", "employee"]


@dataclass(frozen=True)
class DepartmentGrant:
    department_id: UUID
    permissions: frozenset[Permission]
    department_active: bool = True
    membership_active: bool = True
    role_active: bool = True
    assignment_active: bool = True


@dataclass(frozen=True)
class SupportContext:
    organization_id: UUID
    reason: str
    expires_at: datetime
    ended_at: datetime | None = None


@dataclass(frozen=True)
class AuthContext:
    user_id: UUID
    organization_id: UUID | None
    role: Role
    user_active: bool
    organization_active: bool
    must_change_password: bool = False
    grants: tuple[DepartmentGrant, ...] = ()
    support: SupportContext | None = None


def permits(
    context: AuthContext,
    organization_id: UUID,
    department_id: UUID | None,
    permission: Permission,
    *,
    now: datetime | None = None,
) -> bool:
    """Decide one action after the caller has resolved the resource's actual scope."""
    if not context.user_active or not context.organization_active or context.must_change_password:
        return False
    if context.role == "super_admin":
        support = context.support
        return bool(
            support
            and support.organization_id == organization_id
            and support.reason.strip()
            and support.ended_at is None
            and support.expires_at > (now or datetime.now(UTC))
            and permission in SUPPORT_READ_PERMISSIONS
        )
    if context.organization_id != organization_id:
        return False
    if context.role == "org_admin":
        return permission in DEPARTMENT_PERMISSIONS | ADMIN_ONLY_PERMISSIONS
    if context.role != "employee" or department_id is None:
        return False
    if permission not in DEPARTMENT_PERMISSIONS:
        return False
    return any(
        grant.department_id == department_id
        and grant.department_active
        and grant.membership_active
        and grant.role_active
        and grant.assignment_active
        and permission in grant.permissions
        for grant in context.grants
    )
