"""Reviewed PO email outbox with one attempt per approved order."""

import base64
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

import httpx

from austra.store import Store, _id, _now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GmailConfig:
    client_id: str
    client_secret: str
    refresh_token: str
    sender_email: str

    @classmethod
    def from_environment(cls) -> "GmailConfig":
        values = {
            "client_id": os.getenv("GMAIL_CLIENT_ID", ""),
            "client_secret": os.getenv("GMAIL_CLIENT_SECRET", ""),
            "refresh_token": os.getenv("GMAIL_REFRESH_TOKEN", ""),
            "sender_email": os.getenv("GMAIL_SENDER_EMAIL", ""),
        }
        if not all(values.values()):
            raise ValueError("Gmail sender credentials are not configured")
        return cls(**values)


@dataclass(frozen=True)
class SentMessage:
    message_id: str
    thread_id: str


class TokenUnavailable(RuntimeError):
    """OAuth failed before any message send attempt."""


def prepare_po_email(
    connection: sqlite3.Connection, order: sqlite3.Row, supplier: sqlite3.Row
) -> None:
    subject = f"Purchase order {order['id']}"
    body = (
        f"Hello {supplier['name']},\n\n"
        f"Please confirm this purchase order.\n\n"
        f"Material: {order['material']}\n"
        f"Grade: {order['grade']}\n"
        f"Quantity: {order['quantity_tonnes']} metric tonnes\n"
        f"Unit price: USD {order['unit_price_usd']} per metric tonne\n"
        f"Material value: USD {order['total_usd']}\n"
        f"Payment terms: {order['payment_terms']}\n"
        f"Delivery terms: {order['delivery_terms']}\n"
        f"Freight: {order['freight_terms']}\n"
        f"Taxes: {order['tax_terms']}\n\n"
        "Please reply in this thread with your confirmation or any requested changes.\n"
    )
    timestamp = _now()
    connection.execute(
        "INSERT INTO email_outbox VALUES (?, ?, ?, ?, ?, 'ready', NULL, NULL, ?, ?)",
        (_id(), order["id"], supplier["email"], subject, body, timestamp, timestamp),
    )


def send_gmail_message(draft: dict[str, Any], config: GmailConfig) -> SentMessage:
    message = EmailMessage()
    message["From"] = config.sender_email
    message["To"] = draft["recipient"]
    message["Subject"] = draft["subject"]
    domain = config.sender_email.rsplit("@", maxsplit=1)[-1]
    message["Message-ID"] = f"<austra-po-{draft['po_id']}@{domain}>"
    message.set_content(draft["body"])
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    with httpx.Client(timeout=20.0) as client:
        token = _access_token(client, config)
        response = client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {token}"},
            json={"raw": raw},
        )
        response.raise_for_status()
        result = response.json()
        return SentMessage(message_id=result["id"], thread_id=result["threadId"])


def _access_token(client: httpx.Client, config: GmailConfig) -> str:
    try:
        response = client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "refresh_token": config.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        response.raise_for_status()
        return response.json()["access_token"]
    except (httpx.HTTPError, KeyError, ValueError) as error:
        raise TokenUnavailable("Gmail token refresh failed") from error


def _plain_body(payload: dict[str, Any]) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        encoded = payload["body"]["data"]
        padded = encoded + "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        found = _plain_body(part)
        if found:
            return found
    return ""


def fetch_gmail_thread(thread_id: str, config: GmailConfig) -> list[dict[str, str]]:
    with httpx.Client(timeout=20.0) as client:
        token = _access_token(client, config)
        response = client.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"format": "full"},
        )
        response.raise_for_status()
        thread = response.json()
    messages = []
    for message in thread.get("messages", [])[-100:]:
        payload = message.get("payload", {})
        headers = {
            header.get("name", "").lower(): header.get("value", "")
            for header in payload.get("headers", [])
        }
        sender = parseaddr(headers.get("from", ""))[1]
        if not sender or not message.get("id"):
            continue
        messages.append(
            {
                "id": message["id"],
                "sender": sender,
                "body": (_plain_body(payload) or message.get("snippet", ""))[:10000],
            }
        )
    return messages


class EmailOutbox:
    def __init__(
        self,
        store: Store,
        sender: Callable[[dict[str, Any], GmailConfig], SentMessage] = send_gmail_message,
        thread_fetcher: Callable[[str, GmailConfig], list[dict[str, str]]] = fetch_gmail_thread,
    ) -> None:
        self.store = store
        self.sender = sender
        self.thread_fetcher = thread_fetcher

    def for_case(self, case_id: str) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT email_outbox.* FROM email_outbox "
                    "JOIN purchase_orders ON purchase_orders.id = email_outbox.po_id "
                    "WHERE purchase_orders.case_id = ? ORDER BY email_outbox.created_at",
                    (case_id,),
                )
            ]

    def send(self, po_id: str, config: GmailConfig | None = None) -> dict[str, Any]:
        credentials = config or GmailConfig.from_environment()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            draft = connection.execute(
                "SELECT email_outbox.* FROM email_outbox "
                "JOIN purchase_orders ON purchase_orders.id = email_outbox.po_id "
                "WHERE email_outbox.po_id = ? AND purchase_orders.status = 'approved'",
                (po_id,),
            ).fetchone()
            if draft is None:
                raise KeyError(po_id)
            if draft["status"] != "ready":
                raise ValueError("This PO email is already sent or requires reconciliation")
            connection.execute(
                "UPDATE email_outbox SET status = 'sending', updated_at = ? WHERE po_id = ?",
                (_now(), po_id),
            )
            data = dict(draft)
        try:
            sent = self.sender(data, credentials)
        except TokenUnavailable as error:
            self._set_status(po_id, "ready")
            raise ValueError(str(error)) from error
        except Exception as error:
            logger.exception("Gmail send outcome is uncertain for PO %s", po_id)
            self._set_status(po_id, "uncertain")
            raise ValueError("Gmail outcome is uncertain; reconcile before retrying") from error
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE email_outbox SET status = 'sent', gmail_message_id = ?, "
                "gmail_thread_id = ?, updated_at = ? WHERE po_id = ? AND status = 'sending'",
                (sent.message_id, sent.thread_id, _now(), po_id),
            )
            self.store._event(
                connection,
                connection.execute(
                    "SELECT case_id FROM purchase_orders WHERE id = ?", (po_id,)
                ).fetchone()["case_id"],
                "po_email_sent",
                {"po_id": po_id, "gmail_message_id": sent.message_id, "thread_id": sent.thread_id},
            )
            return dict(
                connection.execute(
                    "SELECT * FROM email_outbox WHERE po_id = ?", (po_id,)
                ).fetchone()
            )

    def _set_status(self, po_id: str, status: str) -> None:
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE email_outbox SET status = ?, updated_at = ? WHERE po_id = ?",
                (status, _now(), po_id),
            )

    def sync_thread(self, po_id: str, config: GmailConfig | None = None) -> dict[str, int]:
        credentials = config or GmailConfig.from_environment()
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT email_outbox.gmail_message_id, email_outbox.gmail_thread_id, "
                "purchase_orders.case_id FROM email_outbox "
                "JOIN purchase_orders ON purchase_orders.id = email_outbox.po_id "
                "WHERE email_outbox.po_id = ? AND email_outbox.status = 'sent'",
                (po_id,),
            ).fetchone()
            if row is None:
                raise KeyError(po_id)
            case_id = row["case_id"]
            thread_id = row["gmail_thread_id"]
            sent_id = row["gmail_message_id"]
        try:
            messages = self.thread_fetcher(thread_id, credentials)
        except (httpx.HTTPError, TokenUnavailable, ValueError) as error:
            raise ValueError("Gmail thread sync failed") from error
        added = 0
        for message in messages:
            if (
                message["id"] == sent_id
                or message["sender"].casefold() == credentials.sender_email.casefold()
            ):
                continue
            result = self.store.add_message(
                case_id,
                message["body"],
                message["sender"],
                channel="gmail",
                external_id=message["id"],
            )
            added += not result["duplicate"]
        return {"new_messages": added, "thread_messages": len(messages)}
