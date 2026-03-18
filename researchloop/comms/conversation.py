"""Slack conversation manager -- maps threads to Claude sessions."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from researchloop.db.database import Database

logger = logging.getLogger(__name__)


class ConversationManager:
    """Maps Slack threads to Claude CLI sessions.

    When a user messages in a thread, we look up the session_id
    and use ``claude -p --resume <session_id>`` for continuity.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_session(self, thread_ts: str) -> dict | None:
        """Look up an existing Slack session by thread timestamp."""
        return await self.db.fetch_one(
            "SELECT * FROM slack_sessions WHERE thread_ts = ?",
            (thread_ts,),
        )

    async def create_session(
        self,
        thread_ts: str,
        study_name: str | None = None,
        sprint_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Create a new Slack session record."""
        await self.db.execute(
            """INSERT INTO slack_sessions
               (thread_ts, sprint_id, session_id, study_name)
               VALUES (?, ?, ?, ?)""",
            (thread_ts, sprint_id, session_id, study_name),
        )

    async def update_session_id(self, thread_ts: str, session_id: str) -> None:
        """Update the Claude session ID for a thread."""
        await self.db.execute(
            """UPDATE slack_sessions
               SET session_id = ? WHERE thread_ts = ?""",
            (session_id, thread_ts),
        )

    async def _build_context(self) -> str:
        """Build context about studies and recent sprints."""
        parts = [
            "You are the ResearchLoop assistant. "
            "You help researchers plan and manage "
            "automated research sprints on HPC clusters.",
            "",
            "The user can ask you to:",
            "- Discuss research ideas for upcoming sprints",
            "- Review results from completed sprints",
            "- Suggest what to investigate next",
            "- Help formulate sprint ideas",
            "",
        ]

        # Add study info.
        studies = await self.db.fetch_all(
            "SELECT name, cluster, description FROM studies"
        )
        if studies:
            parts.append("Available studies:")
            for s in studies:
                desc = s.get("description") or ""
                parts.append(f"- {s['name']}: {desc}")
            parts.append("")

        # Add recent sprint summaries.
        sprints = await self.db.fetch_all(
            "SELECT id, study_name, idea, status, summary "
            "FROM sprints ORDER BY created_at DESC LIMIT 10"
        )
        if sprints:
            parts.append("Recent sprints:")
            for sp in sprints:
                idea = (sp.get("idea") or "")[:80]
                summary = (sp.get("summary") or "")[:100]
                parts.append(f"- {sp['id']} [{sp['status']}] {idea}")
                if summary:
                    parts.append(f"  Summary: {summary}")
            parts.append("")

        return "\n".join(parts)

    async def handle_message(
        self,
        thread_ts: str,
        user_text: str,
        study_name: str | None = None,
    ) -> str:
        """Handle a conversational message from Slack.

        Runs ``claude -p`` with the user's message, using
        ``--resume`` if there is an existing session.
        Returns the response text.
        """
        session = await self.get_session(thread_ts)
        resume_id = session["session_id"] if session else None

        # Build the prompt with system context.
        prompt = user_text
        if session is None:
            # First message — add context about ResearchLoop.
            context = await self._build_context()
            prompt = f"{context}\n\nUser message: {user_text}"

        # Run Claude CLI
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
        ]
        if resume_id:
            cmd.extend(["--resume", resume_id])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            return "Sorry, the request timed out."
        except FileNotFoundError:
            return (
                "Claude CLI is not available on this host. "
                "Conversational mode requires `claude` in PATH."
            )

        if proc.returncode != 0:
            logger.error(
                "Claude CLI failed: %s",
                stderr.decode()[:500],
            )
            return "Sorry, something went wrong."

        # Parse response
        raw = stdout.decode("utf-8", errors="replace").strip()
        response_text = raw
        new_session_id = None
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                response_text = (
                    data.get("result", "")
                    or data.get("text", "")
                    or data.get("content", "")
                    or raw
                )
                new_session_id = data.get("session_id")
        except json.JSONDecodeError:
            pass

        # Persist session
        if session is None:
            await self.create_session(
                thread_ts,
                study_name=study_name,
                session_id=new_session_id,
            )
        elif new_session_id:
            await self.update_session_id(thread_ts, new_session_id)

        return response_text
