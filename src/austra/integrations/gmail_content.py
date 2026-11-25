"""Read and extract bounded evidence from one already matched Gmail reply."""

import re
from base64 import urlsafe_b64decode
from hashlib import sha256
from html.parser import HTMLParser
from itertools import islice
from pathlib import PurePosixPath
from typing import Literal, NotRequired, TypedDict
from zipfile import BadZipFile

import httpx
from docx.opc.exceptions import PackageNotFoundError
from lxml.etree import XMLSyntaxError
from openpyxl.utils.exceptions import InvalidFileException
from pypdf.errors import PyPdfError

from austra.onboarding.extract import units, validate_file

GMAIL_MESSAGES = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
BYTE_LIMIT = 2 * 1024 * 1024
TEXT_LIMIT = 12_000
ATTACHMENT_LIMIT = 4
ATTACHMENT_SECTION_LIMIT = 128
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".csv", ".txt", ".png", ".jpg", ".jpeg"}


class ReplyExcerpt(TypedDict):
    location: str
    text: str


class ReplyAttachment(TypedDict):
    name: str
    status: Literal["extracted", "review_needed"]
    reason: NotRequired[str]
    sha256: NotRequired[str]
    excerpts: NotRequired[list[ReplyExcerpt]]


class GmailReplyContent(TypedDict):
    body: str
    attachments: list[ReplyAttachment]
    quoted_terms: dict[str, str]


class PlainEmail(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.ignored = set()

    def handle_starttag(self, tag, attrs):
        properties = dict(attrs)
        if tag == "blockquote" or properties.get("id", "").casefold() == "divrplyfwdmsg":
            self.parts.append("\n-----Original Message\n")
        if tag in {"style", "script", "head"}:
            self.ignored.add(tag)
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        self.ignored.discard(tag)

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def _parts(payload):
    yield payload
    if payload.get("filename") or payload.get("mimeType") == "message/rfc822":
        return
    for part in payload.get("parts", []):
        yield from _parts(part)


def _bytes(url, info, headers, *, limit):
    if info.get("size", 0) > limit:
        raise ValueError("File exceeds the automatic processing limit")
    encoded = info.get("data", "")
    identifier = info.get("attachmentId")
    if identifier:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,2048}", identifier):
            raise ValueError("Invalid Gmail attachment identifier")
        response = httpx.get(f"{url}/attachments/{identifier}", headers=headers, timeout=10)
        response.raise_for_status()
        encoded = response.json().get("data", "")
    if len(encoded) > (limit * 4 // 3) + 4:
        raise ValueError("File exceeds the automatic processing limit")
    data = urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    if len(data) > limit:
        raise ValueError("File exceeds the automatic processing limit")
    return data


def _attachment(url, part, headers, index):
    name = PurePosixPath(part["filename"].replace("\\", "/")).name[:200]
    item = {"name": name, "status": "review_needed"}
    if index >= ATTACHMENT_LIMIT:
        return {**item, "reason": "Too many attachments for automatic processing"}
    if PurePosixPath(name.lower()).suffix not in DOCUMENT_EXTENSIONS:
        return {**item, "reason": "Unsupported document type"}
    try:
        content = _bytes(url, part.get("body", {}), headers, limit=BYTE_LIMIT)
        media = validate_file(name, content)
        excerpts = []
        remaining = TEXT_LIMIT
        for section_index, (location, text) in enumerate(units(media, content)):
            text = text.strip()
            if section_index >= ATTACHMENT_SECTION_LIMIT or len(text) > remaining:
                return {**item, "reason": "Document exceeds the automatic review limit"}
            if text:
                excerpts.append({"location": location, "text": text})
                remaining -= len(text)
        return {**item, "status": "extracted" if excerpts else "review_needed",
                "sha256": sha256(content).hexdigest(), "excerpts": excerpts}
    except httpx.HTTPError:
        raise
    except (ValueError, OSError, TypeError, KeyError, IndexError, BadZipFile, PackageNotFoundError,
            XMLSyntaxError, InvalidFileException, PyPdfError):
        return {**item, "reason": "The attachment could not be extracted"}


def _quoted_terms(sources):
    """Record clearly labelled supplier statements as evidence, not PO authority."""
    patterns = {
        "quote_reference": r"^\s*Quote reference\s*:\s*([A-Za-z0-9._/-]{1,60})\s*$",
        "lead_time_days": r"^\s*Lead time\s*:\s*([0-9]{1,3})\s+days?\s*$",
        "quoted_unit_price": r"^\s*Quoted unit price\s*:\s*([A-Z]{3}\s+\d{1,10}(?:\.\d{1,4})?)\s*$",
    }
    output = {}
    for key, pattern in patterns.items():
        matches = {
            " ".join(match.split()) for source in sources
            for match in re.findall(pattern, source, re.MULTILINE | re.IGNORECASE)
        }
        if len(matches) == 1:
            output[key] = matches.pop()
    return output


def reply_content(access_token, message_id, expected_thread) -> GmailReplyContent:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id):
        raise ValueError("Invalid Gmail message identifier")
    url = f"{GMAIL_MESSAGES}/{message_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    response = httpx.get(url, headers=headers, params={"format": "full"}, timeout=10)
    response.raise_for_status()
    message = response.json()
    if message.get("id") != message_id or message.get("threadId") != expected_thread:
        raise ValueError("Gmail message did not match the tracked thread")
    files, plain, html = [], [], []
    for part in islice(_parts(message["payload"]), 100):
        if part.get("filename"):
            if any(item["name"].casefold() == "content-disposition"
                   and item["value"].strip().casefold().startswith("inline") for item in part.get("headers", [])):
                continue
            files.append(_attachment(url, part, headers, len(files)))
            continue
        mime = part.get("mimeType")
        if mime in {"text/plain", "text/html"}:
            try:
                data = _bytes(url, part.get("body", {}), headers, limit=BYTE_LIMIT)
            except ValueError:
                continue
            target = plain if mime == "text/plain" else html
            target.append(data.decode("utf-8", errors="replace")[:TEXT_LIMIT])
    if not plain and html:
        parser = PlainEmail()
        parser.feed("\n".join(html)[:TEXT_LIMIT])
        plain.append("".join(parser.parts))
    body = "\n".join(plain)[:TEXT_LIMIT].strip()
    sources = [body, *(unit["text"] for item in files for unit in item.get("excerpts", []))]
    return {"body": body, "attachments": files, "quoted_terms": _quoted_terms(sources)}
