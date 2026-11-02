"""Connection checks routed through Austra's MCP provider server."""

from collections.abc import Callable
from urllib.parse import urlencode

from austra.agent import ConnectionCheckError
from austra.mcp_gateway import ExternalToolError, call_external

Verifier = Callable[[dict[str, str]], None]
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"


def gmail_authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": GMAIL_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


def exchange_gmail_code(
    code: str, client_id: str, client_secret: str, redirect_uri: str
) -> tuple[str, str]:
    try:
        result = call_external(
            "gmail_exchange_code",
            {"code": code, "redirect_uri": redirect_uri},
            {"GMAIL_CLIENT_ID": client_id, "GMAIL_CLIENT_SECRET": client_secret},
        )
    except ExternalToolError as error:
        raise ConnectionCheckError(
            "Could not complete Gmail authorization. Try again.", 503
        ) from error
    refresh_token = result.get("refresh_token")
    email = result.get("email")
    if not isinstance(refresh_token, str) or not refresh_token or not isinstance(email, str):
        raise ConnectionCheckError("Google did not grant offline access. Reconnect Gmail.", 400)
    return refresh_token, email


def verify_slack(values: dict[str, str]) -> None:
    try:
        identity = call_external("slack_verify", {}, values)
    except ExternalToolError as error:
        raise ConnectionCheckError("Could not verify Slack through MCP. Try again.", 503) from error
    if identity.get("team_id") != values["SLACK_ALLOWED_TEAM_ID"]:
        raise ConnectionCheckError(
            "Slack bot belongs to a different workspace. Check the saved team ID (starts with T).",
            400,
        )
    try:
        call_external("slack_socket_url", {}, values)
    except ExternalToolError as error:
        raise ConnectionCheckError("Could not check Slack Socket Mode through MCP.", 503) from error


def verify_gmail(values: dict[str, str]) -> None:
    try:
        result = call_external("gmail_verify", {}, values)
    except ExternalToolError as error:
        raise ConnectionCheckError("Could not verify Gmail through MCP. Try again.", 503) from error
    email = result.get("email")
    if not isinstance(email, str) or email.casefold() != values["GMAIL_SENDER_EMAIL"].casefold():
        raise ConnectionCheckError("Gmail account does not match the sender address.", 400)


VERIFIERS: dict[str, Verifier] = {"gmail": verify_gmail, "slack": verify_slack}
