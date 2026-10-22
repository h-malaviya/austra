"""Local chat interface for an initially blank business workspace."""

import hashlib
import io
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from docx import Document
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pypdf import PdfReader

from austra.access import CONNECTORS, GUIDES, Access
from austra.agent import (
    AgentReply,
    AnalysisUnavailable,
    ConnectionCheckError,
    analyze,
    verify_anthropic_key,
)
from austra.files import SECRET_PATTERN, Files
from austra.infrastructure import Infrastructure, InfrastructureError
from austra.skills import PLATFORM_SKILLS
from austra.slack import event_message, verify_signature

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
SESSION_COOKIE = "austra_session"


class Message(BaseModel):
    body: str = Field(min_length=1, max_length=10000)

    @property
    def safe_body(self) -> str:
        if not self.body.strip():
            raise ValueError("Message cannot be blank")
        if SECRET_PATTERN.search(self.body):
            raise ValueError("Use the private Connections prompt for credentials")
        return self.body.strip()


class Passphrase(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class Credentials(BaseModel):
    values: dict[str, str]


class Review(BaseModel):
    decision: Literal["approved", "rejected"]


def _extract_attachment(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)
    if suffix == ".docx":
        return "\n".join(paragraph.text for paragraph in Document(io.BytesIO(content)).paragraphs)
    if suffix == ".csv":
        return content.decode("utf-8-sig")
    raise ValueError("Only PDF, DOCX, and CSV files are supported")


def create_app(
    root: str | Path | None = None,
    planner: Callable[[Files, Access, str], AgentReply] = analyze,
    anthro_verifier: Callable[[str], None] = verify_anthropic_key,
) -> FastAPI:
    workspace = Path(root or os.getenv("AUSTRA_WORKSPACE", "data/blank"))
    files = Files(workspace)
    access = Access(workspace)
    infrastructure = Infrastructure()
    app = FastAPI(title="Austra")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def restrict_local_access(request: Request, call_next):
        if (
            request.url.path != "/integrations/slack/events"
            and os.getenv("AUSTRA_DOCKER_PROXY") != "1"
        ):
            host = request.client.host if request.client else ""
            if host not in {"127.0.0.1", "::1", "testclient"}:
                return JSONResponse({"detail": "Local access only"}, status_code=403)
        return await call_next(request)

    def require_admin(request: Request) -> None:
        if not access.authenticated(request.cookies.get(SESSION_COOKIE)):
            raise HTTPException(status_code=401, detail="Administrator sign in required")

    def analyzed_chat(chat_id: str) -> dict[str, Any]:
        analysis_error = None
        try:
            result = planner(files, access, chat_id)
        except AnalysisUnavailable as error:
            logger.warning("Chat analysis unavailable: %s", error)
            files.record_event(
                "agent.analysis_failed",
                "Austra",
                "chat",
                chat_id,
                chat_id=chat_id,
                skills_used=list(PLATFORM_SKILLS),
                outcome="failed",
            )
            analysis_error = str(error)
            result = AgentReply(reply=str(error))
        if result.drafts:
            try:
                files.add_drafts(
                    chat_id,
                    [draft.model_dump() for draft in result.drafts],
                    skills_used=list(PLATFORM_SKILLS),
                )
            except ValueError:
                files.record_event(
                    "agent.draft_rejected",
                    "Austra",
                    "chat",
                    chat_id,
                    chat_id=chat_id,
                    skills_used=list(PLATFORM_SKILLS),
                    outcome="failed",
                )
                result = AgentReply(reply="A draft failed validation. Please clarify the request.")
        files.add_message(chat_id, "assistant", result.reply, "Austra")
        files.record_event(
            "agent.replied",
            "Austra",
            "chat",
            chat_id,
            chat_id=chat_id,
            skills_used=list(PLATFORM_SKILLS),
        )
        return {
            "chat": files.chat(chat_id),
            "drafts": files.drafts(chat_id),
            "credential_requests": result.credential_requests,
            "analysis_error": analysis_error,
        }

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    def config(request: Request) -> dict[str, Any]:
        return {
            "admin_ready": access.ready,
            "authenticated": access.authenticated(request.cookies.get(SESSION_COOKIE)),
            "connectors": access.configured(),
            "guides": GUIDES,
            "missing": {name: access.missing(name) for name in CONNECTORS},
        }

    @app.post("/api/setup")
    def setup(payload: Passphrase, response: Response) -> dict[str, bool]:
        try:
            session = access.setup(payload.password)
        except ValueError as error:
            files.record_event(
                "admin.setup_failed", "unknown", "access", "administrator", outcome="failed"
            )
            raise HTTPException(status_code=400, detail=str(error)) from error
        response.set_cookie(SESSION_COOKIE, session, httponly=True, samesite="strict")
        files.record_event("admin.setup", "Admin", "access", "administrator")
        return {"authenticated": True}

    @app.post("/api/login")
    def login(payload: Passphrase, response: Response) -> dict[str, bool]:
        try:
            session = access.login(payload.password)
        except ValueError as error:
            files.record_event(
                "admin.login_failed", "unknown", "access", "administrator", outcome="failed"
            )
            raise HTTPException(status_code=401, detail=str(error)) from error
        response.set_cookie(SESSION_COOKIE, session, httponly=True, samesite="strict")
        files.record_event("admin.login", "Admin", "access", "administrator")
        return {"authenticated": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response) -> dict[str, bool]:
        access.logout(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE)
        files.record_event("admin.logout", "Admin", "access", "administrator")
        return {"authenticated": False}

    @app.post("/api/credentials/{connector}", dependencies=[Depends(require_admin)])
    def save_credentials(connector: str, payload: Credentials) -> dict[str, Any]:
        try:
            if connector == "anthropic":
                key = payload.values.get("ANTHROPIC_API_KEY", "")
                if not key:
                    raise ValueError("Enter an Anthropic API key")
                if len(key) > 8000 or "\n" in key or "\r" in key:
                    raise ValueError("Credential values must be nonempty single lines")
                anthro_verifier(key)
            missing = access.save(connector, payload.values)
            files.record_event("credential.updated", "Admin", "connector", connector)
        except ConnectionCheckError as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"connector": connector, "configured": not missing, "missing": missing}

    @app.post("/api/credentials/anthropic/test", dependencies=[Depends(require_admin)])
    def test_anthropic_connection() -> dict[str, bool]:
        key = access.get("ANTHROPIC_API_KEY")
        if not key:
            raise HTTPException(status_code=400, detail="Enter a Claude API key first")
        try:
            anthro_verifier(key)
        except ConnectionCheckError as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        return {"connected": True}

    @app.get("/api/chats", dependencies=[Depends(require_admin)])
    def list_chats() -> list[dict[str, Any]]:
        return files.chats()

    @app.post("/api/chats", dependencies=[Depends(require_admin)], status_code=201)
    def create_chat(payload: Message) -> dict[str, Any]:
        try:
            body = payload.safe_body
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        chat = files.create_chat(body, "Admin")
        return analyzed_chat(chat["id"])

    @app.get("/api/chats/{chat_id}", dependencies=[Depends(require_admin)])
    def get_chat(chat_id: str) -> dict[str, Any]:
        try:
            return {"chat": files.chat(chat_id), "drafts": files.drafts(chat_id)}
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Chat not found") from error

    @app.delete("/api/chats/{chat_id}", dependencies=[Depends(require_admin)])
    def delete_chat(chat_id: str) -> dict[str, bool]:
        try:
            files.delete_chat(chat_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Chat not found") from error
        return {"deleted": True, "memory_preserved": True}

    @app.post("/api/chats/{chat_id}/messages", dependencies=[Depends(require_admin)])
    def add_message(chat_id: str, payload: Message) -> dict[str, Any]:
        try:
            files.add_message(chat_id, "user", payload.safe_body, "Admin")
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Chat not found") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return analyzed_chat(chat_id)

    @app.post("/api/chats/{chat_id}/retry", dependencies=[Depends(require_admin)])
    def retry_chat(chat_id: str) -> dict[str, Any]:
        try:
            files.chat(chat_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Chat not found") from error
        return analyzed_chat(chat_id)

    @app.post("/api/chats/{chat_id}/attachments", dependencies=[Depends(require_admin)])
    async def add_attachment(chat_id: str, upload: Annotated[UploadFile, File()]) -> dict[str, Any]:
        try:
            files.chat(chat_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Chat not found") from error
        content = await upload.read(MAX_ATTACHMENT_BYTES + 1)
        await upload.close()
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail="Attachment exceeds 5 MB")
        filename = Path(upload.filename or "").name
        try:
            extracted = _extract_attachment(filename, content)
        except Exception as error:
            raise HTTPException(status_code=400, detail="Attachment could not be read") from error
        if not extracted.strip():
            raise HTTPException(status_code=400, detail="Attachment has no readable text")
        if SECRET_PATTERN.search(extracted):
            raise HTTPException(
                status_code=400, detail="Attachment appears to contain a credential"
            )
        file_id = hashlib.sha256(content).hexdigest()
        destination = workspace / "evidence" / f"{file_id}{Path(filename).suffix.lower()}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            destination.write_bytes(content)
            files.record_event(
                "evidence.uploaded",
                "Admin",
                "evidence",
                file_id,
                chat_id=chat_id,
                details={"type": Path(filename).suffix.lower()},
            )
        files.add_message(
            chat_id, "user", f"Attachment {filename} ({file_id}):\n{extracted[:15000]}", "Admin"
        )
        return analyzed_chat(chat_id)

    @app.get("/api/artifacts", dependencies=[Depends(require_admin)])
    def list_artifacts() -> list[dict[str, Any]]:
        return files.approved()

    @app.get("/api/records", dependencies=[Depends(require_admin)])
    def list_records() -> list[dict[str, Any]]:
        return files.records()

    @app.get("/api/audit", dependencies=[Depends(require_admin)])
    def list_audit() -> list[dict[str, Any]]:
        return files.audit_entries()

    @app.get("/api/infrastructure", dependencies=[Depends(require_admin)])
    def infrastructure_status() -> dict[str, Any]:
        try:
            return {"services": infrastructure.status()}
        except InfrastructureError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/drafts/{draft_id}/review", dependencies=[Depends(require_admin)])
    def review_draft(draft_id: str, payload: Review) -> dict[str, Any]:
        try:
            return files.review(draft_id, payload.decision, infrastructure)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Draft not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except InfrastructureError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    def analyze_slack(chat_id: str, channel: str, thread_ts: str) -> None:
        result = analyzed_chat(chat_id)
        token = access.get("SLACK_BOT_TOKEN")
        if not token:
            return
        try:
            response = httpx.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "text": result["chat"]["messages"][-1]["body"],
                },
                timeout=15,
            )
            response.raise_for_status()
            if not response.json().get("ok"):
                logger.warning("Slack did not accept the reply")
        except (httpx.HTTPError, ValueError):
            logger.exception("Slack reply failed")

    @app.post("/integrations/slack/events")
    async def slack_events(request: Request, background: BackgroundTasks) -> JSONResponse:
        signing_secret = access.get("SLACK_SIGNING_SECRET")
        if not signing_secret:
            raise HTTPException(status_code=503, detail="Slack is not configured")
        body = await request.body()
        if not verify_signature(
            body,
            request.headers.get("X-Slack-Request-Timestamp", ""),
            request.headers.get("X-Slack-Signature", ""),
            signing_secret,
        ):
            raise HTTPException(status_code=403, detail="Invalid Slack signature")
        try:
            payload = await request.json()
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid Slack event") from error
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid Slack event")
        if payload.get("type") == "url_verification":
            return JSONResponse({"challenge": payload.get("challenge", "")})
        allowed_team = access.get("SLACK_ALLOWED_TEAM_ID")
        if allowed_team and payload.get("team_id") != allowed_team:
            raise HTTPException(status_code=403, detail="Slack workspace not allowed")
        message = event_message(payload)
        if message is None:
            return JSONResponse({"ok": True})
        body_text, sender, event_id = message
        if SECRET_PATTERN.search(body_text):
            return JSONResponse({"ok": True})
        event = payload["event"]
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        if (
            not isinstance(channel, str)
            or not isinstance(thread_ts, str)
            or not channel
            or not thread_ts
        ):
            raise HTTPException(status_code=400, detail="Invalid Slack thread")
        external_thread = f"{channel}:{thread_ts}"
        chat, fresh = files.ingest_external(event_id, external_thread, body_text, sender)
        if fresh:
            background.add_task(analyze_slack, chat["id"], channel, thread_ts)
        return JSONResponse({"ok": True})

    return app


app = create_app()
