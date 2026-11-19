"""Authenticated organization retrieval and cited answer endpoints."""

import json
import logging
import os
import queue
import threading
from collections.abc import Callable
from datetime import datetime
from time import perf_counter
from uuid import UUID

from anthropic import Anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from austra.access import Access
from austra.build_release.monitoring import record_metric
from austra.integrations.model_credentials import ModelCredentials
from austra.memory import (
    MEMORY_COMPACTION_CHARS,
    MEMORY_COMPACTION_MESSAGES,
    extract_memory_update,
)
from austra.observability import trace_turn
from austra.onboarding.routes import discovery_status_requested
from austra.platform.auth import SaasIdentity
from austra.retrieval.context import answer_packet
from austra.retrieval.service import retrieve
from austra.tenant.context import TenantContext
from austra.tenant.memory import (
    add_assistant_message,
    add_user_message,
    list_chats,
    memory_context,
    save_memory_update,
)
from austra.tenant.memory import (
    chat as stored_chat,
)
from austra.tenant.memory import (
    delete_chat as delete_stored_chat,
)

logger = logging.getLogger(__name__)


class RetrievalInput(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    chat_id: UUID | None = None
    department_id: UUID | None = None
    blueprint_id: UUID | None = None
    valid_time: datetime | None = None
    recorded_time: datetime | None = None


def install_retrieval_routes(app: FastAPI, identity: SaasIdentity) -> None:
    def chat_context(request: Request) -> TenantContext:
        actor, _remaining = identity.session(request.cookies.get("austra_session"))
        if actor is None or request.state.organization_slug in {None, "platform"}:
            raise HTTPException(status_code=403, detail="Organization session required")
        context = TenantContext.from_auth(actor)
        context.require("chat.use")
        return context

    @app.get("/api/org/chats")
    def organization_chats(request: Request):
        context = chat_context(request)
        with identity.connect() as connection:
            return list_chats(connection, context)

    @app.get("/api/org/chats/{chat_id}")
    def organization_chat(chat_id: UUID, request: Request):
        context = chat_context(request)
        try:
            with identity.connect() as connection:
                return {"chat": stored_chat(connection, context, chat_id), "drafts": []}
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.delete("/api/org/chats/{chat_id}")
    def delete_organization_chat(chat_id: UUID, request: Request):
        context = chat_context(request)
        try:
            with identity.connect() as connection:
                delete_stored_chat(connection, context, chat_id)
            return {"deleted": True, "audit_preserved": True}
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    def packet(request: Request, payload: RetrievalInput) -> dict:
        actor, _remaining = identity.session(request.cookies.get("austra_session"))
        if actor is None or request.state.organization_slug in {None, "platform"}:
            raise HTTPException(status_code=403, detail="Organization session required")
        try:
            context = TenantContext.from_auth(actor)
            with identity.connect() as connection:
                result = retrieve(connection, context, **payload.model_dump(exclude={"chat_id"}))
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

    def answer_question(
        payload: RetrievalInput,
        request: Request,
        on_text: Callable[[str], None] | None = None,
        on_accepted: Callable[[UUID, UUID], None] | None = None,
    ) -> dict:
        context = chat_context(request)
        started = perf_counter()
        with identity.connect() as connection:
            try:
                chat_id, message_id = add_user_message(
                    connection, context, payload.question, payload.chat_id
                )
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            if on_accepted:
                on_accepted(chat_id, message_id)
            prior_chat = stored_chat(connection, context, chat_id)
            memory = memory_context(connection, context, "ask", chat_id)
            organization = connection.execute(
                "SELECT name FROM organizations WHERE id = %s", (context.organization_id,)
            ).fetchone()
        key = (
            Access(identity.legacy_root).get("ANTHROPIC_API_KEY")
            if request.state.organization_slug == os.getenv("AUSTRA_LEGACY_ORGANIZATION_SLUG")
            else ModelCredentials(identity.legacy_root).get(context.organization_id)
        )
        if discovery_status_requested(payload.question):
            evidence = {"status": "not_applicable"}
            answer = {
                "status": "not_applicable",
                "answer": (
                    "This view searches approved business information. Switch to "
                    "Plan a process to ask about saved notes or connected apps."
                ),
                "citation_ids": [],
            }
        else:
            evidence = packet(request, payload)
            if evidence["status"] == "unknown" or evidence.get("aggregate"):
                answer = answer_packet(None, payload.question, evidence)
            else:
                if not key:
                    raise HTTPException(
                        status_code=503, detail="Anthropic connection is unavailable"
                    )
                try:
                    input_price = os.getenv("AUSTRA_INPUT_USD_PER_MILLION")
                    output_price = os.getenv("AUSTRA_OUTPUT_USD_PER_MILLION")
                    with trace_turn(
                        "retrieval_message",
                        organization_id=str(context.organization_id),
                        organization_name=organization[0] if organization else "",
                        user_id=str(context.actor.user_id),
                        chat_id=str(chat_id),
                        message_id=str(message_id),
                        source="organization_ask",
                    ):
                        answer = answer_packet(
                            Anthropic(api_key=key),
                            payload.question,
                            evidence,
                            input_usd_per_million=float(input_price) if input_price else None,
                            output_usd_per_million=float(output_price) if output_price else None,
                            conversation_context={
                                "recent_messages": prior_chat["messages"][-8:],
                                "memory": memory,
                            },
                            on_text=on_text,
                        )
                except ValueError as error:
                    _measure(
                        request,
                        message_id,
                        {
                            "status": "token_budget_breach"
                            if "token" in str(error).lower()
                            else "unknown"
                        },
                        started,
                    )
                    raise HTTPException(status_code=422, detail=str(error)) from error
        with identity.connect() as connection:
            add_assistant_message(connection, context, chat_id, answer["answer"])
            if key and evidence["status"] != "not_applicable":
                update = extract_memory_update(
                    key,
                    payload.question,
                    prior_chat["messages"][-20:]
                    + [{"role": "assistant", "body": answer["answer"]}],
                    memory["facts"],
                    [],
                    prior_compaction=memory["working_compaction"],
                    compaction_needed=(
                        sum(len(fact) for fact in memory["facts"]) >= MEMORY_COMPACTION_CHARS
                        or memory["pending_message_count"] + 1 >= MEMORY_COMPACTION_MESSAGES
                    ),
                )
                save_memory_update(
                    connection, context, "ask", chat_id, message_id, payload.question, update
                )
            result_chat = stored_chat(connection, context, chat_id)
        if evidence["status"] != "not_applicable":
            _measure(request, message_id, answer, started)
        return {"evidence": evidence, "answer": answer, "chat": result_chat}

    @app.post("/api/org/ask")
    def ask(payload: RetrievalInput, request: Request):
        return answer_question(payload, request)

    @app.post("/api/org/ask/stream")
    def stream_answer(payload: RetrievalInput, request: Request) -> StreamingResponse:
        chat_context(request)
        updates: queue.Queue[tuple[str, dict] | None] = queue.Queue()

        def produce() -> None:
            try:
                result = answer_question(
                    payload,
                    request,
                    on_text=lambda chunk: updates.put(("delta", {"text": chunk})),
                    on_accepted=lambda chat_id, message_id: updates.put(
                        ("accepted", {"chat_id": str(chat_id), "message_id": str(message_id)})
                    ),
                )
                updates.put(("done", result))
            except HTTPException as error:
                updates.put(("error", {"detail": error.detail}))
            except Exception:
                logger.exception("Organization answer stream failed")
                updates.put(("error", {"detail": "Austra could not finish the answer"}))
            finally:
                updates.put(None)

        def events():
            threading.Thread(target=produce, daemon=True, name="austra-answer-stream").start()
            yield 'event: stage\ndata: {"stage": "Searching evidence"}\n\n'
            while (update := updates.get()) is not None:
                event, data = update
                yield f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    def _measure(request: Request, task_id: UUID, answer: dict, started: float) -> None:
        actor, _remaining = identity.session(request.cookies.get("austra_session"))
        context = TenantContext.from_auth(actor)
        with identity.connect() as connection:
            record_metric(
                connection,
                context,
                "task_latency_ms",
                round((perf_counter() - started) * 1000, 3),
                task_id,
            )
            if answer["status"] == "supported":
                record_metric(connection, context, "task_resolved", task_id=task_id)
            if answer.get("estimated_cost_usd") is not None:
                record_metric(
                    connection, context, "task_cost_usd", answer["estimated_cost_usd"], task_id
                )
            if answer["status"] == "token_budget_breach":
                record_metric(connection, context, "token_budget_breach", task_id=task_id)
            elif answer.get("failure_reason") == "citation":
                record_metric(connection, context, "citation_failure", task_id=task_id)
