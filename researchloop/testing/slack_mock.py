"""Mock Slack API server for local testing.

Provides a lightweight FastAPI app that mimics Slack's API endpoints so
the orchestrator's SlackNotifier can be exercised without hitting the
real Slack service.

Start via CLI::

    researchloop mock-slack --port 9876

Then set ``RESEARCHLOOP_SLACK_API_URL=http://localhost:9876/api`` so
all outbound Slack API calls land here instead of ``slack.com``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# In-memory stores
# ------------------------------------------------------------------


@dataclass
class CapturedMessage:
    """A message captured by the mock ``chat.postMessage`` endpoint."""

    channel: str
    text: str
    thread_ts: str | None
    ts: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class CapturedUpload:
    """A file upload captured by the mock ``files.upload`` endpoint."""

    channels: str
    filename: str
    initial_comment: str
    content_length: int
    timestamp: float = field(default_factory=time.time)


class MessageStore:
    """Thread-safe (single-process) store for captured Slack interactions."""

    def __init__(self) -> None:
        self.messages: list[CapturedMessage] = []
        self.uploads: list[CapturedUpload] = []

    def clear(self) -> None:
        self.messages.clear()
        self.uploads.clear()

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [
                {
                    "channel": m.channel,
                    "text": m.text,
                    "thread_ts": m.thread_ts,
                    "ts": m.ts,
                    "timestamp": m.timestamp,
                }
                for m in self.messages
            ],
            "uploads": [
                {
                    "channels": u.channels,
                    "filename": u.filename,
                    "initial_comment": u.initial_comment,
                    "content_length": u.content_length,
                    "timestamp": u.timestamp,
                }
                for u in self.uploads
            ],
        }


# ------------------------------------------------------------------
# Pydantic models for request bodies
# ------------------------------------------------------------------


class SendEventRequest(BaseModel):
    """Body for ``POST /send-event``."""

    text: str
    user: str = "U_TEST"
    channel: str = "C_TEST"
    channel_type: str = "channel"
    thread_ts: str | None = None
    event_type: str = "message"


# ------------------------------------------------------------------
# App factory
# ------------------------------------------------------------------

_ts_counter: int = 0


def _next_ts() -> str:
    """Generate a monotonically increasing Slack-style timestamp."""
    global _ts_counter
    _ts_counter += 1
    return f"{int(time.time())}.{_ts_counter:06d}"


def create_mock_slack_app(
    *,
    target_url: str = "http://localhost:8080",
    signing_secret: str = "mock_signing_secret",
) -> FastAPI:
    """Create a FastAPI app that mocks the Slack API.

    Parameters
    ----------
    target_url:
        The orchestrator URL to send events to via ``/send-event``.
    signing_secret:
        Signing secret used when generating Slack signatures for
        outbound test events via ``/send-event``.
    """
    app = FastAPI(title="Mock Slack API")
    store = MessageStore()

    # Expose the store so tests can inspect it directly.
    app.state.store = store  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Slack API mocks
    # ------------------------------------------------------------------

    @app.post("/api/chat.postMessage")
    async def chat_post_message(request: Request) -> JSONResponse:
        """Mock ``chat.postMessage``."""
        body = await request.json()
        ts = _next_ts()
        msg = CapturedMessage(
            channel=body.get("channel", ""),
            text=body.get("text", ""),
            thread_ts=body.get("thread_ts"),
            ts=ts,
        )
        store.messages.append(msg)
        logger.info("Captured message to %s: %s", msg.channel, msg.text[:80])
        return JSONResponse({"ok": True, "ts": ts, "channel": msg.channel})

    @app.post("/api/files.upload")
    async def files_upload(request: Request) -> JSONResponse:
        """Mock ``files.upload``."""
        form = await request.form()
        content_length = 0
        file_field = form.get("file")
        if file_field is not None and hasattr(file_field, "read"):
            data = await file_field.read()  # type: ignore[union-attr]
            content_length = len(data)

        upload = CapturedUpload(
            channels=str(form.get("channels", "")),
            filename=str(form.get("filename", "")),
            initial_comment=str(form.get("initial_comment", "")),
            content_length=content_length,
        )
        store.uploads.append(upload)
        logger.info("Captured file upload: %s", upload.filename)
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------
    # Inspection endpoints
    # ------------------------------------------------------------------

    @app.get("/captured")
    async def get_captured() -> JSONResponse:
        """Return all captured messages and uploads."""
        return JSONResponse(store.to_dict())

    @app.post("/clear")
    async def clear_captured() -> JSONResponse:
        """Reset captured messages and uploads."""
        store.clear()
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------
    # Test event sender
    # ------------------------------------------------------------------

    @app.post("/send-event")
    async def send_event(req: SendEventRequest) -> JSONResponse:
        """Build a signed Slack event and POST it to the orchestrator.

        This lets you trigger the orchestrator's ``/api/slack/events``
        endpoint as if the real Slack platform sent the event.
        """
        event_id = f"Ev{uuid.uuid4().hex[:10].upper()}"
        payload = {
            "type": "event_callback",
            "event_id": event_id,
            "event": {
                "type": req.event_type,
                "text": req.text,
                "user": req.user,
                "channel": req.channel,
                "channel_type": req.channel_type,
                "ts": req.thread_ts or _next_ts(),
            },
        }
        if req.thread_ts:
            payload["event"]["thread_ts"] = req.thread_ts  # type: ignore[index]

        import json as _json

        body_bytes = _json.dumps(payload).encode()

        timestamp = str(int(time.time()))
        basestring = f"v0:{timestamp}:{body_bytes.decode('utf-8')}"
        sig = (
            "v0="
            + hmac.new(
                signing_secret.encode(),
                basestring.encode(),
                hashlib.sha256,
            ).hexdigest()
        )

        headers = {
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": sig,
        }

        target = target_url.rstrip("/") + "/api/slack/events"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    target,
                    content=body_bytes,
                    headers=headers,
                    timeout=10.0,
                )
                return JSONResponse(
                    {
                        "ok": True,
                        "status_code": resp.status_code,
                        "response": resp.json(),
                        "event_id": event_id,
                    }
                )
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=502,
            )

    return app
