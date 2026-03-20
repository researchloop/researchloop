"""Slack integration -- Events API webhook handler and notifier."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any

import httpx

from researchloop.comms.base import BaseNotifier

logger = logging.getLogger(__name__)

_SLACK_API = os.environ.get("RESEARCHLOOP_SLACK_API_URL", "https://slack.com/api")


class SlackNotifier(BaseNotifier):
    """Sends notifications to Slack channels/threads."""

    def __init__(
        self,
        bot_token: str,
        channel_id: str | None = None,
        dashboard_url: str | None = None,
        conversation_manager: Any = None,
    ) -> None:
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.dashboard_url = dashboard_url
        self._cm = conversation_manager

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

    async def _upload_file(
        self,
        filepath: str,
        filename: str,
        channel: str | None = None,
        initial_comment: str = "",
    ) -> dict[str, Any]:
        """Upload a file to a Slack channel."""
        ch = channel or self.channel_id
        if not ch:
            return {}
        try:
            with open(filepath, "rb") as f:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"{_SLACK_API}/files.upload",
                        headers={
                            "Authorization": (f"Bearer {self.bot_token}"),
                        },
                        data={
                            "channels": ch,
                            "filename": filename,
                            "initial_comment": initial_comment,
                        },
                        files={"file": (filename, f)},
                        timeout=30.0,
                    )
                    data = resp.json()
                    if not data.get("ok"):
                        logger.error(
                            "Slack file upload error: %s",
                            data.get("error"),
                        )
                    return data
        except Exception:
            logger.exception("Failed to upload file to Slack")
            return {}

    def _link(self, sprint_id: str) -> str:
        if self.dashboard_url:
            url = self.dashboard_url.rstrip("/")
            return f"<{url}/dashboard/sprints/{sprint_id}|{sprint_id}>"
        return sprint_id

    async def notify_sprint_started(
        self,
        sprint_id: str,
        study_name: str,
        idea: str,
    ) -> None:
        link = self._link(sprint_id)
        idea_trunc = idea[:300] + "…" if len(idea) > 300 else idea
        msg = (
            f":rocket: Sprint *{link}* started\n"
            f"*Study:* {study_name}\n"
            f"*Idea:* {idea_trunc}"
        )
        resp = await self._post_message(msg)
        ts = resp.get("ts", "")
        if ts and self._cm:
            await self._cm.store_bot_message(ts, msg)

    async def notify_sprint_completed(
        self,
        sprint_id: str,
        study_name: str,
        summary: str,
        pdf_path: str | None = None,
    ) -> None:
        link = self._link(sprint_id)
        summary_trunc = summary[:500] + "…" if len(summary) > 500 else summary
        msg = (
            f":white_check_mark: Sprint *{link}* completed\n"
            f"*Study:* {study_name}\n"
            f"*Summary:* {summary_trunc}"
        )
        resp = await self._post_message(msg)
        # Store the notification for thread context.
        ts = resp.get("ts", "")
        if ts and self._cm:
            await self._cm.store_bot_message(ts, msg)
        if pdf_path:
            await self._upload_file(
                pdf_path,
                f"{sprint_id}-report.pdf",
                initial_comment=f"Report for sprint {sprint_id}",
            )

    async def notify_sprint_failed(
        self,
        sprint_id: str,
        study_name: str,
        error: str,
    ) -> None:
        link = self._link(sprint_id)
        msg = (
            f":x: Sprint *{link}* failed\n*Study:* {study_name}\n*Error:* {error[:500]}"
        )
        resp = await self._post_message(msg)
        ts = resp.get("ts", "")
        if ts and self._cm:
            await self._cm.store_bot_message(ts, msg)


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
