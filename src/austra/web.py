"""Local web API and browser UI for the first implementation slice."""

import logging
import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from austra.agent import AnalysisUnavailable, CasePlan, analyze
from austra.catalog import Catalog
from austra.documents import MAX_FILE_BYTES, DocumentLibrary
from austra.email_outbox import EmailOutbox, GmailConfig
from austra.initial_policy import ensure_initial_policy
from austra.inventory import Inventory
from austra.knowledge_store import KnowledgeStore
from austra.policy_manager import PolicyManager
from austra.purchasing import Purchasing
from austra.slack import event_message, verify_signature
from austra.store import Store
from austra.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)
UI_PATH = Path(__file__).parent / "static" / "index.html"
STATIC_DIR = UI_PATH.parent


class NewCase(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    sender: str = Field(default="owner", min_length=1, max_length=200)

    @field_validator("message", "sender")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value cannot be blank")
        return value.strip()


class NewPolicy(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=5, max_length=10000)


class Review(BaseModel):
    decision: Literal["approved", "rejected"]
    manager: str = Field(min_length=1, max_length=200)


class NewBusinessRecord(BaseModel):
    definition_id: str
    values: dict[str, str | int | float | bool]
    actor: str = Field(min_length=1, max_length=200)


def require_manager(request: Request) -> None:
    expected = os.getenv("AUSTRA_MANAGER_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Manager token is not configured")
    provided = request.headers.get("X-Manager-Token", "")
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Manager access required")


def create_app(
    db_path: str | Path | None = None,
    planner: Callable[[str, list[dict[str, object]], list[dict[str, object]]], CasePlan]
    | None = None,
) -> FastAPI:
    path = db_path or os.getenv("AUSTRA_DB_PATH", "data/austra.db")
    store = Store(path)
    ensure_initial_policy(store)
    catalog = Catalog(store)
    knowledge = KnowledgeStore(store)
    purchasing = Purchasing(store)
    inventory = Inventory(store)
    outbox = EmailOutbox(store)
    policy_manager = PolicyManager(store)
    document_dir = os.getenv("AUSTRA_DOCUMENT_DIR") or str(Path(path).parent / "documents")
    documents = DocumentLibrary(store, document_dir)
    registry = ToolRegistry(store, documents, knowledge)
    app = FastAPI(title="Austra Stage 1")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def local_ui_only(request: Request, call_next):
        if request.url.path != "/integrations/slack/events":
            client_host = request.client.host if request.client else ""
            if client_host not in {"127.0.0.1", "::1", "testclient"}:
                return JSONResponse({"detail": "Local access only"}, status_code=403)
        return await call_next(request)

    def case_detail(case_id: str) -> dict:
        detail = store.get_case(case_id)
        detail["business_records"] = catalog.records_for_case(case_id)
        detail["documents"] = documents.for_case(case_id)
        detail.update(knowledge.proposals_for_case(case_id))
        detail.update(purchasing.for_case(case_id))
        detail.update(inventory.for_case(case_id))
        detail["email_outbox"] = outbox.for_case(case_id)
        detail["policy_drafts"] = policy_manager.for_case(case_id)
        return detail

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(UI_PATH)

    @app.get("/api/config")
    def config() -> dict[str, bool]:
        return {
            "model_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
            "manager_configured": bool(os.getenv("AUSTRA_MANAGER_TOKEN")),
            "gmail_configured": all(
                os.getenv(name)
                for name in (
                    "GMAIL_CLIENT_ID",
                    "GMAIL_CLIENT_SECRET",
                    "GMAIL_REFRESH_TOKEN",
                    "GMAIL_SENDER_EMAIL",
                )
            ),
        }

    @app.post("/integrations/slack/events")
    async def slack_events(request: Request) -> JSONResponse:
        signing_secret = os.getenv("SLACK_SIGNING_SECRET")
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
            challenge = payload.get("challenge")
            if not isinstance(challenge, str) or not challenge:
                raise HTTPException(status_code=400, detail="Invalid Slack challenge")
            return JSONResponse({"challenge": challenge})
        if payload.get("type") != "event_callback":
            return JSONResponse({"ok": True})
        allowed_team = os.getenv("SLACK_ALLOWED_TEAM_ID")
        if allowed_team and payload.get("team_id") != allowed_team:
            raise HTTPException(status_code=403, detail="Slack workspace not allowed")
        message = event_message(payload)
        if message is not None:
            text, sender, event_id = message
            store.create_case(text, "slack", sender, event_id)
        return JSONResponse({"ok": True})

    @app.post("/api/cases", status_code=201)
    def create_case(payload: NewCase) -> dict:
        return store.create_case(payload.message.strip(), "ui", payload.sender.strip())

    @app.get("/api/cases")
    def list_cases() -> list[dict]:
        return store.list_cases()

    @app.get("/api/cases/{case_id}")
    def get_case(case_id: str) -> dict:
        try:
            return case_detail(case_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Case not found") from error

    @app.post("/api/cases/{case_id}/messages", status_code=201)
    def add_case_message(case_id: str, payload: NewCase) -> dict:
        try:
            return store.add_message(case_id, payload.message.strip(), payload.sender.strip())
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Case not found") from error

    @app.post("/api/cases/{case_id}/analyze")
    def analyze_case(case_id: str) -> dict:
        try:
            case = store.get_case(case_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Case not found") from error
        policies = store.list_policies(active_only=True)
        conversation = "\n\n".join(
            f"{message['sender']}: {message['body']}" for message in case["messages"][-10:]
        )[-20000:]
        references = documents.search(conversation)
        try:
            plan = (
                planner(conversation, policies, references)
                if planner is not None
                else analyze(conversation, policies, references, registry, case_id)
            )
        except AnalysisUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except Exception as error:
            logger.exception("Case analysis failed for %s", case_id)
            raise HTTPException(status_code=502, detail="Case analysis failed") from error
        store.save_plan(
            case_id,
            plan.model_dump(),
            [policy["id"] for policy in policies],
            [reference["id"] for reference in references],
        )
        return case_detail(case_id)

    @app.get("/api/definitions")
    def list_definitions() -> list[dict]:
        return catalog.list_definitions()

    @app.get("/api/skills")
    def list_skills() -> list[dict]:
        return knowledge.active_skills()

    @app.get("/api/suppliers")
    def list_suppliers() -> list[dict]:
        return purchasing.verified_suppliers()

    @app.get("/api/stock")
    def get_stock() -> list[dict]:
        return inventory.stock_snapshot()

    @app.post("/api/purchase-orders/{po_id}/send", dependencies=[Depends(require_manager)])
    def send_po(po_id: str) -> dict:
        try:
            credentials = GmailConfig.from_environment()
        except ValueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        try:
            return outbox.send(po_id, credentials)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Approved PO email not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/purchase-orders/{po_id}/sync", dependencies=[Depends(require_manager)])
    def sync_po_thread(po_id: str) -> dict:
        try:
            credentials = GmailConfig.from_environment()
        except ValueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        try:
            return outbox.sync_thread(po_id, credentials)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Sent PO thread not found") from error
        except ValueError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.get("/api/documents")
    def list_documents() -> list[dict]:
        return documents.list_documents()

    @app.get("/api/documents/search")
    def search_documents(q: str) -> list[dict]:
        return documents.search(q)

    @app.get("/api/admin/documents/{document_id}", dependencies=[Depends(require_manager)])
    def get_document_text(document_id: str) -> dict:
        try:
            return documents.get_text(document_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Document not found") from error

    @app.post("/api/admin/documents", status_code=201, dependencies=[Depends(require_manager)])
    async def upload_document(
        file: Annotated[UploadFile, File()],
        uploaded_by: Annotated[str, Form()],
        case_id: Annotated[str | None, Form()] = None,
    ) -> dict:
        content = await file.read(MAX_FILE_BYTES + 1)
        await file.close()
        if not uploaded_by.strip():
            raise HTTPException(status_code=400, detail="Uploader name is required")
        try:
            return documents.add(file.filename or "", content, uploaded_by.strip(), case_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Case not found") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/cases/{case_id}/records", status_code=201, dependencies=[Depends(require_manager)]
    )
    def create_business_record(case_id: str, payload: NewBusinessRecord) -> dict:
        try:
            return catalog.create_record(
                case_id, payload.definition_id, payload.values, payload.actor
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Case not found") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/policies")
    def list_policies() -> list[dict]:
        return store.list_policies()

    @app.post("/api/policies", status_code=201, dependencies=[Depends(require_manager)])
    def add_policy(payload: NewPolicy) -> dict:
        return store.add_policy(payload.title.strip(), payload.body.strip())

    @app.post("/api/policies/{policy_id}/activate", dependencies=[Depends(require_manager)])
    def activate_policy(policy_id: str, review: Review) -> dict:
        if review.decision != "approved":
            raise HTTPException(status_code=400, detail="Activation requires approval")
        try:
            return store.activate_policy(policy_id, review.manager)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Policy not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/api/proposals/{proposal_type}/{proposal_id}/review",
        dependencies=[Depends(require_manager)],
    )
    def review_proposal(proposal_type: str, proposal_id: str, review: Review) -> dict:
        if proposal_type not in {
            "actions",
            "records",
            "skills",
            "memory",
            "system",
            "suppliers",
            "purchase_orders",
            "stock_receipts",
            "sales_orders",
            "policies",
        }:
            raise HTTPException(status_code=404, detail="Proposal type not found")
        try:
            if proposal_type == "records":
                return catalog.review_definition(proposal_id, review.decision, review.manager)
            if proposal_type == "skills":
                return knowledge.review_skill(proposal_id, review.decision, review.manager)
            if proposal_type == "memory":
                return knowledge.review_memory(proposal_id, review.decision, review.manager)
            if proposal_type == "system":
                return knowledge.review_system_change(proposal_id, review.decision, review.manager)
            if proposal_type == "suppliers":
                return purchasing.review_supplier(proposal_id, review.decision, review.manager)
            if proposal_type == "purchase_orders":
                return purchasing.review_po(proposal_id, review.decision, review.manager)
            if proposal_type == "stock_receipts":
                return inventory.review_receipt(proposal_id, review.decision, review.manager)
            if proposal_type == "sales_orders":
                return inventory.review_sales_order(proposal_id, review.decision, review.manager)
            if proposal_type == "policies":
                return policy_manager.review(proposal_id, review.decision, review.manager)
            return store.review_proposal(
                "action_proposals", proposal_id, review.decision, review.manager
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Proposal not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return app


app = create_app()
