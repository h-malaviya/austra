"""Resolve current authorization from a hashed durable session token."""

import hashlib
from collections import defaultdict
from uuid import UUID

import psycopg

from austra.tenant.permissions.evaluator import AuthContext, DepartmentGrant, SupportContext
from austra.tenant.permissions.keys import Permission


def resolve_session(connection: psycopg.Connection, token: str) -> AuthContext | None:
    if not token or len(token) > 512:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = connection.execute(
        """
        SELECT s.id, u.id, u.organization_id, u.role, u.status, u.must_change_password,
               o.status, sc.organization_id, sc.reason, sc.expires_at, sc.ended_at,
               supported.status
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        LEFT JOIN organizations o ON o.id = u.organization_id
        LEFT JOIN LATERAL (
            SELECT organization_id, reason, expires_at, ended_at
            FROM support_contexts
            WHERE session_id = s.id AND super_admin_user_id = u.id
                  AND ended_at IS NULL AND expires_at > now()
            ORDER BY started_at DESC LIMIT 1
        ) sc ON true
        LEFT JOIN organizations supported ON supported.id = sc.organization_id
        WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now()
              AND u.deleted_at IS NULL
        """,
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    (
        _session_id,
        user_id,
        organization_id,
        role,
        user_status,
        must_change_password,
        organization_status,
        support_organization_id,
        support_reason,
        support_expiry,
        support_end,
        supported_status,
    ) = row
    support = (
        SupportContext(support_organization_id, support_reason, support_expiry, support_end)
        if support_organization_id is not None
        else None
    )
    organization_active = organization_status == "active" if organization_id else True
    if role == "super_admin" and support:
        organization_active = supported_status == "active"
    grants = active_department_grants(connection, user_id) if role == "employee" else ()
    return AuthContext(
        user_id=user_id,
        organization_id=organization_id,
        role=role,
        user_active=user_status == "active",
        organization_active=organization_active,
        must_change_password=must_change_password,
        grants=grants,
        support=support,
    )


def active_department_grants(connection: psycopg.Connection, user_id: UUID) -> tuple[DepartmentGrant, ...]:
    rows = connection.execute(
        """
        SELECT m.department_id, a.role_id, p.permission_key
        FROM department_memberships m
        JOIN departments d ON (d.organization_id, d.id) = (m.organization_id, m.department_id)
        JOIN role_assignments a ON a.department_membership_id = m.id
        JOIN department_roles r ON r.id = a.role_id
        JOIN role_versions v ON v.role_id = r.id AND v.version = r.current_version
        LEFT JOIN role_permissions p ON p.role_version_id = v.id
        WHERE m.user_id = %s AND m.revoked_at IS NULL AND d.archived_at IS NULL
              AND a.revoked_at IS NULL AND r.archived_at IS NULL AND v.effective_at <= now()
        """,
        (user_id,),
    ).fetchall()
    by_role: dict[tuple[UUID, UUID], set[Permission]] = defaultdict(set)
    for department_id, role_id, permission_key in rows:
        if permission_key is not None:
            by_role[(department_id, role_id)].add(permission_key)
    return tuple(
        DepartmentGrant(department_id, frozenset(permissions))
        for (department_id, _role_id), permissions in by_role.items()
    )
