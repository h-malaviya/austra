"""HTTP adapters for the shared capability command and release services."""

from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from austra.build_release.service import (
    create_build,
    fix_defect,
    preview_build,
    publish,
    rollback,
    transition_build,
)
from austra.capabilities.builder import build_capability
from austra.capabilities.contracts import CapabilityCommand
from austra.capabilities.projections import projection_data
from austra.capabilities.runtime import capability_view, execute_command, explain_event
from austra.capabilities.verifier import verify_build
from austra.platform.auth import SaasIdentity
from austra.tenant.context import TenantContext


class BuildInput(BaseModel):
    blueprint_id: UUID
    rollback_plan: str = Field(min_length=1)


class TransitionInput(BaseModel):
    target: str
    expected_revision: int = Field(ge=1)


class PreviewInput(BaseModel):
    expected_revision: int = Field(ge=1)
    accepted: bool
    expected: str | None = None
    observed: str | None = None
    trace: dict | None = None


class FixInput(BaseModel):
    expected_revision: int = Field(ge=1)


def install_capability_routes(app: FastAPI, identity: SaasIdentity) -> None:
    def run(request: Request, operation, *, admin: bool = False):
        actor, _remaining = identity.session(request.cookies.get("austra_session"))
        if actor is None or request.state.organization_slug in {None, "platform"}:
            raise HTTPException(status_code=403, detail="Organization session required")
        if admin and actor.role != "org_admin":
            raise HTTPException(status_code=403, detail="Organization admin required")
        try:
            context = TenantContext.from_auth(actor)
            with identity.connect() as connection:
                return operation(connection, context)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    def command(request: Request, payload: CapabilityCommand):
        return run(request, lambda db, context: execute_command(db, context, payload))

    app.add_api_route("/api/org/capability-commands", command, methods=["POST"])
    app.add_api_route("/api/org/capability-chat/commands", command, methods=["POST"])
    app.add_api_route("/api/org/capability-ui/commands", command, methods=["POST"])

    @app.get("/api/org/capability/{department_id}")
    def view(department_id: UUID, request: Request):
        return run(request, lambda db, context: capability_view(db, context, department_id))

    @app.get("/api/org/capability/{department_id}/projections/{key}")
    def projection(department_id: UUID, key: str, request: Request, offset: int = 0):
        def operation(db, context):
            from austra.capabilities.runtime import _active_build
            from austra.tenant.context import tenant_transaction

            with tenant_transaction(db, context):
                manifest = _active_build(db, context)["manifest"]
            return projection_data(db, context, department_id, key, manifest, offset=offset)

        return run(request, operation)

    @app.get("/api/org/capability-events/{department_id}/{event_id}")
    def event(department_id: UUID, event_id: UUID, request: Request):
        return run(request, lambda db, context: explain_event(db, context, department_id, event_id))

    @app.post("/api/org/capability-builds", status_code=201)
    def create(payload: BuildInput, request: Request):
        def operation(db, context):
            build = create_build(db, context, payload.blueprint_id)
            return build_capability(db, context, build["id"], payload.rollback_plan)

        return run(request, operation, admin=True)

    @app.post("/api/org/capability-builds/{build_id}/transitions")
    def transition(build_id: UUID, payload: TransitionInput, request: Request):
        return run(
            request,
            lambda db, context: {
                "revision": transition_build(
                    db, context, build_id, payload.target, payload.expected_revision, uuid4()
                )
            },
            admin=True,
        )

    @app.post("/api/org/capability-builds/{build_id}/verify")
    def verify(build_id: UUID, request: Request):
        return run(request, lambda db, context: verify_build(db, context, build_id), admin=True)

    @app.post("/api/org/capability-builds/{build_id}/preview")
    def preview(build_id: UUID, payload: PreviewInput, request: Request):
        return run(
            request,
            lambda db, context: preview_build(
                db,
                context,
                build_id,
                payload.expected_revision,
                payload.accepted,
                payload.expected,
                payload.observed,
                payload.trace,
            ),
            admin=True,
        )

    @app.post("/api/org/capability-defects/{defect_id}/fix")
    def fix(defect_id: UUID, payload: FixInput, request: Request):
        return run(
            request,
            lambda db, context: fix_defect(
                db, context, defect_id, payload.expected_revision, uuid4()
            ),
            admin=True,
        )

    @app.post("/api/org/capability-builds/{build_id}/release")
    def release(build_id: UUID, payload: FixInput, request: Request):
        return run(
            request,
            lambda db, context: {
                "release_id": publish(db, context, build_id, payload.expected_revision, uuid4())
            },
            admin=True,
        )

    @app.post("/api/org/capability-releases/{release_id}/rollback")
    def revert(release_id: UUID, request: Request):
        return run(
            request,
            lambda db, context: {"release_id": rollback(db, context, release_id, uuid4())},
            admin=True,
        )
