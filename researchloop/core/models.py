"""Core domain models for researchloop."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class SprintStatus(str, Enum):
    """Lifecycle states for a research sprint."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    RUNNING = "running"
    RESEARCH = "research"
    RED_TEAM = "red_team"
    FIXING = "fixing"
    VALIDATING = "validating"
    REPORTING = "reporting"
    SUMMARIZING = "summarizing"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def generate_sprint_id() -> str:
    """Generate a short hex sprint ID like ``sp-a3f7b2``."""
    return f"sp-{secrets.token_hex(3)}"


def format_sprint_dirname(sprint_id: str, idea: str | None) -> str:
    """Create a directory name for a sprint.

    Format: ``2026-03-15--19-50--sp-a3f7b2--feature-absorption``

    Date comes first so ``ls`` shows sprints in chronological order.
    """
    now = datetime.now(timezone.utc)
    date_part = now.strftime("%Y-%m-%d")
    time_part = now.strftime("%H-%M")
    # Slugify the idea: lowercase, replace non-alnum with hyphens, collapse
    slug = re.sub(r"[^a-z0-9]+", "-", (idea or "auto").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    # Truncate slug to keep directory names reasonable
    slug = slug[:60]
    return f"{date_part}--{time_part}--{sprint_id}--{slug}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Sprint:
    """A single research sprint."""

    id: str
    study_name: str
    idea: str | None
    status: SprintStatus = SprintStatus.PENDING
    job_id: str | None = None
    directory: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    summary: str | None = None
    artifacts: list[str] = field(default_factory=list)


@dataclass
class Study:
    """A research study (read-only view)."""

    name: str
    cluster: str
    description: str = ""
    sprint_count: int = 0
    active_sprints: int = 0


@dataclass
class AutoLoop:
    """An automated multi-sprint loop."""

    id: str
    study_name: str
    total_count: int
    completed_count: int = 0
    current_sprint_id: str | None = None
    status: SprintStatus = SprintStatus.PENDING
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class Artifact:
    """A file artifact produced by a sprint."""

    id: str
    sprint_id: str
    filename: str
    path: str
    size: int = 0
    uploaded_at: datetime = field(default_factory=_utcnow)


@dataclass
class Event:
    """An event recorded during sprint execution."""

    id: str
    sprint_id: str
    event_type: str
    data: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)
