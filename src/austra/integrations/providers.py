"""Reviewed provider calls and account checks."""

import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

from austra.integrations.google_workspace import remote_access_token
from austra.mcp_gateway import ExternalToolError, call_external, call_remote, remote_tools


def safe_mcp_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("MCP URL must be public HTTPS without embedded credentials")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except OSError as error:
        raise ValueError("MCP host could not be resolved") from error
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("MCP host must resolve to public addresses")
    return url


def github_identity(token: str) -> tuple[str, tuple[str, ...]]:
    try:
        response = httpx.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        response.raise_for_status()
        login = response.json().get("login")
    except (httpx.HTTPError, ValueError) as error:
        raise ExternalToolError("GitHub account verification failed; reconnect") from error
    if not isinstance(login, str) or not login:
        raise ExternalToolError("GitHub did not identify the connected account")
    scopes = tuple(
        item.strip()
        for item in response.headers.get("X-OAuth-Scopes", "").split(",")
        if item.strip()
    )
    return login, scopes


def gmail_identity(credentials: dict[str, str]) -> str:
    result = call_external("gmail_verify", {}, credentials)
    email = result.get("email")
    if not isinstance(email, str) or not email:
        raise ExternalToolError("Gmail did not identify the connected mailbox")
    return email


def slack_identity(credentials: dict[str, str]) -> tuple[str, tuple[str, ...]]:
    result = call_external("slack_verify", {}, {"SLACK_BOT_TOKEN": credentials["bot_token"]})
    team = result.get("team_id")
    if not isinstance(team, str) or not team:
        raise ExternalToolError("Slack did not identify the connected workspace")
    scopes = tuple(item.strip() for item in result.get("scopes", "").split(",") if item.strip())
    return team, scopes


def perform(
    template: str, action: str, arguments: dict, secrets: dict, endpoint_url: str | None = None
) -> dict[str, Any]:
    if template == "gmail" and action == "gmail.send_email":
        return call_external("gmail_send_email", arguments, secrets)
    if template == "slack" and action == "slack.post_message":
        return call_external(
            "slack_post_message", arguments, {"SLACK_BOT_TOKEN": secrets["bot_token"]}
        )
    if template == "github":
        headers = {
            "Authorization": f"Bearer {secrets['token']}",
            "Accept": "application/vnd.github+json",
        }
        if action == "github.list_repositories":
            response = httpx.get(
                "https://api.github.com/user/repos",
                headers=headers,
                params={"per_page": arguments.get("per_page", 30)},
                timeout=10,
            )
        elif action == "github.create_issue":
            response = httpx.post(
                f"https://api.github.com/repos/{arguments['owner']}/{arguments['repo']}/issues",
                headers=headers,
                json={key: arguments[key] for key in ("title", "body") if key in arguments},
                timeout=10,
            )
        else:
            raise ValueError("Unreviewed GitHub action")
        if response.status_code in {401, 403}:
            raise ExternalToolError(
                "GitHub permission failed; reconnect or grant the required scope"
            )
        if response.status_code == 429:
            raise ExternalToolError("GitHub rate limit reached; retry later")
        try:
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ExternalToolError(
                "GitHub request failed; check the connection and retry"
            ) from error
        return {"items": data} if isinstance(data, list) else data
    if template == "remote_mcp" and endpoint_url:
        import asyncio

        if not action.startswith("remote_mcp."):
            raise ValueError("Unreviewed remote MCP action")
        return asyncio.run(
            call_remote(
                endpoint_url,
                remote_access_token(endpoint_url, secrets),
                action.removeprefix("remote_mcp."),
                arguments,
            )
        )
    raise ValueError("Unreviewed provider action")


def discover(endpoint_url: str, token: str) -> list[dict]:
    import asyncio

    return asyncio.run(remote_tools(safe_mcp_url(endpoint_url), token))
