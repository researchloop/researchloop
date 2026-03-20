"""SlackSimulator -- in-process test harness for the Slack bot.

Provides a high-level API for writing integration tests against the
``/api/slack/events`` endpoint.  Captures all outbound Slack API calls
(``chat.postMessage``, ``files.upload``) so tests can assert on the
bot's replies without hitting a real Slack workspace.

Example::

    async def test_help():
        sim = SlackSimulator(app, signing_secret="test_secret")
        resp = await sim.send_message("help")
        assert "sprint" in resp

Usage requires an app created via ``create_app(orchestrator)`` where
the orchestrator's Slack config uses the same signing secret passed
to the simulator.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import httpx

_TS_COUNTER: int = 0


def _next_ts() -> str:
    """Generate a monotonically increasing Slack-style timestamp."""
    global _TS_COUNTER
    _TS_COUNTER += 1
    return f"{int(time.time())}.{_TS_COUNTER:06d}"


@dataclass
class SlackResponse:
    """Captured response from the Slack bot.

    Contains all messages and file uploads the bot sent in reply
    to a single ``send_message`` call.
    """

    messages: list[str] = field(default_factory=list)
    raw_messages: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[dict[str, Any]] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The first (usually only) message text."""
        return self.messages[0] if self.messages else ""

    def __contains__(self, item: str) -> bool:
        """Check if any message contains the given string."""
        return any(item in m for m in self.messages)

    def __bool__(self) -> bool:
        """True if the bot sent at least one message or upload."""
        return bool(self.messages or self.uploads)

    def __repr__(self) -> str:
        n_msg = len(self.messages)
        n_up = len(self.uploads)
        preview = self.text[:60] + "..." if len(self.text) > 60 else self.text
        return f"SlackResponse(messages={n_msg}, uploads={n_up}, text={preview!r})"


class SlackSimulator:
    """In-process Slack bot test harness.

    Sends properly signed Slack events to a FastAPI app and captures
    the bot's outbound Slack API calls so tests can inspect them.

    Parameters
    ----------
    app:
        A FastAPI app returned by ``create_app(orchestrator)``.
    signing_secret:
        The Slack signing secret configured on the orchestrator.
        Must match ``orchestrator.config.slack.signing_secret``.
    wait_seconds:
        How long to wait (in seconds) for background tasks to
        complete after sending an event.  The Slack events handler
        returns 200 immediately and processes events via
        ``asyncio.create_task``, so the simulator needs to yield
        control briefly.
    """

    def __init__(
        self,
        app: Any,
        signing_secret: str = "test_secret",
        wait_seconds: float = 1.0,
    ) -> None:
        self.app = app
        self.signing_secret = signing_secret
        self.wait_seconds = wait_seconds

        # Internal state -- reset on each send_message call.
        self._captured_messages: list[dict[str, Any]] = []
        self._captured_uploads: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_message(
        self,
        text: str,
        user: str = "U_TEST",
        channel: str = "C_TEST",
        channel_type: str = "channel",
        thread_ts: str | None = None,
        *,
        event_type: str = "message",
    ) -> SlackResponse:
        """Send a simulated Slack message and return the bot's response.

        Builds a signed Slack event payload, POSTs it to the app's
        ``/api/slack/events`` endpoint, waits for background tasks,
        and returns a :class:`SlackResponse` with captured messages.

        Parameters
        ----------
        text:
            The message text to send.
        user:
            Slack user ID of the sender.
        channel:
            Slack channel ID where the message is sent.
        channel_type:
            Type of channel (``"channel"``, ``"im"``, ``"group"``).
        thread_ts:
            Thread timestamp for threaded replies.
        event_type:
            Slack event type (``"message"`` or ``"app_mention"``).
        """
        self._captured_messages.clear()
        self._captured_uploads.clear()

        payload = self._build_event(
            text=text,
            user=user,
            channel=channel,
            channel_type=channel_type,
            thread_ts=thread_ts,
            event_type=event_type,
        )
        body_bytes, headers = self._sign(payload)

        return await self._send_and_capture(body_bytes, headers)

    async def send_bot_message(
        self,
        text: str,
        channel: str = "C_TEST",
        thread_ts: str | None = None,
    ) -> SlackResponse:
        """Send a message with a ``bot_id`` set (should be ignored)."""
        self._captured_messages.clear()
        self._captured_uploads.clear()

        event_id = f"Ev{uuid.uuid4().hex[:10].upper()}"
        event: dict[str, Any] = {
            "type": "message",
            "text": text,
            "bot_id": "B_TESTBOT",
            "channel": channel,
            "channel_type": "channel",
            "ts": thread_ts or _next_ts(),
        }
        if thread_ts:
            event["thread_ts"] = thread_ts

        payload: dict[str, Any] = {
            "type": "event_callback",
            "event_id": event_id,
            "event": event,
        }
        body_bytes, headers = self._sign(payload)

        return await self._send_and_capture(body_bytes, headers)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _send_and_capture(
        self,
        body_bytes: bytes,
        headers: dict[str, str],
    ) -> SlackResponse:
        """POST an event and capture outbound Slack API calls.

        Patches ``SlackNotifier._post_message`` and
        ``SlackNotifier._upload_file`` at the class level so every
        instance (including ad-hoc ones created in the handler)
        uses our capturing replacements.

        The patch remains active during the sleep window so
        background tasks spawned by the handler can complete.
        """
        # Build capturing closures that reference our lists.
        captured_msgs = self._captured_messages
        captured_ups = self._captured_uploads

        async def _capture_post(
            notifier_self: Any,
            text: str,
            channel: str | None = None,
            thread_ts: str | None = None,
        ) -> dict[str, Any]:
            ch = channel or getattr(notifier_self, "channel_id", "")
            payload: dict[str, Any] = {
                "channel": ch,
                "text": text,
            }
            if thread_ts:
                payload["thread_ts"] = thread_ts
            captured_msgs.append(payload)
            return {"ok": True, "ts": _next_ts(), "channel": ch}

        async def _capture_upload(
            notifier_self: Any,
            filepath: str,
            filename: str,
            channel: str | None = None,
            initial_comment: str = "",
        ) -> dict[str, Any]:
            ch = channel or getattr(notifier_self, "channel_id", "")
            captured_ups.append(
                {
                    "filepath": filepath,
                    "filename": filename,
                    "channel": ch,
                    "initial_comment": initial_comment,
                }
            )
            return {"ok": True}

        transport = httpx.ASGITransport(app=self.app)

        # Patch both the canonical module path AND the orchestrator's
        # import of SlackNotifier.  This is needed because
        # importlib.reload() in other tests can cause the orchestrator
        # to hold a reference to a different class object than the
        # one currently in researchloop.comms.slack.
        with (
            patch(
                "researchloop.comms.slack.SlackNotifier._post_message",
                _capture_post,
            ),
            patch(
                "researchloop.comms.slack.SlackNotifier._upload_file",
                _capture_upload,
            ),
            patch(
                "researchloop.core.orchestrator.SlackNotifier._post_message",
                _capture_post,
            ),
            patch(
                "researchloop.core.orchestrator.SlackNotifier._upload_file",
                _capture_upload,
            ),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                await client.post(
                    "/api/slack/events",
                    content=body_bytes,
                    headers=headers,
                )

            # The handler returns 200 immediately and spawns a
            # background task.  Wait for it to finish.
            await asyncio.sleep(self.wait_seconds)

        return SlackResponse(
            messages=[m.get("text", "") for m in self._captured_messages],
            raw_messages=list(self._captured_messages),
            uploads=list(self._captured_uploads),
        )

    def _build_event(
        self,
        text: str,
        user: str,
        channel: str,
        channel_type: str,
        thread_ts: str | None,
        event_type: str,
    ) -> dict[str, Any]:
        """Build a Slack Events API payload."""
        event_id = f"Ev{uuid.uuid4().hex[:10].upper()}"
        ts = thread_ts or _next_ts()
        event: dict[str, Any] = {
            "type": event_type,
            "text": text,
            "user": user,
            "channel": channel,
            "channel_type": channel_type,
            "ts": ts,
        }
        if thread_ts:
            event["thread_ts"] = thread_ts

        return {
            "type": "event_callback",
            "event_id": event_id,
            "event": event,
        }

    def _sign(self, payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
        """Sign a payload and return ``(body_bytes, headers)``."""
        body_bytes = json.dumps(payload).encode("utf-8")
        timestamp = str(int(time.time()))
        basestring = f"v0:{timestamp}:{body_bytes.decode('utf-8')}"
        sig = (
            "v0="
            + hmac.new(
                self.signing_secret.encode(),
                basestring.encode(),
                hashlib.sha256,
            ).hexdigest()
        )
        headers = {
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": sig,
        }
        return body_bytes, headers
