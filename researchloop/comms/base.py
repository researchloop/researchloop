"""Abstract notification interface for researchloop."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseNotifier(ABC):
    """Every notification backend must implement this interface."""

    @abstractmethod
    async def notify_sprint_started(
        self, sprint_id: str, study_name: str, idea: str
    ) -> None:
        """Called when a sprint has been submitted to a cluster."""
        ...

    @abstractmethod
    async def notify_sprint_completed(
        self,
        sprint_id: str,
        study_name: str,
        summary: str,
        pdf_path: str | None = None,
    ) -> None:
        """Called when a sprint finishes successfully."""
        ...

    @abstractmethod
    async def notify_sprint_failed(
        self, sprint_id: str, study_name: str, error: str
    ) -> None:
        """Called when a sprint fails."""
        ...
