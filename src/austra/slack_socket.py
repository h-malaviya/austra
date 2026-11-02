"""Receive Slack events over an outbound Socket Mode connection."""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from austra.access import Access
from austra.files import Files
from austra.mcp_gateway import ExternalToolError, call_external_async

logger = logging.getLogger(__name__)
RECONNECT_SECONDS = 5


async def socket_url(token: str) -> str:
    result = await call_external_async("slack_socket_url", {}, {"SLACK_APP_TOKEN": token})
    url = result.get("url")
    if not isinstance(url, str) or not url.startswith("wss://"):
        raise ValueError("Slack returned an invalid Socket Mode URL")
    return url


async def receive_events(
    websocket: Any, token: str, access: Access, on_event: Callable[[dict[str, Any]], None]
) -> None:
    pending: set[asyncio.Task[None]] = set()
    try:
        while access.get("SLACK_APP_TOKEN") == token:
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=RECONNECT_SECONDS)
            except TimeoutError:
                continue
            try:
                envelope = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(envelope, dict):
                continue
            envelope_id = envelope.get("envelope_id")
            if isinstance(envelope_id, str) and envelope_id:
                await websocket.send(json.dumps({"envelope_id": envelope_id}))
            if envelope.get("type") == "disconnect":
                break
            payload = envelope.get("payload")
            if envelope.get("type") != "events_api" or not isinstance(payload, dict):
                continue
            task = asyncio.create_task(asyncio.to_thread(on_event, payload))
            pending.add(task)
            task.add_done_callback(pending.discard)
    finally:
        if pending:
            results = await asyncio.gather(*pending, return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results):
                logger.warning("Slack event processing failed")


async def run_socket_mode(
    access: Access,
    files: Files,
    on_event: Callable[[dict[str, Any]], None],
    status: dict[str, bool],
) -> None:
    failed = False
    while True:
        token = access.get("SLACK_APP_TOKEN")
        if not token:
            status["connected"] = False
            await asyncio.sleep(RECONNECT_SECONDS)
            continue
        try:
            url = await socket_url(token)
            async with connect(url, max_size=1_048_576) as websocket:
                status["connected"] = True
                failed = False
                files.record_event("connection.socket_connected", "Austra", "connector", "slack")
                await receive_events(websocket, token, access, on_event)
        except asyncio.CancelledError:
            raise
        except (ExternalToolError, OSError, TimeoutError, ValueError, WebSocketException) as error:
            if not failed:
                files.record_event(
                    "connection.socket_failed", "Austra", "connector", "slack", outcome="failed"
                )
                logger.warning("Slack Socket Mode connection failed: %s", type(error).__name__)
            failed = True
        finally:
            status["connected"] = False
        await asyncio.sleep(RECONNECT_SECONDS)
