"""Read only checks for configured external connections."""

from collections.abc import Callable
from urllib.parse import urlencode

import httpx

from austra.agent import ConnectionCheckError

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


def _gmail_profile_email(access_token: str) -> str:
    response = httpx.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10.0,
    )
    if response.status_code in {401, 403}:
        raise ConnectionCheckError("Gmail access was denied. Check the OAuth scopes.", 400)
    response.raise_for_status()
    profile = response.json()
    email = profile.get("emailAddress", "") if isinstance(profile, dict) else ""
    if not isinstance(email, str) or not email:
        raise ConnectionCheckError("Google did not return a Gmail mailbox.", 400)
    return email


def exchange_gmail_code(
    code: str, client_id: str, client_secret: str, redirect_uri: str
) -> tuple[str, str]:
    try:
        response = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=10.0,
        )
        if response.status_code in {400, 401}:
            raise ConnectionCheckError("Google rejected the authorization. Reconnect Gmail.", 400)
        response.raise_for_status()
        tokens = response.json()
        if not isinstance(tokens, dict):
            raise ConnectionCheckError("Google returned an invalid authorization response.", 503)
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(refresh_token, str)
            or not refresh_token
        ):
            raise ConnectionCheckError("Google did not grant offline access. Reconnect Gmail.", 400)
        return refresh_token, _gmail_profile_email(access_token)
    except ConnectionCheckError:
        raise
    except (httpx.HTTPError, ValueError) as error:
        raise ConnectionCheckError(
            "Could not complete Gmail authorization. Try again.", 503
        ) from error


def verify_slack(values: dict[str, str]) -> None:
    try:
        response = httpx.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {values['SLACK_BOT_TOKEN']}"},
            timeout=10.0,
        )
        response.raise_for_status()
        identity = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise ConnectionCheckError("Could not reach Slack. Try again.", 503) from error
    if not isinstance(identity, dict) or not identity.get("ok") or not identity.get("bot_id"):
        raise ConnectionCheckError("Slack rejected the bot token.", 400)
    if identity.get("team_id") != values["SLACK_ALLOWED_TEAM_ID"]:
        raise ConnectionCheckError(
            "Slack bot belongs to a different workspace. Check the saved team ID (starts with T).",
            400,
        )
    try:
        response = httpx.post(
            "https://slack.com/api/apps.connections.open",
            headers={"Authorization": f"Bearer {values['SLACK_APP_TOKEN']}"},
            timeout=10.0,
        )
        response.raise_for_status()
        connection = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise ConnectionCheckError("Could not check Slack Socket Mode. Try again.", 503) from error
    if (
        not isinstance(connection, dict)
        or not connection.get("ok")
        or not str(connection.get("url", "")).startswith("wss://")
    ):
        raise ConnectionCheckError(
            "Slack Socket Mode rejected the app-level token. Enable Socket Mode and use an "
            "app-level token with connections:write.",
            400,
        )


def verify_gmail(values: dict[str, str]) -> None:
    try:
        token_response = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": values["GMAIL_CLIENT_ID"],
                "client_secret": values["GMAIL_CLIENT_SECRET"],
                "refresh_token": values["GMAIL_REFRESH_TOKEN"],
                "grant_type": "refresh_token",
            },
            timeout=10.0,
        )
        if token_response.status_code in {400, 401}:
            raise ConnectionCheckError("Google rejected the Gmail OAuth credentials.", 400)
        token_response.raise_for_status()
        token_data = token_response.json()
        token = token_data.get("access_token") if isinstance(token_data, dict) else None
        if not isinstance(token, str) or not token:
            raise ConnectionCheckError("Google did not return an access token.", 503)
        email = _gmail_profile_email(token)
    except ConnectionCheckError:
        raise
    except (httpx.HTTPError, ValueError) as error:
        raise ConnectionCheckError("Could not verify Gmail. Try again.", 503) from error
    if not isinstance(email, str) or email.casefold() != values["GMAIL_SENDER_EMAIL"].casefold():
        raise ConnectionCheckError("Gmail account does not match the sender address.", 400)


VERIFIERS: dict[str, Verifier] = {"gmail": verify_gmail, "slack": verify_slack}
