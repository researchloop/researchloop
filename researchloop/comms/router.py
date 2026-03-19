"""Notification router -- fans out notifications to all configured backends."""

from __future__ import annotations

import logging

from researchloop.comms.base import BaseNotifier

logger = logging.getLogger(__name__)


class NotificationRouter:
    """Routes notifications to every registered :class:`BaseNotifier`.

    Errors from individual notifiers are caught and logged so that a
    single broken backend does not prevent the others from firing.
    """

    def __init__(self) -> None:
        self._notifiers: list[BaseNotifier] = []

    def add_notifier(self, notifier: BaseNotifier) -> None:
        """Register a notification backend."""
        self._notifiers.append(notifier)
        logger.info("Registered notifier: %s", type(notifier).__name__)

    # ------------------------------------------------------------------
    # Fan-out methods
    # ------------------------------------------------------------------

    async def notify_sprint_started(
        self, sprint_id: str, study_name: str, idea: str
    ) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.notify_sprint_started(sprint_id, study_name, idea)
            except Exception:
                logger.exception(
                    "Error in %s.notify_sprint_started",
                    type(notifier).__name__,
                )

    async def notify_sprint_completed(
        self,
        sprint_id: str,
        study_name: str,
        summary: str,
        pdf_path: str | None = None,
    ) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.notify_sprint_completed(
                    sprint_id, study_name, summary, pdf_path=pdf_path
                )
            except Exception:
                logger.exception(
                    "Error in %s.notify_sprint_completed",
                    type(notifier).__name__,
                )

    async def notify_sprint_failed(
        self, sprint_id: str, study_name: str, error: str
    ) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.notify_sprint_failed(sprint_id, study_name, error)
            except Exception:
                logger.exception(
                    "Error in %s.notify_sprint_failed",
                    type(notifier).__name__,
                )
