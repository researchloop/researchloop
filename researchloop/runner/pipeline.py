"""Sub-agent pipeline orchestration.

Each step invokes the Claude CLI as a subprocess and writes progress into
``.researchloop/status.json`` so that the orchestrator can track liveness.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from researchloop.runner.claude import render_template, run_claude
from researchloop.runner.upload import send_heartbeat

logger = logging.getLogger(__name__)

# Step labels (indexed from 1).
_STEP_LABELS: list[str] = [
    "research",
    "red_team",
    "validate",
    "report",
    "summarize",
]
_TOTAL_STEPS = len(_STEP_LABELS) + 1  # +1 because red_team counts as 2 (loop)


class Pipeline:
    """Runs the full research sprint pipeline inside an HPC job."""

    def __init__(
        self,
        sprint_id: str,
        sprint_dir: str,
        claude_md: str,
        idea: str,
        orchestrator_url: str,
        shared_secret: str,
        red_team_rounds: int = 3,
    ) -> None:
        self.sprint_id = sprint_id
        self.sprint_dir = sprint_dir
        self.claude_md = claude_md
        self.idea = idea
        self.orchestrator_url = orchestrator_url
        self.shared_secret = shared_secret
        self.red_team_rounds = red_team_rounds

        self._started_at = datetime.now(timezone.utc).isoformat()
        self._status_path = Path(sprint_dir) / ".researchloop" / "status.json"
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._session_id: str | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self) -> str | None:
        """Execute the full pipeline and return the summary text (or None)."""
        self._start_heartbeat()

        # Read study context from CLAUDE.md if available.
        study_context = ""
        claude_md_path = Path(self.claude_md)
        if claude_md_path.exists():
            study_context = claude_md_path.read_text(encoding="utf-8")

        # Step 1 - Research
        await self._update_status("research", step=1)
        research_prompt = render_template(
            "research_sprint.md.j2",
            study_context=study_context,
            idea=self.idea,
            sprint_dir=self.sprint_dir,
        )
        output, self._session_id = await run_claude(
            prompt=research_prompt,
            working_dir=self.sprint_dir,
            claude_md=self.claude_md,
        )
        logger.info("Research step complete (%d chars output)", len(output))

        # Step 2 - Red-team / fix loop
        await self._update_status("red_team", step=2)
        for round_num in range(1, self.red_team_rounds + 1):
            substep = f"round_{round_num}"
            await self._update_status("red_team", step=2, substep=substep)

            # Run the red-team critique.
            rt_prompt = render_template(
                "red_team.md.j2",
                idea=self.idea,
                round_number=round_num,
                max_rounds=self.red_team_rounds,
            )
            rt_output, self._session_id = await run_claude(
                prompt=rt_prompt,
                working_dir=self.sprint_dir,
                claude_md=self.claude_md,
                session_id=self._session_id,
            )
            logger.info(
                "Red-team round %d complete (%d chars)", round_num, len(rt_output)
            )

            # Check whether the red-team found critical issues.
            rt_file = Path(self.sprint_dir) / f"red_team_round_{round_num}.md"
            if rt_file.exists():
                content = rt_file.read_text(encoding="utf-8")
                if "NO CRITICAL ISSUES" in content:
                    logger.info(
                        "Red-team round %d: no critical issues, stopping loop.",
                        round_num,
                    )
                    break

            # Run fix step for this round.
            await self._update_status("red_team", step=2, substep=f"fix_{round_num}")
            fix_prompt = render_template(
                "fix_issues.md.j2",
                round_number=round_num,
            )
            fix_output, self._session_id = await run_claude(
                prompt=fix_prompt,
                working_dir=self.sprint_dir,
                claude_md=self.claude_md,
                session_id=self._session_id,
            )
            logger.info("Fix round %d complete (%d chars)", round_num, len(fix_output))

        # Step 3 - Validation
        await self._update_status("validate", step=3)
        val_prompt = render_template(
            "validation.md.j2",
            idea=self.idea,
        )
        val_output, self._session_id = await run_claude(
            prompt=val_prompt,
            working_dir=self.sprint_dir,
            claude_md=self.claude_md,
            session_id=self._session_id,
        )
        logger.info("Validation step complete (%d chars)", len(val_output))

        # Step 4 - Report
        await self._update_status("report", step=4)
        report_prompt = render_template(
            "report.md.j2",
            idea=self.idea,
        )
        report_output, self._session_id = await run_claude(
            prompt=report_prompt,
            working_dir=self.sprint_dir,
            claude_md=self.claude_md,
            session_id=self._session_id,
        )
        logger.info("Report step complete (%d chars)", len(report_output))

        # Step 5 - Summarize
        await self._update_status("summarize", step=5)
        summary_prompt = render_template("summarizer.md.j2")
        summary_output, self._session_id = await run_claude(
            prompt=summary_prompt,
            working_dir=self.sprint_dir,
            claude_md=self.claude_md,
            session_id=self._session_id,
        )
        logger.info("Summary step complete (%d chars)", len(summary_output))

        # Read summary.txt written by the summarizer agent.
        summary_path = Path(self.sprint_dir) / "summary.txt"
        summary: str | None = None
        if summary_path.exists():
            summary = summary_path.read_text(encoding="utf-8").strip()

        await self._update_status("completed", step=_TOTAL_STEPS)
        return summary

    async def stop(self) -> None:
        """Clean up background tasks."""
        await self._stop_heartbeat()

    # ------------------------------------------------------------------
    # Status tracking
    # ------------------------------------------------------------------

    async def _update_status(
        self,
        status: str,
        step: int = 0,
        substep: str | None = None,
        error: str | None = None,
    ) -> None:
        """Write the current status to ``.researchloop/status.json``."""
        data: dict[str, Any] = {
            "sprint_id": self.sprint_id,
            "status": status,
            "step": step,
            "total_steps": _TOTAL_STEPS,
            "substep": substep,
            "heartbeat": datetime.now(timezone.utc).isoformat(),
            "started_at": self._started_at,
            "error": error,
        }
        self._status_path.parent.mkdir(parents=True, exist_ok=True)
        self._status_path.write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
        logger.info("Status updated: %s step=%d substep=%s", status, step, substep)

        # Best-effort heartbeat to orchestrator.
        try:
            await send_heartbeat(
                orchestrator_url=self.orchestrator_url,
                shared_secret=self.shared_secret,
                sprint_id=self.sprint_id,
                status=status,
                step=step,
            )
        except Exception:
            logger.debug("Heartbeat POST failed (non-fatal)", exc_info=True)

    # ------------------------------------------------------------------
    # Heartbeat background task
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        """Start a background task that updates status.json every 60 seconds."""
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="heartbeat"
        )
        logger.info("Heartbeat background task started.")

    async def _heartbeat_loop(self) -> None:
        """Periodically refresh the heartbeat timestamp."""
        try:
            while True:
                await asyncio.sleep(60)
                # Re-read current status to update only the heartbeat field.
                if self._status_path.exists():
                    try:
                        data = json.loads(self._status_path.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        data = {}
                else:
                    data = {}

                data["heartbeat"] = datetime.now(timezone.utc).isoformat()
                self._status_path.write_text(
                    json.dumps(data, indent=2) + "\n", encoding="utf-8"
                )

                # Also ping the orchestrator.
                try:
                    await send_heartbeat(
                        orchestrator_url=self.orchestrator_url,
                        shared_secret=self.shared_secret,
                        sprint_id=self.sprint_id,
                        status=data.get("status", "running"),
                        step=data.get("step", 0),
                    )
                except Exception:
                    logger.debug("Heartbeat POST failed (non-fatal)", exc_info=True)
        except asyncio.CancelledError:
            return

    async def _stop_heartbeat(self) -> None:
        """Cancel the heartbeat background task."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
            logger.info("Heartbeat background task stopped.")
