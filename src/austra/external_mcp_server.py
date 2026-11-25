"""Local MCP server for Austra's approved provider operations."""

import os
import re
from base64 import b64decode, urlsafe_b64encode
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses
from typing import TypedDict

import httpx
from mcp.server.fastmcp import FastMCP

from austra.integrations.gmail_content import GmailReplyContent, reply_content

mcp = FastMCP("Austra external providers")
HTTP_TIMEOUT_SECONDS = 10.0


GmailReply = TypedDict("GmailReply", {
    "message_id": str,
    "thread_id": str,
    "from": str,
    "subject": str,
    "received_ms": int,
})


class GmailReplyResult(TypedDict):
    reply: GmailReply | None


class GmailRepliesResult(TypedDict):
    replies: list[GmailReply]
    latest_outbound_ms: int
    original_sent_ms: int


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
    return {
        "refresh_token": refresh_token,
        "email": _gmail_profile(access_token),
        "scopes": tokens.get("scope", ""),
    }


@mcp.tool()
def gmail_verify() -> dict[str, str]:
    tokens = _gmail_refresh()
    return {"email": _gmail_profile(tokens["access_token"]), "scopes": tokens.get("scope", "")}


@mcp.tool()
def gmail_find_reply(message_id: str, recipient: str) -> GmailReplyResult:
    """Return the first matching reply for an existing single-reply caller."""
    replies = gmail_list_replies(message_id, recipient)["replies"]
    return {"reply": replies[0] if replies else None}


@mcp.tool()
def gmail_list_replies(message_id: str, recipient: str) -> GmailRepliesResult:
    """List matching reply metadata in the thread of one confirmed outbound message."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id):
        raise ValueError("Invalid Gmail receipt")
    base = "https://gmail.googleapis.com/gmail/v1/users/me"
    headers = {"Authorization": f"Bearer {_gmail_access_token()}"}
    sent = httpx.get(
        f"{base}/messages/{message_id}", headers=headers, params={"format": "minimal"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    sent.raise_for_status()
    thread_id = sent.json().get("threadId", "")
    if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
        raise ValueError("Gmail did not identify the sent message thread")
    thread = httpx.get(
        f"{base}/threads/{thread_id}", headers=headers,
        params=[("format", "metadata"), *[("metadataHeaders", name) for name in
                ("From", "Subject", "Message-ID", "In-Reply-To", "References")]],
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    thread.raise_for_status()
    messages = thread.json().get("messages", [])

    def metadata(message):
        return {item["name"].lower(): item["value"]
                for item in message.get("payload", {}).get("headers", [])}

    original = next((item for item in messages if item.get("id") == message_id), None)
    if original is None or "SENT" not in original.get("labelIds", []):
        raise ValueError("The outbound message is unavailable in this mailbox")
    original_id = metadata(original).get("message-id", "")
    if not original_id:
        raise ValueError("Gmail did not return the outbound message reference")
    replies: list[GmailReply] = []
    linked = {original_id}
    original_date = int(original["internalDate"])
    latest_outbound = original_date
    for item in sorted(messages, key=lambda row: int(row.get("internalDate", "0"))):
        fields = metadata(item)
        received = int(item.get("internalDate", "0"))
        references = (fields.get("in-reply-to", "") + " " + fields.get("references", "")).split()
        labels = set(item.get("labelIds", []))
        if (
            item.get("id") == message_id or labels & {"DRAFT", "SPAM", "TRASH"}
            or received < original_date or linked.isdisjoint(references)
        ):
            continue
        senders = getaddresses([fields.get("from", "")])
        expected_sender = [address.casefold() for _, address in senders] == [recipient.casefold()]
        if "SENT" not in labels and not expected_sender:
            continue
        if fields.get("message-id"):
            linked.add(fields["message-id"])
        if "SENT" in labels:
            latest_outbound = max(latest_outbound, received)
        if "SENT" not in labels:
            replies.append({"message_id": item["id"], "thread_id": thread_id,
                            "from": recipient, "subject": str(make_header(decode_header(
                                fields.get("subject", ""))))[:250], "received_ms": received})
    return {"replies": replies, "latest_outbound_ms": latest_outbound, "original_sent_ms": original_date}


@mcp.tool()
def gmail_read_reply_content(message_id: str, thread_id: str) -> GmailReplyContent:
    """Read untrusted content for a reply already matched to a tracked delivery."""
    return reply_content(_gmail_access_token(), message_id, thread_id)


def _gmail_access_token() -> str:
    return _gmail_refresh()["access_token"]


def _gmail_refresh() -> dict:
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
    tokens = response.json()
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Google did not return an access token")
    return tokens


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
    _attach_pdf(message, attachment_name, attachment_base64)
    return _submit_gmail(message, _gmail_access_token())


def _attach_pdf(message, attachment_name, attachment_base64):
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


def _submit_gmail(message, token, thread_id=None):
    payload = {"raw": urlsafe_b64encode(message.as_bytes()).decode("ascii")}
    if thread_id:
        payload["threadId"] = thread_id
    response = httpx.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if thread_id and response.status_code == 429:
        return {"sent": False, "safe_retry": True}
    if response.status_code in (401, 403):
        return {
            "sent": False,
            "error": "Reconnect Gmail on the Connections page to grant send access.",
        }
    response.raise_for_status()
    sent = response.json()
    if not isinstance(sent, dict) or not isinstance(sent.get("id"), str):
        raise TypeError("Gmail did not confirm the message")
    if thread_id and sent.get("threadId") != thread_id:
        raise ValueError("Gmail did not confirm delivery in the approved thread")
    return {"sent": True, "message_id": sent["id"]}


@mcp.tool()
def gmail_reply_in_thread(
    to: str, subject: str, body: str, thread_id: str, parent_id: str, dispatch_id: str,
    attachment_name: str = "", attachment_base64: str = "",
) -> dict[str, str | bool]:
    """Send the approved reply to the matched sender in its original Gmail thread."""
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
               for value in (thread_id, parent_id, dispatch_id)):
        raise ValueError("Invalid Gmail reply reference")
    try:
        token = _gmail_access_token()
        response = httpx.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{parent_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Message-ID",
                    "References", "Auto-Submitted", "List-Id"]}, timeout=HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.TransportError:
        return {"sent": False, "safe_retry": True}
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        return {"sent": False, "safe_retry": status == 429 or status >= 500, "rejected": True}
    parent = response.json()
    headers = {item["name"].casefold(): item["value"] for item in parent["payload"]["headers"]}
    parent_subject = headers.get("subject", "")
    bare_subject = re.sub(r"^(?:re:\s*)+", "", parent_subject, flags=re.IGNORECASE)
    header_id = headers.get("message-id", "")
    id_pattern = r"<[^<>\s]{1,320}>"
    if (parent.get("id") != parent_id or parent.get("threadId") != thread_id
            or [address.casefold() for _, address in getaddresses([headers.get("from", "")])] != [to.casefold()]
            or bare_subject.casefold() != subject.casefold() or not re.fullmatch(id_pattern, header_id)
            or headers.get("auto-submitted", "no").strip().casefold() != "no" or headers.get("list-id")):
        raise ValueError("Gmail reply could not be bound to the approved recipient and thread")
    message = EmailMessage()
    message["From"] = _credential("GMAIL_SENDER_EMAIL")
    message["To"] = to
    message["Subject"] = f"Re: {subject}"
    message["In-Reply-To"] = header_id
    message["References"] = " ".join([*re.findall(id_pattern, headers.get("references", ""))[-5:], header_id])
    message["Message-ID"] = f"<{dispatch_id}@austra.local>"
    message["Auto-Submitted"] = "auto-replied"
    message.set_content(body)
    _attach_pdf(message, attachment_name, attachment_base64)
    return _submit_gmail(message, token, thread_id)


@mcp.tool()
def gmail_find_automatic_reply(thread_id: str, dispatch_id: str, recipient: str) -> dict[str, str | None]:
    """Look for an already sent message using the persisted ID, without resending it."""
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item) for item in (thread_id, dispatch_id)):
        raise ValueError("Invalid automatic reply reference")
    response = httpx.get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}",
        headers={"Authorization": f"Bearer {_gmail_access_token()}"},
        params={"format": "metadata", "metadataHeaders": ["Message-ID", "To"]}, timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    thread = response.json()
    if thread.get("id") != thread_id:
        raise ValueError("Gmail returned an unexpected thread")
    matches = []
    for message in thread.get("messages", []):
        headers = {item["name"].casefold(): item["value"] for item in message.get("payload", {}).get("headers", [])}
        if (message.get("threadId") == thread_id and "SENT" in message.get("labelIds", [])
                and headers.get("message-id") == f"<{dispatch_id}@austra.local>"
                and [email.casefold() for _, email in getaddresses([headers.get("to", "")])] == [recipient.casefold()]):
            matches.append(message["id"])
    return {"message_id": matches[0] if len(matches) == 1 else None}


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
    return {
        "team_id": identity.get("team_id", ""),
        "scopes": response.headers.get("x-oauth-scopes", ""),
    }


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
