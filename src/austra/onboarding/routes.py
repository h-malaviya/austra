"""Organization administrator interview and evidence intake routes."""

from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from austra.onboarding.evidence import (
    MAX_SOURCE_BYTES,
    blob_root,
    exclude_source,
    list_sources,
    preview_source,
    process_job,
    save_file,
    save_interview,
    save_manual,
    save_website_page,
    set_source_access,
)
from austra.onboarding.service import current_case
from austra.onboarding.website import crawl, public_target
from austra.platform.auth import SaasIdentity
from austra.tenant.context import TenantContext


class InterviewInput(BaseModel):
    business_description: str = Field(min_length=1, max_length=10000)
    desired_outcome: str = Field(min_length=1, max_length=10000)


class WebsiteInput(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class ManualInput(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_SOURCE_BYTES)


class ExclusionInput(BaseModel):
    excluded: bool


class AccessInput(BaseModel):
    access_label: str
    department_id: UUID | None = None
    owner_user_id: UUID | None = None


def install_onboarding_routes(app: FastAPI, identity: SaasIdentity) -> None:
    def admin_context(request: Request) -> TenantContext:
        actor, _remaining = identity.session(request.cookies.get("austra_session"))
        if actor is None or actor.role != "org_admin":
            raise HTTPException(status_code=403, detail="Organization admin required")
        if request.state.organization_slug in {None, "platform"}:
            raise HTTPException(status_code=403, detail="Organization host required")
        try:
            return TenantContext.from_auth(actor)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @app.get("/api/org/onboarding-cases/current")
    def get_current_case(request: Request):
        context = admin_context(request)
        with identity.connect() as connection:
            case = current_case(connection, context)
        if case is None:
            raise HTTPException(status_code=404, detail="No onboarding case")
        next_actions = {
            "created": ["start_collecting"],
            "collecting": ["continue_collecting", "resolve_questions"],
            "clarifying": ["answer_questions"],
            "ready_for_review": ["review_candidate"],
            "locked_candidate": ["build_candidate"],
        }
        return {**case, "next_actions": next_actions[case["state"]]}

    @app.put("/api/org/onboarding-cases/current/interview")
    def put_interview(payload: InterviewInput, request: Request):
        context = admin_context(request)
        try:
            with identity.connect() as connection:
                return save_interview(connection, context, payload.business_description,
                                      payload.desired_outcome)
        except (ValueError, PermissionError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/org/onboarding-sources")
    def get_sources(request: Request):
        context = admin_context(request)
        with identity.connect() as connection:
            return list_sources(connection, context)

    @app.post("/api/org/onboarding-sources/files", status_code=201)
    async def upload_file(request: Request, upload: Annotated[UploadFile, File()], linkedin: bool = False):
        context = admin_context(request)
        content = await upload.read(MAX_SOURCE_BYTES + 1)
        await upload.close()
        try:
            with identity.connect() as connection:
                return save_file(connection, context, blob_root(), upload.filename or "", content,
                                 linkedin=linkedin)
        except (ValueError, UnicodeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/org/onboarding-sources/linkedin-text", status_code=201)
    def linkedin_text(payload: ManualInput, request: Request):
        context = admin_context(request)
        try:
            with identity.connect() as connection:
                return save_manual(connection, context, blob_root(), payload.text)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/org/onboarding-sources/website", status_code=201)
    def website(payload: WebsiteInput, request: Request):
        context = admin_context(request)
        try:
            public_target(payload.url)
            pages = []
            for url, content in crawl(payload.url):
                with identity.connect() as connection:
                    pages.append(save_website_page(connection, context, blob_root(), url, content))
            return {"pages": pages}
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/org/onboarding-sources/revisions/{revision_id}/process")
    def run_extraction(revision_id: UUID, request: Request):
        context = admin_context(request)
        try:
            with identity.connect() as connection:
                return process_job(connection, context, blob_root(), revision_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/org/onboarding-sources/{source_id}/preview")
    def source_preview(source_id: UUID, request: Request):
        context = admin_context(request)
        with identity.connect() as connection:
            return preview_source(connection, context, source_id)

    @app.patch("/api/org/onboarding-sources/{source_id}")
    def set_excluded(source_id: UUID, payload: ExclusionInput, request: Request):
        context = admin_context(request)
        try:
            with identity.connect() as connection:
                exclude_source(connection, context, source_id, payload.excluded)
            return {"id": source_id, "excluded": payload.excluded}
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.patch("/api/org/onboarding-sources/{source_id}/access")
    def source_access(source_id: UUID, payload: AccessInput, request: Request):
        context = admin_context(request)
        try:
            with identity.connect() as connection:
                set_source_access(connection, context, source_id, **payload.model_dump())
            return {"id": source_id, **payload.model_dump()}
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (ValueError, PermissionError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
