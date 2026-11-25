"""File-scoped OAuth for the official Google Workspace MCP endpoints."""

from typing import Literal
from urllib.parse import urlencode

import httpx

from austra.mcp_gateway import ExternalToolError

GOOGLE_WORKSPACE_SCOPE = "https://www.googleapis.com/auth/drive.file"
GOOGLE_WORKSPACE_ENDPOINTS = {
    "Google Docs": "https://docsmcp.googleapis.com/mcp/v1",
    "Google Sheets": "https://sheetsmcp.googleapis.com/mcp/v1",
    "Google Slides": "https://slidesmcp.googleapis.com/mcp/v1",
    "Google Drive": "https://drivemcp.googleapis.com/mcp/v1",
}
GoogleWorkspaceApp = Literal["Google Docs", "Google Sheets", "Google Slides", "Google Drive"]
WORKSPACE_ACCESS_PROMPT = (
    "Set google_workspace_required to the exact Google Docs, Google Sheets, Google Slides, "
    "or Google Drive app if the administrator asks to connect it or access is required for "
    "the requested work right now. Use Google Drive for a general Google Workspace connection "
    "request. Explain why access is needed in the reply. The application checks access and "
    "offers consent when available. Leave it null for Gmail, hypothetical needs, and drafting "
    "or previewing workflows with sample data."
)
TOKEN_URL = "https://oauth2.googleapis.com/token"
HTTP_TIMEOUT = 10.0


class WorkspaceConsentError(ExternalToolError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def authorization_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": f"openid email {GOOGLE_WORKSPACE_SCOPE}",
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


def _token(data: dict[str, str]) -> dict:
    try:
        response = httpx.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT)
        result = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise WorkspaceConsentError(
            "token_unavailable", "Google Workspace authorization was unavailable."
        ) from error
    if response.is_error:
        error_code = result.get("error") if isinstance(result, dict) else None
        if error_code in ("invalid_client", "unauthorized_client"):
            raise WorkspaceConsentError(
                "client_rejected", "Google rejected the Workspace OAuth client."
            )
        if error_code == "invalid_grant":
            raise WorkspaceConsentError(
                "grant_rejected", "Google rejected or expired the Workspace grant."
            )
        raise WorkspaceConsentError(
            "token_rejected", "Google did not exchange the Workspace consent."
        )
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("access_token"), str)
        or not result["access_token"]
    ):
        raise WorkspaceConsentError(
            "token_rejected", "Google did not return a Workspace access token."
        )
    return result


def exchange_code(
    code: str,
    client_id: str,
    secret: str,
    redirect_uri: str,
    verifier: str,
) -> tuple[str, str, str]:
    result = _token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": secret,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
    )
    scopes = result.get("scope")
    if not isinstance(scopes, str) or GOOGLE_WORKSPACE_SCOPE not in scopes.split():
        raise WorkspaceConsentError(
            "file_scope_missing", "Google Workspace file access was declined."
        )
    refresh = result.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise WorkspaceConsentError(
            "offline_missing", "Google did not grant offline Workspace access."
        )
    token = result["access_token"]
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = httpx.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        account = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise WorkspaceConsentError(
            "account_unverified", "Could not verify the Google Workspace account."
        ) from error
    if not isinstance(account, dict) or account.get("email_verified") is not True:
        raise WorkspaceConsentError(
            "account_unverified", "Google did not verify the account email."
        )
    email = account.get("email")
    if not isinstance(email, str) or not email.strip() or len(email) > 254:
        raise WorkspaceConsentError(
            "account_unverified", "Google did not identify the Workspace account."
        )
    return token, refresh, email.strip().casefold()


def workspace_credentials(client_id: str, secret: str, refresh: str) -> dict[str, str]:
    return {
        "google_workspace_client_id": client_id,
        "google_workspace_client_secret": secret,
        "google_workspace_refresh_token": refresh,
    }


def remote_access_token(endpoint_url: str, credentials: dict[str, str]) -> str:
    if "google_workspace_refresh_token" not in credentials:
        return credentials.get("token", "")
    if endpoint_url not in GOOGLE_WORKSPACE_ENDPOINTS.values():
        raise ValueError("Google OAuth credentials require an official Workspace endpoint")
    result = _token(
        {
            "grant_type": "refresh_token",
            "client_id": credentials["google_workspace_client_id"],
            "client_secret": credentials["google_workspace_client_secret"],
            "refresh_token": credentials["google_workspace_refresh_token"],
        }
    )
    scopes = result.get("scope")
    if scopes is not None and (
        not isinstance(scopes, str) or GOOGLE_WORKSPACE_SCOPE not in scopes.split()
    ):
        raise ExternalToolError("Google Workspace file access expired. Connect again.")
    return result["access_token"]
