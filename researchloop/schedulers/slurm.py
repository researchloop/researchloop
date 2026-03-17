"""SLURM scheduler implementation."""

from __future__ import annotations

import logging
import re
import textwrap

from researchloop.schedulers.base import BaseScheduler

logger = logging.getLogger(__name__)

# Maps SLURM state names to normalised status strings.
_SLURM_STATE_MAP: dict[str, str] = {
    "PENDING": "pending",
    "CONFIGURING": "pending",
    "RUNNING": "running",
    "COMPLETING": "running",
    "SUSPENDED": "running",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "CANCELLED": "failed",
    "TIMEOUT": "failed",
    "NODE_FAIL": "failed",
    "PREEMPTED": "failed",
    "OUT_OF_MEMORY": "failed",
    "BOOT_FAIL": "failed",
    "DEADLINE": "failed",
}


class SlurmScheduler(BaseScheduler):
    """Job scheduler that talks to SLURM via ``sbatch`` / ``squeue`` / ``sacct``."""

    # ------------------------------------------------------------------
    # submit
    # ------------------------------------------------------------------

    async def submit(
        self,
        ssh: object,
        script: str,
        job_name: str,
        working_dir: str,
        env: dict[str, str] | None = None,
    ) -> str:
        """Submit *script* (a remote file path) via ``sbatch``.

        Returns the SLURM job ID parsed from the ``sbatch`` output.
        """
        submit_cmd = f"cd {working_dir} && sbatch {script}"
        stdout, stderr, rc = await ssh.run(submit_cmd, timeout=60)  # type: ignore[attr-defined]
        if rc != 0:
            raise RuntimeError(f"sbatch failed (exit {rc}): {stderr}")

        # Parse job ID from "Submitted batch job XXXXX".
        match = re.search(r"Submitted batch job\s+(\d+)", stdout)
        if not match:
            raise RuntimeError(f"Could not parse job ID from sbatch output: {stdout!r}")

        job_id = match.group(1)
        logger.info(
            "Submitted SLURM job %s (name=%s, dir=%s)", job_id, job_name, working_dir
        )

        return job_id

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    async def status(self, ssh: object, job_id: str) -> str:
        """Query SLURM for the current state of *job_id*.

        Tries ``squeue`` first (for queued / running jobs). If the job is no
        longer in the queue, falls back to ``sacct`` to retrieve its final
        state.
        """
        # --- Try squeue first ---
        stdout, stderr, rc = await ssh.run(  # type: ignore[attr-defined]
            f"squeue -j {job_id} -h -o %T", timeout=30
        )
        if rc == 0:
            state = stdout.strip().upper()
            if state:
                normalised = _SLURM_STATE_MAP.get(state, "unknown")
                logger.debug(
                    "squeue reports job %s state=%s -> %s",
                    job_id,
                    state,
                    normalised,
                )
                return normalised

        # --- Fall back to sacct ---
        stdout, stderr, rc = await ssh.run(  # type: ignore[attr-defined]
            f"sacct -j {job_id} -n -o State --parsable2", timeout=30
        )
        if rc == 0:
            # sacct can return multiple lines (job + job steps).  Take the
            # first non-empty state, which corresponds to the overall job.
            for line in stdout.strip().splitlines():
                state = line.strip().upper()
                # sacct sometimes appends qualifiers like "CANCELLED by ..."
                state = state.split()[0] if state else ""
                if state:
                    normalised = _SLURM_STATE_MAP.get(state, "unknown")
                    logger.debug(
                        "sacct reports job %s state=%s -> %s",
                        job_id,
                        state,
                        normalised,
                    )
                    return normalised

        logger.warning("Could not determine status for SLURM job %s", job_id)
        return "unknown"

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------

    async def cancel(self, ssh: object, job_id: str) -> bool:
        """Cancel a SLURM job via ``scancel``."""
        stdout, stderr, rc = await ssh.run(  # type: ignore[attr-defined]
            f"scancel {job_id}", timeout=30
        )
        if rc == 0:
            logger.info("Cancelled SLURM job %s", job_id)
            return True

        logger.error("scancel failed for job %s (exit %d): %s", job_id, rc, stderr)
        return False

    # ------------------------------------------------------------------
    # generate_script
    # ------------------------------------------------------------------

    def generate_script(
        self,
        command: str,
        job_name: str,
        working_dir: str,
        time_limit: str = "8:00:00",
        env: dict[str, str] | None = None,
    ) -> str:
        """Generate a SLURM batch submission script."""
        env_exports = ""
        if env:
            lines = [
                f"export {key}={_shell_quote(value)}" for key, value in env.items()
            ]
            env_exports = "\n".join(lines) + "\n"

        script = textwrap.dedent(f"""\
            #!/usr/bin/env bash
            #SBATCH --job-name={job_name}
            #SBATCH --output={working_dir}/{job_name}_%j.out
            #SBATCH --error={working_dir}/{job_name}_%j.err
            #SBATCH --time={time_limit}
            #SBATCH --chdir={working_dir}

            set -euo pipefail

            echo "=== SLURM Job $SLURM_JOB_ID started at $(date -u) ==="
            echo "Host: $(hostname)"
            echo "Working directory: $(pwd)"

            {env_exports}# --- Run the command ---
            {command}

            echo "=== SLURM Job $SLURM_JOB_ID finished at $(date -u) ==="
        """)
        return script


def _shell_quote(value: str) -> str:
    """Wrap *value* in single quotes, escaping embedded single quotes."""
    return "'" + value.replace("'", "'\\''") + "'"
