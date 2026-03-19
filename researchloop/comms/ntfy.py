"""ntfy.sh notification backend for researchloop."""

from __future__ import annotations

import logging

import httpx

from researchloop.comms.base import BaseNotifier

logger = logging.getLogger(__name__)


class NtfyNotifier(BaseNotifier):
    """Sends push notifications via `ntfy.sh <https://ntfy.sh>`_.

    Each notification is a simple HTTP POST with headers controlling
    priority, title, and tags.
    """

    def __init__(self, url: str = "https://ntfy.sh", topic: str = "") -> None:
        self.url = url.rstrip("/")
        self.topic = topic

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    async def _send(
        self,
        message: str,
        title: str,
        priority: int = 3,
        tags: str = "",
    ) -> None:
        """POST a notification to ntfy."""
        endpoint = f"{self.url}/{self.topic}"
        headers: dict[str, str] = {
            "Title": title,
            "Priority": str(priority),
        }
        if tags:
            headers["Tags"] = tags

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    endpoint,
                    content=message,
                    headers=headers,
                    timeout=10.0,
                )
                response.raise_for_status()
            logger.debug("ntfy notification sent: %s", title)
        except httpx.HTTPError:
            logger.exception("Failed to send ntfy notification: %s", title)
            raise

    # ------------------------------------------------------------------
    # BaseNotifier implementation
    # ------------------------------------------------------------------

    async def notify_sprint_started(
        self, sprint_id: str, study_name: str, idea: str
    ) -> None:
        await self._send(
            message=f"Sprint {sprint_id} started\nIdea: {idea}",
            title=f"ResearchLoop: {study_name}",
            priority=3,
            tags="rocket",
        )

    async def notify_sprint_completed(
        self,
        sprint_id: str,
        study_name: str,
        summary: str,
        pdf_path: str | None = None,
    ) -> None:
        await self._send(
            message=f"Sprint {sprint_id} completed\nSummary: {summary}",
            title=f"ResearchLoop: {study_name}",
            priority=3,
            tags="white_check_mark",
        )

    async def notify_sprint_failed(
        self, sprint_id: str, study_name: str, error: str
    ) -> None:
        await self._send(
            message=f"Sprint {sprint_id} failed\nError: {error}",
            title=f"ResearchLoop: {study_name}",
            priority=4,
            tags="x",
        )
