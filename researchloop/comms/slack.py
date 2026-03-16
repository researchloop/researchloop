"""Slack integration -- Events API webhook handler and notifier."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

import httpx

from researchloop.comms.base import BaseNotifier

logger = logging.getLogger(__name__)

_SLACK_API = "https://slack.com/api"


class SlackNotifier(BaseNotifier):
    """Sends notifications to Slack channels/threads."""

    def __init__(
        self,
        bot_token: str,
        channel_id: str | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.channel_id = channel_id

    async def _post_message(
        self,
        text: str,
        channel: str | None = None,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        ch = channel or self.channel_id
        if not ch:
            logger.warning("No Slack channel configured")
            return {}
        payload: dict[str, Any] = {
            "channel": ch,
            "text": text,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_SLACK_API}/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {self.bot_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10.0,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.error("Slack API error: %s", data.get("error"))
            return data

    async def notify_sprint_started(
        self,
        sprint_id: str,
        study_name: str,
        idea: str,
    ) -> None:
        await self._post_message(
            f":rocket: *Sprint {sprint_id}* started\n"
            f"*Study:* {study_name}\n"
            f"*Idea:* {idea}"
        )

    async def notify_sprint_completed(
        self,
        sprint_id: str,
        study_name: str,
        summary: str,
    ) -> None:
        await self._post_message(
            f":white_check_mark: *Sprint {sprint_id}* completed\n"
            f"*Study:* {study_name}\n"
            f"*Summary:* {summary}"
        )

    async def notify_sprint_failed(
        self,
        sprint_id: str,
        study_name: str,
        error: str,
    ) -> None:
        await self._post_message(
            f":x: *Sprint {sprint_id}* failed\n*Study:* {study_name}\n*Error:* {error}"
        )


def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
) -> bool:
    """Verify a Slack request signature."""
    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    computed = (
        "v0="
        + hmac.new(
            signing_secret.encode(),
            basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(computed, signature)
