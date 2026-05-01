"""Auto-loop controller -- manages multi-sprint automated research loops."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from researchloop.core.config import Config
    from researchloop.db.database import Database
    from researchloop.sprints.manager import SprintManager

from researchloop.db import queries

logger = logging.getLogger(__name__)


def _generate_loop_id() -> str:
    """Generate a short hex loop ID like ``loop-b4e1c9``."""
    return f"loop-{secrets.token_hex(3)}"


class AutoLoopController:
    """Controls automated multi-sprint research loops.

    The cluster-side runner generates each next research idea via the
    embedded idea_generator.md.j2 prompt; this controller just chains
    sprints together as each one completes.
    """

    def __init__(
        self,
        db: Database,
        sprint_manager: SprintManager,
        config: Config,
    ) -> None:
        self.db = db
        self.sprint_manager = sprint_manager
        self.config = config

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    async def start(
        self,
        study_name: str,
        count: int,
        context: str = "",
        job_options: dict[str, str] | None = None,
    ) -> str:
        """Start a new auto-loop for *study_name* with *count* sprints.

        *context* is optional guidance for the idea generator
        (e.g. "Focus on improving F1 score").
        *job_options* are SLURM overrides applied to every sprint.

        Raises ``ValueError`` if the study has ``allow_loop = false``.
        """
        # Check if the study allows auto-loops.
        for s in self.config.studies:
            if s.name == study_name and not s.allow_loop:
                raise ValueError(f"Study {study_name!r} has allow_loop = false")

        loop_id = _generate_loop_id()

        await queries.create_auto_loop(
            self.db,
            id=loop_id,
            study_name=study_name,
            total_count=count,
        )

        # Store loop context and job_options in metadata.
        meta: dict[str, object] = {}
        if context:
            meta["context"] = context
        if job_options:
            meta["job_options"] = job_options
        if meta:
            await queries.update_auto_loop(
                self.db,
                loop_id,
                metadata_json=json.dumps(meta),
            )

        logger.info(
            "Auto-loop %s started for study %r with %d sprints",
            loop_id,
            study_name,
            count,
        )

        # First sprint — idea will be auto-generated on the cluster.
        # Set loop_id BEFORE submission so submit_sprint includes the
        # idea generator prompt in the job script.
        sprint = await self.sprint_manager.create_sprint(study_name, None)
        await queries.update_sprint(self.db, sprint.id, loop_id=loop_id)
        job_id = await self.sprint_manager.submit_sprint(
            sprint.id, extra_job_options=job_options
        )
        sprint.job_id = job_id

        await queries.update_auto_loop(
            self.db,
            loop_id,
            current_sprint_id=sprint.id,
            status="running",
        )

        logger.info("Auto-loop %s: first sprint %s submitted", loop_id, sprint.id)

        return loop_id

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------

    async def resume(self, loop_id: str) -> str:
        """Resume a stopped or failed auto-loop.

        Submits the next sprint and marks the loop as running again.
        Returns the new sprint ID.
        """
        loop = await queries.get_auto_loop(self.db, loop_id)
        if loop is None:
            raise ValueError(f"Auto-loop not found: {loop_id}")

        if loop["status"] not in ("stopped", "failed"):
            raise ValueError(f"Cannot resume loop in status {loop['status']!r}")

        if loop["completed_count"] >= loop["total_count"]:
            raise ValueError("Loop already completed all sprints")

        study_name: str = loop["study_name"]

        # Extract job_options from loop metadata.
        loop_job_options: dict[str, str] | None = None
        meta_str = loop.get("metadata_json")
        if meta_str:
            try:
                loop_job_options = json.loads(meta_str).get("job_options")
            except (json.JSONDecodeError, TypeError):
                pass

        sprint = await self.sprint_manager.create_sprint(study_name, None)
        await queries.update_sprint(self.db, sprint.id, loop_id=loop_id)
        job_id = await self.sprint_manager.submit_sprint(
            sprint.id, extra_job_options=loop_job_options
        )
        sprint.job_id = job_id

        await queries.update_auto_loop(
            self.db,
            loop_id,
            current_sprint_id=sprint.id,
            status="running",
            stopped_at=None,
        )

        logger.info(
            "Auto-loop %s resumed: sprint %s submitted (%d/%d)",
            loop_id,
            sprint.id,
            loop["completed_count"] + 1,
            loop["total_count"],
        )

        return sprint.id

    # ------------------------------------------------------------------
    # Sprint completion callback
    # ------------------------------------------------------------------

    async def on_sprint_complete(self, sprint_id: str) -> None:
        """Handle completion of a sprint that belongs to an auto-loop.

        1. Look up the auto-loop that owns this sprint.
        2. Increment ``completed_count``.
        3. If all sprints are done, mark the loop completed.
        4. Otherwise generate the next idea and start a new sprint.
        """
        # Find auto-loops where this sprint is the current one.
        all_loops = await queries.list_auto_loops(self.db)
        parent_loop = None
        for loop in all_loops:
            if loop.get("current_sprint_id") == sprint_id:
                parent_loop = loop
                break

        if parent_loop is None:
            logger.debug(
                "Sprint %s is not part of any auto-loop",
                sprint_id,
            )
            return

        loop_id = parent_loop["id"]
        completed = parent_loop.get("completed_count", 0) + 1
        total = parent_loop["total_count"]
        study_name: str = parent_loop["study_name"]

        # Check if the sprint failed — stop the loop instead of advancing.
        sprint = await queries.get_sprint(self.db, sprint_id)
        if sprint and sprint.get("status") == "failed":
            await queries.update_auto_loop(
                self.db,
                loop_id,
                status="failed",
                stopped_at=datetime.now(timezone.utc).isoformat(),
            )
            logger.warning(
                "Auto-loop %s stopped: sprint %s failed",
                loop_id,
                sprint_id,
            )
            return

        await queries.update_auto_loop(
            self.db,
            loop_id,
            completed_count=completed,
        )

        if completed >= total:
            await queries.update_auto_loop(
                self.db,
                loop_id,
                status="completed",
                stopped_at=(datetime.now(timezone.utc).isoformat()),
            )
            logger.info(
                "Auto-loop %s completed (%d/%d sprints done)",
                loop_id,
                completed,
                total,
            )
            return

        # Extract job_options from loop metadata.
        loop_job_options: dict[str, str] | None = None
        meta_str = parent_loop.get("metadata_json")
        if meta_str:
            try:
                loop_job_options = json.loads(meta_str).get("job_options")
            except (json.JSONDecodeError, TypeError):
                pass

        # Submit next sprint — idea will be auto-generated
        # on the cluster where Claude is authenticated.
        # Set loop_id BEFORE submission so submit_sprint includes the
        # idea generator prompt in the job script.
        sprint = await self.sprint_manager.create_sprint(study_name, None)
        await queries.update_sprint(self.db, sprint.id, loop_id=loop_id)
        job_id = await self.sprint_manager.submit_sprint(
            sprint.id, extra_job_options=loop_job_options
        )
        sprint.job_id = job_id

        await queries.update_auto_loop(
            self.db,
            loop_id,
            current_sprint_id=sprint.id,
        )

        logger.info(
            "Auto-loop %s: started sprint %d/%d (%s)",
            loop_id,
            completed + 1,
            total,
            sprint.id,
        )

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    async def stop(self, loop_id: str) -> None:
        """Stop a running auto-loop.

        Marks the loop as ``stopped`` and cancels the current sprint if
        one is in progress.
        """
        loop = await queries.get_auto_loop(self.db, loop_id)
        if loop is None:
            raise ValueError(f"Auto-loop not found: {loop_id}")

        if loop["status"] not in ("running", "pending"):
            logger.warning(
                "Auto-loop %s is already in status %r, not stopping",
                loop_id,
                loop["status"],
            )
            return

        # Cancel the current sprint if one exists.
        current_sprint_id = loop.get("current_sprint_id")
        if current_sprint_id:
            try:
                await self.sprint_manager.cancel_sprint(current_sprint_id)
                logger.info(
                    "Auto-loop %s: cancelled current sprint %s",
                    loop_id,
                    current_sprint_id,
                )
            except Exception:
                logger.exception(
                    "Auto-loop %s: failed to cancel sprint %s",
                    loop_id,
                    current_sprint_id,
                )

        await queries.update_auto_loop(
            self.db,
            loop_id,
            status="stopped",
            stopped_at=datetime.now(timezone.utc).isoformat(),
        )

        logger.info("Auto-loop %s stopped", loop_id)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def status(self, loop_id: str) -> dict:
        """Return the current status of an auto-loop.

        Raises :class:`ValueError` if the loop is not found.
        """
        loop = await queries.get_auto_loop(self.db, loop_id)
        if loop is None:
            raise ValueError(f"Auto-loop not found: {loop_id}")
        return loop
