"""Code reviewed connection templates; provider metadata never grants an effect."""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from austra.connectors import GMAIL_SCOPE

Effect = Literal["read", "write"]


@dataclass(frozen=True)
class Action:
    name: str
    label: str
    effect: Effect
    scopes: tuple[str, ...]
    input_schema: dict
    receipt_field: str

    @property
    def schema_hash(self) -> str:
        payload = {"name": self.name, "effect": self.effect, "scopes": self.scopes,
                   "input_schema": self.input_schema, "receipt_field": self.receipt_field}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def public(self) -> dict:
        return {"name": self.name, "label": self.label, "effect": self.effect,
                "scopes": self.scopes, "input_schema": self.input_schema,
                "schema_hash": self.schema_hash}


@dataclass(frozen=True)
class Template:
    key: str
    label: str
    transport: Literal["mcp", "http"]
    setup: Literal["oauth", "private_key", "remote_mcp"]
    scopes: tuple[str, ...]
    private_fields: tuple[str, ...]
    verification: str
    health_check: str
    triggers: tuple[str, ...]
    rate_per_minute: int
    error_handling: str
    revocation: str
    actions: tuple[Action, ...]

    @property
    def schema_hash(self) -> str:
        return hashlib.sha256(json.dumps({"key": self.key,
            "actions": [action.schema_hash for action in self.actions]},
            sort_keys=True).encode()).hexdigest()

    def public(self) -> dict:
        return {"key": self.key, "label": self.label, "transport": self.transport,
                "setup": self.setup, "scopes": self.scopes,
                "schema_hash": self.schema_hash,
                "private_fields": self.private_fields, "verification": self.verification,
                "health_check": self.health_check, "triggers": self.triggers,
                "rate_per_minute": self.rate_per_minute,
                "error_handling": self.error_handling, "revocation": self.revocation,
                "actions": [action.public() for action in self.actions]}


OBJECT = {"type": "object", "additionalProperties": False}
GMAIL_SEND = Action(
    "gmail.send_email", "Send an email", "write",
    ("https://www.googleapis.com/auth/gmail.send",),
    {**OBJECT, "required": ["to", "subject", "body"], "properties": {
        "to": {"type": "string", "format": "email", "maxLength": 254},
        "subject": {"type": "string", "minLength": 1, "maxLength": 200},
        "body": {"type": "string", "minLength": 1, "maxLength": 10000},
    }}, "message_id",
)
GITHUB_REPOS = Action(
    "github.list_repositories", "List your repositories", "read", ("repo",),
    {**OBJECT, "properties": {"per_page": {"type": "integer", "minimum": 1,
                                           "maximum": 100}}}, "items",
)
GITHUB_ISSUE = Action(
    "github.create_issue", "Create an issue", "write", ("repo",),
    {**OBJECT, "required": ["owner", "repo", "title"], "properties": {
        "owner": {"type": "string", "pattern": "^[A-Za-z0-9-]{1,39}$"},
        "repo": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,100}$"},
        "title": {"type": "string", "minLength": 1, "maxLength": 256},
        "body": {"type": "string", "maxLength": 10000},
    }}, "id",
)
SLACK_POST = Action(
    "slack.post_message", "Post a Slack message", "write", ("chat:write",),
    {**OBJECT, "required": ["channel", "text"], "properties": {
        "channel": {"type": "string", "minLength": 1, "maxLength": 100},
        "thread_ts": {"type": "string", "maxLength": 40},
        "text": {"type": "string", "minLength": 1, "maxLength": 10000},
    }}, "ts",
)

TEMPLATES = {
    "gmail": Template(
        "gmail", "Gmail", "mcp", "oauth", tuple(GMAIL_SCOPE.split()), (),
        "Verify the exact mailbox with Gmail profile", "Refresh and read Gmail profile",
        ("Approved email outbox job",), 30,
        "Stop on auth, scope, rate, or uncertain send; reconnect or reconcile",
        "Disconnect in Austra and revoke Austra access in Google Account", (GMAIL_SEND,)),
    "github": Template(
        "github", "GitHub", "http", "private_key", ("repo",), ("token",),
        "Read the authenticated GitHub account", "GET /user",
        (), 60, "Honor rate limits; uncertain writes need receipt reconciliation",
        "Disconnect in Austra and revoke the token in GitHub settings",
        (GITHUB_REPOS, GITHUB_ISSUE)),
    "slack": Template(
        "slack", "Slack", "mcp", "private_key", ("chat:write",),
        ("bot_token", "team_id"), "Verify the bot and exact workspace",
        "Call Slack auth.test and inspect granted bot scopes", (), 60,
        "Stop on missing scope, rate limit, or uncertain message receipt",
        "Disconnect in Austra and revoke the bot token in Slack", (SLACK_POST,)),
    "remote_mcp": Template(
        "remote_mcp", "Remote MCP server", "mcp", "remote_mcp", (), ("token",),
        "Connect and page through tools, then review each action",
        "Rediscover and compare approved tool schemas", (), 30,
        "Stop on changed schemas, rate limits, and uncertain results",
        "Disconnect in Austra and revoke the token at its provider", ()),
}


def catalog() -> list[dict]:
    return [template.public() for template in TEMPLATES.values()]
