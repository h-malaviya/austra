"""Local MCP server for Austra's approved provider operations."""

import os
from base64 import b64decode, urlsafe_b64encode
from email.message import EmailMessage
from email.utils import getaddresses

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Austra external providers")
HTTP_TIMEOUT_SECONDS = 10.0


@mcp.tool()
def provider_health() -> dict[str, bool]:
    return {"ready": True}


def _credential(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise ValueError("Provider connection is incomplete")
    return value


def _gmail_profile(access_token: str) -> str:
    response = httpx.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    email = response.json().get("emailAddress")
    if not isinstance(email, str) or not email:
        raise ValueError("Google did not return a Gmail mailbox")
    return email


@mcp.tool()
def gmail_exchange_code(code: str, redirect_uri: str) -> dict[str, str]:
    response = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": _credential("GMAIL_CLIENT_ID"),
            "client_secret": _credential("GMAIL_CLIENT_SECRET"),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    tokens = response.json()
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise TypeError("Google did not grant offline access")
    return {"refresh_token": refresh_token, "email": _gmail_profile(access_token),
            "scopes": tokens.get("scope", "")}


@mcp.tool()
def gmail_verify() -> dict[str, str]:
    return {"email": _gmail_profile(_gmail_access_token())}


def _gmail_access_token() -> str:
    response = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": _credential("GMAIL_CLIENT_ID"),
            "client_secret": _credential("GMAIL_CLIENT_SECRET"),
            "refresh_token": _credential("GMAIL_REFRESH_TOKEN"),
            "grant_type": "refresh_token",
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code == 400 and response.json().get("error") == "invalid_grant":
        raise ValueError(
            "Gmail authorization expired or was revoked. Reconnect Gmail on Connections."
        )
    response.raise_for_status()
    access_token = response.json().get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Google did not return an access token")
    return access_token


@mcp.tool()
def gmail_send_email(
    to: str, subject: str, body: str, attachment_name: str = "", attachment_base64: str = ""
) -> dict[str, str | bool]:
    if (
        not to
        or not subject
        or not body
        or any("\n" in value or "\r" in value for value in (to, subject))
    ):
        raise ValueError("Recipient, subject, and body are required")
    addresses = getaddresses([to])
    if not addresses or any(not address or "@" not in address for _, address in addresses):
        raise ValueError("Invalid recipient address")
    message = EmailMessage()
    message["From"] = _credential("GMAIL_SENDER_EMAIL")
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    if bool(attachment_name) != bool(attachment_base64):
        raise ValueError("A PDF attachment needs both a name and content")
    if attachment_name:
        if (
            not attachment_name.lower().endswith(".pdf")
            or "/" in attachment_name
            or "\\" in attachment_name
        ):
            raise ValueError("Only named PDF attachments are supported")
        try:
            content = b64decode(attachment_base64, validate=True)
        except ValueError as error:
            raise ValueError("Invalid PDF attachment encoding") from error
        if not content.startswith(b"%PDF-") or len(content) > 1_000_000:
            raise ValueError("Invalid PDF attachment")
        message.add_attachment(
            content, maintype="application", subtype="pdf", filename=attachment_name
        )
    response = httpx.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {_gmail_access_token()}"},
        json={"raw": urlsafe_b64encode(message.as_bytes()).decode("ascii")},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code in (401, 403):
        return {
            "sent": False,
            "error": "Reconnect Gmail on the Connections page to grant send access.",
        }
    response.raise_for_status()
    sent = response.json()
    if not isinstance(sent, dict) or not isinstance(sent.get("id"), str):
        raise TypeError("Gmail did not confirm the message")
    return {"sent": True, "message_id": sent["id"]}


@mcp.tool()
def slack_verify() -> dict[str, str]:
    response = httpx.post(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {_credential('SLACK_BOT_TOKEN')}"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    identity = response.json()
    if not isinstance(identity, dict) or not identity.get("ok") or not identity.get("bot_id"):
        raise ValueError("Slack rejected the bot token")
    return {"team_id": identity.get("team_id", ""),
            "scopes": response.headers.get("x-oauth-scopes", "")}


@mcp.tool()
def slack_socket_url() -> dict[str, str]:
    response = httpx.post(
        "https://slack.com/api/apps.connections.open",
        headers={"Authorization": f"Bearer {_credential('SLACK_APP_TOKEN')}"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    connection = response.json()
    url = connection.get("url") if isinstance(connection, dict) and connection.get("ok") else None
    if not isinstance(url, str) or not url.startswith("wss://"):
        raise ValueError("Slack Socket Mode is unavailable")
    return {"url": url}


@mcp.tool()
def slack_post_message(channel: str, text: str, thread_ts: str = "") -> dict[str, str | bool]:
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    response = httpx.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {_credential('SLACK_BOT_TOKEN')}"},
        json=payload,
        timeout=15.0,
    )
    response.raise_for_status()
    result = response.json()
    return {"ok": result.get("ok") is True, "ts": result.get("ts", "")}


if __name__ == "__main__":
    mcp.run()
