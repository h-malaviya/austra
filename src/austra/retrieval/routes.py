"""Authenticated organization retrieval and cited answer endpoints."""

import os
from datetime import datetime
from time import perf_counter
from uuid import UUID, uuid4

from anthropic import Anthropic
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from austra.access import Access
from austra.build_release.monitoring import record_metric
from austra.platform.auth import SaasIdentity
from austra.retrieval.context import answer_packet
from austra.retrieval.service import retrieve
from austra.tenant.context import TenantContext


class RetrievalInput(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    department_id: UUID | None = None
    blueprint_id: UUID | None = None
    valid_time: datetime | None = None
    recorded_time: datetime | None = None


def install_retrieval_routes(app: FastAPI, identity: SaasIdentity) -> None:
    def packet(request: Request, payload: RetrievalInput) -> dict:
        actor, _remaining = identity.session(request.cookies.get("austra_session"))
        if actor is None or request.state.organization_slug in {None, "platform"}:
            raise HTTPException(status_code=403, detail="Organization session required")
        try:
            context = TenantContext.from_auth(actor)
            with identity.connect() as connection:
                result = retrieve(connection, context, **payload.model_dump())
                if result["status"] == "unknown":
                    record_metric(connection, context, "retrieval_miss")
                return result
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/org/retrieval")
    def retrieve_evidence(payload: RetrievalInput, request: Request):
        return packet(request, payload)

    @app.post("/api/org/ask")
    def ask(payload: RetrievalInput, request: Request):
        started = perf_counter()
        task_id = uuid4()
        evidence = packet(request, payload)
        if evidence["status"] == "unknown" or evidence.get("aggregate"):
            answer = answer_packet(None, payload.question, evidence)
            _measure(request, task_id, answer, started)
            return {"evidence": evidence, "answer": answer}
        key = Access(identity.legacy_root).get("ANTHROPIC_API_KEY")
        if not key:
            raise HTTPException(status_code=503, detail="Anthropic connection is unavailable")
        try:
            input_price = os.getenv("AUSTRA_INPUT_USD_PER_MILLION")
            output_price = os.getenv("AUSTRA_OUTPUT_USD_PER_MILLION")
            answer = answer_packet(
                Anthropic(api_key=key), payload.question, evidence,
                input_usd_per_million=float(input_price) if input_price else None,
                output_usd_per_million=float(output_price) if output_price else None,
            )
        except ValueError as error:
            _measure(request, task_id,
                     {"status": "token_budget_breach" if "token" in str(error).lower()
                      else "unknown"}, started)
            raise HTTPException(status_code=422, detail=str(error)) from error
        _measure(request, task_id, answer, started)
        return {"evidence": evidence, "answer": answer}

    def _measure(request: Request, task_id: UUID, answer: dict, started: float) -> None:
        actor, _remaining = identity.session(request.cookies.get("austra_session"))
        context = TenantContext.from_auth(actor)
        with identity.connect() as connection:
            record_metric(connection, context, "task_latency_ms",
                          round((perf_counter() - started) * 1000, 3), task_id)
            if answer["status"] == "supported":
                record_metric(connection, context, "task_resolved", task_id=task_id)
            if answer.get("estimated_cost_usd") is not None:
                record_metric(connection, context, "task_cost_usd",
                              answer["estimated_cost_usd"], task_id)
            if answer["status"] == "token_budget_breach":
                record_metric(connection, context, "token_budget_breach", task_id=task_id)
            elif answer.get("failure_reason") == "citation":
                record_metric(connection, context, "citation_failure", task_id=task_id)
