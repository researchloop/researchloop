"""Claude CLI wrapper for running sub-agent prompts.

Invokes ``claude -p "..." --output-format json`` as a subprocess and
parses the structured output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import jinja2

logger = logging.getLogger(__name__)

# Template directory lives alongside this module.
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined,
)


def render_template(template_name: str, **kwargs: object) -> str:
    """Load a Jinja2 template from the ``templates/`` directory and render it."""
    template = _jinja_env.get_template(template_name)
    return template.render(**kwargs)


async def run_claude(
    prompt: str,
    working_dir: str,
    claude_md: str | None = None,
    session_id: str | None = None,
    timeout: int = 3600,
    claude_command: str = "claude --dangerously-skip-permissions",
) -> tuple[str, str | None]:
    """Run the Claude CLI with the given prompt.

    Parameters
    ----------
    prompt:
        The full prompt text to send.
    working_dir:
        The working directory in which Claude should operate.
    claude_md:
        Optional path to a CLAUDE.md file.  When provided the
        ``CLAUDE_MD`` environment variable is set so the CLI picks it up.
    session_id:
        If continuing a conversation, the session ID from a previous call.
    timeout:
        Maximum seconds to wait for the process (default 3600 = 1 hour).

    Returns
    -------
    tuple[str, str | None]
        ``(output_text, session_id)`` parsed from the JSON output.
    """
    # Split the command string to support things like
    # "singularity exec img.sif claude --dangerously-skip-permissions"
    base_cmd = claude_command.split()
    cmd: list[str] = [
        *base_cmd,
        "-p",
        prompt,
        "--output-format",
        "json",
    ]

    if session_id:
        cmd.extend(["--resume", session_id])

    # Build environment with optional CLAUDE_MD override.
    env = os.environ.copy()
    if claude_md:
        env["CLAUDE_MD"] = claude_md

    logger.info(
        "Running Claude CLI (session=%s, timeout=%ds, cwd=%s)",
        session_id or "new",
        timeout,
        working_dir,
    )
    logger.debug("Prompt length: %d chars", len(prompt))

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=env,
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.error("Claude CLI timed out after %ds", timeout)
        # Attempt to kill the hung process.
        try:
            process.kill()  # type: ignore[possibly-undefined]
        except ProcessLookupError:
            pass
        raise TimeoutError(f"Claude CLI did not finish within {timeout} seconds")

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    if process.returncode != 0:
        logger.error(
            "Claude CLI exited with code %d\nstderr: %s",
            process.returncode,
            stderr[:2000],
        )
        raise RuntimeError(
            f"Claude CLI exited with code {process.returncode}: {stderr[:500]}"
        )

    if stderr:
        logger.debug("Claude CLI stderr: %s", stderr[:1000])

    # Parse JSON output.
    output_text, new_session_id = _parse_output(stdout)
    logger.info(
        "Claude CLI finished: %d chars output, session=%s",
        len(output_text),
        new_session_id or "none",
    )
    return output_text, new_session_id


def _parse_output(raw: str) -> tuple[str, str | None]:
    """Parse the JSON output from ``claude --output-format json``.

    The CLI emits a JSON object with at least a ``result`` field.
    The ``session_id`` field may or may not be present.
    """
    raw = raw.strip()
    if not raw:
        return "", None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # If the output is not valid JSON, treat the whole thing as plain text.
        logger.warning("Could not parse Claude CLI output as JSON; using raw text")
        return raw, None

    # The CLI may nest the text in different fields depending on version.
    output_text: str = ""
    if isinstance(data, dict):
        output_text = (
            data.get("result", "")
            or data.get("text", "")
            or data.get("content", "")
            or ""
        )
        session_id = data.get("session_id")
    else:
        output_text = str(data)
        session_id = None

    return output_text, session_id
