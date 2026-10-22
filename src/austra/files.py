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

ArtifactKind = Literal["policy", "skill", "workflow", "schema", "record", "backend", "frontend"]
KINDS = ("policy", "skill", "workflow", "schema", "record", "backend", "frontend")
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SECRET_PATTERN = re.compile(r"sk-ant-|xox[baprs]-|-----BEGIN .*PRIVATE KEY-----", re.IGNORECASE)
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

    def chat(self, chat_id: str) -> dict[str, Any]:
        try:
            return json.loads(self._chat_path(chat_id).read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise KeyError("Chat not found") from error

    def chats(self) -> list[dict[str, Any]]:
        result = []
        for path in (self.root / "chats").glob("*.json"):
            entry = json.loads(path.read_text(encoding="utf-8"))
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
        self, message: str, sender: str, source: str = "ui", external_thread: str = ""
    ) -> dict[str, Any]:
        with self._lock:
            timestamp = _now()
            chat = {
                "id": uuid4().hex,
                "title": message[:80],
                "source": source,
                "external_thread": external_thread,
                "created_at": timestamp,
                "updated_at": timestamp,
                "messages": [],
            }
            self._add_message(chat, "user", message, sender)
            return chat

    def _add_message(
        self, chat: dict[str, Any], role: str, body: str, sender: str
    ) -> dict[str, Any]:
        chat["messages"].append(
            {"id": uuid4().hex, "role": role, "body": body, "sender": sender, "at": _now()}
        )
        chat["updated_at"] = _now()
        _save(self._chat_path(chat["id"]), chat)
        return chat

    def add_message(self, chat_id: str, role: str, body: str, sender: str) -> dict[str, Any]:
        with self._lock:
            return self._add_message(self.chat(chat_id), role, body, sender)

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
                return self.chat(event_path.read_text(encoding="utf-8")), False
            chat = self.external_chat(thread)
            if chat:
                chat = self.add_message(chat["id"], "user", body, sender)
            else:
                chat = self.create_chat(body, sender, "slack", thread)
            event_path.parent.mkdir(parents=True, exist_ok=True)
            event_path.write_text(chat["id"], encoding="utf-8")
            return chat, True

    def add_drafts(self, chat_id: str, drafts: list[dict[str, str]]) -> list[dict[str, Any]]:
        with self._lock:
            self.chat(chat_id)
            saved = []
            for draft in drafts:
                kind, name = draft["kind"], draft["name"]
                if kind not in KINDS or not NAME_PATTERN.fullmatch(name):
                    raise ValueError("Invalid artifact kind or name")
                content = draft["content"].strip()
                if not content or len(content) > 30000:
                    raise ValueError("Invalid artifact content")
                if SECRET_PATTERN.search(content):
                    raise ValueError("Draft appears to contain a credential")
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
                }
                saved.append(item)
            for item in saved:
                _save(self.root / "drafts" / f"{item['id']}.json", item)
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

    def review(self, draft_id: str, decision: Literal["approved", "rejected"]) -> dict[str, Any]:
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
                if draft["kind"] == "record":
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
            draft["status"] = decision
            draft["reviewed_at"] = _now()
            _save(path, draft)
            return draft

    def approved(self) -> list[dict[str, Any]]:
        items = []
        for folder in (self.root / "approved").glob("*/*"):
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

    def records(self) -> list[dict[str, Any]]:
        return sorted(
            (
                json.loads(path.read_text(encoding="utf-8"))
                for path in (self.root / "records").glob("*/*.json")
            ),
            key=lambda item: item["approved_at"],
        )

    def record_tool_use(self, chat_id: str, name: str, arguments: dict[str, Any]) -> None:
        self.chat(chat_id)
        entry = {
            "id": uuid4().hex,
            "chat_id": chat_id,
            "tool": name,
            "input_hash": hashlib.sha256(
                json.dumps(arguments, sort_keys=True).encode()
            ).hexdigest(),
            "at": _now(),
        }
        _save(self.root / "audit" / f"{entry['id']}.json", entry)
