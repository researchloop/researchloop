"""Main orchestrator -- ties every subsystem together and exposes a FastAPI app."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from researchloop.clusters.monitor import JobMonitor
from researchloop.clusters.ssh import SSHManager
from researchloop.comms.conversation import ConversationManager
from researchloop.comms.ntfy import NtfyNotifier
from researchloop.comms.router import NotificationRouter
from researchloop.comms.slack import (
    SlackNotifier,
    verify_slack_signature,
)
from researchloop.core.config import Config
from researchloop.db.database import Database
from researchloop.schedulers.base import BaseScheduler
from researchloop.sprints.auto_loop import AutoLoopController
from researchloop.sprints.manager import SprintManager
from researchloop.studies.manager import StudyManager

logger = logging.getLogger(__name__)


class Orchestrator:
    """Central coordinator that initialises and owns every subsystem.

    Call :meth:`start` to bring everything up and :meth:`stop` for a
    clean shutdown.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

        # Subsystem references (populated by start()).
        self.db: Database | None = None
        self.ssh_manager: SSHManager | None = None
        self.schedulers: dict[str, BaseScheduler] = {}
        self.study_manager: StudyManager | None = None
        self.sprint_manager: SprintManager | None = None
        self.auto_loop: AutoLoopController | None = None
        self.notification_router: NotificationRouter | None = None
        self.job_monitor: JobMonitor | None = None
        self.conversation_manager: ConversationManager | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the database, managers, and background tasks."""
        logger.info("Orchestrator starting...")

        # 1. Database
        self.db = Database(self.config.db_path)
        await self.db.connect()
        logger.info("Database connected: %s", self.config.db_path)

        # 2. SSH manager
        self.ssh_manager = SSHManager()

        # 3. Schedulers -- import concrete implementations lazily so that
        #    the base package has no hard dependency on them.
        self.schedulers = _build_schedulers(self.config)

        # 4. Study manager
        self.study_manager = StudyManager(self.db, self.config)
        await self.study_manager.sync_from_config()

        # 5. Notification router
        self.notification_router = NotificationRouter()
        if self.config.ntfy and self.config.ntfy.topic:
            ntfy = NtfyNotifier(
                url=self.config.ntfy.url,
                topic=self.config.ntfy.topic,
            )
            self.notification_router.add_notifier(ntfy)
            logger.info(
                "ntfy notifier configured for topic %r",
                self.config.ntfy.topic,
            )
        if self.config.slack and self.config.slack.bot_token:
            slack_notifier = SlackNotifier(
                bot_token=self.config.slack.bot_token,
                channel_id=self.config.slack.channel_id,
                dashboard_url=self.config.orchestrator_url,
            )
            self.notification_router.add_notifier(slack_notifier)
            logger.info("Slack notifier configured")

        # 6. Sprint manager
        self.sprint_manager = SprintManager(
            db=self.db,
            config=self.config,
            ssh_manager=self.ssh_manager,
            schedulers=self.schedulers,
            study_manager=self.study_manager,
            notification_router=self.notification_router,
        )

        # 6b. Conversation manager
        self.conversation_manager = ConversationManager(
            self.db, sprint_manager=self.sprint_manager
        )

        # 7. Auto-loop controller
        self.auto_loop = AutoLoopController(
            db=self.db,
            sprint_manager=self.sprint_manager,
            config=self.config,
        )

        # 8. Job monitor
        self.job_monitor = JobMonitor(
            ssh_manager=self.ssh_manager,
            db=self.db,
            schedulers=self.schedulers,
            config=self.config,
        )
        await self.job_monitor.start_polling()

        logger.info("Orchestrator started.")

    async def stop(self) -> None:
        """Shut down all subsystems cleanly."""
        logger.info("Orchestrator shutting down...")

        if self.job_monitor is not None:
            await self.job_monitor.stop_polling()

        if self.ssh_manager is not None:
            await self.ssh_manager.close_all()

        if self.db is not None:
            await self.db.close()

        logger.info("Orchestrator stopped.")


# ----------------------------------------------------------------------
# Scheduler factory
# ----------------------------------------------------------------------


def _build_schedulers(config: Config) -> dict[str, BaseScheduler]:
    """Build a scheduler instance for every cluster in *config*.

    The dict is keyed by cluster name **and** by scheduler type so that
    lookups by either key succeed.
    """
    schedulers: dict[str, BaseScheduler] = {}

    for cluster in config.clusters:
        stype = cluster.scheduler_type
        if stype in schedulers:
            # Reuse an existing scheduler of the same type.
            schedulers[cluster.name] = schedulers[stype]
            continue

        scheduler: BaseScheduler | None = None
        try:
            if stype == "slurm":
                from researchloop.schedulers.slurm import (
                    SlurmScheduler,  # type: ignore[import-not-found]
                )

                scheduler = SlurmScheduler()
            elif stype == "sge":
                from researchloop.schedulers.sge import (
                    SGEScheduler,  # type: ignore[import-not-found]
                )

                scheduler = SGEScheduler()
            elif stype == "local":
                from researchloop.schedulers.local import (
                    LocalScheduler,  # type: ignore[import-not-found]
                )

                scheduler = LocalScheduler()
            else:
                logger.warning(
                    "Unknown scheduler type %r for cluster %r", stype, cluster.name
                )
        except ImportError:
            logger.warning(
                "Scheduler %r not available (import failed) for cluster %r",
                stype,
                cluster.name,
            )

        if scheduler is not None:
            schedulers[cluster.name] = scheduler
            schedulers[stype] = scheduler

    return schedulers


# ======================================================================
# FastAPI application factory
# ======================================================================


def create_app(orchestrator: Orchestrator) -> FastAPI:
    """Build and return the FastAPI application.

    Routes:
      - ``POST /api/webhook/sprint-complete``
      - ``POST /api/webhook/heartbeat``
      - ``POST /api/artifacts/{sprint_id}``
      - ``GET  /api/sprints``
      - ``GET  /api/sprints/{sprint_id}``
      - ``GET  /api/studies``
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        await orchestrator.start()
        yield
        await orchestrator.stop()

    app = FastAPI(
        title="ResearchLoop API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # -- Root redirect ---------------------------------------------------
    @app.get("/")
    async def root():  # type: ignore[no-untyped-def]
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/dashboard/", status_code=303)

    # -- CORS middleware ------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- Auth helper ----------------------------------------------------

    from researchloop.dashboard.auth import (
        SessionManager,
        check_password,
    )

    # Signing key is auto-generated and persisted in the DB.
    # Loaded lazily on first use so the DB is ready.
    _api_session_mgr: SessionManager | None = None

    async def _get_session_mgr() -> SessionManager:
        nonlocal _api_session_mgr
        if _api_session_mgr is not None:
            return _api_session_mgr

        key: str | None = None
        if orchestrator.db is not None:
            row = await orchestrator.db.fetch_one(
                "SELECT value FROM settings WHERE key = ?",
                ("signing_key",),
            )
            if row:
                key = row["value"]
            else:
                import secrets as _secrets

                key = _secrets.token_hex(32)
                await orchestrator.db.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)",
                    ("signing_key", key),
                )
        _api_session_mgr = SessionManager(secret_key=key)
        return _api_session_mgr

    async def _get_password_hash() -> str | None:
        """Resolve dashboard password hash from config or DB."""
        cfg_hash = orchestrator.config.dashboard.password_hash
        if cfg_hash:
            return cfg_hash
        if orchestrator.db is not None:
            row = await orchestrator.db.fetch_one(
                "SELECT value FROM settings WHERE key = ?",
                ("dashboard_password_hash",),
            )
            if row:
                return row["value"]
        return None

    async def _check_auth(
        x_shared_secret: str | None = None,
        authorization: str | None = None,
    ) -> None:
        """Raise 401 if neither shared secret nor bearer token is valid."""
        # Check bearer token (from `researchloop connect`).
        if authorization and authorization.startswith("Bearer "):
            token = authorization[7:]
            mgr = await _get_session_mgr()
            if mgr.verify_token(token):
                return

        # Check shared secret (from runner webhooks / config).
        expected = orchestrator.config.shared_secret
        if expected and x_shared_secret == expected:
            return

        # If no auth mechanism is configured, allow access.
        if not expected:
            return

        raise HTTPException(
            status_code=401,
            detail="Invalid or missing credentials",
        )

    @app.post("/api/auth")
    async def api_auth(request: Request) -> JSONResponse:
        """Authenticate with dashboard password, get API token."""
        body = await request.json()
        password = body.get("password", "")

        pw_hash = await _get_password_hash()
        if not pw_hash:
            raise HTTPException(
                status_code=400,
                detail="No password configured on this server",
            )

        if not check_password(password, pw_hash):
            raise HTTPException(
                status_code=401,
                detail="Invalid password",
            )

        mgr = await _get_session_mgr()
        token = mgr.create_token()
        return JSONResponse({"token": token})

    # -- Webhook routes -------------------------------------------------

    async def _check_webhook_token(
        sprint_id: str,
        x_webhook_token: str | None = None,
    ) -> None:
        """Verify the per-sprint webhook token."""
        from researchloop.db import queries

        if not sprint_id or orchestrator.db is None:
            raise HTTPException(
                status_code=400,
                detail="sprint_id is required",
            )
        sprint = await queries.get_sprint(orchestrator.db, sprint_id)
        if sprint is None:
            raise HTTPException(
                status_code=404,
                detail="Sprint not found",
            )
        expected = sprint.get("webhook_token")
        if expected and x_webhook_token != expected:
            raise HTTPException(
                status_code=401,
                detail="Invalid webhook token",
            )

    @app.post("/api/webhook/sprint-complete")
    async def webhook_sprint_complete(
        request: Request,
        x_webhook_token: str | None = Header(default=None),
    ) -> JSONResponse:
        """Handle sprint completion webhook from the runner."""
        body: dict[str, Any] = await request.json()
        sprint_id: str = body.get("sprint_id", "")
        await _check_webhook_token(sprint_id, x_webhook_token)
        status: str = body.get("status", "completed")
        summary: str | None = body.get("summary")
        error: str | None = body.get("error")

        if not sprint_id:
            raise HTTPException(status_code=400, detail="sprint_id is required")

        assert orchestrator.sprint_manager is not None
        await orchestrator.sprint_manager.handle_completion(
            sprint_id=sprint_id,
            status=status,
            summary=summary,
            error=error,
        )

        # Trigger auto-loop advancement if applicable.
        if orchestrator.auto_loop is not None:
            await orchestrator.auto_loop.on_sprint_complete(sprint_id)

        logger.info(
            "Webhook: sprint %s completion processed (status=%s)",
            sprint_id,
            status,
        )
        return JSONResponse({"ok": True, "sprint_id": sprint_id})

    @app.post("/api/webhook/heartbeat")
    async def webhook_heartbeat(
        request: Request,
        x_webhook_token: str | None = Header(default=None),
    ) -> JSONResponse:
        """Handle heartbeat from the runner."""
        body: dict[str, Any] = await request.json()
        sprint_id = body.get("sprint_id", "")
        await _check_webhook_token(sprint_id, x_webhook_token)
        sprint_id: str = body.get("sprint_id", "")
        phase: str | None = body.get("phase")

        if not sprint_id:
            raise HTTPException(status_code=400, detail="sprint_id is required")

        assert orchestrator.db is not None

        from researchloop.db import queries

        update_fields: dict[str, Any] = {
            "metadata_json": json.dumps(
                {
                    "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                    "phase": phase,
                }
            ),
        }
        if phase:
            update_fields["status"] = phase

        await queries.update_sprint(orchestrator.db, sprint_id, **update_fields)

        logger.debug("Heartbeat received for sprint %s (phase=%s)", sprint_id, phase)
        return JSONResponse({"ok": True})

    # -- Artifact upload ------------------------------------------------

    @app.post("/api/artifacts/{sprint_id}")
    async def upload_artifact(
        sprint_id: str,
        file: UploadFile,
        x_webhook_token: str | None = Header(default=None),
    ) -> JSONResponse:
        """Receive and store an artifact file for a sprint."""
        await _check_webhook_token(sprint_id, x_webhook_token)

        assert orchestrator.db is not None

        from researchloop.db import queries

        sprint = await queries.get_sprint(orchestrator.db, sprint_id)
        if sprint is None:
            raise HTTPException(status_code=404, detail="Sprint not found")

        # Determine storage path.
        artifact_dir = Path(orchestrator.config.artifact_dir) / sprint_id
        artifact_dir.mkdir(parents=True, exist_ok=True)

        filename = file.filename or "upload"
        dest = artifact_dir / filename

        # Stream the upload to disk.
        size = 0
        with open(dest, "wb") as f:
            while chunk := await file.read(1024 * 256):  # 256 KB chunks
                f.write(chunk)
                size += len(chunk)

        # Record in database.
        await queries.create_artifact(
            orchestrator.db,
            sprint_id=sprint_id,
            filename=filename,
            path=str(dest),
            size=size,
            content_type=file.content_type,
        )

        logger.info(
            "Artifact %r uploaded for sprint %s (%d bytes)",
            filename,
            sprint_id,
            size,
        )
        return JSONResponse(
            {"ok": True, "filename": filename, "size": size},
            status_code=201,
        )

    # -- Read-only JSON endpoints ---------------------------------------

    @app.get("/api/sprints")
    async def list_sprints(
        study_name: str | None = None,
        limit: int = 50,
        x_shared_secret: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """List sprints, optionally filtered by study name."""
        await _check_auth(x_shared_secret, authorization)
        assert orchestrator.sprint_manager is not None
        sprints = await orchestrator.sprint_manager.list_sprints(
            study_name=study_name, limit=limit
        )
        return JSONResponse({"sprints": sprints})

    @app.get("/api/sprints/{sprint_id}")
    async def get_sprint(
        sprint_id: str,
        x_shared_secret: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """Get a single sprint by ID."""
        await _check_auth(x_shared_secret, authorization)
        assert orchestrator.sprint_manager is not None
        sprint = await orchestrator.sprint_manager.get_sprint(sprint_id)
        if sprint is None:
            raise HTTPException(status_code=404, detail="Sprint not found")
        return JSONResponse({"sprint": sprint})

    @app.get("/api/studies")
    async def list_studies(
        x_shared_secret: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """List all studies."""
        await _check_auth(x_shared_secret, authorization)
        assert orchestrator.study_manager is not None
        studies = await orchestrator.study_manager.list_all()
        return JSONResponse({"studies": studies})

    # -- Sprint / loop management API -----------------------------------

    @app.post("/api/sprints")
    async def create_sprint(
        request: Request,
        x_shared_secret: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """Create and submit a sprint."""
        await _check_auth(x_shared_secret, authorization)
        assert orchestrator.sprint_manager is not None
        body = await request.json()
        study_name = body.get("study_name", "")
        idea = body.get("idea", "")
        if not study_name or not idea:
            raise HTTPException(
                status_code=400,
                detail="study_name and idea are required",
            )
        job_options = body.get("job_options", {})
        sprint = await orchestrator.sprint_manager.run_sprint(
            study_name, idea, job_options=job_options
        )
        return JSONResponse(
            {
                "sprint_id": sprint.id,
                "study_name": sprint.study_name,
                "status": sprint.status.value,
                "job_id": sprint.job_id,
            },
            status_code=201,
        )

    @app.post("/api/sprints/{sprint_id}/cancel")
    async def cancel_sprint(
        sprint_id: str,
        x_shared_secret: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """Cancel a sprint."""
        await _check_auth(x_shared_secret, authorization)
        assert orchestrator.sprint_manager is not None
        success = await orchestrator.sprint_manager.cancel_sprint(sprint_id)
        return JSONResponse({"cancelled": success})

    @app.post("/api/loops")
    async def create_loop(
        request: Request,
        x_shared_secret: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """Start an auto-loop."""
        await _check_auth(x_shared_secret, authorization)
        assert orchestrator.auto_loop is not None
        body = await request.json()
        study_name = body.get("study_name", "")
        count = body.get("count", 5)
        if not study_name:
            raise HTTPException(
                status_code=400,
                detail="study_name is required",
            )
        context = body.get("context", "")
        loop_id = await orchestrator.auto_loop.start(study_name, count, context=context)
        return JSONResponse({"loop_id": loop_id}, status_code=201)

    @app.post("/api/loops/{loop_id}/stop")
    async def stop_loop(
        loop_id: str,
        x_shared_secret: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """Stop an auto-loop."""
        await _check_auth(x_shared_secret, authorization)
        assert orchestrator.auto_loop is not None
        await orchestrator.auto_loop.stop(loop_id)
        return JSONResponse({"stopped": True})

    # -- Slack Events API -----------------------------------------------

    @app.post("/api/slack/events")
    async def slack_events(request: Request) -> JSONResponse:
        """Handle Slack Events API callbacks."""
        raw_body = await request.body()
        body: dict[str, Any] = json.loads(raw_body)

        # URL verification challenge
        if body.get("type") == "url_verification":
            return JSONResponse({"challenge": body.get("challenge", "")})

        # Signature verification
        slack_cfg = orchestrator.config.slack
        if slack_cfg and slack_cfg.signing_secret:
            ts = request.headers.get("X-Slack-Request-Timestamp", "")
            sig = request.headers.get("X-Slack-Signature", "")
            if not verify_slack_signature(
                slack_cfg.signing_secret,
                ts,
                raw_body,
                sig,
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Invalid Slack signature",
                )

        if body.get("type") != "event_callback":
            return JSONResponse({"ok": True})

        event = body.get("event", {})
        event_type = event.get("type", "")

        # Ignore bot messages to avoid loops
        if event.get("bot_id"):
            return JSONResponse({"ok": True})

        if event_type not in ("app_mention", "message"):
            return JSONResponse({"ok": True})

        # Check if user is allowed.
        user_id: str = event.get("user", "")
        allowed = slack_cfg.allowed_user_ids if slack_cfg else []
        if allowed and user_id not in allowed:
            if slack_cfg and slack_cfg.bot_token:
                ch = event.get("channel", "")
                ts = event.get("thread_ts") or event.get("ts", "")
                n = SlackNotifier(
                    bot_token=slack_cfg.bot_token,
                    channel_id=ch,
                )
                await n._post_message(
                    "Sorry, you're not authorized to use this bot.",
                    thread_ts=ts,
                )
            return JSONResponse({"ok": True})

        text: str = event.get("text", "")
        thread_ts: str = event.get("thread_ts") or event.get("ts", "")
        channel: str = event.get("channel", "")
        channel_type: str = event.get("channel_type", "")

        # Restrict to configured channel if enabled.
        # DMs (channel_type "im") are always allowed.
        if (
            slack_cfg
            and slack_cfg.restrict_to_channel
            and slack_cfg.channel_id
            and channel != slack_cfg.channel_id
            and channel_type != "im"
        ):
            logger.debug(
                "Ignoring message in channel %s (not %s)",
                channel,
                slack_cfg.channel_id,
            )
            return JSONResponse({"ok": True})
        text_lower = text.lower().strip()

        # Handle "auth status" / "login" commands
        if any(kw in text_lower for kw in ("auth status", "auth check", "login")):
            if slack_cfg and slack_cfg.bot_token:
                from researchloop.core.auth import (
                    check_claude_auth_async,
                )

                ok, detail = await check_claude_auth_async()
                notifier = SlackNotifier(
                    bot_token=slack_cfg.bot_token,
                    channel_id=channel,
                )
                if ok:
                    msg = (
                        ":white_check_mark: Claude is"
                        f" authenticated on this server ({detail})."
                    )
                else:
                    msg = (
                        ":information_source: Claude is not"
                        " authenticated on this server"
                        " (not required — AI runs on the"
                        " HPC cluster)."
                    )
                await notifier._post_message(msg, thread_ts=thread_ts)
            return JSONResponse({"ok": True})

        # Handle "help" command.
        if text_lower == "help":
            if slack_cfg and slack_cfg.bot_token:
                notifier = SlackNotifier(
                    bot_token=slack_cfg.bot_token,
                    channel_id=channel,
                )
                await notifier._post_message(
                    "Available commands:\n"
                    "• `sprint run <study> <idea>`"
                    " — start a sprint\n"
                    "• `sprint list` — list recent sprints\n"
                    "• `loop start <study> <count>`"
                    " — start an auto-loop\n"
                    "• `auth status` — check Claude auth\n"
                    "• `help` — show this message",
                    thread_ts=thread_ts,
                )
            return JSONResponse({"ok": True})

        # Handle "sprint list" command.
        if "sprint list" in text_lower:
            if orchestrator.sprint_manager and slack_cfg and slack_cfg.bot_token:
                notifier = SlackNotifier(
                    bot_token=slack_cfg.bot_token,
                    channel_id=channel,
                )
                sprints = await orchestrator.sprint_manager.list_sprints(limit=10)
                if not sprints:
                    await notifier._post_message(
                        "No sprints found.",
                        thread_ts=thread_ts,
                    )
                else:
                    lines = [
                        f"• *{s['id']}* [{s['status']}] {(s.get('idea') or '')[:50]}"
                        for s in sprints
                    ]
                    await notifier._post_message(
                        "Recent sprints:\n" + "\n".join(lines),
                        thread_ts=thread_ts,
                    )
            return JSONResponse({"ok": True})

        # Handle "sprint run <study> <idea>" commands.
        if "sprint run" in text.lower():
            parts = text.lower().split("sprint run", 1)[1]
            tokens = parts.strip().split(None, 1)
            study_name = tokens[0] if tokens else ""
            idea = tokens[1] if len(tokens) > 1 else ""

            if (
                study_name
                and idea
                and orchestrator.sprint_manager is not None
                and slack_cfg
                and slack_cfg.bot_token
            ):
                notifier = SlackNotifier(
                    bot_token=slack_cfg.bot_token,
                    channel_id=channel,
                )
                try:
                    sprint = await orchestrator.sprint_manager.run_sprint(
                        study_name, idea
                    )
                    await notifier._post_message(
                        f"Sprint *{sprint.id}* submitted for study *{study_name}*.",
                        thread_ts=thread_ts,
                    )
                except Exception as exc:
                    await notifier._post_message(
                        f"Failed to start sprint: {exc}",
                        thread_ts=thread_ts,
                    )
                return JSONResponse({"ok": True})

        # Free-form chat — pass to Claude via ConversationManager.
        cm = orchestrator.conversation_manager
        if cm is not None and slack_cfg and slack_cfg.bot_token:
            notifier = SlackNotifier(
                bot_token=slack_cfg.bot_token,
                channel_id=channel,
            )
            try:
                response_text = await cm.handle_message(
                    thread_ts=thread_ts,
                    user_text=text,
                )
                await notifier._post_message(response_text, thread_ts=thread_ts)
            except Exception as exc:
                logger.exception("Chat handler failed: %s", exc)
                await notifier._post_message(
                    "Sorry, something went wrong. Try `help` for available commands.",
                    thread_ts=thread_ts,
                )

        return JSONResponse({"ok": True})

    # -- Dashboard HTML routes -----------------------------------------
    from researchloop.dashboard.routes import (
        add_dashboard_routes,
    )

    add_dashboard_routes(app, orchestrator)

    return app
