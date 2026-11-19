"""Organization administrator interview and evidence intake routes."""

import json
import logging
import queue
import re
import threading
from collections.abc import Callable
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from austra.memory import (
    MEMORY_COMPACTION_CHARS,
    MEMORY_COMPACTION_MESSAGES,
    extract_memory_update,
)
from austra.observability import trace_turn
from austra.onboarding.conversation import (
    InterviewTurn,
    generate_turn,
    ingest_social_page,
    ingest_website,
)
from austra.onboarding.discovery import (
    WORKING_CONTEXT_CHARS,
    DiscoveryTurn,
    generate_discovery_turn,
    merge_unresolved,
)
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
from austra.tenant.context import TenantContext, tenant_transaction
from austra.tenant.memory import memory_context, save_memory_update

logger = logging.getLogger(__name__)


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


class TourMessage(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    revision: int = Field(ge=1)


def explicit_company_confirmation(body: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*(?:yes|yes,? that(?:'|’)s right|correct|confirmed|that(?:'|’)s right|"
            r"nothing to add+|no changes|no,? that(?:'|’)s it|that(?:'|’)s (?:all|it)|"
            r"everything is (?:ok|okay|correct|right)|all (?:good|correct|set)|"
            r"(?:looks|sounds) (?:good|right|correct|fine)|no corrections)[.!\s]*",
            body.casefold(),
        )
    )


def supplied_public_links(body: str) -> list[str]:
    normalized = body.replace(r"\.", ".")
    candidates = re.findall(
        r"(?<![\w@])(?:https?://|www\.|linkedin\.com/|facebook\.com/|"
        r"instagram\.com/|x\.com/|twitter\.com/|youtube\.com/|clutch\.co/)"
        r"[^\s<>\[\](){}]+",
        normalized,
        re.IGNORECASE,
    )
    links = []
    for candidate in candidates:
        link = candidate.rstrip(".,;:!?")
        if not re.match(r"https?://", link, re.IGNORECASE):
            link = f"https://{link}"
        if link not in links:
            links.append(link)
    return links


def recap_visible(summary: str, reply: str) -> bool:
    summary_words = {word for word in re.findall(r"\w{4,}", summary.casefold())}
    reply_words = set(re.findall(r"\w{4,}", reply.casefold()))
    return bool(summary_words and len(summary_words & reply_words) / len(summary_words) >= 0.4)


def social_page_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in (
            "linkedin.com",
            "facebook.com",
            "instagram.com",
            "x.com",
            "twitter.com",
            "youtube.com",
            "clutch.co",
        )
    )


def offer_social_page(
    turn: InterviewTurn,
    state: dict,
    body: str,
    social_candidate: str,
    website_status: str | None,
) -> InterviewTurn:
    social_already_offered = any(
        message["role"] == "assistant"
        and re.search(r"\b(?:linkedin|social page)\b", message["body"], re.IGNORECASE)
        for message in state["messages"]
    )
    social_declined = bool(
        re.search(
            r"\b(?:no|none|don't|do not)\b.*\b(?:linkedin|social)\b",
            body,
            re.IGNORECASE,
        )
    )
    if (
        turn.ready_for_confirmation
        and not (state["social_url"] or social_candidate or social_already_offered)
        and not social_declined
        and not re.search(r"\b(?:linkedin|social page)\b", turn.reply, re.IGNORECASE)
    ):
        turn.ready_for_confirmation = False
        turn.confirmed = False
        turn.reply = (f"{website_status}\n\n" if website_status else "") + (
            "Do you have a LinkedIn or other public company page I should check "
            "before we finish the introduction? It’s optional."
        )
    return turn


def discovery_status_requested(body: str) -> bool:
    return bool(
        re.search(
            r"\b(?:did you|have you|can you|what .*available|check|inspect)\b.*"
            r"\b(?:sav(?:e|ed)|handoff|handed off|connections?|connectors?|tools?|docker|mocks?)\b",
            body,
            re.IGNORECASE,
        )
    )


class OrganizationInfo(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    about_organization: str = Field(min_length=1, max_length=5000)
    desired_outcome: str | None = Field(default=None, max_length=5000)
    website_url: str | None = Field(default=None, max_length=2048)
    social_url: str | None = Field(default=None, max_length=2048)


def public_link(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    link = value.strip()
    parsed = urlparse(link)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or any(character.isspace() for character in link)
    ):
        raise ValueError("Enter a full http or https URL")
    return link


def install_onboarding_routes(
    app: FastAPI, identity: SaasIdentity, model_key: Callable[[UUID], str]
) -> None:
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

    def tour_state(request: Request) -> dict:
        context = admin_context(request)
        with identity.connect() as connection:
            row = connection.execute(
                "SELECT o.name, u.username_normalized, u.full_name, u.email, u.contact_number "
                "FROM organizations o JOIN users u ON u.id = %s WHERE o.id = %s",
                (context.actor.user_id, context.organization_id),
            ).fetchone()
            with tenant_transaction(connection, context):
                tour = connection.execute(
                    "SELECT about_admin, about_organization, website_url, social_url, "
                    "completed_at, desired_outcome, summary, review_ready, website_status, "
                    "revision, discovery_state, social_status, website_evidence, "
                    "social_evidence FROM organization_tours "
                    "WHERE organization_id = %s",
                    (context.organization_id,),
                ).fetchone()
                messages = connection.execute(
                    "SELECT id, role, body FROM onboarding_messages WHERE organization_id = %s "
                    "ORDER BY position",
                    (context.organization_id,),
                ).fetchall()
        if row is None:
            raise HTTPException(status_code=404, detail="Organization not found")
        return {
            "organization_name": row[0],
            "username": row[1],
            "full_name": row[2],
            "email": row[3],
            "contact_number": row[4],
            "about_admin": tour[0] if tour else None,
            "about_organization": tour[1] if tour else None,
            "website_url": tour[2] if tour else None,
            "social_url": tour[3] if tour else None,
            "completed": bool(tour and tour[4]),
            "desired_outcome": tour[5] if tour else None,
            "summary": tour[6] if tour else None,
            "review_ready": bool(tour and tour[7]),
            "website_status": tour[8] if tour else None,
            "social_status": tour[11] if tour else None,
            "website_evidence": tour[12] if tour else None,
            "social_evidence": tour[13] if tour else None,
            "revision": tour[9] if tour else 0,
            "discovery_state": tour[10] if tour else {},
            "messages": [
                {"id": str(message_id), "role": role, "body": body}
                for message_id, role, body in messages
            ],
        }

    def observed_turn(request: Request, state: dict, message_id: UUID, source: str):
        context = admin_context(request)
        return trace_turn(
            "onboarding_message",
            organization_id=str(context.organization_id),
            organization_name=state["organization_name"],
            user_id=str(context.actor.user_id),
            session_id=f"onboarding:{context.organization_id}",
            message_id=str(message_id),
            source=source,
        )

    @app.get("/api/org/tour")
    def get_tour(request: Request):
        return tour_state(request)

    def save_turn(
        request: Request,
        expected_revision: int,
        turn: InterviewTurn,
        user_body: str | None = None,
        website_status: str | None = None,
        social_status: str | None = None,
        website_evidence: str | None = None,
        social_evidence: str | None = None,
        message_id: UUID | None = None,
    ) -> dict:
        context = admin_context(request)
        with identity.connect() as connection, tenant_transaction(connection, context):
            connection.execute(
                "INSERT INTO organization_tours (organization_id) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                (context.organization_id,),
            )
            prior = connection.execute(
                "SELECT revision, review_ready, completed_at, summary, about_admin, "
                "about_organization "
                "FROM organization_tours "
                "WHERE organization_id = %s FOR UPDATE",
                (context.organization_id,),
            ).fetchone()
            if prior[0] != expected_revision:
                raise HTTPException(
                    status_code=409, detail="Onboarding changed. Refresh and try again"
                )
            confirmed = bool(user_body and prior[1] and explicit_company_confirmation(user_body))
            summary = turn.summary.strip() or None
            ready = bool(
                prior[1]
                if confirmed
                else (
                    turn.ready_for_confirmation
                    and summary
                    and (turn.about_organization.strip() or bool(prior[5]))
                    and (turn.about_admin.strip() or bool(prior[4]))
                )
            )
            summary_to_store = summary if ready else prior[3] if confirmed else None
            reply = turn.reply.strip()
            if (
                ready
                and summary
                and (not prior[1] or (website_status or social_status) and summary != prior[3])
                and not recap_visible(summary, reply)
            ):
                reply = f"{summary}\n\nDoes that look right?"
            if turn.confirmed and not confirmed:
                if (
                    user_body
                    and explicit_company_confirmation(user_body)
                    and ready
                    and summary
                    and not prior[1]
                ):
                    reply = f"{summary}\n\nDoes that look right?"
                elif website_status:
                    reply = (
                        f"{website_status}\n\nDo you have a LinkedIn or other public "
                        "company page you’d like me to check too? It’s optional."
                    )
                elif social_status:
                    reply = f"{social_status}\n\nIs there anything you’d like me to clarify?"
                else:
                    reply = "I’m still checking the company details. What should I clarify?"
            if confirmed:
                reply = (
                    "Thanks, your company introduction is saved. You can explore the "
                    "other pages now, or tell me what you’d like to work on next. 🙂"
                )
            connection.execute(
                "UPDATE organization_tours SET about_admin = coalesce(nullif(%s, ''), about_admin), "
                "about_organization = coalesce(nullif(%s, ''), about_organization), "
                "website_url = coalesce(nullif(%s, ''), website_url), "
                "social_url = coalesce(nullif(%s, ''), social_url), "
                "desired_outcome = coalesce(nullif(%s, ''), desired_outcome), "
                "summary = %s, review_ready = %s, "
                "website_status = coalesce(%s, website_status), "
                "social_status = coalesce(%s, social_status), "
                "website_evidence = coalesce(%s, website_evidence), "
                "social_evidence = coalesce(%s, social_evidence), revision = revision + 1, "
                "completed_at = CASE WHEN %s THEN now() ELSE completed_at END, "
                "updated_at = now() WHERE organization_id = %s",
                (
                    turn.about_admin.strip(),
                    turn.about_organization.strip(),
                    turn.website_url.strip(),
                    turn.social_url.strip(),
                    turn.desired_outcome.strip(),
                    summary_to_store,
                    ready,
                    website_status,
                    social_status,
                    website_evidence,
                    social_evidence,
                    confirmed,
                    context.organization_id,
                ),
            )
            position = connection.execute(
                "SELECT coalesce(max(position), 0) FROM onboarding_messages "
                "WHERE organization_id = %s",
                (context.organization_id,),
            ).fetchone()[0]
            if user_body:
                connection.execute(
                    "INSERT INTO onboarding_messages (id, organization_id, position, role, body) "
                    "VALUES (%s, %s, %s, 'user', %s)",
                    (message_id or uuid4(), context.organization_id, position + 1, user_body),
                )
                position += 1
            connection.execute(
                "INSERT INTO onboarding_messages (id, organization_id, position, role, body) "
                "VALUES (%s, %s, %s, 'assistant', %s)",
                (
                    message_id if not user_body and message_id else uuid4(),
                    context.organization_id,
                    position + 1,
                    reply,
                ),
            )
            if turn.about_organization.strip() and turn.desired_outcome.strip():
                connection.execute(
                    "UPDATE onboarding_cases SET business_description = %s, "
                    "desired_outcome = %s, state = CASE WHEN state = 'created' "
                    "THEN 'collecting' ELSE state END, revision = revision + 1, "
                    "updated_at = now() WHERE organization_id = %s "
                    "AND state IN ('created', 'collecting', 'clarifying')",
                    (
                        turn.about_organization.strip(),
                        turn.desired_outcome.strip(),
                        context.organization_id,
                    ),
                )
        updated_state = tour_state(request)
        if user_body and message_id:
            key = model_key(context.organization_id)
            if key:
                with identity.connect() as connection:
                    prior_memory = memory_context(
                        connection, context, "onboarding", context.organization_id
                    )
                with observed_turn(request, updated_state, message_id, "tour_memory"):
                    update = extract_memory_update(
                        key,
                        user_body,
                        updated_state["messages"],
                        prior_memory["facts"],
                        [],
                        prior_compaction=prior_memory["working_compaction"],
                        compaction_needed=(
                            sum(len(fact) for fact in prior_memory["facts"])
                            >= MEMORY_COMPACTION_CHARS
                            or prior_memory["pending_message_count"] >= MEMORY_COMPACTION_MESSAGES
                        ),
                    )
                with identity.connect() as connection:
                    save_memory_update(
                        connection,
                        context,
                        "onboarding",
                        context.organization_id,
                        message_id,
                        user_body,
                        update,
                        confirmed_summary=(summary_to_store or "") if confirmed else "",
                    )
        return updated_state

    @app.post("/api/org/tour/start")
    def start_tour(request: Request):
        context = admin_context(request)
        state = tour_state(request)
        if state["messages"] or state["completed"]:
            return state
        key = model_key(context.organization_id)
        if not key:
            raise HTTPException(status_code=409, detail="Add your model API key first")
        message_id = uuid4()
        try:
            with observed_turn(request, state, message_id, "tour"):
                turn = generate_turn(key, state["organization_name"], [], state)
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return save_turn(request, state["revision"], turn, message_id=message_id)

    def send_tour_message(
        payload: TourMessage,
        request: Request,
        on_text: Callable[[str], None] | None = None,
    ):
        context = admin_context(request)
        state = tour_state(request)
        if state["completed"]:
            raise HTTPException(status_code=409, detail="Onboarding is complete")
        if not state["messages"] or state["revision"] != payload.revision:
            raise HTTPException(status_code=409, detail="Onboarding changed. Refresh and try again")
        key = model_key(context.organization_id)
        if not key:
            raise HTTPException(status_code=409, detail="Add your model API key first")
        body = payload.body.strip()
        if not body:
            raise HTTPException(status_code=400, detail="Please write a message")
        message_id = uuid4()
        with identity.connect() as connection:
            known_memory = memory_context(
                connection, context, "onboarding", context.organization_id
            )
        history = [*state["messages"], {"role": "user", "body": body}]
        try:
            prior_evidence = "\n\n".join(
                filter(None, (state.get("website_evidence"), state.get("social_evidence")))
            )
            with observed_turn(request, state, message_id, "tour"):
                turn = generate_turn(
                    key,
                    state["organization_name"],
                    history,
                    {**state, "memory_context": known_memory},
                    prior_evidence,
                    **({"on_text": on_text} if on_text else {}),
                )
            website_status = None
            social_status = None
            website_evidence = None
            social_evidence = None
            accepted_website_url = ""
            accepted_social_url = ""
            supplied_urls = supplied_public_links(body)
            model_social_urls = set(supplied_public_links(turn.social_url))
            social_candidate = next(
                (url for url in supplied_urls if social_page_url(url) or url in model_social_urls),
                "",
            )
            website_candidate = next((url for url in supplied_urls if url != social_candidate), "")
            read_all_requested = bool(
                re.search(
                    r"\b(?:read|crawl|scan|check|go through)\b.*"
                    r"\b(?:all|every|whole|entire)\b.*\b(?:pages?|site|website)\b",
                    body,
                    re.IGNORECASE,
                )
            )
            if read_all_requested and not website_candidate:
                website_candidate = state["website_url"] or ""
            if website_candidate and (
                website_candidate != state["website_url"]
                or not state["website_status"]
                or (state["website_status"] or "").startswith("Could not read")
                or read_all_requested
            ):
                try:
                    website_url = public_link(website_candidate)
                except ValueError as error:
                    website_status = str(error)
                    turn.website_url = ""
                else:
                    result = ingest_website(
                        website_url, identity, context, full_scan=read_all_requested
                    )
                    website_status = result.status
                    website_evidence = result.evidence
                    accepted_website_url = website_url
            if social_candidate and (
                social_candidate != state["social_url"]
                or not state["social_status"]
                or (state["social_status"] or "").startswith("Could not read")
            ):
                try:
                    social_url = public_link(social_candidate)
                except ValueError as error:
                    social_status = str(error)
                    turn.social_url = ""
                else:
                    result = ingest_social_page(social_url, identity, context)
                    social_status = result.status
                    social_evidence = result.evidence
                    accepted_social_url = social_url
            if website_status or social_status:
                with observed_turn(request, state, message_id, "tour"):
                    turn = generate_turn(
                        key,
                        state["organization_name"],
                        history,
                        {
                            **state,
                            "memory_context": known_memory,
                            "website_url": accepted_website_url or state["website_url"],
                            "social_url": accepted_social_url or state["social_url"],
                            "website_status": website_status or state["website_status"],
                            "social_status": social_status or state["social_status"],
                        },
                        "\n\n".join(
                            filter(
                                None,
                                (
                                    website_evidence
                                    if website_evidence is not None
                                    else state.get("website_evidence"),
                                    social_evidence
                                    if social_evidence is not None
                                    else state.get("social_evidence"),
                                ),
                            )
                        ),
                    )
                if website_status and (
                    "page" not in turn.reply.casefold()
                    or ("safety limit" in website_status and "limit" not in turn.reply.casefold())
                ):
                    turn.reply = f"{website_status}\n\n{turn.reply}"
                if (
                    social_status
                    and social_status.startswith("Could not read")
                    and not re.search(
                        r"\b(?:could not|couldn't|unable to|was blocked|is blocked|failed to)\b",
                        turn.reply,
                        re.IGNORECASE,
                    )
                ):
                    turn.reply = f"{social_status}\n\n{turn.reply}"
            turn.website_url = accepted_website_url
            turn.social_url = accepted_social_url
            turn = offer_social_page(turn, state, body, social_candidate, website_status)
            return save_turn(
                request,
                payload.revision,
                turn,
                body,
                website_status,
                social_status,
                website_evidence,
                social_evidence,
                message_id,
            )
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/org/tour/message")
    def post_tour_message(payload: TourMessage, request: Request):
        return send_tour_message(payload, request)

    def send_discovery_message(
        payload: TourMessage,
        request: Request,
        on_text: Callable[[str], None] | None = None,
    ):
        context = admin_context(request)
        state = tour_state(request)
        if not state["completed"]:
            raise HTTPException(status_code=409, detail="Finish the company introduction first")
        if state["revision"] != payload.revision:
            raise HTTPException(
                status_code=409, detail="Conversation changed. Refresh and try again"
            )
        body = payload.body.strip()
        if not body:
            raise HTTPException(status_code=400, detail="Please write a message")
        message_id = uuid4()
        current = state["discovery_state"] or {}
        with identity.connect() as connection:
            prior_memory = memory_context(
                connection, context, "onboarding", context.organization_id
            )
        key = model_key(context.organization_id)
        pending_history = state["messages"][current.get("compacted_through", 0) :]
        compact = (
            sum(len(item["body"]) for item in pending_history) + len(body) >= WORKING_CONTEXT_CHARS
            or len(pending_history) + 2 >= MEMORY_COMPACTION_MESSAGES
        )
        if discovery_status_requested(body):
            with identity.connect() as connection, tenant_transaction(connection, context):
                connected = connection.execute(
                    "SELECT template_key, health FROM integration_connections "
                    "WHERE organization_id = %s AND revoked_at IS NULL ORDER BY template_key",
                    (context.organization_id,),
                ).fetchall()
            apps = ", ".join(f"{name} ({health})" for name, health in connected)
            turn = DiscoveryTurn(
                reply=(
                    f"This conversation is saved ({len(state['messages'])} messages). "
                    "This chat has not created or handed off a workflow. "
                    f"Connected app records: {apps or 'none'}. "
                    "This chat cannot run app tools. Review their details in Connections "
                    "and workflow drafts in Onboarding."
                ),
                working_summary=current.get("working_summary", ""),
                unresolved_questions=current.get("unresolved_questions", []),
                resolved_questions=[],
            )
        else:
            if not key:
                raise HTTPException(status_code=409, detail="Add your model API key first")
            try:
                with observed_turn(request, state, message_id, "discovery"):
                    turn = generate_discovery_turn(
                        key,
                        state["organization_name"],
                        state["about_organization"] or "",
                        [*state["messages"], {"role": "user", "body": body}],
                        {**current, "memory": prior_memory},
                        **({"on_text": on_text} if on_text else {}),
                    )
            except RuntimeError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
        compact = compact and bool(turn.working_summary.strip())
        updated = {
            "working_summary": turn.working_summary
            if compact
            else current.get("working_summary", ""),
            "compacted_through": len(state["messages"]) + 2
            if compact
            else current.get("compacted_through", 0),
            "unresolved_questions": merge_unresolved(current.get("unresolved_questions", []), turn),
        }
        with identity.connect() as connection, tenant_transaction(connection, context):
            prior = connection.execute(
                "SELECT revision, completed_at FROM organization_tours "
                "WHERE organization_id = %s FOR UPDATE",
                (context.organization_id,),
            ).fetchone()
            if prior is None or prior[1] is None or prior[0] != payload.revision:
                raise HTTPException(
                    status_code=409, detail="Conversation changed. Refresh and try again"
                )
            position = connection.execute(
                "SELECT coalesce(max(position), 0) FROM onboarding_messages "
                "WHERE organization_id = %s",
                (context.organization_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO onboarding_messages (id, organization_id, position, role, body) "
                "VALUES (%s, %s, %s, 'user', %s), (%s, %s, %s, 'assistant', %s)",
                (
                    message_id,
                    context.organization_id,
                    position + 1,
                    body,
                    uuid4(),
                    context.organization_id,
                    position + 2,
                    turn.reply,
                ),
            )
            connection.execute(
                "UPDATE organization_tours SET discovery_state = %s, revision = revision + 1, "
                "updated_at = now() WHERE organization_id = %s",
                (Jsonb(updated), context.organization_id),
            )
            if updated["working_summary"].strip():
                connection.execute(
                    "UPDATE onboarding_cases SET business_description = %s, "
                    "desired_outcome = %s, state = CASE WHEN state = 'created' "
                    "THEN 'collecting' ELSE state END, revision = revision + 1, "
                    "updated_at = now() WHERE organization_id = %s "
                    "AND state IN ('created', 'collecting', 'clarifying')",
                    (
                        state["about_organization"] or state["organization_name"],
                        updated["working_summary"],
                        context.organization_id,
                    ),
                )
        if key and not discovery_status_requested(body):
            updated_state = tour_state(request)
            with observed_turn(request, updated_state, message_id, "discovery_memory"):
                memory_update = extract_memory_update(
                    key,
                    body,
                    updated_state["messages"],
                    prior_memory["facts"],
                    updated["unresolved_questions"],
                    prior_compaction=prior_memory["working_compaction"],
                    compaction_needed=(
                        sum(len(fact) for fact in prior_memory["facts"]) >= MEMORY_COMPACTION_CHARS
                        or prior_memory["pending_message_count"] + 2 >= MEMORY_COMPACTION_MESSAGES
                    ),
                )
            with identity.connect() as connection:
                save_memory_update(
                    connection,
                    context,
                    "onboarding",
                    context.organization_id,
                    message_id,
                    body,
                    memory_update,
                    unresolved_questions=updated["unresolved_questions"],
                )
        return tour_state(request)

    @app.post("/api/org/discovery/message")
    def post_discovery_message(payload: TourMessage, request: Request):
        return send_discovery_message(payload, request)

    def stream_message(
        payload: TourMessage,
        request: Request,
        handler: Callable[[TourMessage, Request, Callable[[str], None]], dict],
    ) -> StreamingResponse:
        admin_context(request)
        updates: queue.Queue[tuple[str, dict] | None] = queue.Queue()

        def produce() -> None:
            try:
                result = handler(
                    payload,
                    request,
                    lambda chunk: updates.put(("delta", {"text": chunk})),
                )
                updates.put(("done", result))
            except HTTPException as error:
                updates.put(("error", {"detail": error.detail}))
            except Exception:
                logger.exception("Onboarding conversation stream failed")
                updates.put(("error", {"detail": "Austra could not finish the reply"}))
            finally:
                updates.put(None)

        def events():
            threading.Thread(target=produce, daemon=True, name="austra-onboarding-stream").start()
            yield 'event: stage\ndata: {"stage": "Thinking"}\n\n'
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

    @app.post("/api/org/tour/message/stream")
    def stream_tour_message(payload: TourMessage, request: Request):
        return stream_message(payload, request, send_tour_message)

    @app.post("/api/org/discovery/message/stream")
    def stream_discovery_message(payload: TourMessage, request: Request):
        return stream_message(payload, request, send_discovery_message)

    @app.put("/api/org/info")
    def update_organization_info(payload: OrganizationInfo, request: Request):
        context = admin_context(request)
        if not payload.name.strip() or not payload.about_organization.strip():
            raise HTTPException(status_code=400, detail="Name and description are required")
        try:
            website_url = public_link(payload.website_url)
            social_url = public_link(payload.social_url)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        with identity.connect() as connection:
            with tenant_transaction(connection, context):
                row = connection.execute(
                    "UPDATE organization_tours SET about_organization = %s, "
                    "desired_outcome = coalesce(%s, desired_outcome), website_url = %s, "
                    "social_url = %s, updated_at = now() WHERE organization_id = %s "
                    "AND completed_at IS NOT NULL RETURNING organization_id",
                    (
                        payload.about_organization.strip(),
                        payload.desired_outcome.strip() if payload.desired_outcome else None,
                        website_url,
                        social_url,
                        context.organization_id,
                    ),
                ).fetchone()
                if payload.desired_outcome and payload.desired_outcome.strip():
                    connection.execute(
                        "UPDATE onboarding_cases SET business_description = %s, "
                        "desired_outcome = %s, revision = revision + 1, updated_at = now() "
                        "WHERE organization_id = %s "
                        "AND state IN ('created', 'collecting', 'clarifying')",
                        (
                            payload.about_organization.strip(),
                            payload.desired_outcome.strip(),
                            context.organization_id,
                        ),
                    )
            if row is None:
                raise HTTPException(status_code=409, detail="Complete your welcome tour first")
            connection.execute(
                "UPDATE organizations SET name = %s WHERE id = %s",
                (payload.name.strip(), context.organization_id),
            )
        return tour_state(request)

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
                return save_interview(
                    connection, context, payload.business_description, payload.desired_outcome
                )
        except (ValueError, PermissionError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/org/onboarding-sources")
    def get_sources(request: Request):
        context = admin_context(request)
        with identity.connect() as connection:
            return list_sources(connection, context)

    @app.post("/api/org/onboarding-sources/files", status_code=201)
    async def upload_file(
        request: Request, upload: Annotated[UploadFile, File()], linkedin: bool = False
    ):
        context = admin_context(request)
        content = await upload.read(MAX_SOURCE_BYTES + 1)
        await upload.close()
        try:
            with identity.connect() as connection:
                return save_file(
                    connection,
                    context,
                    blob_root(),
                    upload.filename or "",
                    content,
                    linkedin=linkedin,
                )
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
