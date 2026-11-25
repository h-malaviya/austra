"""Organization administrator interview and evidence intake routes."""

import base64
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
from sqlalchemy import select

from austra.a2ui import workspace_consent_surface
from austra.integrations.connections import workspace_access_missing
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
    candidate_proposal,
    clarification_reply,
    draft_paused,
    generate_discovery_turn,
    generate_workflow_draft,
    merge_unresolved,
    pending_workflow_draft,
    planning_assignment,
    reconcile_saved_draft_reply,
    workflow_draft_requested,
)
from austra.onboarding.evidence import (
    MAX_SOURCE_BYTES,
    blob_root,
    exclude_source,
    image_source_bytes,
    list_sources,
    preview_source,
    process_job,
    save_file,
    save_interview,
    save_manual,
    save_website_page,
    set_source_access,
)
from austra.onboarding.profile import introduced_name, record_profile
from austra.onboarding.service import current_case, draft_case, workflow_cases
from austra.onboarding.website import crawl, public_target
from austra.platform.auth import SaasIdentity
from austra.retrieval.workspace_status import secret_disclosure_requested, workspace_status
from austra.semantics.service import propose_candidate
from austra.storage.orm import BlueprintCandidate
from austra.storage.schema_contract import mapped_query
from austra.tenant.context import TenantContext, tenant_transaction
from austra.tenant.memory import memory_context, save_memory_update

logger = logging.getLogger(__name__)
MAX_MESSAGE_FILES = 5
MAX_CHAT_FILE_SOURCES = 20
MAX_FILE_EXCERPT_CHARS = 2500
MAX_FILE_CONTEXT_CHARS = 12000
MAX_IMAGE_REQUEST_BYTES = 18 * 1024 * 1024


class InterviewInput(BaseModel):
    business_description: str = Field(min_length=1, max_length=10000)
    desired_outcome: str = Field(min_length=1, max_length=10000)


class WebsiteInput(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class ManualInput(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_SOURCE_BYTES)


class ExclusionInput(BaseModel):
    excluded: bool
    reason: str | None = Field(default=None, max_length=500)


class AccessInput(BaseModel):
    access_label: str
    department_id: UUID | None = None
    owner_user_id: UUID | None = None


class TourMessage(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    revision: int = Field(ge=1)
    new_workflow: bool = False
    workflow_case_id: UUID | None = None
    attachments: list[UUID] = Field(default_factory=list, max_length=MAX_MESSAGE_FILES)


def explicit_company_confirmation(body: str) -> bool:
    normalized = body.casefold()
    if re.search(r"\b(?:but|however|except|unless|incorrect|wrong|partially)\b", normalized):
        return False
    if re.search(r"\b(?:that|the|your) (?:four[- ]point )?(?:company )?(?:summary|recap|profile)"
                 r" (?:is|was|are) (?:accurate|correct|right)\b", normalized):
        return True
    if re.search(r"\bi confirm (?:that |the |this |your |our )(?:company )?"
                 r"(?:profile|summary|recap|description)\s*(?:[:.!]|$)", normalized):
        return True
    if re.search(
        r"\b(?:but|however|except|actually)\b.{0,100}"
        r"\b(?:wrong|incorrect|correction|change|amend|clarify)\b",
        normalized,
    ):
        return False
    if re.search(
        r"\b(?:your|the|this|that) (?:company )?(?:summary|recap|profile|description)"
        r"(?: of [\w ]{1,40})? (?:is |was )?(?:accurate|correct|right)\b",
        normalized,
    ) or re.search(
        r"\bi confirm (?:your|the|this|that) (?:company )?"
        r"(?:summary|recap|profile|description) (?:is )?(?:accurate|correct|right)\b",
        normalized,
    ):
        return True
    if re.fullmatch(
        r"\s*(?:yes|yes,? that(?:'|’)s right|correct|confirmed|that(?:'|’)s right|"
        r"nothing to add+|no changes|no,? that(?:'|’)s it|that(?:'|’)s (?:all|it)|"
        r"everything is (?:ok|okay|correct|right)|all (?:good|correct|set)|"
        r"(?:looks|sounds) (?:good|right|correct|fine)|no corrections)[.!\s]*",
        normalized,
    ):
        return True
    follow_up = re.fullmatch(
        r"\s*(?:yes,?\s*)?(?:that(?:'|’)s right|that looks right|that (?:profile|summary|recap|"
        r"company description) (?:looks|sounds) right)[.!]\s+(.+)",
        normalized,
        re.DOTALL,
    )
    return bool(
        follow_up
        and not re.search(
            r"\b(?:but|however|except|actually|wrong|incorrect|correction|change|amend)\b",
            follow_up.group(1),
        )
    )


def requested_handoff_topic(body: str) -> str | None:
    if re.search(
        r"\b(?:do not|don't|not yet)\s+(?:move on|take me|discuss|start|begin)",
        body,
        re.IGNORECASE,
    ):
        return None
    match = re.search(
        r"\b(?:move on to|take me to|discuss|start|begin|work on)\s+"
        r"(?:(?:the|our|my|a)\s+)?"
        r"([a-z][a-z0-9 -]{0,65}?\b(?:handoff|workflow|process))\b",
        body,
        re.IGNORECASE,
    )
    return match.group(1).strip().lower() if match else None


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


def organization_links(body: str) -> list[str]:
    links = supplied_public_links(body)
    external = re.compile(
        r"\b(?:background|benchmark|competitor|third.party|industry (?:example|context)|"
        r"(?:context|reference) only|only (?:a |an )?(?:reference|example)|"
        r"not (?:ours|our\b|[\w’'-]+(?:'s|’s) (?:own |official )?(?:site|website))|"
        r"no (?:official |public )?(?:website|site))\b", re.IGNORECASE,
    )
    if not links:
        return []
    # A bare URL is a direct answer to the company-link prompt. For prose, require attribution.
    bare = body.replace(r"\.", ".").strip().strip("<>[]()").rstrip(".,;:!?")
    if len(links) == 1 and bare in {links[0], re.sub(r"^https?://", "", links[0])}:
        return links
    ownership = re.compile(r"\b(?:our|my|official|company) (?:own |official |company )?"
                           r"(?:website|site|linkedin|social|page|url|link)\b", re.IGNORECASE)
    clauses = re.split(r"[;\n]|(?<=[.!?])\s+", body)
    if len(links) == 1 and external.search(body):
        return []
    return [link for link in links if any(
        link in supplied_public_links(clause) and not external.search(clause) and ownership.search(clause)
        for clause in clauses
    )]


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
    if re.search(r"\b(?:file|attachment|document|note|source)\b", body, re.IGNORECASE) and re.search(
        r"\b(?:chang\w*|authoriz\w*|approv\w*|trust\w*|evidence)\b", body,
        re.IGNORECASE,
    ):
        return False
    if workflow_draft_requested(body):
        return False
    return bool(
        re.search(
            r"\b(?:did you|have you|can you|what .*available|check|inspect)\b.*"
            r"\b(?:sav(?:e|ed)|creat(?:e|ed)|handoff|handed off|connections?|connectors?|tools?|docker|mocks?)\b"
            r".*\b(?:draft|candidate|workflow|connections?|connectors?|tools?|docker|mocks?|handoff)?",
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
    app: FastAPI,
    identity: SaasIdentity,
    model_key: Callable[[UUID], str],
    workspace_consent_configured: Callable[[], bool],
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
                "FROM organizations o JOIN users u ON u.organization_id = o.id AND u.id = %s WHERE o.id = %s",
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
            workflow_list = workflow_cases(connection, context) if tour and tour[4] else []
            conversation = [
                {"id": str(message_id), "role": role, "body": body}
                for message_id, role, body in messages
            ]
            if tour and tour[4] and conversation:
                latest = conversation[-1]
                consent = (tour[10] or {}).get("workspace_consent") or {}
                if (
                    latest["role"] == "assistant"
                    and consent.get("message_id") == latest["id"]
                    and workspace_consent_configured()
                    and workspace_access_missing(connection, context, consent["app"])
                ):
                    latest["workspace_consent"] = workspace_consent_surface(
                        latest["id"], consent["app"], consent.get("workflow_id")
                    )
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
            "workflows": [{k: item[k] for k in ("id", "name", "state")} for item in workflow_list],
            "messages": conversation,
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

    def attached_file_context(
        context: TenantContext, current_ids: list[UUID], prior_ids: list[str]
    ) -> tuple[str, str, list[str], list[dict]]:
        if len(set(current_ids)) != len(current_ids):
            raise HTTPException(status_code=400, detail="Attach each file once per message")
        current_id_strings = {str(item) for item in current_ids}
        all_ids = list(dict.fromkeys([
            *(item for item in prior_ids if item not in current_id_strings),
            *(str(item) for item in current_ids),
        ]))[
            -MAX_CHAT_FILE_SOURCES:
        ]
        if not all_ids:
            return "", "", [], []
        with identity.connect() as connection:
            sources = {str(item["id"]): item for item in list_sources(connection, context)}
            current = current_id_strings
            names = []
            excerpts = []
            retained = []
            current_hashes = set()
            images = []
            image_bytes = 0
            for source_id in all_ids:
                source = sources.get(source_id)
                if source is None or source["kind"] != "file" or source["excluded"]:
                    if source_id in current:
                        raise HTTPException(status_code=400, detail="Attached file is unavailable")
                    continue
                if source_id in current:
                    if source["sha256"] in current_hashes:
                        raise HTTPException(
                            status_code=400, detail="Attach each unique file once per message"
                        )
                    current_hashes.add(source["sha256"])
                preview = preview_source(connection, context, source["id"])
                is_image = source["media_type"].startswith("image/")
                if not preview and not is_image:
                    if source_id in current:
                        raise HTTPException(
                            status_code=409, detail=f"{source['name']} has no readable text yet"
                        )
                    continue
                retained.append(source_id)
                if source_id in current:
                    names.append(source["name"])
                if is_image and source_id in all_ids[-MAX_MESSAGE_FILES:]:
                    content = image_source_bytes(connection, context, blob_root(), source["id"])
                    image_bytes += len(content)
                    if image_bytes > MAX_IMAGE_REQUEST_BYTES:
                        raise HTTPException(
                            status_code=400, detail="Images in one message exceed 18 MB total"
                        )
                    images.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": source["media_type"],
                                   "data": base64.b64encode(content).decode("ascii")},
                    })
                if source_id in current:
                    excerpt = " ".join(item["text"] for item in preview)[:MAX_FILE_EXCERPT_CHARS]
                    excerpts.append(
                        f"Source: {source['name']} ({source_id}); extraction: {source['status']}; "
                        + (f"OCR excerpt, not the full image:\n{excerpt}" if is_image and excerpt
                           else "Image attached for visual inspection; no readable text extracted."
                           if is_image else f"excerpt, not the full file:\n{excerpt}")
                    )
        display = "\n".join(f"📎 {name}" for name in names)
        return display, "\n\n".join(excerpts)[-MAX_FILE_CONTEXT_CHARS:], retained, images

    @app.get("/api/org/tour")
    def get_tour(request: Request):
        return tour_state(request)

    def save_turn(
        request: Request,
        expected_revision: int,
        turn: InterviewTurn,
        organization_name: str,
        user_body: str | None = None,
        website_status: str | None = None,
        social_status: str | None = None,
        website_evidence: str | None = None,
        social_evidence: str | None = None,
        message_id: UUID | None = None,
        attachment_source_ids: list[str] | None = None,
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
                elif not ready:
                    reply = (
                        "Before I can save the introduction, tell me about your role "
                        "and what your organization does."
                    )
                else:
                    reply = (
                        "I have the company summary ready. Please confirm that it is accurate "
                        "or tell me which company detail to correct."
                    )
            if confirmed:
                topic = requested_handoff_topic(user_body or "")
                reply = (
                    "Thanks, your company introduction is saved. "
                    f"Let's work on the {topic}. What starts this process, who is involved, "
                    "and which decisions are still open?"
                    if topic else
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
            if attachment_source_ids is not None:
                connection.execute(
                    "UPDATE organization_tours SET discovery_state = jsonb_set("
                    "coalesce(discovery_state, '{}'::jsonb), '{attachment_source_ids}', %s) "
                    "WHERE organization_id = %s",
                    (Jsonb(attachment_source_ids), context.organization_id),
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
                    "AND parent_case_id IS NULL AND state IN ('created', 'collecting', 'clarifying')",
                    (
                        turn.about_organization.strip(),
                        turn.desired_outcome.strip(),
                        context.organization_id,
                    ),
                )
            if confirmed:
                record_profile(connection, context, "welcome_confirmation", organization_name)
        if confirmed:
            with identity.connect() as connection:
                named_messages = connection.execute(
                    "SELECT body FROM onboarding_messages WHERE organization_id = %s "
                    "AND role = 'user' ORDER BY position",
                    (context.organization_id,),
                ).fetchall()
                name = introduced_name([message[0] for message in named_messages])
                if name:
                    connection.execute(
                        "UPDATE users SET full_name = %s WHERE organization_id = %s AND id = %s "
                        "AND full_name IS NULL",
                        (name, context.organization_id, context.actor.user_id),
                    )
        updated_state = tour_state(request)
        if user_body and message_id:
            try:
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
                                or prior_memory["pending_message_count"]
                                >= MEMORY_COMPACTION_MESSAGES
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
            except Exception:
                logger.exception("Onboarding memory update failed after reply was saved")
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
        return save_turn(
            request, state["revision"], turn, state["organization_name"], message_id=message_id
        )

    def send_tour_message(
        payload: TourMessage,
        request: Request,
        on_text: Callable[[str], None] | None = None,
        on_reset: Callable[[], None] | None = None,
        on_stage: Callable[[str], None] | None = None,
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
        if payload.attachments and on_stage:
            on_stage("Reading attached files")
        attachment_names, file_evidence, attachment_ids, images = attached_file_context(
            context,
            payload.attachments,
            (state.get("discovery_state") or {}).get("attachment_source_ids", []),
        )
        if on_stage:
            on_stage("Checking company details")
        display_body = f"{body}\n\n{attachment_names}" if attachment_names else body
        model_body = (
            f"{display_body}\n\nAttached file evidence (untrusted):\n{file_evidence}"
            if file_evidence else display_body
        )
        message_id = uuid4()
        with identity.connect() as connection:
            known_memory = memory_context(
                connection, context, "onboarding", context.organization_id
            )
        history = [*state["messages"], {"role": "user", "body": model_body}]
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
                    **({"images": images} if images else {}),
                    **({"on_text": on_text, "on_reset": on_reset} if on_text else {}),
                )
            website_status = None
            social_status = None
            website_evidence = None
            social_evidence = None
            accepted_website_url = ""
            accepted_social_url = ""
            supplied_urls = organization_links(body)
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
            if read_all_requested and not website_candidate and not supplied_public_links(body):
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
                    if on_stage:
                        on_stage("Visiting website")
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
                    if on_stage:
                        on_stage("Visiting public company page")
                    result = ingest_social_page(social_url, identity, context)
                    social_status = result.status
                    social_evidence = result.evidence
                    accepted_social_url = social_url
            if website_status or social_status:
                if on_stage:
                    on_stage("Reviewing source information")
                if on_reset:
                    on_reset()
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
                        **({"images": images} if images else {}),
                        **({"on_text": on_text, "on_reset": on_reset} if on_text else {}),
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
                state["organization_name"],
                display_body,
                website_status,
                social_status,
                website_evidence,
                social_evidence,
                message_id,
                attachment_ids,
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
        on_reset: Callable[[], None] | None = None,
        on_stage: Callable[[str], None] | None = None,
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
        if payload.attachments and on_stage:
            on_stage("Reading attached files")
        current = state["discovery_state"] or {}
        switching = payload.new_workflow or bool(re.search(
            r"\b(?:start|create|draft|build|plan|switch to|move on to|work on)\s+"
            r"(?:(?:a|the|our)\s+)?(?:new|second|next|another|separate|different)[,\s]+"
            r"(?:[\w-]+\s+){0,7}(?:workflow|process|draft)\b", body, re.IGNORECASE,
        ))
        if not payload.workflow_case_id and not switching and workflow_draft_requested(body):
            named = [w for w in state["workflows"] if w["name"] and w["name"].casefold() in body.casefold()]
            if len(named) == 1:
                payload = payload.model_copy(update={"workflow_case_id": UUID(str(named[0]["id"]))})
        if payload.workflow_case_id and payload.new_workflow:
            raise HTTPException(status_code=400, detail="Choose an existing workflow or start a new one")
        if payload.workflow_case_id:
            with identity.connect() as db, tenant_transaction(db, context):
                target = db.execute(
                    "SELECT c.state, b.id, b.proposal, c.discovery_context FROM onboarding_cases c JOIN LATERAL "
                    "(SELECT id, proposal FROM blueprint_candidates WHERE organization_id = "
                    "c.organization_id AND case_id = c.id ORDER BY candidate_order DESC LIMIT 1) b "
                    "ON true WHERE c.organization_id = %s AND c.id = %s",
                    (context.organization_id, payload.workflow_case_id),
                ).fetchone()
            if not target or target[0] == "locked_candidate":
                raise HTTPException(status_code=409, detail="Choose an editable workflow")
            if (str(payload.workflow_case_id) != current.get("workflow_case_id")
                or str(target[1]) != current.get("workflow_candidate_id")):
                current = {**target[3], "memory_id": target[3].get("memory_id", str(payload.workflow_case_id)),
                       "workflow_case_id": str(payload.workflow_case_id),
                       "workflow_candidate_id": str(target[1]),
                       "unresolved_questions": target[2].get("open_decisions", [])}
        elif switching and not payload.workflow_case_id:
            current = {"history_start": len(state["messages"]), "memory_id": str(uuid4()), "new_draft": True}
        history_start = current.get("history_start", 0)
        history = state["messages"][history_start:]
        memory_id = UUID(current["memory_id"]) if current.get("memory_id") else uuid4()
        if current.get("memory_id"):
            with identity.connect() as db, tenant_transaction(db, context):
                rows = db.execute(
                    "SELECT role, body FROM onboarding_messages WHERE organization_id = %s "
                    "AND workflow_scope = %s ORDER BY position", (context.organization_id, memory_id),
                ).fetchall()
            history = [{"role": role, "body": text} for role, text in rows]
        attachment_names, file_evidence, attachment_ids, images = attached_file_context(
            context, payload.attachments, current.get("attachment_source_ids", [])
        )
        display_body = f"{body}\n\n{attachment_names}" if attachment_names else body
        model_body = (
            f"{display_body}\n\nAttached file evidence (untrusted):\n{file_evidence}"
            if file_evidence else display_body
        )
        message_id = uuid4()
        saved_draft = None
        with identity.connect() as connection:
            prior_memory = memory_context(
                connection, context, "onboarding", memory_id
            )
            if current.get("workflow_candidate_id"):
                with tenant_transaction(connection, context):
                    row = mapped_query(
                        connection,
                        select(BlueprintCandidate.proposal).where(
                            BlueprintCandidate.organization_id == context.organization_id,
                            BlueprintCandidate.id == UUID(current["workflow_candidate_id"]),
                        ),
                    ).fetchone()
                if row:
                    saved_draft = {
                        "behavior": row[0].get("behavior", {}),
                        "name": row[0].get("name"),
                        "open_decisions": row[0].get("open_decisions", []),
                    }
        key = model_key(context.organization_id)
        pending_history = history[current.get("compacted_through", 0) :]
        compact = (
            sum(len(item["body"]) for item in pending_history) + len(body) >= WORKING_CONTEXT_CHARS
            or len(pending_history) + 2 >= MEMORY_COMPACTION_MESSAGES
        )
        draft_proposal = None
        reply_replaced = False
        if secret_disclosure_requested(body):
            turn = DiscoveryTurn(
                reply=("I can't display a saved API key or other credential. You can test or "
                       "replace the model connection in Connections."),
                working_summary=current.get("working_summary", ""),
                unresolved_questions=current.get("unresolved_questions", []),
                resolved_questions=[],
            )
        elif discovery_status_requested(body):
            with identity.connect() as connection:
                saved = workspace_status(connection, context)["answer"]
            turn = DiscoveryTurn(
                reply=(
                    f"This onboarding conversation has {len(state['messages'])} saved messages.\n\n"
                    + saved
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
                        [*history, {"role": "user", "body": model_body}],
                        {**current, "memory": prior_memory, "saved_draft": saved_draft},
                        **({"images": images} if images else {}),
                        **({"on_text": on_text, "on_reset": on_reset} if on_text else {}),
                    )
            except RuntimeError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
            grounded_turn = reconcile_saved_draft_reply(turn, saved_draft)
            reply_replaced = grounded_turn.reply != turn.reply
            turn = grounded_turn
            open_questions = merge_unresolved(current.get("unresolved_questions", []), turn)
            draft_requested = pending_workflow_draft(body, history, current)
            assignment = planning_assignment(
                turn,
                current.get("unresolved_questions", []),
                draft_requested,
                bool(current.get("workflow_candidate_id")),
                draft_paused(body),
            )
            if assignment == "continue" and re.search(
                r"\b(?:I(?:['’]ll| will| have|['’]ve)|planner (?:will|is)|I(?: am|['’]m))\s+"
                r"(?:now\s+)?(?:sav\w*|draft\w*|creat\w*|hand\w* off|prepar\w*)\b",
                turn.reply, re.IGNORECASE,
            ):
                turn.reply = ("The saved draft is unchanged. " if saved_draft else "No workflow draft was saved. ")
                turn.reply += clarification_reply(open_questions) if open_questions else "Tell me which workflow you would like me to draft or revise."
                reply_replaced = True
            if assignment == "clarify" and not (
                "?" in body
                or re.search(r"\b(?:what|why|how|whether|does|is it|can you)\b", body, re.IGNORECASE)
            ):
                turn.reply = clarification_reply(open_questions)
                reply_replaced = True
            elif assignment == "plan":
                if on_stage:
                    on_stage("Preparing workflow draft")
                try:
                    with observed_turn(request, state, message_id, "workflow_draft"):
                        draft = generate_workflow_draft(
                            key,
                            state["organization_name"],
                            [*history, {"role": "user", "body": model_body}],
                            {
                                **current, "memory": prior_memory,
                                "manager_brief": turn.planning_brief, "saved_draft": saved_draft,
                                "unresolved_questions": open_questions,
                            },
                            **({"images": images} if images else {}),
                        )
                except RuntimeError as error:
                    raise HTTPException(status_code=503, detail=str(error)) from error
                proposal = candidate_proposal(draft, open_questions)
                draft_proposal = proposal
                turn.unresolved_questions = proposal["open_decisions"]
                reply_replaced = True
        compact = compact and bool(turn.working_summary.strip())
        updated = {
            "working_summary": turn.working_summary
            if compact
            else current.get("working_summary", ""),
            "history_start": history_start, "memory_id": str(memory_id),
            "new_draft": bool(current.get("new_draft") and draft_proposal is None),
            "workflow_case_id": current.get("workflow_case_id"),
            "compacted_through": (len(history) if current.get("memory_id") else 0) + 2
            if compact
            else current.get("compacted_through", 0),
            "unresolved_questions": merge_unresolved(current.get("unresolved_questions", []), turn),
            "workflow_candidate_id": current.get("workflow_candidate_id"),
            "chat_deleted": False,
            "attachment_source_ids": attachment_ids,
        }
        with identity.connect() as connection, connection.transaction():
            with tenant_transaction(connection, context):
                prior = connection.execute(
                    "SELECT revision, completed_at FROM organization_tours "
                    "WHERE organization_id = %s FOR UPDATE",
                    (context.organization_id,),
                ).fetchone()
                if prior is None or prior[1] is None or prior[0] != payload.revision:
                    raise HTTPException(
                        status_code=409,
                        detail="Conversation changed. Refresh and try again",
                    )
            if draft_proposal is not None:
                try:
                    if current.get("workflow_case_id") and saved_draft and (
                        payload.workflow_case_id or draft_proposal["name"].casefold() == saved_draft["name"].casefold()
                        or re.search(r"\b(?:revise|update|edit)\b", body, re.IGNORECASE)
                    ):
                        case = {"id": UUID(current["workflow_case_id"])}
                        draft_proposal["name"] = saved_draft["name"]
                    else:
                        case = draft_case(connection, context, draft_proposal["name"], message_id,
                                          create_only=bool(current.get("new_draft") or current.get("workflow_case_id")))
                except ValueError as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
                if case is None:
                    raise HTTPException(
                        status_code=409, detail="An onboarding case is required"
                    )
                try:
                    if current.get("workflow_case_id") == str(case["id"]):
                        with tenant_transaction(connection, context):
                            connection.execute("SELECT id FROM onboarding_cases WHERE organization_id = %s "
                                               "AND id = %s FOR UPDATE", (context.organization_id, case["id"]))
                            latest = connection.execute(
                                "SELECT id FROM blueprint_candidates WHERE organization_id = %s AND case_id = %s "
                                "ORDER BY candidate_order DESC LIMIT 1", (context.organization_id, case["id"]),
                            ).fetchone()
                            if not latest or str(latest[0]) != current.get("workflow_candidate_id"):
                                raise HTTPException(status_code=409, detail="Workflow changed. Select the workflow again to review its current draft.")
                    candidate = propose_candidate(
                        connection, context, case["id"], draft_proposal
                    )
                except (ValueError, PermissionError) as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
                updated["workflow_case_id"] = str(case["id"])
                updated["workflow_candidate_id"] = str(candidate["id"])
                steps = "\n".join(
                    f"{index}. {step}"
                    for index, step in enumerate(draft_proposal["behavior"]["manual_steps"], 1)
                )
                open_decisions = "\n".join(
                    f"- {decision}" for decision in draft_proposal["open_decisions"]
                )
                turn.reply = (
                    f"I saved **{draft_proposal['name']}** as an unapproved workflow draft "
                    "for your review.\n\n"
                    f"Proposed steps:\n{steps}\n\n"
                    + (f"Decisions still open (confirmation is blocked):\n{open_decisions}\n\n" if open_decisions else "")
                    + "This draft has not been approved, built, run, or connected to external apps. "
                    f"[Review it in Workflow reviews](/organization/workflow-reviews?workflow={case['id']}) "
                    "with sample cases."
                )
            with tenant_transaction(connection, context):
                position = connection.execute(
                    "SELECT coalesce(max(position), 0) FROM onboarding_messages "
                    "WHERE organization_id = %s",
                    (context.organization_id,),
                ).fetchone()[0]
                reply_id = uuid4()
                connection.execute(
                    "INSERT INTO onboarding_messages (id, organization_id, position, role, body, workflow_scope) "
                    "VALUES (%s, %s, %s, 'user', %s, %s), (%s, %s, %s, 'assistant', %s, %s)",
                    (
                        message_id, context.organization_id, position + 1, display_body, memory_id,
                        reply_id, context.organization_id, position + 2, turn.reply, memory_id,
                    ),
                )
                if updated.get("workflow_case_id"):
                    connection.execute(
                        "UPDATE onboarding_cases SET discovery_context = %s WHERE organization_id = %s "
                        "AND id = %s", (Jsonb(updated), context.organization_id, UUID(updated["workflow_case_id"])),
                    )
                if turn.google_workspace_required:
                    updated["workspace_consent"] = {
                        "message_id": str(reply_id),
                        "app": turn.google_workspace_required,
                        "workflow_id": updated["workflow_case_id"],
                    }
                connection.execute(
                    "UPDATE organization_tours SET discovery_state = %s, "
                    "revision = revision + 1, updated_at = now() "
                    "WHERE organization_id = %s",
                    (Jsonb(updated), context.organization_id),
                )
                if updated["working_summary"].strip():
                    connection.execute(
                        "UPDATE onboarding_cases SET business_description = %s, "
                        "state = CASE WHEN state = 'created' "
                        "THEN 'collecting' ELSE state END, revision = revision + 1, "
                        "updated_at = now() WHERE organization_id = %s "
                        "AND parent_case_id IS NULL AND state IN ('created', 'collecting', 'clarifying')",
                        (
                            state["about_organization"] or state["organization_name"],
                            context.organization_id,
                        ),
                    )
        if reply_replaced and on_reset and on_text:
            on_reset()
            on_text(turn.reply)
        if key and not (discovery_status_requested(body) or secret_disclosure_requested(body)):
            try:
                updated_state = tour_state(request)
                with observed_turn(request, updated_state, message_id, "discovery_memory"):
                    memory_update = extract_memory_update(
                        key,
                        display_body,
                        [*history, {"role": "user", "body": display_body},
                         {"role": "assistant", "body": turn.reply}],
                        prior_memory["facts"],
                        updated["unresolved_questions"],
                        prior_compaction=prior_memory["working_compaction"],
                        compaction_needed=(
                            sum(len(fact) for fact in prior_memory["facts"])
                            >= MEMORY_COMPACTION_CHARS
                            or prior_memory["pending_message_count"] + 2
                            >= MEMORY_COMPACTION_MESSAGES
                        ),
                    )
                with identity.connect() as connection:
                    save_memory_update(
                        connection,
                        context,
                        "onboarding",
                        memory_id,
                        message_id,
                        display_body,
                        memory_update,
                        unresolved_questions=updated["unresolved_questions"],
                    )
            except Exception:
                logger.exception("Discovery memory update failed after reply was saved")
        return tour_state(request)

    @app.post("/api/org/discovery/message")
    def post_discovery_message(payload: TourMessage, request: Request):
        return send_discovery_message(payload, request)

    @app.delete("/api/org/discovery/chat")
    def delete_discovery_chat(request: Request):
        context = admin_context(request)
        with identity.connect() as connection, connection.transaction():
            with tenant_transaction(connection, context):
                tour = connection.execute(
                    "SELECT completed_at FROM organization_tours WHERE organization_id = %s "
                    "FOR UPDATE",
                    (context.organization_id,),
                ).fetchone()
                if tour is None or tour[0] is None:
                    raise HTTPException(status_code=409, detail="Finish the company introduction first")
                connection.execute(
                    "DELETE FROM onboarding_messages WHERE organization_id = %s",
                    (context.organization_id,),
                )
                connection.execute(
                    "UPDATE onboarding_cases SET discovery_context = '{}' WHERE organization_id = %s",
                    (context.organization_id,),
                )
                connection.execute(
                    "UPDATE organization_tours SET discovery_state = %s, revision = revision + 1, "
                    "updated_at = now() WHERE organization_id = %s",
                    (Jsonb({"chat_deleted": True}), context.organization_id),
                )
            connection.execute(
                "DELETE FROM conversation_memory_notes WHERE organization_id = %s "
                "AND chat_kind = 'onboarding'",
                (context.organization_id,),
            )
            connection.execute(
                "DELETE FROM conversation_memory_summaries WHERE organization_id = %s "
                "AND chat_kind = 'onboarding'",
                (context.organization_id,),
            )
        return tour_state(request)

    def stream_message(
        payload: TourMessage,
        request: Request,
        handler: Callable[
            [TourMessage, Request, Callable[[str], None], Callable[[], None], Callable[[str], None]], dict
        ],
        initial_stage: str = "Thinking",
    ) -> StreamingResponse:
        admin_context(request)
        updates: queue.Queue[tuple[str, dict] | None] = queue.Queue()

        def produce() -> None:
            try:
                result = handler(
                    payload,
                    request,
                    lambda chunk: updates.put(("delta", {"text": chunk})),
                    lambda: updates.put(("reset", {})),
                    lambda stage: updates.put(("stage", {"stage": stage})),
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
            yield f"event: stage\ndata: {json.dumps({'stage': initial_stage})}\n\n"
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
        initial_stage = "Reading attached files" if payload.attachments else "Checking company details"
        return stream_message(payload, request, send_tour_message, initial_stage)

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
        with identity.connect() as connection, connection.transaction():
            with tenant_transaction(connection, context):
                row = connection.execute(
                    "UPDATE organization_tours SET about_organization = %s, "
                    "desired_outcome = coalesce(%s, desired_outcome), website_url = %s, "
                    "social_url = %s, revision = revision + 1, updated_at = now() "
                    "WHERE organization_id = %s "
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
                        "AND parent_case_id IS NULL AND state IN ('created', 'collecting', 'clarifying')",
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
            with tenant_transaction(connection, context):
                record_profile(connection, context, "organization_edit", payload.name.strip())
        return tour_state(request)

    @app.get("/api/org/onboarding-cases/current")
    def get_current_case(request: Request):
        context = admin_context(request)
        with identity.connect() as connection:
            case = current_case(connection, context)
            workflows = workflow_cases(connection, context)
        if case is None:
            raise HTTPException(status_code=404, detail="No onboarding case")
        next_actions = {
            "created": ["start_collecting"],
            "collecting": ["continue_collecting", "resolve_questions"],
            "clarifying": ["answer_questions"],
            "ready_for_review": ["review_candidate"],
            "locked_candidate": ["build_candidate"],
        }
        return {**case, "workflows": workflows, "next_actions": next_actions[case["state"]]}

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
                review_state = exclude_source(
                    connection, context, source_id, payload.excluded, payload.reason
                )
            return {"id": source_id, "excluded": payload.excluded, "review_state": review_state}
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

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
