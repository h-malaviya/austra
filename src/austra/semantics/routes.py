"""Organization administrator semantic review endpoints."""

import json
from datetime import datetime
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from austra.memory import MemoryUpdate
from austra.platform.auth import SaasIdentity
from austra.semantics.service import (
    add_edge,
    add_revision,
    answer_question,
    confirm_candidate,
    decide_claim,
    extract_claims,
    propose_candidate,
    record_conflict,
    review_candidate,
)
from austra.tenant.context import TenantContext
from austra.tenant.memory import save_memory_update


class ClaimDecisionInput(BaseModel):
    decision: str
    rationale: str = Field(min_length=1)


class ConflictInput(BaseModel):
    first_claim_id: UUID
    second_claim_id: UUID
    reason: str = Field(min_length=1)


class RevisionInput(BaseModel):
    kind: str
    stable_key: str
    content: dict
    rationale: str = Field(min_length=1)
    claim_ids: list[UUID] = Field(min_length=1)
    owner_user_id: UUID | None = None
    valid_from: datetime
    valid_to: datetime | None = None


class EdgeInput(BaseModel):
    from_revision_id: UUID
    relation: str
    to_revision_id: UUID
    rationale: str = Field(min_length=1)


class CandidateInput(BaseModel):
    revision_ids: list[UUID]
    edge_ids: list[UUID]
    behavior: dict
    acceptance_examples: list[dict]


class ConfirmInput(BaseModel):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class AnswerInput(BaseModel):
    question_index: int = Field(ge=0)
    answer: str = Field(min_length=1, max_length=5000)


def install_semantic_routes(app: FastAPI, identity: SaasIdentity) -> None:
    def admin_context(request: Request) -> TenantContext:
        actor, _remaining = identity.session(request.cookies.get("austra_session"))
        if (
            actor is None
            or actor.role != "org_admin"
            or request.state.organization_slug in {None, "platform"}
        ):
            raise HTTPException(status_code=403, detail="Organization admin required")
        try:
            return TenantContext.from_auth(actor)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    def run(request: Request, operation):
        context = admin_context(request)
        try:
            with identity.connect() as connection:
                return operation(connection, context)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (ValueError, PermissionError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/org/semantic-chunks/{chunk_id}/claims", status_code=201)
    def claims(chunk_id: UUID, request: Request):
        return run(request, lambda db, actor: extract_claims(db, actor, chunk_id))

    @app.post("/api/org/semantic-claims/{claim_id}/decisions", status_code=201)
    def claim_decision(claim_id: UUID, payload: ClaimDecisionInput, request: Request):
        return run(
            request,
            lambda db, actor: {
                "id": decide_claim(db, actor, claim_id, payload.decision, payload.rationale)
            },
        )

    @app.post("/api/org/semantic-claims/conflicts", status_code=201)
    def conflict(payload: ConflictInput, request: Request):
        def save(db, actor):
            record_conflict(db, actor, **payload.model_dump())
            return {"recorded": True}

        return run(request, save)

    @app.post("/api/org/semantic-revisions", status_code=201)
    def revision(payload: RevisionInput, request: Request):
        return run(request, lambda db, actor: add_revision(db, actor, **payload.model_dump()))

    @app.post("/api/org/semantic-edges", status_code=201)
    def edge(payload: EdgeInput, request: Request):
        return run(request, lambda db, actor: {"id": add_edge(db, actor, **payload.model_dump())})

    @app.post("/api/org/onboarding-cases/{case_id}/candidate", status_code=201)
    def candidate(case_id: UUID, payload: CandidateInput, request: Request):
        return run(
            request,
            lambda db, actor: propose_candidate(
                db, actor, case_id, payload.model_dump(mode="json")
            ),
        )

    @app.get("/api/org/onboarding-cases/{case_id}/candidate")
    def candidate_review(case_id: UUID, request: Request):
        result = run(request, lambda db, actor: review_candidate(db, actor, case_id))
        if result is None:
            raise HTTPException(status_code=404, detail="No candidate")
        return result

    @app.post("/api/org/onboarding-cases/{case_id}/candidate/answers", status_code=201)
    def answer(case_id: UUID, payload: AnswerInput, request: Request):
        return run(
            request,
            lambda db, actor: answer_question(
                db, actor, case_id, payload.question_index, payload.answer
            ),
        )

    @app.post("/api/org/onboarding-cases/{case_id}/candidate/confirm")
    def confirm(case_id: UUID, payload: ConfirmInput, request: Request):
        def confirm_and_remember(db, actor):
            result = confirm_candidate(db, actor, case_id, payload.content_hash)
            candidate = review_candidate(db, actor, case_id)
            summary = json.dumps(
                {
                    "confirmed_blueprint_version": result["version"],
                    "behavior": candidate["behavior"],
                    "revisions": [
                        {"kind": item["kind"], "key": item["key"]}
                        for item in candidate["revisions"]
                    ],
                    "acceptance_examples": candidate["acceptance_examples"],
                },
                ensure_ascii=False,
                default=str,
            )
            save_memory_update(
                db,
                actor,
                "onboarding",
                actor.organization_id,
                result["blueprint_id"],
                "",
                MemoryUpdate(),
                confirmed_summary=summary,
            )
            return result

        return run(request, confirm_and_remember)
