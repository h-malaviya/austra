"""Validate and apply data-only mappings approved in a workflow definition."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTENT_SOURCES = {"body_label", "attachment_label"}
RESERVED_FIELDS = {"external_id", "state", "units", "capability_key"}
TRACKED_EMAIL_EVENT = "capability.record_email_queued"


class ReplyField(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    source: Literal["sender", "subject", "received_at", "body_label", "attachment_label"]
    label: str | None = Field(default=None, pattern=r"^[^:\x00-\x1f]{1,100}$")

    @model_validator(mode="after")
    def validate_label(self):
        if (self.source in CONTENT_SOURCES) != bool(self.label):
            raise ValueError("Provide an exact label for a message or attachment field")
        return self


class ReplyMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    states: list[str] = Field(min_length=1, max_length=32)
    fields: list[ReplyField] = Field(min_length=1, max_length=16)


class EmailDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=100)
    fields: list[str] = Field(min_length=1, max_length=16)


def validate_mapping(raw, fields, states, protected):
    mapping = ReplyMapping.model_validate(raw)
    targets = [entry.target for entry in mapping.fields]
    if len(set(targets)) != len(targets) or any(
        target in RESERVED_FIELDS | protected or fields.get(target, {}).get("type") not in {"text", "decimal"}
        for target in targets
    ):
        raise ValueError("Map each reply to a unique text or number field outside approval controls")
    if any(fields[entry.target]["type"] == "decimal" and entry.source not in CONTENT_SOURCES
           for entry in mapping.fields):
        raise ValueError("Map number fields from an exact message or attachment label with the approved unit")
    if len(set(mapping.states)) != len(mapping.states) or not set(mapping.states) <= set(states):
        raise ValueError("Select workflow states where replies may update the record")
    return mapping.model_dump(exclude_none=True)


def validate_document(raw, fields):
    document = EmailDocument.model_validate(raw)
    if len(set(document.fields)) != len(document.fields) or any(
        name not in fields or name in RESERVED_FIELDS or fields[name]["type"] == "reference"
        for name in document.fields
    ):
        raise ValueError("Choose distinct business fields for the email document")
    return document.model_dump()


def _label_values(label, texts):
    pattern = re.compile(rf"^[ \t]*{re.escape(label)}[ \t]*:[ \t]*(.+)$", re.IGNORECASE)
    return {
        match[1].strip()
        for text in texts
        for line in text.splitlines()
        if (match := pattern.fullmatch(line)) and match[1].strip()
    }


def mapped_value(spec, field, reply, content):
    source = spec["source"]
    if source in CONTENT_SOURCES:
        if not content:
            return None
        texts = [content["body"]]
        if source == "attachment_label":
            texts = [excerpt["text"] for attachment in content["attachments"]
                     if attachment["status"] == "extracted"
                     for excerpt in attachment["excerpts"]]
        else:
            # A forwarded or quoted earlier message is evidence for review.
            lines = []
            for line in texts[0].splitlines():
                if re.match(
                    r"^\s*(?:>+|On .+ wrote:|-+\s*(?:Original|Forwarded) Message)", line, re.IGNORECASE
                ):
                    break
                lines.append(line)
            texts = ["\n".join(lines)]
        values = _label_values(spec["label"], texts)
        value = next(iter(values)) if len(values) == 1 else None
    elif source == "received_at":
        value = datetime.fromtimestamp(reply["received_ms"] / 1000, UTC).isoformat()
    else:
        value = reply["from" if source == "sender" else "subject"].strip()
    if not value or len(value) > 10000 or "\x00" in value:
        return None
    if field["type"] == "decimal":
        match = re.fullmatch(rf"(\d{{1,18}}(?:\.\d{{1,6}})?)\s+{re.escape(field['unit'])}", value, re.ASCII)
        return format(Decimal(match[1]), "f") if match else None
    return value
