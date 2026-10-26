"""Schema-free local storage for conversations and agent-authored artifacts."""

import hashlib
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from austra.infrastructure import SERVICES, InfrastructureError

ArtifactKind = Literal[
    "policy", "skill", "workflow", "schema", "record", "backend", "frontend", "infrastructure"
]
KINDS = ("policy", "skill", "workflow", "schema", "record", "backend", "frontend", "infrastructure")
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SECRET_PATTERN = re.compile(r"sk-ant-|xox[baprs]-|-----BEGIN .*PRIVATE KEY-----", re.IGNORECASE)
LEGACY_ATTACHMENT = re.compile(r"Attachment (.+?) \(([0-9a-f]{64})\):")
EXTENSIONS = {"schema": ".json", "record": ".json", "backend": ".py", "frontend": ".js"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


class Files:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _chat_path(self, chat_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", chat_id):
            raise KeyError("Chat not found")
        return self.root / "chats" / f"{chat_id}.json"

    def _read_chat(self, chat_id: str) -> dict[str, Any]:
        try:
            chat = json.loads(self._chat_path(chat_id).read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise KeyError("Chat not found") from error
        if chat.get("deleted_at"):
            raise KeyError("Chat not found")
        return chat

    def chat(self, chat_id: str) -> dict[str, Any]:
        chat = self._read_chat(chat_id)
        chat["messages"] = [item for item in chat["messages"] if not item.get("deleted_at")]
        return self._with_legacy_attachments(chat)

    def audit_chat(self, chat_id: str) -> dict[str, Any]:
        try:
            chat = json.loads(self._chat_path(chat_id).read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            memory_path = self.root / "memory" / "short_term" / f"{chat_id}.json"
            deletion = next(
                (
                    item
                    for item in self.audit_entries()
                    if item["action"] == "chat.deleted" and item.get("chat_id") == chat_id
                ),
                None,
            )
            if not memory_path.is_file() or deletion is None:
                raise KeyError("Chat not found") from error
            memory = json.loads(memory_path.read_text(encoding="utf-8"))
            chat = {
                "id": chat_id,
                "title": "Deleted chat",
                "source": "legacy",
                "updated_at": memory["updated_at"],
                "deleted_at": deletion["at"],
                "messages": memory["messages"],
            }
        return self._with_legacy_attachments(chat)

    def _with_legacy_attachments(self, chat: dict[str, Any]) -> dict[str, Any]:
        for message in chat["messages"]:
            if message.get("attachments"):
                continue
            legacy = []
            for name, digest in LEGACY_ATTACHMENT.findall(message["body"]):
                path = self._evidence_path(digest)
                if path:
                    legacy.append({"id": digest, "name": name, "size": path.stat().st_size})
            if legacy:
                message["attachments"] = legacy
                message.setdefault("display_body", "")
        return chat

    def _evidence_path(self, digest: str) -> Path | None:
        folder = self.root / "evidence"
        exact = folder / digest
        if exact.is_file():
            return exact
        return next((path for path in folder.glob(f"{digest}.*") if path.is_file()), None)

    def attachment(
        self, chat_id: str, attachment_id: str, *, audit: bool = False
    ) -> tuple[Path, str]:
        if not re.fullmatch(r"[0-9a-f]{64}", attachment_id):
            raise KeyError("Attachment not found")
        chat = self.audit_chat(chat_id) if audit else self.chat(chat_id)
        match = next(
            (
                attachment
                for message in chat["messages"]
                for attachment in message.get("attachments", [])
                if attachment["id"] == attachment_id
            ),
            None,
        )
        if match is None:
            raise KeyError("Attachment not found")
        path = self._evidence_path(attachment_id)
        if path is None:
            raise KeyError("Attachment not found")
        return path, match["name"]

    def chats(self) -> list[dict[str, Any]]:
        result = []
        for path in (self.root / "chats").glob("*.json"):
            entry = json.loads(path.read_text(encoding="utf-8"))
            if entry.get("deleted_at"):
                continue
            result.append(
                {
                    "id": entry["id"],
                    "title": entry["title"],
                    "updated_at": entry["updated_at"],
                    "source": entry["source"],
                }
            )
        return sorted(result, key=lambda item: item["updated_at"], reverse=True)

    def create_chat(
        self,
        message: str,
        sender: str,
        source: str = "ui",
        external_thread: str = "",
        *,
        title: str | None = None,
        display_body: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            timestamp = _now()
            chat = {
                "id": uuid4().hex,
                "title": (title or message)[:80],
                "source": source,
                "external_thread": external_thread,
                "created_at": timestamp,
                "updated_at": timestamp,
                "deleted_at": None,
                "messages": [],
            }
            self._add_message(
                chat,
                "user",
                message,
                sender,
                display_body=display_body,
                attachments=attachments,
            )
            self.record_event("chat.created", sender, "chat", chat["id"], chat_id=chat["id"])
            return chat

    def _add_message(
        self,
        chat: dict[str, Any],
        role: str,
        body: str,
        sender: str,
        *,
        display_body: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        message = {"id": uuid4().hex, "role": role, "body": body, "sender": sender, "at": _now()}
        if display_body is not None:
            message["display_body"] = display_body
        if attachments:
            message["attachments"] = attachments
        chat["messages"].append(message)
        chat["updated_at"] = _now()
        _save(self._chat_path(chat["id"]), chat)
        self._save_memory(chat)
        return chat

    def _save_memory(self, chat: dict[str, Any]) -> None:
        memory_path = self.root / "memory" / "short_term" / f"{chat['id']}.json"
        _save(
            memory_path,
            {
                "source_chat": chat["id"],
                "updated_at": chat["updated_at"],
                "deleted_at": chat.get("deleted_at"),
                "messages": chat["messages"],
            },
        )

    def add_message(self, chat_id: str, role: str, body: str, sender: str) -> dict[str, Any]:
        with self._lock:
            chat = self._add_message(self._read_chat(chat_id), role, body, sender)
            self.record_event("chat.message_added", sender, "chat", chat_id, chat_id=chat_id)
            return chat

    def set_chat_title(self, chat_id: str, title: str) -> None:
        with self._lock:
            chat = self._read_chat(chat_id)
            chat["title"] = title.strip()
            chat["title_generated"] = True
            _save(self._chat_path(chat_id), chat)

    def delete_chat(self, chat_id: str) -> None:
        with self._lock:
            chat = self._read_chat(chat_id)
            chat["deleted_at"] = _now()
            _save(self._chat_path(chat_id), chat)
            self._save_memory(chat)
            self.record_event("chat.deleted", "Admin", "chat", chat_id, chat_id=chat_id)

    def recent_memory(self, exclude_chat: str | None = None) -> list[dict[str, str]]:
        entries = []
        memory_paths = list((self.root / "memory" / "short_term").glob("*.json"))
        legacy_paths = [
            path
            for path in (self.root / "chats").glob("*.json")
            if not (self.root / "memory" / "short_term" / path.name).exists()
        ]
        for path in [*memory_paths, *legacy_paths]:
            memory = json.loads(path.read_text(encoding="utf-8"))
            source_chat = memory.get("source_chat", memory.get("id"))
            if source_chat == exclude_chat:
                continue
            if (
                memory.get("deleted_at")
                or not isinstance(source_chat, str)
                or not re.fullmatch(r"[0-9a-f]{32}", source_chat)
                or not self._chat_path(source_chat).exists()
            ):
                continue
            entries.extend(
                {
                    "role": item["role"],
                    "body": item.get("display_body", item["body"]),
                    "at": item["at"],
                }
                for item in memory["messages"]
                if not item.get("deleted_at")
                if item.get("display_body", item["body"])
                and not item["body"].startswith("Attachment ")
            )
        return sorted(entries, key=lambda item: item["at"])[-12:]

    def submit_message(
        self, chat_id: str | None, text: str, uploads: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], list[str], bool]:
        with self._lock:
            chat = self._read_chat(chat_id) if chat_id else None
            known = set()
            if chat:
                for message in chat["messages"]:
                    if message.get("deleted_at"):
                        continue
                    known.update(item["id"] for item in message.get("attachments", []))
                    known.update(re.findall(r"Attachment .+? \(([0-9a-f]{64})\):", message["body"]))
            unique = []
            duplicates = []
            for upload in uploads:
                if upload["id"] in known:
                    duplicates.append(upload["name"])
                else:
                    unique.append(upload)
                    known.add(upload["id"])
            if not text and not unique:
                if chat:
                    return self.chat(chat["id"]), duplicates, False
                raise ValueError("Enter a message or attach a new file")

            attachment_details = [
                {"id": item["id"], "name": item["name"], "size": len(item["content"])}
                for item in unique
            ]
            parts = [text] if text else []
            parts.extend(
                f"Attachment {item['name']} ({item['id']}):\n{item['excerpt']}" for item in unique
            )
            message_body = "\n\n".join(parts)
            for item in unique:
                destination = self.root / "evidence" / item["id"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists() and not any(destination.parent.glob(f"{item['id']}.*")):
                    destination.write_bytes(item["content"])
                    self.record_event(
                        "evidence.uploaded",
                        "Admin",
                        "evidence",
                        item["id"],
                        chat_id=chat_id,
                        details={"type": item["extension"]},
                    )
                if item.get("text"):
                    text_path = self.root / "evidence_text" / f"{item['id']}.txt"
                    text_path.parent.mkdir(parents=True, exist_ok=True)
                    if not text_path.exists():
                        text_path.write_text(item["text"], encoding="utf-8")

            if chat:
                chat = self._add_message(
                    chat,
                    "user",
                    message_body,
                    "Admin",
                    display_body=text,
                    attachments=attachment_details,
                )
                self.record_event(
                    "chat.message_added", "Admin", "chat", chat["id"], chat_id=chat["id"]
                )
            else:
                title = text or f"Uploaded {unique[0]['name']}"
                chat = self.create_chat(
                    message_body,
                    "Admin",
                    title=title,
                    display_body=text,
                    attachments=attachment_details,
                )
            for item in unique:
                self.record_event(
                    "evidence.attached", "Admin", "evidence", item["id"], chat_id=chat["id"]
                )
            return self.chat(chat["id"]), duplicates, True

    def external_chat(self, thread: str) -> dict[str, Any] | None:
        for summary in self.chats():
            chat = self.chat(summary["id"])
            if chat["external_thread"] == thread:
                return chat
        return None

    def ingest_external(
        self, event_id: str, thread: str, body: str, sender: str
    ) -> tuple[dict[str, Any], bool]:
        event_path = self.root / "slack_events" / hashlib.sha256(event_id.encode()).hexdigest()
        with self._lock:
            if event_path.exists():
                return {"id": event_path.read_text(encoding="utf-8")}, False
            chat = self.external_chat(thread)
            if chat:
                chat = self.add_message(chat["id"], "user", body, sender)
            else:
                chat = self.create_chat(body, sender, "slack", thread)
            event_path.parent.mkdir(parents=True, exist_ok=True)
            event_path.write_text(chat["id"], encoding="utf-8")
            return chat, True

    def add_drafts(
        self,
        chat_id: str,
        drafts: list[dict[str, str]],
        skills_used: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            self.chat(chat_id)
            saved = []
            for draft in drafts:
                kind, name = draft["kind"], draft["name"]
                if kind not in KINDS or not NAME_PATTERN.fullmatch(name):
                    raise ValueError("Invalid artifact kind or name")
                content = draft["content"].strip()
                if not content or len(content.encode("utf-8")) > 1_000_000:
                    raise ValueError("Invalid artifact content")
                if SECRET_PATTERN.search(content):
                    raise ValueError("Draft appears to contain a credential")
                if kind == "infrastructure":
                    try:
                        request = json.loads(content)
                    except json.JSONDecodeError as error:
                        raise ValueError("Infrastructure draft must be JSON") from error
                    if (
                        not isinstance(request, dict)
                        or set(request) != {"service", "action"}
                        or request["service"] != name
                        or name not in SERVICES
                        or request["action"] not in {"start", "stop"}
                    ):
                        raise ValueError(
                            "Infrastructure draft must use an allowed service and action"
                        )
                if kind in {"schema", "record"}:
                    try:
                        value = json.loads(content)
                    except json.JSONDecodeError as error:
                        raise ValueError("Schema and record drafts must be JSON") from error
                    if not isinstance(value, dict):
                        raise ValueError("Schema and record drafts must describe an object")
                    if kind == "schema":
                        if value.get("type") != "object":
                            raise ValueError("Schema draft must describe an object")
                        try:
                            Draft202012Validator.check_schema(value)
                        except SchemaError as error:
                            raise ValueError("Schema draft is not valid JSON Schema") from error
                    else:
                        self._validate_record(name, value)
                item = {
                    "id": uuid4().hex,
                    "chat_id": chat_id,
                    "kind": kind,
                    "name": name,
                    "summary": draft["summary"][:500],
                    "content": content,
                    "status": "proposed",
                    "created_at": _now(),
                    "proposed_by": "Austra" if skills_used else "Admin",
                    "skills_used": skills_used or [],
                }
                saved.append(item)
            for item in saved:
                _save(self.root / "drafts" / f"{item['id']}.json", item)
                self.record_event(
                    "draft.proposed",
                    item["proposed_by"],
                    item["kind"],
                    item["name"],
                    chat_id=chat_id,
                    draft_id=item["id"],
                    skills_used=item["skills_used"],
                )
            return saved

    def drafts(self, chat_id: str | None = None) -> list[dict[str, Any]]:
        items = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.root / "drafts").glob("*.json")
        ]
        return [
            item
            for item in sorted(items, key=lambda entry: entry["created_at"])
            if chat_id is None or item["chat_id"] == chat_id
        ]

    def apply_agent_drafts(
        self, chat_id: str, drafts: list[dict[str, str]], skills_used: list[str]
    ) -> list[dict[str, Any]]:
        saved = []
        for draft in sorted(drafts, key=lambda item: item["kind"] != "schema"):
            if draft["kind"] == "record":
                values = json.loads(draft["content"])
                if any(
                    record["schema"] == draft["name"] and record["values"] == values
                    for record in self.records()
                ):
                    continue
            if draft["kind"] == "schema" and any(
                item["kind"] == "schema"
                and item["name"] == draft["name"]
                and item["content"].strip() == draft["content"].strip()
                for item in self.approved()
            ):
                continue
            if draft["kind"] == "schema":
                candidate = json.loads(draft["content"])
                try:
                    for record in self.records():
                        if record["schema"] == draft["name"]:
                            Draft202012Validator(candidate).validate(record["values"])
                except ValidationError as error:
                    raise ValueError(
                        "Schema change conflicts with existing business records"
                    ) from error
            item = self.add_drafts(chat_id, [draft], skills_used=skills_used)[0]
            if item["kind"] in {"schema", "record"}:
                item = self.review(item["id"], "approved", actor="Austra")
            saved.append(item)
        return saved

    def review(
        self,
        draft_id: str,
        decision: Literal["approved", "rejected"],
        infrastructure: Any = None,
        *,
        actor: str = "Admin",
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", draft_id):
            raise KeyError("Draft not found")
        with self._lock:
            path = self.root / "drafts" / f"{draft_id}.json"
            try:
                draft = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as error:
                raise KeyError("Draft not found") from error
            if draft["status"] != "proposed":
                raise ValueError("Draft was already reviewed")
            if decision == "approved":
                if draft["kind"] == "infrastructure":
                    if infrastructure is None:
                        raise ValueError(
                            "Compose controller is required for infrastructure approval"
                        )
                    request = json.loads(draft["content"])
                    try:
                        draft["service_state"] = infrastructure.change(
                            request["service"], request["action"]
                        )
                    except InfrastructureError:
                        self.record_event(
                            "infrastructure.failed",
                            actor,
                            "infrastructure",
                            draft["name"],
                            chat_id=draft["chat_id"],
                            draft_id=draft_id,
                            skills_used=draft.get("skills_used", []),
                            details={"action": request["action"]},
                            outcome="failed",
                        )
                        raise
                    self.record_event(
                        "infrastructure.changed",
                        actor,
                        "infrastructure",
                        draft["name"],
                        chat_id=draft["chat_id"],
                        draft_id=draft_id,
                        skills_used=draft.get("skills_used", []),
                        details={"action": request["action"], "state": draft["service_state"]},
                    )
                elif draft["kind"] == "record":
                    schema_version = self._validate_record(
                        draft["name"], json.loads(draft["content"])
                    )
                    record = {
                        "id": draft["id"],
                        "schema": draft["name"],
                        "schema_version": schema_version,
                        "values": json.loads(draft["content"]),
                        "source_chat": draft["chat_id"],
                        "approved_at": _now(),
                    }
                    _save(self.root / "records" / draft["name"] / f"{draft_id}.json", record)
                    draft["schema_version"] = schema_version
                else:
                    folder = self.root / "approved" / draft["kind"] / draft["name"]
                    folder.mkdir(parents=True, exist_ok=True)
                    extension = EXTENSIONS.get(draft["kind"], ".md")
                    version = (
                        max(
                            (
                                int(file.stem[1:])
                                for file in folder.glob(f"v*{extension}")
                                if file.stem[1:].isdigit()
                            ),
                            default=0,
                        )
                        + 1
                    )
                    (folder / f"v{version}{extension}").write_text(
                        draft["content"] + "\n", encoding="utf-8"
                    )
                    draft["version"] = version
                    if draft["kind"] == "policy":
                        (folder / "retired.json").unlink(missing_ok=True)
            draft["status"] = decision
            draft["reviewed_at"] = _now()
            _save(path, draft)
            self.record_event(
                f"draft.{decision}",
                actor,
                draft["kind"],
                draft["name"],
                chat_id=draft["chat_id"],
                draft_id=draft_id,
                skills_used=draft.get("skills_used", []),
                details={"proposed_by": draft.get("proposed_by", "unknown")},
            )
            return draft

    def approved(self) -> list[dict[str, Any]]:
        items = []
        for folder in (self.root / "approved").glob("*/*"):
            if folder.parent.name == "policy" and (folder / "retired.json").exists():
                continue
            extension = EXTENSIONS.get(folder.parent.name, ".md")
            files = sorted(
                (file for file in folder.glob(f"v*{extension}") if file.stem[1:].isdigit()),
                key=lambda file: int(file.stem[1:]),
            )
            if files:
                latest = files[-1]
                items.append(
                    {
                        "kind": folder.parent.name,
                        "name": folder.name,
                        "version": int(latest.stem[1:]),
                        "content": latest.read_text(encoding="utf-8"),
                    }
                )
        return sorted(items, key=lambda item: (item["kind"], item["name"]))

    def save_policy(self, chat_id: str, name: str, content: str, reason: str) -> dict[str, Any]:
        """Activate an administrator's explicit Markdown rule as a versioned policy."""
        with self._lock:
            self._read_chat(chat_id)
            if not NAME_PATTERN.fullmatch(name):
                raise ValueError("Invalid policy name")
            content = content.strip()
            if not content or len(content.encode("utf-8")) > 1_000_000:
                raise ValueError("Invalid policy content")
            if SECRET_PATTERN.search(content):
                raise ValueError("Policy appears to contain a credential")
            folder = self.root / "approved" / "policy" / name
            versions = [
                int(path.stem[1:]) for path in folder.glob("v*.md") if path.stem[1:].isdigit()
            ]
            current = folder / f"v{max(versions)}.md" if versions else None
            if current and current.read_text(encoding="utf-8").strip() == content:
                retired = folder / "retired.json"
                if retired.exists():
                    retired.unlink()
                    self.record_event(
                        "policy.activated",
                        "Admin",
                        "policy",
                        name,
                        chat_id=chat_id,
                        details={"version": str(max(versions)), "reason": reason},
                    )
                return {
                    "kind": "policy",
                    "name": name,
                    "version": max(versions),
                    "content": content,
                }
            version = max(versions, default=0) + 1
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"v{version}.md").write_text(content + "\n", encoding="utf-8")
            (folder / "retired.json").unlink(missing_ok=True)
            self.record_event(
                "policy.activated",
                "Admin",
                "policy",
                name,
                chat_id=chat_id,
                details={"version": str(version), "reason": reason},
            )
            return {"kind": "policy", "name": name, "version": version, "content": content}

    def retire_policy(self, chat_id: str, name: str, reason: str) -> None:
        with self._lock:
            self._read_chat(chat_id)
            if not NAME_PATTERN.fullmatch(name):
                raise ValueError("Invalid policy name")
            if not any(
                item["kind"] == "policy" and item["name"] == name for item in self.approved()
            ):
                raise ValueError("Active policy not found")
            _save(
                self.root / "approved" / "policy" / name / "retired.json",
                {
                    "retired_at": _now(),
                    "chat_id": chat_id,
                    "reason": reason,
                },
            )
            self.record_event(
                "policy.retired",
                "Admin",
                "policy",
                name,
                chat_id=chat_id,
                details={"reason": reason},
            )

    def _validate_record(self, name: str, values: dict[str, Any]) -> int:
        schemas = [
            item for item in self.approved() if item["kind"] == "schema" and item["name"] == name
        ]
        if not schemas:
            raise ValueError("An approved schema is required for this record")
        current = schemas[0]
        try:
            Draft202012Validator(json.loads(current["content"])).validate(values)
        except ValidationError as error:
            raise ValueError("Record does not match its approved schema") from error
        return current["version"]

    def records(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        return sorted(
            (
                record
                for path in (self.root / "records").glob("*/*.json")
                if (record := json.loads(path.read_text(encoding="utf-8")))
                and (include_deleted or not record.get("deleted_at"))
            ),
            key=lambda item: item["approved_at"],
        )

    def update_record(
        self, chat_id: str, record_id: str, values: dict[str, Any], reason: str
    ) -> dict[str, Any]:
        with self._lock:
            self._read_chat(chat_id)
            if not re.fullmatch(r"[0-9a-f]{32}", record_id):
                raise ValueError("Invalid record ID")
            record = next((item for item in self.records() if item["id"] == record_id), None)
            if record is None:
                raise ValueError("Active record not found")
            if SECRET_PATTERN.search(json.dumps(values, ensure_ascii=False)):
                raise ValueError("Record contains a credential")
            version = self._validate_record(record["schema"], values)
            if values == record["values"]:
                return record
            revision_id = uuid4().hex
            _save(
                self.root / "record_history" / record["schema"] / record_id / f"{revision_id}.json",
                record,
            )
            record["values"] = values
            record["schema_version"] = version
            record["updated_at"] = _now()
            record["updated_by"] = "Admin"
            record["updated_from_chat"] = chat_id
            _save(self.root / "records" / record["schema"] / f"{record_id}.json", record)
            self.record_event(
                "record.updated",
                "Admin",
                "record",
                record_id,
                chat_id=chat_id,
                details={"schema": record["schema"], "reason": reason, "revision_id": revision_id},
            )
            return record

    def update_records(
        self,
        chat_id: str,
        schema_name: str,
        patch: dict[str, Any],
        reason: str,
        source_attachment: str = "",
    ) -> int:
        """Apply an administrator's scoped field change after validating every result."""
        with self._lock:
            self._read_chat(chat_id)
            if not NAME_PATTERN.fullmatch(schema_name):
                raise ValueError("Invalid record schema")
            if source_attachment and not re.fullmatch(r"[0-9a-f]{64}", source_attachment):
                raise ValueError("Invalid source attachment")
            if not patch:
                raise ValueError("Bulk update requires at least one field")
            selected = [
                record
                for record in self.records()
                if record["schema"] == schema_name
                and (not source_attachment or record.get("source_attachment") == source_attachment)
            ]
            if not selected:
                raise ValueError("No matching active records found")
            replacements = []
            for record in selected:
                values = {**record["values"], **patch}
                if SECRET_PATTERN.search(json.dumps(values, ensure_ascii=False)):
                    raise ValueError("Record contains a credential")
                self._validate_record(schema_name, values)
                if values != record["values"]:
                    replacements.append((record["id"], values))
            for record_id, values in replacements:
                self.update_record(chat_id, record_id, values, reason)
            return len(replacements)

    def delete_records(self, chat_id: str, scope: str, identifier: str, reason: str) -> int:
        with self._lock:
            self._read_chat(chat_id)
            if scope == "id":
                if not re.fullmatch(r"[0-9a-f]{32}", identifier):
                    raise ValueError("Invalid record ID")
                selected = [item for item in self.records() if item["id"] == identifier]
            elif scope == "schema":
                if not NAME_PATTERN.fullmatch(identifier):
                    raise ValueError("Invalid record schema")
                selected = [item for item in self.records() if item["schema"] == identifier]
            elif scope == "all":
                selected = self.records()
            else:
                raise ValueError("Invalid deletion scope")
            for record in selected:
                record["deleted_at"] = _now()
                record["deleted_by"] = "Admin"
                record["deleted_from_chat"] = chat_id
                record["deletion_reason"] = reason
                _save(self.root / "records" / record["schema"] / f"{record['id']}.json", record)
                self.record_event(
                    "record.deleted",
                    "Admin",
                    "record",
                    record["id"],
                    chat_id=chat_id,
                    details={"schema": record["schema"], "reason": reason},
                )
            return len(selected)

    def save_imported_records(
        self,
        chat_id: str,
        schema_name: str,
        attachment_id: str,
        filename: str,
        rows: list[tuple[int, dict[str, Any]]],
        section: str = "",
    ) -> int:
        with self._lock:
            self._read_chat(chat_id)
            schema_version = []
            existing_records = [
                record for record in self.records() if record["schema"] == schema_name
            ]
            order_key = next(
                (
                    key
                    for key in ("purchase_order_id", "sales_order_id")
                    if rows and key in rows[0][1]
                ),
                None,
            )
            existing_orders = {}
            if order_key:
                for record in existing_records:
                    identifier = record["values"].get(order_key)
                    if identifier:
                        if identifier in existing_orders:
                            raise ValueError(
                                f"Multiple active records have {order_key} {identifier}"
                            )
                        existing_orders[identifier] = record
            existing_lot_ids = {
                record["values"]["lot_id"]
                for record in existing_records
                if "lot_id" in record["values"]
            }
            pending_lot_ids = set()
            pending_order_ids = set()
            normalized_rows = []
            for row_number, values in rows:
                previous = existing_orders.get(values.get(order_key)) if order_key else None
                if previous:
                    values = {**previous["values"], **values}
                normalized_rows.append((row_number, values))
                try:
                    schema_version.append(self._validate_record(schema_name, values))
                except ValueError as error:
                    raise ValueError(f"CSV row {row_number}: {error}") from error
                if order_key:
                    order_id = values.get(order_key)
                    if not order_id or order_id in pending_order_ids:
                        raise ValueError(f"CSV row {row_number}: missing or duplicate {order_key}")
                    pending_order_ids.add(order_id)
                lot_id = values.get("lot_id")
                if lot_id is not None:
                    if lot_id in pending_lot_ids:
                        raise ValueError(f"CSV row {row_number}: duplicate lot_id")
                    pending_lot_ids.add(lot_id)
                    identity = f"{schema_name}:{attachment_id}:{section}:{row_number}"
                    record_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
                    path = self.root / "records" / schema_name / f"{record_id}.json"
                    if not path.exists() and lot_id in existing_lot_ids:
                        raise ValueError(f"CSV row {row_number}: lot_id already exists")
            saved = 0
            for (row_number, values), version in zip(normalized_rows, schema_version):
                previous = existing_orders.get(values[order_key]) if order_key else None
                if previous:
                    old_values = {
                        key: value
                        for key, value in previous["values"].items()
                        if key != "provenance"
                    }
                    new_values = {
                        key: value for key, value in values.items() if key != "provenance"
                    }
                    if old_values == new_values:
                        continue
                    revision_id = uuid4().hex
                    _save(
                        self.root
                        / "record_history"
                        / schema_name
                        / previous["id"]
                        / f"{revision_id}.json",
                        previous,
                    )
                    previous.update(
                        {
                            "values": values,
                            "schema_version": version,
                            "source_attachment": attachment_id,
                            "source_file": filename,
                            "source_section": section,
                            "source_row": row_number,
                            "updated_at": _now(),
                            "updated_by": "Austra",
                            "updated_from_chat": chat_id,
                        }
                    )
                    _save(self.root / "records" / schema_name / f"{previous['id']}.json", previous)
                    self.record_event(
                        "record.import_updated",
                        "Austra",
                        "record",
                        previous["id"],
                        chat_id=chat_id,
                        details={
                            "schema": schema_name,
                            "source_row": row_number,
                            "revision_id": revision_id,
                        },
                    )
                    saved += 1
                    continue
                identity = f"{schema_name}:{attachment_id}:{section}:{row_number}"
                record_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
                path = self.root / "records" / schema_name / f"{record_id}.json"
                if path.exists():
                    continue
                _save(
                    path,
                    {
                        "id": record_id,
                        "schema": schema_name,
                        "schema_version": version,
                        "values": values,
                        "source_chat": chat_id,
                        "source_attachment": attachment_id,
                        "source_file": filename,
                        "source_section": section,
                        "source_row": row_number,
                        "approved_at": _now(),
                    },
                )
                self.record_event(
                    "record.imported",
                    "Austra",
                    "record",
                    record_id,
                    chat_id=chat_id,
                    details={"schema": schema_name, "source_row": row_number},
                )
                saved += 1
            return saved

    def record_event(
        self,
        action: str,
        actor: str,
        target_kind: str,
        target_name: str,
        *,
        chat_id: str | None = None,
        draft_id: str | None = None,
        skills_used: list[str] | None = None,
        details: dict[str, str] | None = None,
        outcome: str = "success",
    ) -> None:
        entry = {
            "id": uuid4().hex,
            "at": _now(),
            "action": action,
            "actor": actor,
            "target_kind": target_kind,
            "target_name": target_name,
            "chat_id": chat_id,
            "draft_id": draft_id,
            "skills_used": skills_used or [],
            "details": details or {},
            "outcome": outcome,
        }
        _save(self.root / "audit" / f"{entry['id']}.json", entry)

    def audit_entries(self) -> list[dict[str, Any]]:
        entries = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.root / "audit").glob("*.json")
        ]
        return sorted(entries, key=lambda item: item["at"], reverse=True)

    def record_tool_use(self, chat_id: str, name: str, arguments: dict[str, Any]) -> None:
        self.chat(chat_id)
        self.record_event(
            "agent.tool_used",
            "Austra",
            "tool",
            name,
            chat_id=chat_id,
            details={
                "input_hash": hashlib.sha256(
                    json.dumps(arguments, sort_keys=True).encode()
                ).hexdigest()
            },
        )
