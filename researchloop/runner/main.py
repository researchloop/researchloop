"""Entry point for the ``researchloop-runner`` CLI.

This runs INSIDE a SLURM/SGE job on HPC clusters.  It executes the
sub-agent pipeline and reports results back to the orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click

from researchloop.runner.pipeline import Pipeline
from researchloop.runner.upload import send_webhook, upload_artifacts

logger = logging.getLogger("researchloop.runner")


async def _run_pipeline(
    sprint_id: str,
    sprint_dir: str,
    claude_md: str,
    idea: str,
    orchestrator_url: str,
    shared_secret: str,
    red_team_rounds: int,
    claude_command: str = "claude --dangerously-skip-permissions",
) -> None:
    """Execute the full pipeline and report back to the orchestrator."""
    sprint_path = Path(sprint_dir)
    sprint_path.mkdir(parents=True, exist_ok=True)
    (sprint_path / ".researchloop").mkdir(parents=True, exist_ok=True)
    (sprint_path / "results").mkdir(parents=True, exist_ok=True)

    pipeline = Pipeline(
        sprint_id=sprint_id,
        sprint_dir=sprint_dir,
        claude_md=claude_md,
        idea=idea,
        orchestrator_url=orchestrator_url,
        shared_secret=shared_secret,
        red_team_rounds=red_team_rounds,
        claude_command=claude_command,
    )

    summary: str | None = None
    error_msg: str | None = None
    final_status = "completed"

    try:
        summary = await pipeline.run()
    except Exception:
        logger.exception("Pipeline failed for sprint %s", sprint_id)
        final_status = "failed"
        error_msg = f"Pipeline error: {sys.exc_info()[1]}"
    finally:
        await pipeline.stop()

    # Upload artifacts before sending the completion webhook so the
    # orchestrator can access them immediately.
    if final_status == "completed":
        try:
            uploaded = await upload_artifacts(
                sprint_dir=sprint_dir,
                orchestrator_url=orchestrator_url,
                shared_secret=shared_secret,
                sprint_id=sprint_id,
            )
            logger.info("Uploaded %d artifact(s)", len(uploaded))
        except Exception:
            logger.exception("Artifact upload failed for sprint %s", sprint_id)

    # Notify orchestrator of completion (or failure).
    try:
        await send_webhook(
            orchestrator_url=orchestrator_url,
            shared_secret=shared_secret,
            sprint_id=sprint_id,
            status=final_status,
            summary=summary,
            error=error_msg,
        )
    except Exception:
        logger.exception("Failed to send completion webhook for sprint %s", sprint_id)


@click.group()
def cli() -> None:
    """ResearchLoop sprint runner - executes inside HPC jobs."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


@cli.command()
@click.option("--sprint-id", required=True, help="Unique sprint identifier")
@click.option("--sprint-dir", required=True, help="Working directory for this sprint")
@click.option("--claude-md", required=True, help="Path to the study's CLAUDE.md")
@click.option("--idea", required=True, help="Research idea / prompt for this sprint")
@click.option(
    "--orchestrator-url", required=True, help="Base URL of the orchestrator API"
)
@click.option(
    "--shared-secret", required=True, help="Shared secret for orchestrator auth"
)
@click.option(
    "--red-team-rounds",
    default=3,
    show_default=True,
    help="Maximum number of red-team / fix rounds",
)
@click.option(
    "--claude-command",
    default="claude --dangerously-skip-permissions",
    show_default=True,
    help="Command to invoke Claude CLI",
)
def run(
    sprint_id: str,
    sprint_dir: str,
    claude_md: str,
    idea: str,
    orchestrator_url: str,
    shared_secret: str,
    red_team_rounds: int,
    claude_command: str,
) -> None:
    """Run the full research sprint pipeline."""
    logger.info(
        "Starting sprint %s in %s (red-team rounds: %d)",
        sprint_id,
        sprint_dir,
        red_team_rounds,
    )
    asyncio.run(
        _run_pipeline(
            sprint_id=sprint_id,
            sprint_dir=sprint_dir,
            claude_md=claude_md,
            idea=idea,
            orchestrator_url=orchestrator_url,
            shared_secret=shared_secret,
            red_team_rounds=red_team_rounds,
            claude_command=claude_command,
        )
    )
    logger.info("Sprint %s finished.", sprint_id)


def main() -> None:
    """Package entry point for ``researchloop-runner``."""
    cli()
