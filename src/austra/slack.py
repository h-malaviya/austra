"""Verify Slack event envelopes before converting them into cases."""

import hashlib
import hmac
import time

REQUEST_MAX_AGE_SECONDS = 300


def verify_signature(body: bytes, timestamp: str, signature: str, signing_secret: str) -> bool:
    try:
        request_time = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - request_time) > REQUEST_MAX_AGE_SECONDS:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)


def event_message(payload: dict) -> tuple[str, str, str] | None:
    event = payload.get("event", {})
    if not isinstance(event, dict) or event.get("bot_id") or event.get("subtype"):
        return None
    is_request = event.get("type") == "app_mention" or (
        event.get("type") == "message" and event.get("channel_type") == "im"
    )
    if not is_request:
        return None
    text = event.get("text", "")
    sender = event.get("user", "")
    event_id = payload.get("event_id", "")
    if not all(isinstance(value, str) and value for value in (text, sender, event_id)):
        return None
    if len(text) > 10000:
        return None
    return text, sender, event_id
