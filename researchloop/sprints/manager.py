"""Sprint lifecycle management -- create, submit, cancel, and complete sprints."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import jinja2

if TYPE_CHECKING:
    from researchloop.clusters.ssh import SSHManager
    from researchloop.comms.router import NotificationRouter
    from researchloop.core.config import Config
    from researchloop.db.database import Database
    from researchloop.schedulers.base import BaseScheduler

from researchloop.core.models import (
    Sprint,
    SprintStatus,
    format_sprint_dirname,
    generate_sprint_id,
)
from researchloop.db import queries
from researchloop.studies.manager import StudyManager

logger = logging.getLogger(__name__)

# Jinja2 environment pointing at the runner/job_templates directory.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "runner" / "job_templates"
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined,
)


class SprintManager:
    """Manages the full lifecycle of research sprints.

    Coordinates between the database, SSH connections, job schedulers,
    and the notification router.
    """

    def __init__(
        self,
        db: Database,
        config: Config,
        ssh_manager: SSHManager,
        schedulers: dict[str, BaseScheduler],
        study_manager: StudyManager | None = None,
        notification_router: NotificationRouter | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.ssh_manager = ssh_manager
        self.schedulers = schedulers
        self.study_manager = study_manager
        self.notification_router = notification_router

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create_sprint(self, study_name: str, idea: str) -> Sprint:
        """Create a new sprint record in the database.

        The sprint is created with status ``PENDING`` -- it has not yet
        been submitted to a cluster scheduler.
        """
        sprint_id = generate_sprint_id()
        directory = format_sprint_dirname(sprint_id, idea)

        await queries.create_sprint(
            self.db,
            id=sprint_id,
            study_name=study_name,
            idea=idea,
            directory=directory,
        )

        sprint = Sprint(
            id=sprint_id,
            study_name=study_name,
            idea=idea,
            status=SprintStatus.PENDING,
            directory=directory,
        )

        logger.info(
            "Created sprint %s for study %r: %s",
            sprint_id,
            study_name,
            idea,
        )
        return sprint

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    async def submit_sprint(self, sprint_id: str) -> str:
        """Submit a pending sprint to its cluster scheduler.

        Returns the scheduler-assigned job ID.
        """
        sprint = await queries.get_sprint(self.db, sprint_id)
        if sprint is None:
            raise ValueError(f"Sprint not found: {sprint_id}")

        study_name: str = sprint["study_name"]

        # Resolve cluster config through study manager or config lookup.
        if self.study_manager is not None:
            cluster_cfg = await self.study_manager.get_cluster_config(study_name)
        else:
            # Fallback: look up directly from config.
            study_row = await queries.get_study(self.db, study_name)
            if study_row is None:
                raise ValueError(f"Study not found: {study_name}")
            cluster_name = study_row["cluster"]
            cluster_cfg = None
            for c in self.config.clusters:
                if c.name == cluster_name:
                    cluster_cfg = c
                    break
            if cluster_cfg is None:
                raise ValueError(f"Cluster not found: {cluster_name}")

        # Look up the study config for template variables.
        study_cfg = None
        for s in self.config.studies:
            if s.name == study_name:
                study_cfg = s
                break

        # Resolve scheduler.
        scheduler = self.schedulers.get(cluster_cfg.name)
        if scheduler is None:
            scheduler = self.schedulers.get(cluster_cfg.scheduler_type)
        if scheduler is None:
            raise ValueError(
                f"No scheduler registered for cluster {cluster_cfg.name!r} "
                f"or type {cluster_cfg.scheduler_type!r}"
            )

        sprint_dirname = sprint.get("directory", sprint_id)

        # Collect context: inline strings + file paths, cluster then study.
        context_parts: list[str] = []

        # 1. Cluster inline context.
        if cluster_cfg.context:
            context_parts.append(cluster_cfg.context)

        # 2. Cluster context files.
        for ctx_path in cluster_cfg.context_paths:
            p = Path(ctx_path)
            if p.exists():
                context_parts.append(p.read_text(encoding="utf-8"))
                logger.info("Loaded cluster context file: %s", p)

        # 3. Study inline context.
        if study_cfg and study_cfg.context:
            context_parts.append(study_cfg.context)

        # 4. Study context file.
        if study_cfg and study_cfg.claude_md_path:
            p = Path(study_cfg.claude_md_path)
            if p.exists():
                context_parts.append(p.read_text(encoding="utf-8"))
                logger.info("Loaded study context file: %s", p)

        has_context = bool(context_parts)

        # Render the job script for the appropriate scheduler.
        template_name = f"{cluster_cfg.scheduler_type}.sh.j2"
        template = _jinja_env.get_template(template_name)
        job_script = template.render(
            sprint_id=sprint_id,
            study_name=study_name,
            idea=sprint["idea"],
            sprint_dirname=sprint_dirname,
            job_name=f"rl-{sprint_id}",
            working_dir=cluster_cfg.working_dir,
            time_limit=f"{study_cfg.max_sprint_duration_hours}:00:00"
            if study_cfg
            else "8:00:00",
            environment=cluster_cfg.environment,
            orchestrator_url=self.config.orchestrator_url or "",
            shared_secret=self.config.shared_secret or "",
            claude_md_path=f"{cluster_cfg.working_dir}/{sprint_dirname}/CLAUDE.md"
            if has_context
            else "",
            red_team_max_rounds=study_cfg.red_team_max_rounds if study_cfg else 3,
        )

        # SSH to cluster: create sprint directory and write job script.
        cluster_dict = {
            "host": cluster_cfg.host,
            "port": cluster_cfg.port,
            "user": cluster_cfg.user,
            "key_path": cluster_cfg.key_path,
        }
        ssh = await self.ssh_manager.get_connection(cluster_dict)

        sprint_remote_dir = f"{cluster_cfg.working_dir}/{sprint_dirname}"
        await ssh.run(f"mkdir -p {sprint_remote_dir}")

        # Upload merged CLAUDE.md to the sprint directory.
        if has_context:
            merged = "\n\n".join(context_parts)
            remote_claude_md = f"{sprint_remote_dir}/CLAUDE.md"
            await ssh.run(
                f"cat > {remote_claude_md} "
                f"<< 'RESEARCHLOOP_EOF'\n{merged}\n"
                f"RESEARCHLOOP_EOF"
            )
            logger.info(
                "Uploaded merged CLAUDE.md (%d parts) to %s",
                len(context_parts),
                remote_claude_md,
            )

        # Write the job script to a temporary approach via stdin.
        script_path = f"{sprint_remote_dir}/run_sprint.sh"
        await ssh.run(
            f"cat > {script_path} << 'RESEARCHLOOP_EOF'\n{job_script}\nRESEARCHLOOP_EOF"
        )
        await ssh.run(f"chmod +x {script_path}")

        # Submit via the scheduler.
        job_id = await scheduler.submit(
            ssh=ssh,
            script=script_path,
            job_name=f"rl-{sprint_id}",
            working_dir=sprint_remote_dir,
            env=cluster_cfg.environment or None,
        )

        # Update the sprint record.
        now = datetime.now(timezone.utc).isoformat()
        await queries.update_sprint(
            self.db,
            sprint_id,
            job_id=job_id,
            status=SprintStatus.SUBMITTED.value,
            started_at=now,
        )

        logger.info(
            "Sprint %s submitted as job %s on cluster %s",
            sprint_id,
            job_id,
            cluster_cfg.name,
        )

        # Notify.
        if self.notification_router is not None:
            await self.notification_router.notify_sprint_started(
                sprint_id=sprint_id,
                study_name=study_name,
                idea=sprint["idea"],
            )

        return job_id

    # ------------------------------------------------------------------
    # Combined create + submit
    # ------------------------------------------------------------------

    async def run_sprint(self, study_name: str, idea: str) -> Sprint:
        """Create a sprint and immediately submit it.

        Returns the :class:`Sprint` with updated status and job ID.
        """
        sprint = await self.create_sprint(study_name, idea)
        job_id = await self.submit_sprint(sprint.id)
        sprint.status = SprintStatus.SUBMITTED
        sprint.job_id = job_id
        return sprint

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    async def cancel_sprint(self, sprint_id: str) -> bool:
        """Cancel a running or submitted sprint.

        Returns ``True`` if the cancellation succeeded.
        """
        sprint = await queries.get_sprint(self.db, sprint_id)
        if sprint is None:
            raise ValueError(f"Sprint not found: {sprint_id}")

        study_name: str = sprint["study_name"]

        # Resolve cluster.
        if self.study_manager is not None:
            cluster_cfg = await self.study_manager.get_cluster_config(study_name)
        else:
            study_row = await queries.get_study(self.db, study_name)
            if study_row is None:
                raise ValueError(f"Study not found: {study_name}")
            cluster_name = study_row["cluster"]
            cluster_cfg = None
            for c in self.config.clusters:
                if c.name == cluster_name:
                    cluster_cfg = c
                    break
            if cluster_cfg is None:
                raise ValueError(f"Cluster not found: {cluster_name}")

        scheduler = self.schedulers.get(cluster_cfg.name)
        if scheduler is None:
            scheduler = self.schedulers.get(cluster_cfg.scheduler_type)
        if scheduler is None:
            raise ValueError(f"No scheduler for cluster {cluster_cfg.name!r}")

        job_id = sprint.get("job_id")
        if not job_id:
            logger.warning("Sprint %s has no job_id, marking as cancelled", sprint_id)
            await queries.update_sprint(
                self.db,
                sprint_id,
                status=SprintStatus.CANCELLED.value,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return True

        cluster_dict = {
            "host": cluster_cfg.host,
            "port": cluster_cfg.port,
            "user": cluster_cfg.user,
            "key_path": cluster_cfg.key_path,
        }
        ssh = await self.ssh_manager.get_connection(cluster_dict)
        success = await scheduler.cancel(ssh, job_id)

        await queries.update_sprint(
            self.db,
            sprint_id,
            status=SprintStatus.CANCELLED.value,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

        logger.info(
            "Sprint %s (job %s) cancelled: %s",
            sprint_id,
            job_id,
            "success" if success else "scheduler reported failure",
        )
        return success

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    async def get_sprint(self, sprint_id: str) -> dict | None:
        """Return a single sprint by ID, or ``None``."""
        return await queries.get_sprint(self.db, sprint_id)

    async def list_sprints(
        self, study_name: str | None = None, limit: int = 50
    ) -> list[dict]:
        """Return sprints, optionally filtered by study name."""
        return await queries.list_sprints(self.db, study_name=study_name, limit=limit)

    # ------------------------------------------------------------------
    # Completion handling
    # ------------------------------------------------------------------

    async def handle_completion(
        self,
        sprint_id: str,
        status: str,
        summary: str | None = None,
        error: str | None = None,
    ) -> None:
        """Handle a sprint completion event.

        Updates the database, sends notifications, and creates an event
        record.
        """
        now = datetime.now(timezone.utc).isoformat()

        await queries.update_sprint(
            self.db,
            sprint_id,
            status=status,
            completed_at=now,
            summary=summary,
            error=error,
        )

        sprint = await queries.get_sprint(self.db, sprint_id)
        study_name = sprint["study_name"] if sprint else "unknown"

        # Create an event record.
        event_data = json.dumps(
            {
                "status": status,
                "summary": summary,
                "error": error,
            }
        )
        await queries.create_event(
            self.db,
            sprint_id=sprint_id,
            event_type="sprint_completed",
            data_json=event_data,
        )

        # Notify via configured channels.
        if self.notification_router is not None:
            if status == SprintStatus.COMPLETED.value:
                await self.notification_router.notify_sprint_completed(
                    sprint_id=sprint_id,
                    study_name=study_name,
                    summary=summary or "No summary provided",
                )
            elif status == SprintStatus.FAILED.value:
                await self.notification_router.notify_sprint_failed(
                    sprint_id=sprint_id,
                    study_name=study_name,
                    error=error or "Unknown error",
                )

        logger.info("Sprint %s completion handled: status=%s", sprint_id, status)
