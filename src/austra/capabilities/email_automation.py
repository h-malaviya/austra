"""Data-only rules for an approved sequence of supplier replies."""

import re
import unicodedata
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from austra.capabilities.reply_mapping import EmailDocument, validate_document

FINDINGS = "[[findings]]"


class AutomationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EmailCheck(AutomationModel):
    source: Literal["attachment", "body", "record"]
    name: str | None = Field(default=None, max_length=160)
    contains: str | None = Field(default=None, min_length=2, max_length=300)
    expected_field: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,39}$")
    finding: str = Field(min_length=1, max_length=300)
    blocking: bool = False
    labels: list[str] = Field(default_factory=list, max_length=8)
    comparison: Literal["text", "decimal"] = "text"

    @model_validator(mode="after")
    def named_source(self):
        if (self.source != "body") != bool(self.name):
            raise ValueError("Name the attachment or record field to check")
        if bool(self.expected_field) == bool(self.contains) or (
            self.expected_field and (self.source == "record" or not self.labels)
        ):
            raise ValueError("Choose a comparison value or an internal expected field for a labelled check")
        if self.labels and (self.source == "record" or any(
            not re.fullmatch(r"[^:\r\n|=]{2,100}", label.strip()) for label in self.labels
        )):
            raise ValueError("Choose concise field labels for the reply or document")
        if self.comparison == "decimal" and (not self.labels or self.contains and _number(self.contains) is None):
            raise ValueError("Decimal checks require labels and an unambiguous expected number or currency amount")
        return self


class EmailOutcome(AutomationModel):
    transition: str | None = Field(default=None, max_length=80)
    fields: dict[str, str] = Field(default_factory=dict, max_length=12)
    body: str | None = Field(default=None, min_length=1, max_length=6000)
    document: EmailDocument | None = None


class EmailReplyRule(AutomationModel):
    state: str = Field(min_length=1, max_length=80)
    attachment_name_contains: str | None = Field(default=None, min_length=2, max_length=100)
    body_contains: str | None = Field(default=None, min_length=2, max_length=100)
    documents: dict[str, list[str]] = Field(default_factory=dict, max_length=4)
    reply_openers: list[str] = Field(default_factory=list, max_length=8)
    checks: list[EmailCheck] = Field(min_length=1, max_length=12)
    on_match: EmailOutcome
    on_mismatch: EmailOutcome

    @model_validator(mode="after")
    def review_sources(self):
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", role) or not 1 <= len(markers) <= 5
               or any(not 2 <= len(marker.strip()) <= 100 for marker in markers)
               for role, markers in self.documents.items()):
            raise ValueError("Each document role needs one to five distinctive content markers")
        if any(not 2 <= len(_normalized(text).strip(" .,!:")) <= 100 for text in self.reply_openers):
            raise ValueError("Choose short affirmative reply openers")
        if self.documents and any(check.source == "attachment" and check.name not in self.documents
                                  for check in self.checks):
            raise ValueError("Name an approved document role for every attachment check")
        return self


class EmailAutomation(AutomationModel):
    rules: list[EmailReplyRule] = Field(min_length=1, max_length=12)


def compile_email_automation(raw, fields, states, transitions, authority):
    config = EmailAutomation.model_validate(raw)
    origins = [rule.state for rule in config.rules]
    if len(set(origins)) != len(origins) or not set(origins) <= set(states):
        raise ValueError("Choose one email rule for each applicable starting state")
    steps = {step["key"]: step for step in transitions}
    for rule in config.rules:
        if not (rule.attachment_name_contains or rule.body_contains or rule.documents or rule.reply_openers):
            raise ValueError("Identify which attachment or reply text triggers the email rule")
        for check in rule.checks:
            if check.source == "record" and check.name not in fields:
                raise ValueError("Email rules can check only declared record fields")
            if check.expected_field and check.expected_field not in fields:
                raise ValueError("Select a declared internal field as the expected value")
        for outcome in (rule.on_match, rule.on_mismatch):
            step = steps.get(outcome.transition)
            if (outcome.transition and (not step or step["from"] != rule.state
                    or set(step["fields"]) != set(outcome.fields)
                    or not authority.get(step["key"])) or (outcome.fields and not step)):
                raise ValueError("Choose an authorized transition and provide its exact required fields")
            if any(fields[key]["type"] != "text" or not value.strip() or len(value) > 4000
                   or "\0" in value for key, value in outcome.fields.items()):
                raise ValueError("Supply bounded text values for automatic updates")
            if outcome.document:
                if not outcome.body:
                    raise ValueError("An outgoing attachment requires an email message")
                validate_document(outcome.document.model_dump(), fields)
            if outcome.body and outcome.body.count(FINDINGS) > 1:
                raise ValueError("Use the findings placeholder at most once per reply")
    result = config.model_dump(exclude_none=True)
    # Keep published legacy policies byte-compatible with their queued Gmail jobs.
    for rule in result["rules"]:
        for name in ("documents", "reply_openers"):
            if not rule[name]:
                del rule[name]
        for check in rule["checks"]:
            if not check["labels"]:
                del check["labels"]
            if check["comparison"] == "text":
                del check["comparison"]
    return result


def _normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().translate(
        str.maketrans({"–": "-", "—": "-", "−": "-", "|": " "})
    ).split())


def _body(text):
    return re.split(
        r"(?im)^[ \t]*(?:>|On .+ wrote:|-+\s*(?:Original|Forwarded) Message|_{5,}|"
        r"(?:From|Sent|To|Subject):[ \t]+[^\n]+)", text, maxsplit=1,
    )[0]


def _number(value):
    match = re.fullmatch(
        r"(?:([a-z]{3})[ \t]+)?([+-]?(?:\d{1,3}(?:,\d{3})+|\d{1,18})(?:\.\d{1,6})?)"
        r"(?:[ \t]+([a-z]{3}))?", _normalized(value), re.ASCII,
    )
    if not match or (match[1] and match[3]) or len(match[2]) > 26:
        return None
    return (match[1] or match[3], Decimal(match[2].replace(",", "")))


def _labelled_values(labels, sections):
    aliases = {_normalized(label) for label in labels}
    result = []
    for section in sections:
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if match := re.fullmatch(r"([^:=|]{2,100})\s*[:=]\s*(.+)", line):
                if _normalized(match[1]) in aliases:
                    result.append(match[2])
            elif _normalized(line) in aliases and index + 1 < len(lines):
                result.append(lines[index + 1])
            cells = line.split("|")
            if len(cells) == 2 and _normalized(cells[0]) in aliases and cells[1].strip():
                result.append(cells[1])
        if lines and "|" in lines[0]:
            headers = lines[0].split("|")
            for line in lines[1:]:
                cells = line.split("|")
                if len(cells) == len(headers):
                    result.extend(cell for label, cell in zip(headers, cells, strict=True)
                                  if _normalized(label) in aliases and cell.strip())
    return result


def _matches_check(check, sections, expected):
    if labels := check.get("labels"):
        convert = _number if check.get("comparison") == "decimal" else _normalized
        values = {convert(value) for value in _labelled_values(labels, sections)}
        comparison = convert(expected)
        if len(values) > 1 or None in values or comparison is None or not expected.strip():
            return None
        return values == {comparison}
    return _normalized(expected) in _normalized("\n".join(sections))


def evaluate_reply(config, state, data, evidence):
    """Return the approved branch and findings, or hold incomplete/irrelevant evidence."""
    rule = next((item for item in config["rules"] if item["state"] == state), None)
    if rule is None:
        return None, []
    fresh_body = _body(evidence["body"])
    body = _normalized(fresh_body)
    attachments = evidence["attachments"]

    def document(name):
        matching = [item for item in attachments if _normalized(name) in _normalized(item["name"])]
        if len(matching) != 1 or matching[0]["status"] != "extracted":
            return None
        return [piece["text"] for piece in matching[0].get("excerpts", [])] or None

    routed = {}
    used = set()
    for role, markers in rule.get("documents", {}).items():
        matches = []
        for index, item in enumerate(attachments):
            parts = [piece["text"] for piece in item.get("excerpts", [])]
            content = _normalized("\n".join(parts))
            if item["status"] == "extracted" and all(_normalized(marker) in content for marker in markers):
                matches.append((index, parts))
        if len(matches) != 1 or matches[0][0] in used:
            return None, [f"Review missing, unreadable or ambiguous {role.replace('_', ' ')} evidence."]
        index, routed[role] = matches[0]
        used.add(index)

    openers = rule.get("reply_openers", [])
    statements = [_normalized(line).strip(" .,!:") for line in fresh_body.splitlines() if line.strip()]
    if openers and not any(
        re.match(rf"^{re.escape(_normalized(opener).strip(' .,!:'))}(?:[.!](?:\s|$)|$)", statement)
        for opener in openers for statement in statements[:4]
    ):
        return None, ["The new reply did not start with an approved confirmation statement."]

    if (rule.get("attachment_name_contains") and document(rule["attachment_name_contains"]) is None
            or rule.get("body_contains") and _normalized(rule["body_contains"]) not in body):
        return None, ["The reply did not contain the evidence required for this step."]
    findings = []
    for check in rule["checks"]:
        if check["source"] == "record":
            value = _normalized(str(data.get(check["name"], "")))
            if _normalized(check["contains"]) != value:
                return None, [check["finding"]]
            continue
        sections = ([fresh_body] if check["source"] == "body"
                    else routed.get(check["name"]) if routed else document(check["name"]))
        if sections is None:
            return None, [f"Review missing or unreadable attachment matching {check['name']}."]
        expected = check.get("contains")
        if check.get("expected_field"):
            expected = str(data.get(check["expected_field"]) or "")
            if not expected.strip():
                return None, [f"The approved record is missing {check['expected_field'].replace('_', ' ')}."]
        matched = _matches_check(check, sections, expected)
        if matched is None:
            return None, [f"Conflicting or unreadable values. {check['finding']}"]
        if not matched:
            if check.get("blocking"):
                return None, [check["finding"]]
            findings.append(check["finding"])
    return rule["on_mismatch"] if findings else rule["on_match"], findings
