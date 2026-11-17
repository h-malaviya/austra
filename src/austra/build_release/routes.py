"""Admin review and observability endpoints."""

from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from austra.build_release.monitoring import (
    list_reviews,
    report_outcome_conflict,
    resolve_review,
    review_dashboard,
    scan_policy_expiry,
    triage_feedback,
)
from austra.platform.auth import SaasIdentity
from austra.tenant.context import TenantContext


class FeedbackInput(BaseModel):
    classification: str
    expected: str = Field(min_length=1, max_length=2000)
    observed: str = Field(min_length=1, max_length=2000)
    citations: list[dict] = Field(min_length=1)


class OutcomeInput(BaseModel):
    expected: str = Field(min_length=1, max_length=2000)
    observed: str = Field(min_length=1, max_length=2000)


def install_monitoring_routes(app: FastAPI, identity: SaasIdentity) -> None:
    def run(request: Request, operation):
        actor, _remaining = identity.session(request.cookies.get("austra_session"))
        if (actor is None or actor.role != "org_admin"
                or request.state.organization_slug in {None, "platform"}):
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

    @app.get("/api/org/review-cases")
    def reviews(request: Request):
        return run(request, list_reviews)

    @app.post("/api/org/review-cases/scan-expiry")
    def expiry(request: Request):
        return run(request, lambda db, context: {"case_ids": scan_policy_expiry(db, context)})

    @app.post("/api/org/review-cases/{case_id}/resolve")
    def resolve(case_id: UUID, request: Request):
        return run(request, lambda db, context: {"resolved":
                   resolve_review(db, context, case_id) is None})

    @app.post("/api/org/review-cases/outcome-conflicts/{event_id}")
    def outcome(event_id: UUID, payload: OutcomeInput, request: Request):
        return run(request, lambda db, context: {"case_id": report_outcome_conflict(
            db, context, event_id, payload.expected, payload.observed)})

    @app.post("/api/org/releases/{release_id}/triage")
    def triage(release_id: UUID, payload: FeedbackInput, request: Request):
        return run(request, lambda db, context: triage_feedback(
            db, context, release_id, payload.classification, payload.expected,
            payload.observed, payload.citations))

    @app.get("/api/org/observability")
    def observability(request: Request):
        return run(request, review_dashboard)
