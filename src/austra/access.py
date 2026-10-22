"""Local administrator setup and private connector configuration."""

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

ITERATIONS = 600_000
CONNECTORS = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gmail": (
        "GMAIL_CLIENT_ID",
        "GMAIL_CLIENT_SECRET",
        "GMAIL_REFRESH_TOKEN",
        "GMAIL_SENDER_EMAIL",
    ),
    "slack": ("SLACK_SIGNING_SECRET", "SLACK_BOT_TOKEN", "SLACK_ALLOWED_TEAM_ID"),
}
GUIDES = {
    "anthropic": {
        "title": "Claude API",
        "url": "https://platform.claude.com/docs/en/get-api-key",
        "steps": "Create an API key in Anthropic Console and enter it here.",
    },
    "gmail": {
        "title": "Google Workspace Gmail",
        "url": "https://developers.google.com/workspace/gmail/api/auth/web-server",
        "steps": (
            "Enable the Gmail API, create OAuth client credentials, authorize the business "
            "mailbox with offline access, and enter its refresh token and sender address."
        ),
    },
    "slack": {
        "title": "Slack bot",
        "url": "https://docs.slack.dev/app-management/quickstart-app-settings/",
        "steps": (
            "Create an app, subscribe to app_mention and message.im events, grant the matching "
            "bot scopes plus chat:write, install the app, and copy its Signing Secret, Bot User "
            "OAuth Token, and workspace team ID. A reachable HTTPS event URL is also required."
        ),
    },
}


class Access:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.admin_path = self.root / "admin.json"
        self.env_path = self.root / ".env"
        self.sessions: set[str] = set()

    @property
    def ready(self) -> bool:
        return self.admin_path.exists()

    def setup(self, password: str) -> str:
        if self.ready:
            raise ValueError("Administrator is already configured")
        if len(password) < 12 or len(password) > 200:
            raise ValueError("Use a passphrase between 12 and 200 characters")
        salt = secrets.token_bytes(32)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
        self.admin_path.write_text(
            json.dumps({"salt": salt.hex(), "hash": digest.hex()}), encoding="utf-8"
        )
        self.admin_path.chmod(0o600)
        return self.login(password)

    def login(self, password: str) -> str:
        try:
            saved = json.loads(self.admin_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ValueError("Administrator setup is required") from error
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(saved["salt"]), ITERATIONS
        )
        if not hmac.compare_digest(digest, bytes.fromhex(saved["hash"])):
            raise ValueError("Incorrect passphrase")
        session = secrets.token_urlsafe(32)
        self.sessions.add(session)
        return session

    def authenticated(self, session: str | None) -> bool:
        return bool(session and session in self.sessions)

    def logout(self, session: str | None) -> None:
        if session:
            self.sessions.discard(session)

    def _stored(self) -> dict[str, str]:
        if not self.env_path.exists():
            return {}
        return dict(
            line.split("=", 1)
            for line in self.env_path.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.startswith("#")
        )

    def get(self, name: str) -> str:
        return os.getenv(name) or self._stored().get(name, "")

    def configured(self) -> dict[str, bool]:
        return {
            connector: all(self.get(name) for name in names)
            for connector, names in CONNECTORS.items()
        }

    def missing(self, connector: str) -> list[str]:
        if connector not in CONNECTORS:
            raise ValueError("Unknown connector")
        return [name for name in CONNECTORS[connector] if not self.get(name)]

    def save(self, connector: str, values: dict[str, str]) -> list[str]:
        if connector not in CONNECTORS:
            raise ValueError("Unknown connector")
        if not values or any(name not in CONNECTORS[connector] for name in values):
            raise ValueError("Unsupported credential name")
        if any(
            not value or len(value) > 8000 or "\n" in value or "\r" in value
            for value in values.values()
        ):
            raise ValueError("Credential values must be nonempty single lines")
        stored = self._stored()
        stored.update(values)
        temporary = self.env_path.with_name(f".env.{uuid4().hex}.tmp")
        temporary.write_text(
            "".join(f"{name}={value}\n" for name, value in sorted(stored.items())),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self.env_path)
        return self.missing(connector)
