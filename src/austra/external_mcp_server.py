"""Local MCP server for Austra's approved provider operations."""

import os

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
    return {"refresh_token": refresh_token, "email": _gmail_profile(access_token)}


@mcp.tool()
def gmail_verify() -> dict[str, str]:
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
    response.raise_for_status()
    access_token = response.json().get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Google did not return an access token")
    return {"email": _gmail_profile(access_token)}


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
    return {"team_id": identity.get("team_id", "")}


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
def slack_post_message(channel: str, thread_ts: str, text: str) -> dict[str, bool]:
    response = httpx.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {_credential('SLACK_BOT_TOKEN')}"},
        json={"channel": channel, "thread_ts": thread_ts, "text": text},
        timeout=15.0,
    )
    response.raise_for_status()
    return {"ok": response.json().get("ok") is True}


if __name__ == "__main__":
    mcp.run()
