"""Private organization connection setup and approved action endpoints."""

import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from austra.connectors import gmail_authorization_url
from austra.integrations.catalog import catalog
from austra.integrations.connections import (
    check_health,
    connect_gmail,
    connect_key,
    list_connections,
    review_remote,
    revoke,
)
from austra.integrations.email_jobs import deliver_email_job, preview_email_job
from austra.integrations.execution import execute_proposal
from austra.integrations.gateway import approve_workflow, preview_action
from austra.mcp_gateway import ExternalToolError, call_external
from austra.platform.auth import SaasIdentity
from austra.tenant.context import TenantContext

OAUTH_SECONDS = 600


class KeyInput(BaseModel):
    template: str
    values: dict[str, str]
    endpoint_url: str | None = None
    account_label: str | None = None


class RemoteReviewInput(BaseModel):
    action_name: str
    effect: str
    scopes: list[str] = Field(default_factory=list)
    receipt_field: str = ""


class ApprovalInput(BaseModel):
    department_id: UUID
    connection_id: UUID
    build_id: UUID
    action_name: str
    fixture: dict
    trigger_fixture: dict
    mapping: dict[str, str] = Field(default_factory=dict)


class PreviewInput(BaseModel):
    department_id: UUID
    approval_id: UUID
    arguments: dict
    source_records: list[dict] = Field(default_factory=list)
    reason: str = ""


class ExecuteInput(BaseModel):
    department_id: UUID
    proposal_id: UUID
    idempotency_key: str


class ReconnectInput(BaseModel):
    connection_id: UUID | None = None


class EmailJobInput(BaseModel):
    department_id: UUID
    proposal_id: UUID


def install_integration_routes(app: FastAPI, identity: SaasIdentity, root: Path) -> None:
    oauth_states: dict[str, tuple[float, UUID, UUID, UUID | None, str, str]] = {}

    def gmail_redirect() -> str:
        configured = os.getenv("AUSTRA_GMAIL_REDIRECT_URI", "")
        if configured:
            return configured
        public = urlsplit(os.getenv("AUSTRA_PUBLIC_URL", ""))
        if public.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("Platform Gmail redirect URI must be configured")
        port = f":{public.port}" if public.port else ""
        return f"{public.scheme}://admin.localhost{port}/api/org/connections/gmail/callback"

    def run(request: Request, operation):
        actor, _ = identity.session(request.cookies.get("austra_session"))
        if actor is None or request.state.organization_slug in {None, "platform"}:
            raise HTTPException(status_code=403, detail="Organization session required")
        try:
            context = TenantContext.from_auth(actor)
            with identity.connect() as db:
                return operation(db, context)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (ValueError, RuntimeError, TypeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ExternalToolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/org/connection-catalog")
    def connection_catalog(request: Request):
        return run(request, lambda _db, context: catalog() if context.actor.role == "org_admin"
                   else [])

    @app.get("/api/org/connections")
    def connections(request: Request):
        return run(request, list_connections)

    @app.post("/api/org/connections")
    def connect(payload: KeyInput, request: Request):
        return run(request, lambda db, context: connect_key(
            db, context, root, payload.template, payload.values, payload.endpoint_url,
            payload.account_label))

    @app.put("/api/org/connections/{connection_id}")
    def reconnect(connection_id: UUID, payload: KeyInput, request: Request):
        return run(request, lambda db, context: connect_key(
            db, context, root, payload.template, payload.values, payload.endpoint_url,
            payload.account_label, connection_id))

    @app.post("/api/org/connections/{connection_id}/review-action")
    def review(connection_id: UUID, payload: RemoteReviewInput, request: Request):
        return run(request, lambda db, context: review_remote(
            db, context, connection_id, payload.action_name, payload.effect,
            payload.scopes, payload.receipt_field))

    @app.post("/api/org/connections/{connection_id}/health")
    def health(connection_id: UUID, request: Request):
        return run(request, lambda db, context: check_health(db, context, root, connection_id))

    @app.delete("/api/org/connections/{connection_id}")
    def disconnect(connection_id: UUID, request: Request):
        return run(request, lambda db, context: revoke(db, context, root, connection_id)
                   or {"revoked": True})

    @app.post("/api/org/connection-approvals")
    def approve(payload: ApprovalInput, request: Request):
        return run(request, lambda db, context: approve_workflow(
            db, context, root, payload.department_id, payload.connection_id,
            payload.build_id, payload.action_name, payload.fixture, payload.trigger_fixture,
            payload.mapping))

    @app.post("/api/org/connection-actions/preview")
    def preview(payload: PreviewInput, request: Request):
        return run(request, lambda db, context: preview_action(
            db, context, payload.department_id, payload.approval_id,
            payload.arguments, payload.source_records, payload.reason))

    @app.post("/api/org/connection-actions/execute")
    def execute(payload: ExecuteInput, request: Request):
        return run(request, lambda db, context: execute_proposal(
            db, context, root, payload.department_id, payload.proposal_id,
            payload.idempotency_key))

    @app.post("/api/org/email-jobs/{department_id}/{job_id}/preview")
    def email_preview(department_id: UUID, job_id: UUID, request: Request):
        return run(request, lambda db, context: preview_email_job(
            db, context, department_id, job_id))

    @app.post("/api/org/email-jobs/{job_id}/deliver")
    def email_deliver(job_id: UUID, payload: EmailJobInput, request: Request):
        return run(request, lambda db, context: deliver_email_job(
            db, context, root, payload.department_id, job_id, payload.proposal_id))

    @app.post("/api/org/connections/gmail/start")
    def start_gmail(request: Request, payload: ReconnectInput | None = None):
        client_id = os.getenv("AUSTRA_GMAIL_CLIENT_ID", "")
        client_secret = os.getenv("AUSTRA_GMAIL_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise HTTPException(status_code=503, detail="Gmail OAuth is unavailable; platform setup is needed")
        def start(_db, context):
            if context.actor.role != "org_admin":
                raise PermissionError("Organization admin required")
            state = secrets.token_urlsafe(32)
            oauth_states[state] = (time.monotonic() + OAUTH_SECONDS, context.organization_id,
                                   context.actor.user_id,
                                   payload.connection_id if payload else None,
                                   request.cookies.get("austra_session", ""),
                                   request.state.organization_slug)
            return {"authorization_url": gmail_authorization_url(
                client_id, gmail_redirect(), state)}
        return run(request, start)

    @app.get("/api/org/connections/gmail/callback")
    def finish_gmail(request: Request):
        state = request.query_params.get("state", "")
        pending = oauth_states.pop(state, None)
        code = request.query_params.get("code", "")
        if not pending or pending[0] < time.monotonic() or not code or len(code) > 2048:
            raise HTTPException(status_code=400, detail="Gmail consent expired; reconnect")
        actor, _ = identity.session(pending[4])
        if actor is None or actor.organization_id != pending[1] or actor.user_id != pending[2]:
            raise HTTPException(status_code=403, detail="Gmail consent session expired")
        context = TenantContext.from_auth(actor)
        try:
            redirect = gmail_redirect()
            result = call_external("gmail_exchange_code", {"code": code, "redirect_uri": redirect},
                                   {"GMAIL_CLIENT_ID": os.environ["AUSTRA_GMAIL_CLIENT_ID"],
                                    "GMAIL_CLIENT_SECRET": os.environ["AUSTRA_GMAIL_CLIENT_SECRET"]})
            token = result.get("refresh_token")
            email = result.get("email")
            raw_scopes = result.get("scopes")
            if (not isinstance(token, str) or not isinstance(email, str)
                    or not isinstance(raw_scopes, str)):
                raise TypeError("Google did not return a usable Gmail consent; reconnect")
            scopes = raw_scopes.split()
            with identity.connect() as db:
                connect_gmail(db, context, root, token, email, tuple(scopes),
                              os.environ["AUSTRA_GMAIL_CLIENT_ID"],
                              os.environ["AUSTRA_GMAIL_CLIENT_SECRET"], pending[3])
        except (ExternalToolError, ValueError, TypeError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        public = urlsplit(os.getenv("AUSTRA_PUBLIC_URL", ""))
        port = public.port
        suffix = f":{port}" if port else ""
        return RedirectResponse(
            f"{public.scheme}://{pending[5]}.localhost{suffix}"
            "/organization/connections?oauth=connected",
            status_code=303)
