"""Organization administration over tenant scoped identity tables."""

import json
import os
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from austra.platform.auth import SaasIdentity, normalize_username, password_hash, temporary_password
from austra.tenant.audit import record_event
from austra.tenant.permissions.evaluator import AuthContext, permits
from austra.tenant.permissions.keys import DEPARTMENT_PERMISSIONS

COOKIE = "austra_session"


class DepartmentInput(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    name: str = Field(min_length=1, max_length=120)


class DepartmentUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class UserInput(BaseModel):
    username: str
    full_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    contact_number: str | None = Field(default=None, max_length=40)


class UserUpdate(UserInput):
    status: str


class RoleInput(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    name: str = Field(min_length=1, max_length=120)
    permissions: list[str]
    reason: str = Field(min_length=3, max_length=500)


class RoleAssignment(BaseModel):
    role_id: UUID


class ArchiveInput(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


def install_tenant_routes(app: FastAPI, identity: SaasIdentity) -> None:
    def actor(request: Request) -> AuthContext:
        context, _remaining = identity.session(request.cookies.get(COOKIE))
        if context is None:
            raise HTTPException(status_code=401, detail="Sign in required")
        if (
            not context.user_active
            or not context.organization_active
            or context.must_change_password
        ):
            raise HTTPException(status_code=403, detail="Account unavailable")
        if context.organization_id is None:
            raise HTTPException(status_code=403, detail="Organization context required")
        return context

    def admin(request: Request) -> AuthContext:
        context = actor(request)
        if context.role != "org_admin":
            raise HTTPException(status_code=403, detail="Organization admin access required")
        return context

    def department(connection: psycopg.Connection, organization_id: UUID, department_id: UUID):
        row = connection.execute(
            "SELECT id, slug, name, archived_at FROM departments "
            "WHERE organization_id = %s AND id = %s",
            (organization_id, department_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Department not found")
        return row

    @app.get("/api/org/departments")
    def list_departments(request: Request):
        context = actor(request)
        with identity.connect() as connection:
            legacy = connection.execute(
                "SELECT id FROM organizations WHERE slug = %s",
                (os.environ["AUSTRA_LEGACY_ORGANIZATION_SLUG"],),
            ).fetchone()
            if context.role == "org_admin" and legacy and legacy[0] == context.organization_id:
                identity.refresh_legacy_catalog_if_changed(connection, context.organization_id)
            rows = connection.execute(
                "SELECT d.id, d.slug, d.name, d.archived_at, count(b.id) "
                "FROM departments d LEFT JOIN business_objects b "
                "ON b.department_id = d.id AND b.archived_at IS NULL "
                "WHERE d.organization_id = %s GROUP BY d.id ORDER BY d.name",
                (context.organization_id,),
            ).fetchall()
        return [
            {
                "id": str(row[0]),
                "slug": row[1],
                "name": row[2],
                "archived": row[3] is not None,
                "object_count": row[4],
            }
            for row in rows
            if context.role == "org_admin"
            or any(
                permits(context, context.organization_id, row[0], permission)
                for permission in DEPARTMENT_PERMISSIONS
            )
        ]

    @app.post("/api/org/departments", status_code=201)
    def create_department(payload: DepartmentInput, request: Request):
        context = admin(request)
        try:
            with identity.connect() as connection:
                row = connection.execute(
                    "INSERT INTO departments (organization_id, slug, name) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (context.organization_id, payload.slug, payload.name.strip()),
                ).fetchone()
                record_event(
                    connection,
                    context,
                    "department.created",
                    resource_id=row[0],
                    department_id=row[0],
                )
        except psycopg.errors.UniqueViolation as error:
            raise HTTPException(status_code=409, detail="Department slug already exists") from error
        return {"id": str(row[0]), "slug": payload.slug, "name": payload.name.strip()}

    @app.patch("/api/org/departments/{department_id}")
    def rename_department(department_id: UUID, payload: DepartmentUpdate, request: Request):
        context = admin(request)
        with identity.connect() as connection:
            row = connection.execute(
                "UPDATE departments SET name = %s WHERE organization_id = %s "
                "AND id = %s AND archived_at IS NULL RETURNING id",
                (payload.name.strip(), context.organization_id, department_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Department not found")
            record_event(
                connection,
                context,
                "department.renamed",
                resource_id=department_id,
                department_id=department_id,
            )
        return {"id": str(row[0]), "name": payload.name.strip()}

    @app.get("/api/org/departments/{department_id}/archive-impact")
    def archive_impact(department_id: UUID, request: Request):
        context = admin(request)
        with identity.connect() as connection:
            department(connection, context.organization_id, department_id)
            rows = connection.execute(
                "SELECT kind, count(*) FROM business_objects WHERE organization_id = %s "
                "AND department_id = %s AND archived_at IS NULL GROUP BY kind",
                (context.organization_id, department_id),
            ).fetchall()
        return {"objects": {kind: count for kind, count in rows}}

    @app.delete("/api/org/departments/{department_id}")
    def archive_department(department_id: UUID, payload: ArchiveInput, request: Request):
        context = admin(request)
        with identity.connect() as connection:
            row = department(connection, context.organization_id, department_id)
            if row[3] is not None:
                raise HTTPException(status_code=409, detail="Department already archived")
            active_objects = connection.execute(
                "SELECT count(*) FROM business_objects WHERE organization_id = %s "
                "AND department_id = %s AND archived_at IS NULL",
                (context.organization_id, department_id),
            ).fetchone()[0]
            if active_objects:
                raise HTTPException(
                    status_code=409,
                    detail="Reassign or archive department objects before archiving this department",
                )
            connection.execute(
                "UPDATE departments SET archived_at = now(), archived_by = %s, "
                "archive_reason = %s WHERE id = %s",
                (context.user_id, payload.reason.strip(), department_id),
            )
            connection.execute(
                "UPDATE department_memberships SET revoked_at = now() "
                "WHERE department_id = %s AND revoked_at IS NULL",
                (department_id,),
            )
            connection.execute(
                "UPDATE role_assignments SET revoked_at = now() "
                "WHERE department_id = %s AND revoked_at IS NULL",
                (department_id,),
            )
            record_event(
                connection,
                context,
                "department.archived",
                resource_id=department_id,
                department_id=department_id,
                reason=payload.reason.strip(),
            )
        return {"archived": True}

    @app.post("/api/org/departments/{department_id}/restore")
    def restore_department(department_id: UUID, request: Request):
        context = admin(request)
        with identity.connect() as connection:
            row = department(connection, context.organization_id, department_id)
            if row[3] is None:
                raise HTTPException(status_code=409, detail="Department is active")
            conflict = connection.execute(
                "SELECT id FROM departments WHERE organization_id = %s AND slug = %s "
                "AND archived_at IS NULL",
                (context.organization_id, row[1]),
            ).fetchone()
            if conflict:
                raise HTTPException(status_code=409, detail="Department slug is in use")
            connection.execute(
                "UPDATE departments SET archived_at = NULL, archived_by = NULL, "
                "archive_reason = NULL WHERE id = %s",
                (department_id,),
            )
            record_event(
                connection,
                context,
                "department.restored",
                resource_id=department_id,
                department_id=department_id,
            )
        return {"restored": True, "assignments_restored": False}

    @app.get("/api/org/departments/{department_id}/objects")
    def list_objects(department_id: UUID, request: Request):
        context = actor(request)
        if context.role != "org_admin" and not any(
            permits(context, context.organization_id, department_id, permission)
            for permission in DEPARTMENT_PERMISSIONS
        ):
            raise HTTPException(status_code=403, detail="Department access required")
        with identity.connect() as connection:
            row = department(connection, context.organization_id, department_id)
            if row[3] is not None:
                raise HTTPException(status_code=404, detail="Department not found")
            rows = connection.execute(
                "SELECT kind, external_id, source_path FROM business_objects "
                "WHERE organization_id = %s AND department_id = %s AND archived_at IS NULL "
                "ORDER BY kind, external_id",
                (context.organization_id, department_id),
            ).fetchall()
        required = {
            "chat": "chat.use",
            "record": "records.read",
            "evidence": "evidence.read",
            "generated": "evidence.read",
            "artifact": "policies.read",
            "draft": "policies.draft",
        }
        return [
            {"kind": kind, "id": external_id}
            for kind, external_id, _path in rows
            if context.role == "org_admin"
            or (
                required.get(kind) is not None
                and permits(context, context.organization_id, department_id, required[kind])
            )
        ]

    @app.get("/api/org/departments/{department_id}/records")
    def list_department_records(department_id: UUID, request: Request):
        context = actor(request)
        if not permits(context, context.organization_id, department_id, "records.read"):
            raise HTTPException(status_code=403, detail="Records access required")
        with identity.connect() as connection:
            row = department(connection, context.organization_id, department_id)
            if row[3] is not None:
                raise HTTPException(status_code=404, detail="Department not found")
            paths = connection.execute(
                "SELECT source_path FROM business_objects WHERE organization_id = %s "
                "AND department_id = %s AND kind = 'record' AND archived_at IS NULL",
                (context.organization_id, department_id),
            ).fetchall()
        records = []
        root = identity.legacy_root.resolve()
        for (source_path,) in paths:
            path = (root / source_path).resolve()
            if not path.is_relative_to(root):
                raise HTTPException(status_code=500, detail="Invalid catalog path")
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            if not item.get("deleted_at"):
                records.append(item)
        return records

    @app.get("/api/org/users")
    def list_users(request: Request):
        context = admin(request)
        with identity.connect() as connection:
            rows = connection.execute(
                "SELECT id, username_normalized, role, status, must_change_password, "
                "full_name, email, contact_number "
                "FROM users WHERE organization_id = %s AND deleted_at IS NULL "
                "ORDER BY username_normalized",
                (context.organization_id,),
            ).fetchall()
        return [
            {
                "id": str(row[0]),
                "username": row[1],
                "role": row[2],
                "status": row[3],
                "must_change_password": row[4],
                "full_name": row[5],
                "email": row[6],
                "contact_number": row[7],
            }
            for row in rows
        ]

    @app.post("/api/org/users", status_code=201)
    def create_user(payload: UserInput, request: Request):
        context = admin(request)
        try:
            username = normalize_username(payload.username)
            credential = temporary_password()
            with identity.connect() as connection:
                row = connection.execute(
                    "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
                    "full_name, email, contact_number) "
                    "VALUES (%s, %s, 'employee', %s, %s, %s, %s) RETURNING id",
                    (context.organization_id, username, password_hash(credential),
                     payload.full_name.strip() or None if payload.full_name else None,
                     payload.email.strip() or None if payload.email else None,
                     payload.contact_number.strip() or None if payload.contact_number else None),
                ).fetchone()
                record_event(connection, context, "user.created", resource_id=row[0])
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except psycopg.errors.UniqueViolation as error:
            raise HTTPException(status_code=409, detail="Username already exists") from error
        return {"id": str(row[0]), "username": username, "temporary_password": credential}

    @app.patch("/api/org/users/{user_id}")
    def update_user(user_id: UUID, payload: UserUpdate, request: Request):
        context = admin(request)
        if payload.status not in {"active", "suspended"}:
            raise HTTPException(status_code=400, detail="Invalid user status")
        try:
            username = normalize_username(payload.username)
            with identity.connect() as connection:
                row = connection.execute(
                    "UPDATE users SET username_normalized = %s, full_name = %s, email = %s, "
                    "contact_number = %s, status = %s "
                    "WHERE id = %s AND organization_id = %s AND role = 'employee' "
                    "AND deleted_at IS NULL RETURNING id",
                    (username, payload.full_name.strip() or None if payload.full_name else None,
                     payload.email.strip() or None if payload.email else None,
                     payload.contact_number.strip() or None if payload.contact_number else None,
                     payload.status, user_id, context.organization_id),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Employee not found")
                if payload.status == "suspended":
                    connection.execute(
                        "UPDATE sessions SET revoked_at = now() WHERE user_id = %s "
                        "AND revoked_at IS NULL", (user_id,),
                    )
                record_event(connection, context, "user.updated", resource_id=user_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except psycopg.errors.UniqueViolation as error:
            raise HTTPException(status_code=409, detail="Username already exists") from error
        return {"id": str(user_id), "username": username, "status": payload.status}

    @app.post("/api/org/users/{user_id}/reset-password")
    def reset_password(user_id: UUID, request: Request):
        context = admin(request)
        credential = temporary_password()
        with identity.connect() as connection:
            row = connection.execute(
                "UPDATE users SET password_hash = %s, must_change_password = true "
                "WHERE id = %s AND organization_id = %s AND deleted_at IS NULL RETURNING id",
                (password_hash(credential), user_id, context.organization_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="User not found")
            connection.execute(
                "UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )
            record_event(connection, context, "user.password_reset", resource_id=user_id)
        return {"id": str(user_id), "temporary_password": credential}

    @app.get("/api/org/departments/{department_id}/roles")
    def list_roles(department_id: UUID, request: Request):
        context = admin(request)
        with identity.connect() as connection:
            department(connection, context.organization_id, department_id)
            rows = connection.execute(
                "SELECT r.id, r.slug, r.name, r.current_version, "
                "array_remove(array_agg(p.permission_key), NULL) "
                "FROM department_roles r LEFT JOIN role_versions v "
                "ON v.role_id = r.id AND v.version = r.current_version "
                "LEFT JOIN role_permissions p ON p.role_version_id = v.id "
                "WHERE r.organization_id = %s AND r.department_id = %s "
                "AND r.archived_at IS NULL GROUP BY r.id ORDER BY r.name",
                (context.organization_id, department_id),
            ).fetchall()
        return [
            {
                "id": str(row[0]),
                "slug": row[1],
                "name": row[2],
                "version": row[3],
                "permissions": row[4],
            }
            for row in rows
        ]

    @app.get("/api/org/departments/{department_id}/members")
    def list_members(department_id: UUID, request: Request):
        context = admin(request)
        with identity.connect() as connection:
            department(connection, context.organization_id, department_id)
            rows = connection.execute(
                "SELECT u.id, u.username_normalized, array_remove(array_agg(r.name), NULL) "
                "FROM department_memberships m JOIN users u ON u.id = m.user_id "
                "LEFT JOIN role_assignments a ON a.department_membership_id = m.id "
                "AND a.revoked_at IS NULL LEFT JOIN department_roles r ON r.id = a.role_id "
                "WHERE m.organization_id = %s AND m.department_id = %s "
                "AND m.revoked_at IS NULL GROUP BY u.id ORDER BY u.username_normalized",
                (context.organization_id, department_id),
            ).fetchall()
        return [{"id": str(row[0]), "username": row[1], "roles": row[2]} for row in rows]

    @app.post("/api/org/departments/{department_id}/roles", status_code=201)
    def create_role(department_id: UUID, payload: RoleInput, request: Request):
        context = admin(request)
        if not set(payload.permissions) <= DEPARTMENT_PERMISSIONS:
            raise HTTPException(status_code=400, detail="Unknown or admin-only permission")
        try:
            with identity.connect() as connection:
                row = department(connection, context.organization_id, department_id)
                if row[3] is not None:
                    raise HTTPException(status_code=409, detail="Department archived")
                role_id = connection.execute(
                    "INSERT INTO department_roles (organization_id, department_id, "
                    "slug, name, current_version) VALUES (%s, %s, %s, %s, 1) RETURNING id",
                    (context.organization_id, department_id, payload.slug, payload.name.strip()),
                ).fetchone()[0]
                version_id = connection.execute(
                    "INSERT INTO role_versions (role_id, version, change_reason, approved_by) "
                    "VALUES (%s, 1, %s, %s) RETURNING id",
                    (role_id, payload.reason.strip(), context.user_id),
                ).fetchone()[0]
                for permission in set(payload.permissions):
                    connection.execute(
                        "INSERT INTO role_permissions (role_version_id, permission_key) "
                        "VALUES (%s, %s)",
                        (version_id, permission),
                    )
                record_event(
                    connection,
                    context,
                    "role.created",
                    resource_id=role_id,
                    department_id=department_id,
                    reason=payload.reason.strip(),
                )
        except psycopg.errors.UniqueViolation as error:
            raise HTTPException(status_code=409, detail="Role slug already exists") from error
        return {"id": str(role_id), "version": 1}

    @app.post("/api/org/departments/{department_id}/members/{user_id}/roles")
    def assign_role(department_id: UUID, user_id: UUID, payload: RoleAssignment, request: Request):
        context = admin(request)
        with identity.connect() as connection:
            row = department(connection, context.organization_id, department_id)
            if row[3] is not None:
                raise HTTPException(status_code=404, detail="Department not found")
            target = connection.execute(
                "SELECT id FROM users WHERE id = %s AND organization_id = %s "
                "AND status = 'active' AND deleted_at IS NULL",
                (user_id, context.organization_id),
            ).fetchone()
            role = connection.execute(
                "SELECT id FROM department_roles WHERE id = %s AND organization_id = %s "
                "AND department_id = %s AND archived_at IS NULL",
                (payload.role_id, context.organization_id, department_id),
            ).fetchone()
            if not target or not role:
                raise HTTPException(status_code=404, detail="User or role not found")
            membership = connection.execute(
                "INSERT INTO department_memberships (organization_id, department_id, user_id) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                (context.organization_id, department_id, user_id),
            ).fetchone()
            if membership is None:
                membership = connection.execute(
                    "SELECT id FROM department_memberships WHERE department_id = %s "
                    "AND user_id = %s AND revoked_at IS NULL",
                    (department_id, user_id),
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
                "role.assigned",
                resource_id=payload.role_id,
                department_id=department_id,
            )
        return {"assigned": True}
