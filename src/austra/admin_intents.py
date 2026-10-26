"""Handle explicit administrator commands that must not depend on model discretion."""

import re

from austra.agent import AgentReply, RecordChange
from austra.files import Files

VERIFY_ALL = re.compile(
    r"^(?:please\s+)?(?:mark|set|update)\b.*\b(?:all|every)\b.*\bverified\b",
    re.IGNORECASE | re.DOTALL,
)
RULE_QUERY = re.compile(
    r"^(?:show|list|what|which)\b.*\b(?:rules?|polic(?:y|ies))\b",
    re.IGNORECASE | re.DOTALL,
)


def is_rule_suggestion(message: str) -> bool:
    return bool(
        re.search(r"\b(?:suggest|recommend|suggestion|ideas for rules?)\b", message, re.IGNORECASE)
    )


def rule_listing(files: Files, chat_id: str) -> str | None:
    latest = files.chat(chat_id)["messages"][-1]
    if latest["role"] != "user" or latest.get("attachments"):
        return None
    message = latest.get("display_body") or latest["body"]
    if not RULE_QUERY.search(message) or re.search(
        r"\b(?:suggest|recommend|create|add)\b", message, re.IGNORECASE
    ):
        return None
    text = message.casefold().replace("updation", "update")
    domains = [
        term for term in ("inventory", "stock", "raw material", "polysilicon") if term in text
    ]
    actions = [
        term for term in ("update", "verification", "display", "purchase", "sale") if term in text
    ]
    active = [item for item in files.approved() if item["kind"] in {"policy", "workflow"}]
    named = [item for item in active if item["name"] in text]
    if re.search(r"\b(?:for|about|on)\b", text) and not (domains or actions or named):
        return None
    rules = [
        item
        for item in (named or active)
        if (
            not domains
            or any(term in (item["name"] + item["content"]).casefold() for term in domains)
        )
        and (
            not actions
            or any(term in (item["name"] + item["content"]).casefold() for term in actions)
        )
    ]
    if not rules:
        return "No active administrator-approved rules match this request."
    return "\n\n".join(
        f"### {item['name']} ({item['kind']}, version {item['version']})\n\n{item['content'].strip()}"
        for item in rules
    )


def verification_update(files: Files, chat_id: str) -> AgentReply | None:
    chat = files.chat(chat_id)
    if chat["source"] != "ui":
        return None
    latest = chat["messages"][-1]
    if latest["role"] != "user" or latest.get("attachments"):
        return None
    message = latest.get("display_body") or latest["body"]
    if not VERIFY_ALL.search(message) or re.search(
        r"\b(?:don't|do not|without)\b", message, re.IGNORECASE
    ):
        return None
    rules = [item for item in files.approved() if item["kind"] in {"policy", "workflow"}]
    if any(item["name"] != "stock_display_units" for item in rules):
        return None
    records = files.records()
    for item in reversed(chat["messages"]):
        if item["role"] != "user":
            continue
        body = (item.get("display_body") or item["body"]).casefold()
        targets = [
            name
            for phrase, name in (
                ("raw material", "raw_material_inventory_lot"),
                ("polysilicon", "polysilicon_stock_lot"),
            )
            if phrase in body
        ]
        if len(targets) == 1 and any(record["schema"] == targets[0] for record in records):
            schema = targets[0]
            break
    else:
        eligible = {
            record["schema"] for record in records if "verification_status" in record["values"]
        }
        if len(eligible) != 1:
            return None
        schema = eligible.pop()
    return AgentReply(
        reply=f"I will mark the active {schema.replace('_', ' ')} records verified on your instruction.",
        record_changes=[
            RecordChange(
                action="update",
                scope="schema",
                schema_name=schema,
                values_json='{"verification_status":"verified"}',
                reason="Administrator explicitly requested verification status update",
            )
        ],
    )
