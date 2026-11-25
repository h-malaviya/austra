"""Google consent started from a tenant's workflow conversation."""

import hashlib
import logging
import re
import secrets
import time
from base64 import urlsafe_b64encode
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from austra.integrations.connections import connect_key, list_connections, workspace_apps
from austra.integrations.google_workspace import (
    GOOGLE_WORKSPACE_ENDPOINTS,
    GOOGLE_WORKSPACE_SCOPE,
    WorkspaceConsentError,
    authorization_url,
    exchange_code,
    workspace_credentials,
)
from austra.integrations.providers import discover
from austra.integrations.secrets import ConnectionSecrets
from austra.mcp_gateway import ExternalToolError
from austra.platform.auth import SaasIdentity
from austra.storage.orm import OnboardingCase
from austra.storage.schema_contract import mapped_query
from austra.tenant.context import TenantContext, tenant_transaction

WORKSPACE_STATE_PREFIX = "workspace."
STATE_FORMAT = re.compile(r"workspace\.([0-9a-f]{32})\.([0-9a-f]{32})")
CONSENT_SECONDS = 600
logger = logging.getLogger(__name__)


class WorkspaceConsent(BaseModel):
    workflow_id: UUID | None = None


def _discover_authorized(endpoint: str, token: str) -> list[dict]:
    try:
        return discover(endpoint, token)
    except (ExternalToolError, ValueError) as error:
        logger.warning(
            "Google Workspace tool discovery unavailable for %s (%s)",
            urlsplit(endpoint).hostname,
            type(error).__name__,
        )
        return []


def install_workspace_oauth(
    app: FastAPI,
    identity: SaasIdentity,
    root: Path,
    redirect_uri: str,
    registration: Callable[[], tuple[str, str]],
) -> Callable[[Request], RedirectResponse]:
    pending = ConnectionSecrets(root / "google_workspace_consent")
    callback_origin = urlsplit(redirect_uri)

    def admin(token: str, expected_org: UUID | None = None) -> TenantContext:
        actor, _ = identity.session(token)
        if (
            actor is None
            or actor.role != "org_admin"
            or (expected_org and actor.organization_id != expected_org)
        ):
            raise HTTPException(status_code=403, detail="Organization admin session required")
        try:
            context = TenantContext.from_auth(actor)
            context.require("integrations.use")
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return context

    @app.post("/api/org/connections/google-workspace/start")
    def start(payload: WorkspaceConsent, request: Request):
        context = admin(request.cookies.get("austra_session", ""))
        slug = request.state.organization_slug
        if not slug or slug == "platform":
            raise HTTPException(status_code=403, detail="Organization session required")
        if payload.workflow_id:
            with identity.connect() as db, tenant_transaction(db, context):
                workflow = mapped_query(
                    db,
                    select(OnboardingCase.id).where(
                        OnboardingCase.organization_id == context.organization_id,
                        OnboardingCase.id == payload.workflow_id,
                    ),
                ).fetchone()
            if workflow is None:
                raise HTTPException(status_code=404, detail="Workflow draft not found")
        client_id, secret = registration()
        if not client_id or not secret:
            raise HTTPException(
                status_code=503, detail="Google Workspace sign-in needs a platform OAuth client"
            )
        attempt = uuid4()
        verifier = secrets.token_urlsafe(48)
        challenge = (
            urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        )
        state = f"{WORKSPACE_STATE_PREFIX}{context.organization_id.hex}.{attempt.hex}"
        try:
            pending.save(
                context.organization_id,
                attempt,
                {
                    "session": request.cookies["austra_session"],
                    "actor": str(context.actor.user_id),
                    "slug": slug,
                    "verifier": verifier,
                    "client_id": client_id,
                    "expires": str(int(time.time()) + CONSENT_SECONDS),
                    **({"workflow": str(payload.workflow_id)} if payload.workflow_id else {}),
                },
            )
        except (RuntimeError, ValueError, OSError) as error:
            raise HTTPException(
                status_code=503, detail="Google Workspace consent could not start"
            ) from error
        return {"authorization_url": authorization_url(client_id, redirect_uri, state, challenge)}

    @app.get("/api/org/connections/google-workspace/status")
    def status(request: Request):
        context = admin(request.cookies.get("austra_session", ""))
        with identity.connect() as db:
            return {"apps": workspace_apps(db, context)}

    def finish(request: Request) -> RedirectResponse:
        if request.state.organization_slug is not None:
            raise HTTPException(status_code=400, detail="Use the registered Google callback")
        match = STATE_FORMAT.fullmatch(request.query_params.get("state", ""))
        if not match:
            raise HTTPException(status_code=400, detail="Google Workspace consent state is invalid")
        organization_id, attempt = (UUID(hex=value) for value in match.groups())
        try:
            values = pending.take(organization_id, attempt)
        except (ValueError, RuntimeError, OSError) as error:
            raise HTTPException(
                status_code=400, detail="Google consent expired. Connect again from the workflow."
            ) from error
        context = admin(values["session"], organization_id)
        if str(context.actor.user_id) != values["actor"]:
            raise HTTPException(status_code=403, detail="Google Workspace consent session changed")

        outcome = "denied"
        code = request.query_params.get("code", "")
        if int(values["expires"]) < time.time():
            outcome = "expired"
        elif code and len(code) <= 2048 and not request.query_params.get("error"):
            outcome = "failed"
            client_id, secret = registration()
            if client_id == values["client_id"] and secret:
                phase = "consent_exchange"
                try:
                    token, refresh, email = exchange_code(
                        code, client_id, secret, redirect_uri, values["verifier"]
                    )
                    phase = "catalog_unavailable"
                    endpoints = list(GOOGLE_WORKSPACE_ENDPOINTS.values())
                    with ThreadPoolExecutor(max_workers=len(endpoints)) as pool:
                        tools = list(
                            pool.map(
                                lambda endpoint: _discover_authorized(endpoint, token), endpoints
                            )
                        )
                    credentials = workspace_credentials(client_id, secret, refresh)
                    phase = "store_unavailable"
                    with identity.connect() as db, db.transaction():
                        db.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (f"google-workspace:{organization_id}:{email}",),
                        )
                        existing = {
                            (item["endpoint_url"], item["account"]): item["id"]
                            for item in list_connections(db, context)
                            if GOOGLE_WORKSPACE_SCOPE in item["scopes"]
                        }
                        for (name, endpoint), discovered in zip(
                            GOOGLE_WORKSPACE_ENDPOINTS.items(), tools, strict=True
                        ):
                            label = f"{name} · {email}"
                            prior = existing.get((endpoint, label))
                            connect_key(
                                db,
                                context,
                                root,
                                "remote_mcp",
                                {},
                                endpoint,
                                label,
                                UUID(prior) if prior else None,
                                google_workspace=(credentials, discovered),
                            )
                    outcome = "connected" if all(tools) else "partial"
                except WorkspaceConsentError as error:
                    outcome = error.reason
                    logger.warning("Google Workspace consent failed (%s)", outcome)
                except (ExternalToolError, ValueError, RuntimeError, OSError) as error:
                    outcome = phase
                    logger.warning(
                        "Google Workspace consent failed during %s (%s)",
                        phase,
                        type(error).__name__,
                    )

        query = {"workspace_oauth": outcome}
        if "workflow" in values:
            query["workflow"] = values["workflow"]
        port = f":{callback_origin.port}" if callback_origin.port else ""
        return RedirectResponse(
            f"{callback_origin.scheme}://{values['slug']}.localhost{port}/?{urlencode(query)}",
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    return finish
