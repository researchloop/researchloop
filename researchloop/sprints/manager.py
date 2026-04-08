"""Sprint lifecycle management -- create, submit, cancel, and complete sprints."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import jinja2

if TYPE_CHECKING:
    from researchloop.clusters.ssh import SSHConnection, SSHManager
    from researchloop.comms.router import NotificationRouter
    from researchloop.core.config import Config
    from researchloop.db.database import Database
    from researchloop.schedulers.base import BaseScheduler

from researchloop.core.models import (
    Sprint,
    SprintStatus,
    format_sprint_dirname,
    generate_sprint_id,
    generate_tweak_id,
)
from researchloop.db import queries
from researchloop.studies.manager import StudyManager

logger = logging.getLogger(__name__)


def _b64encode(text: str) -> str:
    """Base64-encode a string for safe SSH transfer."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# Jinja2 environment pointing at the runner/job_templates directory.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "runner" / "job_templates"
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined,
)

# Prompt templates for the research pipeline steps.
_PROMPT_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "runner" / "templates"
_prompt_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_PROMPT_TEMPLATES_DIR)),
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

    async def create_sprint(self, study_name: str, idea: str | None = None) -> Sprint:
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

    async def submit_sprint(
        self,
        sprint_id: str,
        extra_job_options: dict[str, str] | None = None,
    ) -> str:
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

        # Collect context: global → cluster → study.
        # Each level supports inline text + file paths.
        context_parts: list[str] = []

        # 1. Global inline context.
        if self.config.context:
            context_parts.append(self.config.context)

        # 2. Global context files.
        for ctx_path in self.config.context_paths:
            p = Path(ctx_path)
            if p.exists():
                context_parts.append(p.read_text(encoding="utf-8"))
                logger.info("Loaded global context file: %s", p)

        # 3. Cluster inline context.
        if cluster_cfg.context:
            context_parts.append(cluster_cfg.context)

        # 4. Cluster context files.
        for ctx_path in cluster_cfg.context_paths:
            p = Path(ctx_path)
            if p.exists():
                context_parts.append(p.read_text(encoding="utf-8"))
                logger.info("Loaded cluster context file: %s", p)

        # 5. Study inline context.
        if study_cfg and study_cfg.context:
            context_parts.append(study_cfg.context)

        # 6. Study context file.
        if study_cfg and study_cfg.claude_md_path:
            p = Path(study_cfg.claude_md_path)
            if p.exists():
                context_parts.append(p.read_text(encoding="utf-8"))
                logger.info("Loaded study context file: %s", p)

        has_context = bool(context_parts)
        study_context = "\n\n".join(context_parts) if has_context else ""
        idea = sprint["idea"]
        red_team_rounds = study_cfg.red_team_max_rounds if study_cfg else 3

        # Resolve the base directory for sprints.
        # Priority: study.sprints_dir > working_dir/<study_name>
        if study_cfg and study_cfg.sprints_dir:
            sprints_base = study_cfg.sprints_dir
        else:
            sprints_base = f"{cluster_cfg.working_dir}/{study_name}"
        sprint_remote_dir = f"{sprints_base}/{sprint_dirname}"

        # Pre-render all pipeline prompt templates.
        def _render_prompt(name: str, **kw: object) -> str:
            return _prompt_env.get_template(name).render(**kw)

        prompts: list[dict[str, str]] = []

        # For loop sprints, idea is None — the job script
        # will generate it and overwrite the prompt.
        idea_text = idea or "(will be auto-generated)"

        # Research prompt
        prompts.append(
            {
                "filename": "prompt_research.md",
                "content_b64": _b64encode(
                    _render_prompt(
                        "research_sprint.md.j2",
                        study_context=study_context,
                        idea=idea_text,
                        sprint_dir=sprint_remote_dir,
                    )
                ),
            }
        )

        # Red-team + fix prompts (one pair per round)
        for r in range(1, red_team_rounds + 1):
            prompts.append(
                {
                    "filename": f"prompt_red_team_{r}.md",
                    "content_b64": _b64encode(
                        _render_prompt(
                            "red_team.md.j2",
                            idea=idea_text,
                            round_number=r,
                            max_rounds=red_team_rounds,
                        )
                    ),
                }
            )
            prompts.append(
                {
                    "filename": f"prompt_fix_{r}.md",
                    "content_b64": _b64encode(
                        _render_prompt(
                            "fix_issues.md.j2",
                            round_number=r,
                        )
                    ),
                }
            )

        # Report + summarize prompts
        prompts.append(
            {
                "filename": "prompt_report.md",
                "content_b64": _b64encode(
                    _render_prompt("report.md.j2", idea=idea_text)
                ),
            }
        )
        prompts.append(
            {
                "filename": "prompt_summarize.md",
                "content_b64": _b64encode(_render_prompt("summarizer.md.j2")),
            }
        )

        # If this sprint belongs to an auto-loop, add the idea
        # generator prompt so the job generates its own idea.
        is_loop_sprint = bool(sprint.get("loop_id"))
        if is_loop_sprint:
            # Find the loop for extra context.
            loop_context = ""
            all_loops = await queries.list_auto_loops(self.db)
            for lp in all_loops:
                if lp.get("current_sprint_id") == sprint_id:
                    meta = lp.get("metadata_json")
                    if meta:
                        try:
                            import json as _json

                            loop_context = _json.loads(meta).get("context", "")
                        except Exception:
                            pass
                    break

            # Collect previous summaries.
            prev_sprints = await queries.list_sprints(
                self.db, study_name=study_name, limit=50
            )
            prev_summaries = [
                {
                    "id": s["id"],
                    "summary": s.get("summary", ""),
                }
                for s in prev_sprints
                if s.get("summary")
            ]

            # Build the idea generator prompt with loop context.
            idea_prompt = _render_prompt(
                "idea_generator.md.j2",
                study_context=study_context,
                previous_sprints=prev_summaries,
            )
            if loop_context:
                idea_prompt += f"\n\n## Additional Guidance\n{loop_context}\n"

            prompts.append(
                {
                    "filename": "prompt_generate_idea.md",
                    "content_b64": _b64encode(idea_prompt),
                }
            )

        # Render the job script.
        template_name = f"{cluster_cfg.scheduler_type}.sh.j2"
        template = _jinja_env.get_template(template_name)
        job_script = template.render(
            sprint_id=sprint_id,
            study_name=study_name,
            idea=idea_text,
            sprint_dirname=sprint_dirname,
            job_name=f"rl-{sprint_id}",
            working_dir=sprints_base,
            time_limit=(
                f"{study_cfg.max_sprint_duration_hours}:00:00"
                if study_cfg
                else "8:00:00"
            ),
            environment=cluster_cfg.environment,
            job_options={
                **cluster_cfg.job_options,
                **(study_cfg.job_options if study_cfg else {}),
                **(extra_job_options or {}),
            },
            claude_command=(
                (study_cfg.claude_command if study_cfg else "")
                or cluster_cfg.claude_command
                or self.config.claude_command
                or "claude --dangerously-skip-permissions"
            ),
            orchestrator_url=self.config.orchestrator_url or "",
            webhook_token=sprint.get("webhook_token", ""),
            red_team_max_rounds=red_team_rounds,
            prompts=prompts,
        )

        # SSH to cluster: create sprint directory and write job script.
        cluster_dict = {
            "host": cluster_cfg.host,
            "port": cluster_cfg.port,
            "user": cluster_cfg.user,
            "key_path": cluster_cfg.key_path,
        }
        ssh = await self.ssh_manager.get_connection(cluster_dict)

        sprint_remote_dir = f"{sprints_base}/{sprint_dirname}"
        await ssh.run(
            f"mkdir -p {sprint_remote_dir}/.researchloop {sprint_remote_dir}/results"
        )

        # Upload CLAUDE.md so Claude CLI picks it up automatically.
        if has_context:
            encoded_ctx = _b64encode(study_context)
            await ssh.run(
                f"echo '{encoded_ctx}' | base64 -d > {sprint_remote_dir}/CLAUDE.md"
            )
            logger.info(
                "Uploaded CLAUDE.md (%d parts) to %s",
                len(context_parts),
                sprint_remote_dir,
            )

        # Write idea.txt so it's always available on cluster.
        if idea:
            encoded_idea = _b64encode(idea)
            await ssh.run(
                f"echo '{encoded_idea}' | base64 -d > {sprint_remote_dir}/idea.txt"
            )

        # Write the job script via base64.
        # Prompts are embedded in the script as base64.
        script_path = f"{sprint_remote_dir}/run_sprint.sh"
        encoded_script = _b64encode(job_script)
        await ssh.run(f"echo '{encoded_script}' | base64 -d > {script_path}")
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

    async def run_sprint(
        self,
        study_name: str,
        idea: str | None = None,
        job_options: dict[str, str] | None = None,
    ) -> Sprint:
        """Create a sprint and immediately submit it.

        Returns the :class:`Sprint` with updated status and job ID.
        """
        sprint = await self.create_sprint(study_name, idea)
        job_id = await self.submit_sprint(sprint.id, extra_job_options=job_options)
        sprint.status = SprintStatus.SUBMITTED
        sprint.job_id = job_id
        return sprint

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    async def cancel_sprint(self, sprint_id: str) -> bool:
        """Cancel a running or submitted sprint.

        Returns ``True`` if the cancellation succeeded.
        If the sprint belongs to an auto-loop, the loop is also stopped.
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
        else:
            cluster_dict = {
                "host": cluster_cfg.host,
                "port": cluster_cfg.port,
                "user": cluster_cfg.user,
                "key_path": cluster_cfg.key_path,
            }
            ssh = await self.ssh_manager.get_connection(cluster_dict)
            await scheduler.cancel(ssh, job_id)

            await queries.update_sprint(
                self.db,
                sprint_id,
                status=SprintStatus.CANCELLED.value,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

        logger.info("Sprint %s cancelled", sprint_id)

        # Stop the parent auto-loop if this sprint belongs to one.
        loop_id = sprint.get("loop_id")
        if loop_id:
            try:
                loop = await queries.get_auto_loop(self.db, loop_id)
                if loop and loop["status"] == "running":
                    await queries.update_auto_loop(
                        self.db,
                        loop_id,
                        status="stopped",
                        stopped_at=datetime.now(timezone.utc).isoformat(),
                    )
                    logger.info(
                        "Auto-loop %s stopped (sprint %s cancelled)",
                        loop_id,
                        sprint_id,
                    )
            except Exception:
                logger.warning(
                    "Failed to stop loop %s after cancelling sprint %s",
                    loop_id,
                    sprint_id,
                    exc_info=True,
                )

        # Notify about cancellation.
        if self.notification_router is not None:
            await self.notification_router.notify_sprint_failed(
                sprint_id=sprint_id,
                study_name=study_name,
                error="Sprint cancelled",
            )

        return True

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
        idea: str | None = None,
    ) -> None:
        """Handle a sprint completion event.

        Updates the database, sends notifications, and creates an event
        record.
        """
        now = datetime.now(timezone.utc).isoformat()

        update_kw: dict[str, str | None] = {
            "status": status,
            "completed_at": now,
            "summary": summary,
            "error": error,
        }

        # Update the idea if it was auto-generated (sprint had idea=None).
        sprint_before = await queries.get_sprint(self.db, sprint_id)
        if sprint_before and not sprint_before.get("idea"):
            if idea:
                update_kw["idea"] = idea[:500]
            else:
                # Fallback: try to read idea.txt from the cluster.
                fetched = await self._fetch_idea(sprint_before)
                if fetched:
                    update_kw["idea"] = fetched[:500]

        await queries.update_sprint(self.db, sprint_id, **update_kw)

        sprint = await queries.get_sprint(self.db, sprint_id)
        study_name = sprint["study_name"] if sprint else "unknown"

        # Fetch result files from the cluster into metadata_json.
        if sprint:
            await self._fetch_results(sprint)

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

        # Try to fetch the PDF for the notification.
        pdf_local: str | None = None
        if status == SprintStatus.COMPLETED.value and sprint:
            pdf_local = await self._fetch_pdf(sprint)

        # Notify via configured channels.
        if self.notification_router is not None:
            if status == SprintStatus.COMPLETED.value:
                await self.notification_router.notify_sprint_completed(
                    sprint_id=sprint_id,
                    study_name=study_name,
                    summary=summary or "No summary provided",
                    pdf_path=pdf_local,
                )
            elif status == SprintStatus.FAILED.value:
                await self.notification_router.notify_sprint_failed(
                    sprint_id=sprint_id,
                    study_name=study_name,
                    error=error or "Unknown error",
                )

        logger.info(
            "Sprint %s completion handled: status=%s",
            sprint_id,
            status,
        )

    async def _fetch_idea(self, sprint: dict) -> str | None:
        """Try to read idea.txt from the cluster for auto-loop sprints."""
        try:
            resolved = await self._resolve_sprint_remote(sprint)
            if resolved is None:
                return None
            ssh, sprint_path = resolved
            stdout, _, rc = await ssh.run(
                f"cat {sprint_path}/idea.txt 2>/dev/null"
            )
            if rc == 0 and stdout.strip():
                return stdout.strip()
            return None
        except Exception:
            logger.debug("Idea fetch failed for %s", sprint.get("id"), exc_info=True)
            return None

    async def _resolve_sprint_remote(
        self, sprint: dict
    ) -> tuple[SSHConnection, str] | None:
        """Resolve SSH connection and remote sprint path.

        Returns ``(ssh, sprint_path)`` or ``None`` if resolution fails.
        """
        try:
            study_name = sprint["study_name"]
            if self.study_manager is None:
                return None
            cluster_cfg = await self.study_manager.get_cluster_config(study_name)
            study_cfg = None
            for s in self.config.studies:
                if s.name == study_name:
                    study_cfg = s
                    break
            if study_cfg and study_cfg.sprints_dir:
                sbase = study_cfg.sprints_dir
            else:
                sbase = f"{cluster_cfg.working_dir}/{study_name}"
            sp_dir = sprint.get("directory", "")
            sprint_path = f"{sbase}/{sp_dir}"

            conn = {
                "host": cluster_cfg.host,
                "port": cluster_cfg.port,
                "user": cluster_cfg.user,
                "key_path": cluster_cfg.key_path,
            }
            ssh = await self.ssh_manager.get_connection(conn)
            return ssh, sprint_path
        except Exception:
            logger.debug(
                "Failed to resolve sprint remote for %s",
                sprint.get("id"),
                exc_info=True,
            )
            return None

    async def _fetch_pdf(self, sprint: dict) -> str | None:
        """Try to download report.pdf from the cluster."""
        try:
            resolved = await self._resolve_sprint_remote(sprint)
            if resolved is None:
                logger.warning("PDF fetch: cannot resolve remote for %s", sprint["id"])
                return None
            ssh, sprint_path = resolved
            remote_pdf = f"{sprint_path}/report.pdf"

            # Check if PDF exists.
            _, _, rc = await ssh.run(f"test -f {remote_pdf}")
            if rc != 0:
                logger.info(
                    "No report.pdf for %s at %s",
                    sprint["id"],
                    remote_pdf,
                )
                return None

            # Download to local artifact dir.
            art_dir = Path(self.config.artifact_dir) / sprint["id"]
            art_dir.mkdir(parents=True, exist_ok=True)
            local_pdf = str(art_dir / "report.pdf")
            await ssh.download_file(remote_pdf, local_pdf)
            logger.info("Downloaded PDF for %s", sprint["id"])
            return local_pdf
        except Exception:
            logger.warning(
                "PDF fetch failed for %s",
                sprint.get("id"),
                exc_info=True,
            )
            return None

    async def _fetch_results(self, sprint: dict) -> None:
        """Fetch result files from the cluster and store in metadata_json."""
        resolved = await self._resolve_sprint_remote(sprint)
        if resolved is None:
            return
        ssh, sprint_path = resolved
        sprint_id = sprint["id"]

        try:
            # Read all result files in parallel-ish (sequential but fast).
            report_out, _, _ = await ssh.run(
                f"cat {sprint_path}/report.md 2>/dev/null || true"
            )
            findings_out, _, _ = await ssh.run(
                f"cat {sprint_path}/findings.md 2>/dev/null || true"
            )
            progress_out, _, _ = await ssh.run(
                f"cat {sprint_path}/progress.md 2>/dev/null || true"
            )
            red_team_out, _, _ = await ssh.run(
                f"cat {sprint_path}/red_team_round_1.md 2>/dev/null || true"
            )
            fixes_out, _, _ = await ssh.run(
                f"cat {sprint_path}/fixes_round_1.md 2>/dev/null || true"
            )

            meta_dict: dict[str, object] = {}
            if report_out.strip():
                meta_dict["report"] = report_out.strip()
            elif findings_out.strip():
                meta_dict["report"] = findings_out.strip()
            if findings_out.strip():
                meta_dict["findings"] = findings_out.strip()
            if red_team_out.strip():
                meta_dict["red_team"] = red_team_out.strip()
            if fixes_out.strip():
                meta_dict["fixes"] = fixes_out.strip()
            if progress_out.strip():
                meta_dict["progress"] = progress_out.strip()

            # Check for PDF existence (actual download handled by _fetch_pdf).
            _, _, pdf_rc = await ssh.run(f"test -f {sprint_path}/report.pdf")
            if pdf_rc == 0:
                meta_dict["has_pdf"] = True

            if meta_dict:
                await queries.update_sprint(
                    self.db,
                    sprint_id,
                    metadata_json=json.dumps(meta_dict),
                )
                logger.info("Fetched results for sprint %s", sprint_id)
        except Exception:
            logger.warning(
                "Result fetch failed for %s",
                sprint_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Tweaks
    # ------------------------------------------------------------------

    async def submit_tweak(
        self,
        sprint_id: str,
        instruction: str,
        job_options: dict[str, str] | None = None,
        time_limit: str = "2:00:00",
    ) -> str:
        """Submit a quick tweak job for a completed sprint.

        Returns the tweak ID.
        """
        sprint = await queries.get_sprint(self.db, sprint_id)
        if sprint is None:
            raise ValueError(f"Sprint not found: {sprint_id}")
        if sprint["status"] != SprintStatus.COMPLETED.value:
            raise ValueError(
                f"Sprint {sprint_id} is not completed (status={sprint['status']})"
            )

        # Reject if there's already a running tweak for this sprint.
        existing = await queries.list_tweaks(self.db, sprint_id)
        for t in existing:
            if t["status"] in ("pending", "submitted", "running"):
                raise ValueError(
                    f"Sprint {sprint_id} already has an active tweak: {t['id']}"
                )

        study_name: str = sprint["study_name"]

        # Resolve cluster config.
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

        study_cfg = None
        for s in self.config.studies:
            if s.name == study_name:
                study_cfg = s
                break

        scheduler = self.schedulers.get(cluster_cfg.name)
        if scheduler is None:
            scheduler = self.schedulers.get(cluster_cfg.scheduler_type)
        if scheduler is None:
            raise ValueError(
                f"No scheduler for cluster {cluster_cfg.name!r}"
            )

        # Create tweak record.
        tweak_id = generate_tweak_id()
        await queries.create_tweak(self.db, tweak_id, sprint_id, instruction)

        # Resolve sprint path on cluster.
        if study_cfg and study_cfg.sprints_dir:
            sprints_base = study_cfg.sprints_dir
        else:
            sprints_base = f"{cluster_cfg.working_dir}/{study_name}"
        sp_dir = sprint.get("directory", "")
        sprint_remote_dir = f"{sprints_base}/{sp_dir}"

        # Render prompt templates.
        def _render_prompt(name: str, **kw: object) -> str:
            return _prompt_env.get_template(name).render(**kw)

        idea_text = sprint.get("idea") or "(unknown)"
        prompts = [
            {
                "filename": "prompt_tweak.md",
                "content_b64": _b64encode(
                    _render_prompt("tweak.md.j2", instruction=instruction)
                ),
            },
            {
                "filename": "prompt_report.md",
                "content_b64": _b64encode(
                    _render_prompt("report.md.j2", idea=idea_text)
                ),
            },
        ]

        # Render the tweak job script.
        template_name = f"{cluster_cfg.scheduler_type}_tweak.sh.j2"
        template = _jinja_env.get_template(template_name)
        job_script = template.render(
            sprint_id=sprint_id,
            tweak_id=tweak_id,
            sprint_dir=sprint_remote_dir,
            job_name=f"rl-{tweak_id}",
            time_limit=time_limit,
            environment=cluster_cfg.environment,
            job_options={
                **cluster_cfg.job_options,
                **(study_cfg.job_options if study_cfg else {}),
                **(job_options or {}),
            },
            claude_command=(
                (study_cfg.claude_command if study_cfg else "")
                or cluster_cfg.claude_command
                or self.config.claude_command
                or "claude --dangerously-skip-permissions"
            ),
            orchestrator_url=self.config.orchestrator_url or "",
            webhook_token=sprint.get("webhook_token", ""),
            prompts=prompts,
        )

        # SSH: write script and submit.
        cluster_dict = {
            "host": cluster_cfg.host,
            "port": cluster_cfg.port,
            "user": cluster_cfg.user,
            "key_path": cluster_cfg.key_path,
        }
        ssh = await self.ssh_manager.get_connection(cluster_dict)

        script_path = f"{sprint_remote_dir}/.researchloop/run_tweak_{tweak_id}.sh"
        encoded_script = _b64encode(job_script)
        await ssh.run(f"echo '{encoded_script}' | base64 -d > {script_path}")
        await ssh.run(f"chmod +x {script_path}")

        job_id = await scheduler.submit(
            ssh=ssh,
            script=script_path,
            job_name=f"rl-{tweak_id}",
            working_dir=sprint_remote_dir,
            env=cluster_cfg.environment or None,
        )

        now = datetime.now(timezone.utc).isoformat()
        await queries.update_tweak(
            self.db,
            tweak_id,
            job_id=job_id,
            status="submitted",
            started_at=now,
        )

        logger.info(
            "Tweak %s submitted as job %s for sprint %s",
            tweak_id,
            job_id,
            sprint_id,
        )
        return tweak_id

    async def handle_tweak_completion(
        self,
        tweak_id: str,
        sprint_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Handle a tweak job completion."""
        now = datetime.now(timezone.utc).isoformat()
        await queries.update_tweak(
            self.db,
            tweak_id,
            status=status,
            completed_at=now,
            error=error,
        )

        # Re-fetch results for the parent sprint (report may have changed).
        sprint = await queries.get_sprint(self.db, sprint_id)
        if sprint:
            await self._fetch_results(sprint)
            await self._fetch_pdf(sprint)

        logger.info(
            "Tweak %s completion handled: status=%s",
            tweak_id,
            status,
        )
