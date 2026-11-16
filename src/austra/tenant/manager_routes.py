"""Department role delegation and membership approval."""

from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from austra.platform.auth import SaasIdentity, normalize_username, password_hash, temporary_password
from austra.tenant.audit import record_event
from austra.tenant.permissions.evaluator import AuthContext
from austra.tenant.permissions.keys import DEPARTMENT_PERMISSIONS

COOKIE = "austra_session"
MANAGEMENT_KEYS = frozenset(
    {"department.members.manage", "department.users.create", "department.roles.assign"}
)


class MemberInput(BaseModel):
    username: str
    role_id: UUID
    reason: str = Field(min_length=3, max_length=500)


class ReviewInput(BaseModel):
    decision: str


class AssignableRoles(BaseModel):
    role_ids: list[UUID]


class RoleRevision(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    permissions: list[str]
    reason: str = Field(min_length=3, max_length=500)


def install_manager_routes(app: FastAPI, identity: SaasIdentity) -> None:
    def actor(request: Request) -> AuthContext:
        context, _remaining = identity.session(request.cookies.get(COOKIE))
        if context is None:
            raise HTTPException(status_code=401, detail="Sign in required")
        if (
            not context.user_active
            or not context.organization_active
            or context.must_change_password
            or context.organization_id is None
        ):
            raise HTTPException(status_code=403, detail="Organization access required")
        return context

    def admin(request: Request) -> AuthContext:
        context = actor(request)
        if context.role != "org_admin":
            raise HTTPException(status_code=403, detail="Organization admin access required")
        return context

    def active_department(connection, organization_id, department_id):
        row = connection.execute(
            "SELECT id FROM departments WHERE organization_id = %s AND id = %s "
            "AND archived_at IS NULL FOR UPDATE",
            (organization_id, department_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Department not found")

    def current_permissions(connection, role_id):
        return {
            row[0]
            for row in connection.execute(
                "SELECT p.permission_key FROM department_roles r "
                "JOIN role_versions v ON v.role_id = r.id AND v.version = r.current_version "
                "JOIN role_permissions p ON p.role_version_id = v.id "
                "WHERE r.id = %s AND r.archived_at IS NULL AND v.effective_at <= now()",
                (role_id,),
            ).fetchall()
        }

    def manager_can_assign(connection, context, department_id, role_id):
        if context.role == "org_admin":
            return True
        if context.role != "employee" or current_permissions(connection, role_id) & MANAGEMENT_KEYS:
            return False
        rows = connection.execute(
            "SELECT a.role_id FROM department_memberships m "
            "JOIN role_assignments a ON a.department_membership_id = m.id "
            "JOIN department_roles r ON r.id = a.role_id "
            "JOIN role_assignment_rules rule ON rule.manager_role_id = r.id "
            "WHERE m.user_id = %s AND m.organization_id = %s AND m.department_id = %s "
            "AND m.revoked_at IS NULL AND a.revoked_at IS NULL AND r.archived_at IS NULL "
            "AND rule.assignable_role_id = %s AND rule.revoked_at IS NULL",
            (context.user_id, context.organization_id, department_id, role_id),
        ).fetchall()
        return any(MANAGEMENT_KEYS <= current_permissions(connection, row[0]) for row in rows)

    def is_department_manager(connection, department_id, user_id):
        rows = connection.execute(
            "SELECT a.role_id FROM department_memberships m "
            "JOIN role_assignments a ON a.department_membership_id = m.id "
            "JOIN department_roles r ON r.id = a.role_id "
            "WHERE m.department_id = %s AND m.user_id = %s AND m.revoked_at IS NULL "
            "AND a.revoked_at IS NULL AND r.archived_at IS NULL",
            (department_id, user_id),
        ).fetchall()
        return any(MANAGEMENT_KEYS <= current_permissions(connection, row[0]) for row in rows)

    @app.patch("/api/org/departments/{department_id}/roles/{role_id}")
    def revise_role(department_id: UUID, role_id: UUID, payload: RoleRevision, request: Request):
        context = admin(request)
        if not set(payload.permissions) <= DEPARTMENT_PERMISSIONS:
            raise HTTPException(status_code=400, detail="Unknown or admin-only permission")
        with identity.connect() as connection:
            active_department(connection, context.organization_id, department_id)
            row = connection.execute(
                "SELECT current_version FROM department_roles WHERE id = %s "
                "AND organization_id = %s AND department_id = %s AND archived_at IS NULL "
                "FOR UPDATE",
                (role_id, context.organization_id, department_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Role not found")
            next_version = row[0] + 1
            version_id = connection.execute(
                "INSERT INTO role_versions (role_id, version, change_reason, approved_by) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (role_id, next_version, payload.reason.strip(), context.user_id),
            ).fetchone()[0]
            for permission in set(payload.permissions):
                connection.execute(
                    "INSERT INTO role_permissions (role_version_id, permission_key) VALUES (%s, %s)",
                    (version_id, permission),
                )
            connection.execute(
                "UPDATE department_roles SET name = %s, current_version = %s WHERE id = %s",
                (payload.name.strip(), next_version, role_id),
            )
            record_event(
                connection,
                context,
                "role.revised",
                resource_id=role_id,
                department_id=department_id,
                reason=payload.reason.strip(),
            )
        return {"id": str(role_id), "version": next_version}

    @app.put("/api/org/departments/{department_id}/roles/{role_id}/assignable-roles")
    def set_assignable_roles(
        department_id: UUID, role_id: UUID, payload: AssignableRoles, request: Request
    ):
        context = admin(request)
        with identity.connect() as connection:
            active_department(connection, context.organization_id, department_id)
            manager_role = connection.execute(
                "SELECT id FROM department_roles WHERE id = %s AND organization_id = %s "
                "AND department_id = %s AND archived_at IS NULL",
                (role_id, context.organization_id, department_id),
            ).fetchone()
            if not manager_role or not MANAGEMENT_KEYS <= current_permissions(connection, role_id):
                raise HTTPException(status_code=400, detail="Role is not a department manager")
            for subordinate_id in set(payload.role_ids):
                row = connection.execute(
                    "SELECT id FROM department_roles WHERE id = %s AND organization_id = %s "
                    "AND department_id = %s AND archived_at IS NULL",
                    (subordinate_id, context.organization_id, department_id),
                ).fetchone()
                if not row or current_permissions(connection, subordinate_id) & MANAGEMENT_KEYS:
                    raise HTTPException(status_code=400, detail="Invalid subordinate role")
            connection.execute(
                "UPDATE role_assignment_rules SET revoked_at = now() "
                "WHERE manager_role_id = %s AND revoked_at IS NULL",
                (role_id,),
            )
            for subordinate_id in set(payload.role_ids):
                connection.execute(
                    "INSERT INTO role_assignment_rules (organization_id, department_id, "
                    "manager_role_id, assignable_role_id, approved_by) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        context.organization_id,
                        department_id,
                        role_id,
                        subordinate_id,
                        context.user_id,
                    ),
                )
            record_event(
                connection,
                context,
                "role.delegation_changed",
                resource_id=role_id,
                department_id=department_id,
            )
        return {"assignable_role_ids": [str(item) for item in payload.role_ids]}

    @app.post("/api/org/departments/{department_id}/members")
    def add_member(department_id: UUID, payload: MemberInput, request: Request):
        context = actor(request)
        try:
            username = normalize_username(payload.username)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        with identity.connect() as connection:
            active_department(connection, context.organization_id, department_id)
            role = connection.execute(
                "SELECT id FROM department_roles WHERE id = %s AND organization_id = %s "
                "AND department_id = %s AND archived_at IS NULL",
                (payload.role_id, context.organization_id, department_id),
            ).fetchone()
            if not role:
                raise HTTPException(status_code=404, detail="Role not found")
            if not manager_can_assign(connection, context, department_id, payload.role_id):
                raise HTTPException(status_code=403, detail="Role assignment not permitted")
            target = connection.execute(
                "SELECT id, role, status FROM users WHERE organization_id = %s "
                "AND username_normalized = %s AND deleted_at IS NULL FOR UPDATE",
                (context.organization_id, username),
            ).fetchone()
            credential = None
            if target is None:
                credential = temporary_password()
                user_id = connection.execute(
                    "INSERT INTO users (organization_id, username_normalized, role, password_hash) "
                    "VALUES (%s, %s, 'employee', %s) RETURNING id",
                    (context.organization_id, username, password_hash(credential)),
                ).fetchone()[0]
                record_event(
                    connection,
                    context,
                    "user.created",
                    resource_id=user_id,
                    department_id=department_id,
                )
            else:
                user_id = target[0]
                if target[1] != "employee" or target[2] != "active" or user_id == context.user_id:
                    raise HTTPException(status_code=403, detail="Account requires admin handling")
                if context.role != "org_admin" and is_department_manager(
                    connection, department_id, user_id
                ):
                    raise HTTPException(
                        status_code=403, detail="Manager account requires admin handling"
                    )
            membership = connection.execute(
                "SELECT id FROM department_memberships WHERE department_id = %s "
                "AND user_id = %s AND revoked_at IS NULL",
                (department_id, user_id),
            ).fetchone()
            if target and membership is None and context.role != "org_admin":
                try:
                    row = connection.execute(
                        "INSERT INTO department_join_requests (organization_id, department_id, "
                        "target_user_id, requested_by, requested_role_id, reason) "
                        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                        (
                            context.organization_id,
                            department_id,
                            user_id,
                            context.user_id,
                            payload.role_id,
                            payload.reason.strip(),
                        ),
                    ).fetchone()
                except psycopg.errors.UniqueViolation as error:
                    raise HTTPException(
                        status_code=409, detail="Join request already pending"
                    ) from error
                record_event(
                    connection,
                    context,
                    "department.join_requested",
                    resource_id=row[0],
                    department_id=department_id,
                    reason=payload.reason.strip(),
                )
                return {"status": "pending", "request_id": str(row[0])}
            if membership is None:
                membership = connection.execute(
                    "INSERT INTO department_memberships (organization_id, department_id, user_id) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (context.organization_id, department_id, user_id),
                ).fetchone()
            connection.execute(
                "INSERT INTO role_assignments (organization_id, department_id, "
                "department_membership_id, role_id, assigned_by) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    context.organization_id,
                    department_id,
                    membership[0],
                    payload.role_id,
                    context.user_id,
                ),
            )
            record_event(
                connection,
                context,
                "department.member_assigned",
                resource_id=user_id,
                department_id=department_id,
            )
        return {"status": "assigned", "user_id": str(user_id), "temporary_password": credential}

    @app.get("/api/org/department-join-requests")
    def list_join_requests(request: Request):
        context = admin(request)
        with identity.connect() as connection:
            rows = connection.execute(
                "SELECT j.id, j.department_id, u.username_normalized, j.requested_role_id, "
                "j.reason FROM department_join_requests j "
                "JOIN users u ON u.id = j.target_user_id "
                "WHERE j.organization_id = %s AND j.status = 'pending' ORDER BY j.id",
                (context.organization_id,),
            ).fetchall()
        return [
            {
                "id": str(row[0]),
                "department_id": str(row[1]),
                "username": row[2],
                "role_id": str(row[3]),
                "reason": row[4],
            }
            for row in rows
        ]

    @app.delete("/api/org/departments/{department_id}/members/{user_id}")
    def remove_member(department_id: UUID, user_id: UUID, request: Request):
        context = actor(request)
        if user_id == context.user_id:
            raise HTTPException(status_code=403, detail="Cannot remove own membership")
        with identity.connect() as connection:
            active_department(connection, context.organization_id, department_id)
            if context.role != "org_admin":
                if not is_department_manager(connection, department_id, context.user_id):
                    raise HTTPException(status_code=403, detail="Manager access required")
                if is_department_manager(connection, department_id, user_id):
                    raise HTTPException(
                        status_code=403, detail="Manager account requires admin handling"
                    )
            target = connection.execute(
                "SELECT id FROM users WHERE id = %s AND organization_id = %s "
                "AND role = 'employee' AND deleted_at IS NULL",
                (user_id, context.organization_id),
            ).fetchone()
            if target is None:
                raise HTTPException(status_code=404, detail="Employee not found")
            membership = connection.execute(
                "UPDATE department_memberships SET revoked_at = now() "
                "WHERE organization_id = %s AND department_id = %s AND user_id = %s "
                "AND revoked_at IS NULL RETURNING id",
                (context.organization_id, department_id, user_id),
            ).fetchone()
            if membership is None:
                raise HTTPException(status_code=404, detail="Membership not found")
            connection.execute(
                "UPDATE role_assignments SET revoked_at = now() "
                "WHERE department_membership_id = %s AND revoked_at IS NULL",
                (membership[0],),
            )
            record_event(
                connection,
                context,
                "department.member_removed",
                resource_id=user_id,
                department_id=department_id,
            )
        return {"removed": True}

    @app.delete("/api/org/departments/{department_id}/members/{user_id}/roles/{role_id}")
    def remove_role(department_id: UUID, user_id: UUID, role_id: UUID, request: Request):
        context = actor(request)
        if user_id == context.user_id and context.role != "org_admin":
            raise HTTPException(status_code=403, detail="Cannot change own roles")
        with identity.connect() as connection:
            active_department(connection, context.organization_id, department_id)
            if not manager_can_assign(connection, context, department_id, role_id):
                raise HTTPException(status_code=403, detail="Role assignment not permitted")
            if context.role != "org_admin" and is_department_manager(
                connection, department_id, user_id
            ):
                raise HTTPException(
                    status_code=403, detail="Manager account requires admin handling"
                )
            row = connection.execute(
                "UPDATE role_assignments SET revoked_at = now() WHERE role_id = %s "
                "AND organization_id = %s AND department_id = %s AND revoked_at IS NULL "
                "AND department_membership_id IN ("
                "SELECT id FROM department_memberships WHERE user_id = %s "
                "AND revoked_at IS NULL) RETURNING id",
                (role_id, context.organization_id, department_id, user_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Role assignment not found")
            record_event(
                connection,
                context,
                "role.revoked",
                resource_id=role_id,
                department_id=department_id,
            )
        return {"revoked": True}

    @app.post("/api/org/department-join-requests/{request_id}/review")
    def review_join_request(request_id: UUID, payload: ReviewInput, request: Request):
        context = admin(request)
        if payload.decision not in {"approved", "rejected"}:
            raise HTTPException(status_code=400, detail="Invalid decision")
        with identity.connect() as connection:
            row = connection.execute(
                "SELECT department_id, target_user_id, requested_role_id, requested_by FROM "
                "department_join_requests WHERE id = %s AND organization_id = %s "
                "AND status = 'pending' FOR UPDATE",
                (request_id, context.organization_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Join request not found")
            if payload.decision == "approved":
                active_department(connection, context.organization_id, row[0])
                target = connection.execute(
                    "SELECT id FROM users WHERE id = %s AND organization_id = %s "
                    "AND role = 'employee' AND status = 'active' AND deleted_at IS NULL",
                    (row[1], context.organization_id),
                ).fetchone()
                requester = connection.execute(
                    "SELECT role, status FROM users WHERE id = %s AND organization_id = %s "
                    "AND deleted_at IS NULL",
                    (row[3], context.organization_id),
                ).fetchone()
                role = connection.execute(
                    "SELECT id FROM department_roles WHERE id = %s AND organization_id = %s "
                    "AND department_id = %s AND archived_at IS NULL",
                    (row[2], context.organization_id, row[0]),
                ).fetchone()
                requester_context = AuthContext(
                    row[3], context.organization_id, "employee", True, True
                )
                if (
                    not target
                    or not role
                    or not requester
                    or requester != ("employee", "active")
                    or not manager_can_assign(connection, requester_context, row[0], row[2])
                ):
                    raise HTTPException(status_code=409, detail="Join request is no longer valid")
                membership = connection.execute(
                    "INSERT INTO department_memberships (organization_id, department_id, user_id) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                    (context.organization_id, row[0], row[1]),
                ).fetchone()
                if membership is None:
                    membership = connection.execute(
                        "SELECT id FROM department_memberships WHERE department_id = %s "
                        "AND user_id = %s AND revoked_at IS NULL",
                        (row[0], row[1]),
                    ).fetchone()
                connection.execute(
                    "INSERT INTO role_assignments (organization_id, department_id, "
                    "department_membership_id, role_id, assigned_by) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (context.organization_id, row[0], membership[0], row[2], context.user_id),
                )
            connection.execute(
                "UPDATE department_join_requests SET status = %s, reviewed_by = %s, "
                "reviewed_at = now() WHERE id = %s",
                (payload.decision, context.user_id, request_id),
            )
            record_event(
                connection,
                context,
                f"department.join_{payload.decision}",
                resource_id=request_id,
                department_id=row[0],
            )
        return {"status": payload.decision}
