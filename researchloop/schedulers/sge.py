"""Sun Grid Engine (SGE) / Open Grid Scheduler implementation."""

from __future__ import annotations

import logging
import re
import textwrap

from researchloop.schedulers.base import BaseScheduler

logger = logging.getLogger(__name__)

# Maps SGE state codes to normalised status strings.
_SGE_STATE_MAP: dict[str, str] = {
    "qw": "pending",  # queued waiting
    "hqw": "pending",  # hold queued waiting
    "r": "running",
    "t": "running",  # transferring
    "s": "running",  # suspended
    "S": "running",  # suspended by queue
    "T": "running",  # threshold
    "Eqw": "failed",  # error
    "dr": "failed",  # deleting/running
    "dt": "failed",  # deleting/transferring
}


class SGEScheduler(BaseScheduler):
    """Job scheduler for SGE/Grid Engine clusters."""

    async def submit(
        self,
        ssh: object,
        script: str,
        job_name: str,
        working_dir: str,
        env: dict[str, str] | None = None,
    ) -> str:
        # Submit via qsub — script is a remote file path
        submit_cmd = f"cd {working_dir} && qsub {script}"
        stdout, stderr, rc = await ssh.run(  # type: ignore[attr-defined]
            submit_cmd, timeout=60
        )
        if rc != 0:
            raise RuntimeError(f"qsub failed (exit {rc}): {stderr}")

        # Parse job ID from
        # "Your job XXXXX ("name") has been submitted"
        match = re.search(r"Your job\s+(\d+)", stdout)
        if not match:
            raise RuntimeError(f"Could not parse job ID from qsub: {stdout!r}")

        job_id = match.group(1)
        logger.info(
            "Submitted SGE job %s (name=%s)",
            job_id,
            job_name,
        )

        return job_id

    async def status(self, ssh: object, job_id: str) -> str:
        # Try qstat first
        stdout, stderr, rc = await ssh.run(  # type: ignore[attr-defined]
            f"qstat -j {job_id}", timeout=30
        )
        if rc == 0 and stdout.strip():
            # Parse state from qstat output
            for line in stdout.splitlines():
                if line.strip().startswith("job_state"):
                    # Format: job_state   1:  r
                    parts = line.split(":")
                    if len(parts) >= 2:
                        state = parts[-1].strip()
                        return _SGE_STATE_MAP.get(state, "unknown")
            # Job exists but couldn't parse state
            return "running"

        # Try qstat listing format
        stdout, stderr, rc = await ssh.run(  # type: ignore[attr-defined]
            f"qstat | grep {job_id}", timeout=30
        )
        if rc == 0 and stdout.strip():
            parts = stdout.split()
            if len(parts) >= 5:
                state = parts[4]
                return _SGE_STATE_MAP.get(state, "unknown")

        # Job not in queue -- try qacct for finished jobs
        stdout, stderr, rc = await ssh.run(  # type: ignore[attr-defined]
            f"qacct -j {job_id}", timeout=30
        )
        if rc == 0 and stdout.strip():
            for line in stdout.splitlines():
                if "exit_status" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        code = parts[-1].strip()
                        if code == "0":
                            return "completed"
                        return "failed"
            return "completed"

        logger.warning(
            "Could not determine status for SGE job %s",
            job_id,
        )
        return "unknown"

    async def cancel(self, ssh: object, job_id: str) -> bool:
        stdout, stderr, rc = await ssh.run(  # type: ignore[attr-defined]
            f"qdel {job_id}", timeout=30
        )
        if rc == 0:
            logger.info("Cancelled SGE job %s", job_id)
            return True
        logger.error(
            "qdel failed for job %s (exit %d): %s",
            job_id,
            rc,
            stderr,
        )
        return False

    def generate_script(
        self,
        command: str,
        job_name: str,
        working_dir: str,
        time_limit: str = "8:00:00",
        env: dict[str, str] | None = None,
    ) -> str:
        env_exports = ""
        if env:
            lines = [f"export {k}={_shell_quote(v)}" for k, v in env.items()]
            env_exports = "\n".join(lines) + "\n"

        script = textwrap.dedent(f"""\
            #!/usr/bin/env bash
            #$ -N {job_name}
            #$ -o {working_dir}/{job_name}_$JOB_ID.out
            #$ -e {working_dir}/{job_name}_$JOB_ID.err
            #$ -l h_rt={time_limit}
            #$ -cwd
            #$ -S /bin/bash

            set -euo pipefail

            echo "=== SGE Job $JOB_ID started ==="
            echo "Host: $(hostname)"

            {env_exports}# --- Run the command ---
            {command}

            echo "=== SGE Job $JOB_ID finished ==="
        """)
        return script


def _shell_quote(value: str) -> str:
    """Wrap *value* in single quotes, escaping embedded
    single quotes."""
    return "'" + value.replace("'", "'\\''") + "'"
