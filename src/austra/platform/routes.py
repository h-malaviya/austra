"""Platform provisioning and account routes."""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from austra.platform.auth import SaasIdentity, normalize_username, password_hash, temporary_password
from austra.tenant.permissions.evaluator import AuthContext, permits

COOKIE = "austra_session"
SUPPORT_MINUTES = 30


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=200)


class OrganizationInput(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    name: str = Field(min_length=1, max_length=120)
    admin_username: str
    email: str | None = Field(default=None, max_length=254)
    contact_number: str | None = Field(default=None, max_length=40)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=120)
    admin_full_name: str | None = Field(default=None, max_length=120)
    admin_email: str | None = Field(default=None, max_length=254)
    admin_contact_number: str | None = Field(default=None, max_length=40)


class OrganizationDetails(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    contact_number: str | None = Field(default=None, max_length=40)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=120)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Organization name is required")
        return value.strip()


class AdminInput(BaseModel):
    username: str
    full_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    contact_number: str | None = Field(default=None, max_length=40)


class AdminDetails(AdminInput):
    status: str = "active"


def clean_contact(value: str | None) -> str | None:
    return value.strip() or None if value is not None else None


def validate_email(value: str | None) -> str | None:
    value = clean_contact(value)
    if value and ("@" not in value or "." not in value.rsplit("@", 1)[-1] or " " in value):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    return value


def require_organization(connection: psycopg.Connection, organization_id: UUID) -> None:
    row = connection.execute(
        "SELECT 1 FROM organizations WHERE id = %s AND deleted_at IS NULL", (organization_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Organization not found")


def audit_change(
    connection: psycopg.Connection, organization_id: UUID, actor_id: UUID, action: str,
    resource_id: UUID,
) -> None:
    connection.execute(
        "INSERT INTO audit_events (organization_id, actor_user_id, channel, action, "
        "resource_id, outcome, correlation_id) "
        "VALUES (%s, %s, 'api', %s, %s, 'success', gen_random_uuid())",
        (organization_id, actor_id, action, resource_id),
    )


class SupportInput(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


class OrganizationStatus(BaseModel):
    status: str


def install_platform_routes(app: FastAPI, identity: SaasIdentity) -> None:
    def current(request: Request) -> AuthContext:
        context, _remaining = identity.session(request.cookies.get(COOKIE))
        if context is None:
            raise HTTPException(status_code=401, detail="Sign in required")
        if not context.user_active or (
            context.role != "super_admin" and not context.organization_active
        ):
            raise HTTPException(status_code=403, detail="Account unavailable")
        return context

    def platform(request: Request) -> AuthContext:
        context = current(request)
        if context.role != "super_admin" or context.must_change_password:
            raise HTTPException(status_code=403, detail="Platform access required")
        return context

    @app.post("/api/auth/change-password")
    def change_password(payload: PasswordChange, request: Request, response: Response):
        context = current(request)
        if context.role == "super_admin":
            raise HTTPException(status_code=403, detail="Platform credential is managed in .env")
        try:
            identity.change_password(
                context.user_id, payload.current_password, payload.new_password
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        response.delete_cookie(COOKIE)
        return {"changed": True}

    @app.get("/api/platform/organizations")
    def list_organizations(request: Request):
        platform(request)
        with identity.connect() as connection:
            rows = connection.execute(
                "SELECT o.id, o.slug, o.name, o.status, o.email, o.contact_number, "
                "o.city, o.country, count(u.id) FILTER (WHERE u.id IS NOT NULL) "
                "FROM organizations o LEFT JOIN users u ON u.organization_id = o.id "
                "AND u.role = 'org_admin' AND u.deleted_at IS NULL "
                "WHERE o.deleted_at IS NULL GROUP BY o.id ORDER BY o.name"
            ).fetchall()
        return [
            {"id": str(row[0]), "slug": row[1], "name": row[2], "status": row[3],
             "email": row[4], "contact_number": row[5], "city": row[6],
             "country": row[7], "admin_count": row[8]} for row in rows
        ]

    @app.post("/api/platform/organizations", status_code=201)
    def create_organization(payload: OrganizationInput, request: Request):
        actor = platform(request)
        if payload.slug in {"admin", "platform"}:
            raise HTTPException(status_code=400, detail="Organization slug is reserved")
        if not payload.name.strip():
            raise HTTPException(status_code=400, detail="Organization name is required")
        try:
            admin_username = normalize_username(payload.admin_username)
            email = validate_email(payload.email)
            admin_email = validate_email(payload.admin_email)
            credential = temporary_password()
            with identity.connect() as connection:
                row = connection.execute(
                    "INSERT INTO organizations (slug, name, email, contact_number, city, country) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (payload.slug, payload.name.strip(), email, clean_contact(payload.contact_number),
                     clean_contact(payload.city), clean_contact(payload.country)),
                ).fetchone()
                connection.execute(
                    "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
                    "full_name, email, contact_number) "
                    "VALUES (%s, %s, 'org_admin', %s, %s, %s, %s)",
                    (row[0], admin_username, password_hash(credential),
                     clean_contact(payload.admin_full_name), admin_email,
                     clean_contact(payload.admin_contact_number)),
                )
                connection.execute(
                    "INSERT INTO audit_events (organization_id, actor_user_id, channel, "
                    "action, resource_id, outcome, correlation_id) "
                    "VALUES (%s, %s, 'api', 'organization.created', %s, 'success', gen_random_uuid())",
                    (row[0], actor.user_id, row[0]),
                )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except psycopg.errors.UniqueViolation as error:
            raise HTTPException(status_code=409, detail="Organization already exists") from error
        return {
            "id": str(row[0]),
            "slug": payload.slug,
            "admin_username": admin_username,
            "temporary_password": credential,
        }

    @app.patch("/api/platform/organizations/{organization_id}")
    def update_organization(organization_id: UUID, payload: OrganizationDetails, request: Request):
        actor = platform(request)
        with identity.connect() as connection:
            row = connection.execute(
                "UPDATE organizations SET name = %s, email = %s, contact_number = %s, "
                "city = %s, country = %s WHERE id = %s AND deleted_at IS NULL RETURNING id",
                (payload.name, validate_email(payload.email), clean_contact(payload.contact_number),
                 clean_contact(payload.city), clean_contact(payload.country), organization_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Organization not found")
            audit_change(connection, organization_id, actor.user_id, "organization.updated", row[0])
        return {"id": str(organization_id)}

    @app.get("/api/platform/organizations/{organization_id}/admins")
    def list_admins(organization_id: UUID, request: Request):
        platform(request)
        with identity.connect() as connection:
            require_organization(connection, organization_id)
            rows = connection.execute(
                "SELECT id, username_normalized, full_name, email, contact_number, status "
                "FROM users WHERE organization_id = %s AND role = 'org_admin' "
                "AND deleted_at IS NULL ORDER BY username_normalized",
                (organization_id,),
            ).fetchall()
        return [{"id": str(row[0]), "username": row[1], "full_name": row[2],
                 "email": row[3], "contact_number": row[4], "status": row[5]} for row in rows]

    @app.post("/api/platform/organizations/{organization_id}/admins", status_code=201)
    def create_admin(organization_id: UUID, payload: AdminInput, request: Request):
        actor = platform(request)
        try:
            username = normalize_username(payload.username)
            credential = temporary_password()
            with identity.connect() as connection:
                require_organization(connection, organization_id)
                row = connection.execute(
                    "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
                    "full_name, email, contact_number) "
                    "VALUES (%s, %s, 'org_admin', %s, %s, %s, %s) RETURNING id",
                    (organization_id, username, password_hash(credential),
                     clean_contact(payload.full_name), validate_email(payload.email),
                     clean_contact(payload.contact_number)),
                ).fetchone()
                audit_change(connection, organization_id, actor.user_id, "admin.created", row[0])
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except psycopg.errors.UniqueViolation as error:
            raise HTTPException(status_code=409, detail="Username already exists") from error
        return {"id": str(row[0]), "username": username, "temporary_password": credential}

    @app.patch("/api/platform/organizations/{organization_id}/admins/{admin_id}")
    def update_admin(organization_id: UUID, admin_id: UUID, payload: AdminDetails, request: Request):
        actor = platform(request)
        if payload.status not in {"active", "suspended"}:
            raise HTTPException(status_code=400, detail="Invalid admin status")
        try:
            username = normalize_username(payload.username)
            with identity.connect() as connection:
                require_organization(connection, organization_id)
                row = connection.execute(
                    "UPDATE users SET username_normalized = %s, full_name = %s, email = %s, "
                    "contact_number = %s, status = %s "
                    "WHERE id = %s AND organization_id = %s AND role = 'org_admin' "
                    "AND deleted_at IS NULL RETURNING id",
                    (username, clean_contact(payload.full_name), validate_email(payload.email),
                     clean_contact(payload.contact_number), payload.status, admin_id, organization_id),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Admin not found")
                if payload.status == "suspended":
                    connection.execute(
                        "UPDATE sessions SET revoked_at = now() WHERE user_id = %s "
                        "AND revoked_at IS NULL", (admin_id,),
                    )
                audit_change(connection, organization_id, actor.user_id, "admin.updated", admin_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except psycopg.errors.UniqueViolation as error:
            raise HTTPException(status_code=409, detail="Username already exists") from error
        return {"id": str(admin_id)}

    @app.post("/api/platform/organizations/{organization_id}/admins/{admin_id}/reset-password")
    def reset_admin_password(organization_id: UUID, admin_id: UUID, request: Request):
        actor = platform(request)
        credential = temporary_password()
        with identity.connect() as connection:
            require_organization(connection, organization_id)
            row = connection.execute(
                "UPDATE users SET password_hash = %s, must_change_password = true "
                "WHERE id = %s AND organization_id = %s AND role = 'org_admin' "
                "AND deleted_at IS NULL RETURNING id, username_normalized",
                (password_hash(credential), admin_id, organization_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Admin not found")
            connection.execute(
                "UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                (admin_id,),
            )
            audit_change(connection, organization_id, actor.user_id, "admin.password_reset", admin_id)
        return {"id": str(admin_id), "username": row[1], "temporary_password": credential}

    @app.patch("/api/platform/organizations/{organization_id}/status")
    def change_organization_status(
        organization_id: UUID, payload: OrganizationStatus, request: Request
    ):
        actor = platform(request)
        if payload.status not in {"active", "suspended"}:
            raise HTTPException(status_code=400, detail="Invalid organization status")
        with identity.connect() as connection:
            row = connection.execute(
                "UPDATE organizations SET status = %s, suspended_at = "
                "CASE WHEN %s = 'suspended' THEN now() ELSE NULL END "
                "WHERE id = %s AND deleted_at IS NULL RETURNING id",
                (payload.status, payload.status, organization_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Organization not found")
            if payload.status == "suspended":
                connection.execute(
                    "UPDATE sessions SET revoked_at = now() WHERE user_id IN "
                    "(SELECT id FROM users WHERE organization_id = %s) AND revoked_at IS NULL",
                    (organization_id,),
                )
            connection.execute(
                "INSERT INTO audit_events (organization_id, actor_user_id, channel, "
                "action, outcome, correlation_id) VALUES "
                "(%s, %s, 'api', %s, 'success', gen_random_uuid())",
                (organization_id, actor.user_id, f"organization.{payload.status}"),
            )
        return {"id": str(organization_id), "status": payload.status}

    @app.post("/api/platform/organizations/{organization_id}/support-context")
    def enter_support(organization_id: UUID, payload: SupportInput, request: Request):
        actor = platform(request)
        token = request.cookies.get(COOKIE, "")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with identity.connect() as connection:
            session = connection.execute(
                "SELECT id FROM sessions WHERE token_hash = %s AND revoked_at IS NULL "
                "AND expires_at > now()",
                (token_hash,),
            ).fetchone()
            organization = connection.execute(
                "SELECT id FROM organizations WHERE id = %s AND status = 'active' "
                "AND deleted_at IS NULL",
                (organization_id,),
            ).fetchone()
            if not session or not organization:
                raise HTTPException(status_code=404, detail="Organization not found")
            connection.execute(
                "UPDATE support_contexts SET ended_at = now() "
                "WHERE session_id = %s AND ended_at IS NULL",
                (session[0],),
            )
            expires_at = datetime.now(UTC) + timedelta(minutes=SUPPORT_MINUTES)
            row = connection.execute(
                "INSERT INTO support_contexts (session_id, super_admin_user_id, "
                "organization_id, reason, expires_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (session[0], actor.user_id, organization_id, payload.reason.strip(), expires_at),
            ).fetchone()
            connection.execute(
                "INSERT INTO audit_events (organization_id, actor_user_id, support_context_id, "
                "channel, action, outcome, reason, correlation_id) "
                "VALUES (%s, %s, %s, 'api', 'support.enter', 'success', %s, gen_random_uuid())",
                (organization_id, actor.user_id, row[0], payload.reason.strip()),
            )
        return {
            "id": str(row[0]),
            "organization_id": str(organization_id),
            "expires_at": expires_at,
        }

    @app.delete("/api/platform/support-context")
    def exit_support(request: Request):
        actor = platform(request)
        token_hash = hashlib.sha256(request.cookies[COOKIE].encode()).hexdigest()
        with identity.connect() as connection:
            row = connection.execute(
                "UPDATE support_contexts SET ended_at = now() WHERE session_id = "
                "(SELECT id FROM sessions WHERE token_hash = %s) AND ended_at IS NULL "
                "RETURNING id, organization_id",
                (token_hash,),
            ).fetchone()
            if row:
                connection.execute(
                    "INSERT INTO audit_events (organization_id, actor_user_id, support_context_id, "
                    "channel, action, outcome, correlation_id) "
                    "VALUES (%s, %s, %s, 'api', 'support.exit', 'success', gen_random_uuid())",
                    (row[1], actor.user_id, row[0]),
                )
        return {"exited": bool(row)}

    @app.get("/api/platform/support/objects")
    def support_objects(request: Request):
        context = platform(request)
        support = context.support
        if not support or not permits(context, support.organization_id, None, "records.read"):
            raise HTTPException(status_code=403, detail="Active support context required")
        with identity.connect() as connection:
            rows = connection.execute(
                "SELECT coalesce(d.name, 'Organization shared'), b.kind, count(*) "
                "FROM business_objects b LEFT JOIN departments d ON d.id = b.department_id "
                "WHERE b.organization_id = %s AND b.archived_at IS NULL "
                "GROUP BY d.name, b.kind ORDER BY d.name, b.kind",
                (support.organization_id,),
            ).fetchall()
        return [{"department": row[0], "kind": row[1], "count": row[2]} for row in rows]
