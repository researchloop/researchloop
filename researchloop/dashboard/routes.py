"""Dashboard HTML routes for the web UI."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
)
from starlette.templating import Jinja2Templates

from researchloop.dashboard.auth import (
    SESSION_COOKIE,
    SessionManager,
    check_password,
    hash_password,
)
from researchloop.db import queries

if TYPE_CHECKING:
    from researchloop.core.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def add_dashboard_routes(
    app: FastAPI,
    orchestrator: Orchestrator,
) -> None:
    """Register all dashboard HTML routes on *app*."""

    session_mgr = SessionManager(secret_key=orchestrator.config.shared_secret or None)

    # ----------------------------------------------------------
    # Password resolution — config, env, or DB
    # ----------------------------------------------------------

    async def _get_password_hash() -> str | None:
        """Get password hash from config or DB settings."""
        # Config / env var takes priority
        cfg_hash = orchestrator.config.dashboard.password_hash
        if cfg_hash:
            return cfg_hash
        # Fall back to DB
        if orchestrator.db is not None:
            row = await orchestrator.db.fetch_one(
                "SELECT value FROM settings WHERE key = ?",
                ("dashboard_password_hash",),
            )
            if row:
                return row["value"]
        return None

    async def _set_password_hash(pw_hash: str) -> None:
        """Store password hash in the DB settings table."""
        if orchestrator.db is None:
            return
        await orchestrator.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("dashboard_password_hash", pw_hash),
        )

    # ----------------------------------------------------------
    # Auth helpers
    # ----------------------------------------------------------

    async def _is_authenticated(request: Request) -> bool:
        pw_hash = await _get_password_hash()
        if not pw_hash:
            return False  # no password = needs setup
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return False
        return session_mgr.verify_token(token)

    async def _needs_setup() -> bool:
        return await _get_password_hash() is None

    def _ctx(request: Request, authenticated: bool = False, **kwargs: object) -> dict:
        return {
            "request": request,
            "authenticated": authenticated,
            **kwargs,
        }

    # ----------------------------------------------------------
    # Setup (first run)
    # ----------------------------------------------------------

    @app.get("/dashboard/setup")
    async def dashboard_setup(request: Request):  # type: ignore[no-untyped-def]
        if not await _needs_setup():
            return RedirectResponse("/dashboard/", status_code=303)
        return templates.TemplateResponse("setup.html", _ctx(request, error=None))

    @app.post("/dashboard/setup")
    async def dashboard_setup_post(request: Request):  # type: ignore[no-untyped-def]
        if not await _needs_setup():
            return RedirectResponse("/dashboard/", status_code=303)

        form = await request.form()
        password = str(form.get("password", ""))
        confirm = str(form.get("confirm", ""))

        if len(password) < 8:
            return templates.TemplateResponse(
                "setup.html",
                _ctx(
                    request,
                    error="Password must be at least 8 characters",
                ),
                status_code=400,
            )

        if password != confirm:
            return templates.TemplateResponse(
                "setup.html",
                _ctx(request, error="Passwords do not match"),
                status_code=400,
            )

        pw_hash = hash_password(password)
        await _set_password_hash(pw_hash)

        logger.info("Dashboard password set via first-run setup")

        # Auto-login after setup
        token = session_mgr.create_token()
        response = RedirectResponse("/dashboard/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
        )
        return response

    # ----------------------------------------------------------
    # Login / Logout
    # ----------------------------------------------------------

    @app.get("/dashboard/login")
    async def dashboard_login(request: Request):  # type: ignore[no-untyped-def]
        if await _needs_setup():
            return RedirectResponse("/dashboard/setup", status_code=303)
        return templates.TemplateResponse("login.html", _ctx(request, error=None))

    @app.post("/dashboard/login")
    async def dashboard_login_post(request: Request):  # type: ignore[no-untyped-def]
        if await _needs_setup():
            return RedirectResponse("/dashboard/setup", status_code=303)

        form = await request.form()
        pwd = str(form.get("password", ""))
        pw_hash = await _get_password_hash()

        if pw_hash and check_password(pwd, pw_hash):
            token = session_mgr.create_token()
            response = RedirectResponse("/dashboard/", status_code=303)
            response.set_cookie(
                SESSION_COOKIE,
                token,
                httponly=True,
                samesite="lax",
            )
            return response

        return templates.TemplateResponse(
            "login.html",
            _ctx(request, error="Invalid password"),
            status_code=401,
        )

    @app.get("/dashboard/logout")
    async def dashboard_logout():  # type: ignore[no-untyped-def]
        response = RedirectResponse("/dashboard/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    # ----------------------------------------------------------
    # Auth gate for all pages below
    # ----------------------------------------------------------

    async def _gate(request: Request):  # type: ignore[no-untyped-def]
        """Redirect to setup or login if needed."""
        if await _needs_setup():
            return RedirectResponse("/dashboard/setup", status_code=303)
        if not await _is_authenticated(request):
            return RedirectResponse("/dashboard/login", status_code=303)
        return None

    # ----------------------------------------------------------
    # Studies
    # ----------------------------------------------------------

    @app.get("/dashboard/")
    async def dashboard_studies(request: Request):  # type: ignore[no-untyped-def]
        if redir := await _gate(request):
            return redir
        assert orchestrator.db is not None

        rows = await queries.list_studies(orchestrator.db)
        study_list = []
        for s in rows:
            sprints = await queries.list_sprints(
                orchestrator.db,
                study_name=s["name"],
                limit=10000,
            )
            study_list.append(
                {
                    "name": s["name"],
                    "cluster": s.get("cluster", ""),
                    "description": s.get("description", ""),
                    "sprint_count": len(sprints),
                }
            )
        return templates.TemplateResponse(
            "studies.html",
            _ctx(request, authenticated=True, studies=study_list),
        )

    @app.get("/dashboard/studies/{name}")
    async def dashboard_study_detail(name: str, request: Request):  # type: ignore[no-untyped-def]
        if redir := await _gate(request):
            return redir
        assert orchestrator.db is not None

        study = await queries.get_study(orchestrator.db, name)
        if study is None:
            raise HTTPException(status_code=404, detail="Study not found")

        sprints = await queries.list_sprints(orchestrator.db, study_name=name, limit=50)
        return templates.TemplateResponse(
            "study_detail.html",
            _ctx(
                request,
                authenticated=True,
                study=study,
                sprints=sprints,
            ),
        )

    # ----------------------------------------------------------
    # Sprints
    # ----------------------------------------------------------

    @app.get("/dashboard/sprints")
    async def dashboard_sprints(request: Request):  # type: ignore[no-untyped-def]
        if redir := await _gate(request):
            return redir
        assert orchestrator.db is not None

        sprints = await queries.list_sprints(orchestrator.db, limit=100)
        study_rows = await queries.list_studies(orchestrator.db)
        study_names = [s["name"] for s in study_rows]
        return templates.TemplateResponse(
            "sprints.html",
            _ctx(
                request,
                authenticated=True,
                sprints=sprints,
                studies=study_names,
            ),
        )

    @app.get("/dashboard/sprints/{sprint_id}")
    async def dashboard_sprint_detail(sprint_id: str, request: Request):  # type: ignore[no-untyped-def]
        if redir := await _gate(request):
            return redir
        assert orchestrator.db is not None

        sprint = await queries.get_sprint(orchestrator.db, sprint_id)
        if sprint is None:
            raise HTTPException(status_code=404, detail="Sprint not found")

        artifacts = await queries.list_artifacts(orchestrator.db, sprint_id)
        return templates.TemplateResponse(
            "sprint_detail.html",
            _ctx(
                request,
                authenticated=True,
                sprint=sprint,
                artifacts=artifacts,
            ),
        )

    # ----------------------------------------------------------
    # Sprint actions
    # ----------------------------------------------------------

    @app.post("/dashboard/sprints/{sprint_id}/refresh")
    async def dashboard_sprint_refresh(sprint_id: str, request: Request):  # type: ignore[no-untyped-def]
        """Check real job status on the cluster and update."""
        if redir := await _gate(request):
            return redir
        assert orchestrator.db is not None
        assert orchestrator.sprint_manager is not None

        sprint = await queries.get_sprint(orchestrator.db, sprint_id)
        if sprint and sprint.get("job_id"):
            try:
                # Resolve cluster config
                study_name = sprint["study_name"]
                cluster_cfg = None
                if orchestrator.study_manager:
                    cluster_cfg = await orchestrator.study_manager.get_cluster_config(
                        study_name
                    )

                if cluster_cfg:
                    scheduler = orchestrator.sprint_manager.schedulers.get(
                        cluster_cfg.name
                    ) or orchestrator.sprint_manager.schedulers.get(
                        cluster_cfg.scheduler_type
                    )
                    if scheduler:
                        mgr = orchestrator.sprint_manager
                        conn = {
                            "host": cluster_cfg.host,
                            "port": cluster_cfg.port,
                            "user": cluster_cfg.user,
                            "key_path": cluster_cfg.key_path,
                        }
                        ssh = await mgr.ssh_manager.get_connection(conn)
                        real_status = await scheduler.status(ssh, sprint["job_id"])

                        terminal = {
                            "completed",
                            "failed",
                            "cancelled",
                        }
                        cur = sprint["status"]
                        if real_status in terminal and cur not in terminal:
                            from datetime import (
                                datetime,
                                timezone,
                            )

                            now = datetime.now(timezone.utc).isoformat()
                            await queries.update_sprint(
                                orchestrator.db,
                                sprint_id,
                                status=real_status,
                                completed_at=now,
                            )

                        # Read the SLURM log.
                        sp_dir = sprint.get("directory", "")
                        wd = cluster_cfg.working_dir
                        log_pat = f"{wd}/{sp_dir}/slurm-*.out"
                        stdout, _, _ = await ssh.run(
                            f"tail -50 {log_pat} 2>/dev/null || echo '(no log found)'"
                        )
                        if stdout.strip():
                            err = f"[{real_status}] Last 50 lines:\n{stdout.strip()}"
                            await queries.update_sprint(
                                orchestrator.db,
                                sprint_id,
                                error=err if real_status in terminal else None,
                            )
            except Exception as exc:
                logger.warning("Refresh status failed: %s", exc)

        return RedirectResponse(
            f"/dashboard/sprints/{sprint_id}",
            status_code=303,
        )

    @app.post("/dashboard/sprints/{sprint_id}/cancel")
    async def dashboard_sprint_cancel(sprint_id: str, request: Request):  # type: ignore[no-untyped-def]
        if redir := await _gate(request):
            return redir
        assert orchestrator.sprint_manager is not None
        try:
            await orchestrator.sprint_manager.cancel_sprint(sprint_id)
        except Exception as exc:
            logger.warning("Cancel failed: %s", exc)
        return RedirectResponse(
            f"/dashboard/sprints/{sprint_id}",
            status_code=303,
        )

    @app.post("/dashboard/sprints/{sprint_id}/delete")
    async def dashboard_sprint_delete(sprint_id: str, request: Request):  # type: ignore[no-untyped-def]
        if redir := await _gate(request):
            return redir
        assert orchestrator.db is not None
        await queries.delete_sprint(orchestrator.db, sprint_id)
        return RedirectResponse("/dashboard/sprints", status_code=303)

    @app.post("/dashboard/sprints/new")
    async def dashboard_sprint_new(request: Request):  # type: ignore[no-untyped-def]
        if redir := await _gate(request):
            return redir
        assert orchestrator.sprint_manager is not None

        form = await request.form()
        study_name = str(form.get("study_name", ""))
        idea = str(form.get("idea", "")).strip()

        if not study_name or not idea:
            return RedirectResponse("/dashboard/sprints", status_code=303)

        try:
            sprint = await orchestrator.sprint_manager.run_sprint(study_name, idea)
            return RedirectResponse(
                f"/dashboard/sprints/{sprint.id}",
                status_code=303,
            )
        except Exception as exc:
            logger.warning("Sprint submission failed: %s", exc)
            return RedirectResponse("/dashboard/sprints", status_code=303)

    @app.post("/dashboard/studies/{name}/sprint")
    async def dashboard_study_sprint(name: str, request: Request):  # type: ignore[no-untyped-def]
        if redir := await _gate(request):
            return redir
        assert orchestrator.sprint_manager is not None

        form = await request.form()
        idea = str(form.get("idea", "")).strip()

        if not idea:
            return RedirectResponse(
                f"/dashboard/studies/{name}",
                status_code=303,
            )

        try:
            sprint = await orchestrator.sprint_manager.run_sprint(name, idea)
            return RedirectResponse(
                f"/dashboard/sprints/{sprint.id}",
                status_code=303,
            )
        except Exception as exc:
            logger.warning("Sprint submission failed: %s", exc)
            return RedirectResponse(
                f"/dashboard/studies/{name}",
                status_code=303,
            )

    # ----------------------------------------------------------
    # Auto-Loops
    # ----------------------------------------------------------

    @app.get("/dashboard/loops")
    async def dashboard_loops(request: Request):  # type: ignore[no-untyped-def]
        if redir := await _gate(request):
            return redir
        assert orchestrator.db is not None

        loops = await queries.list_auto_loops(orchestrator.db)
        return templates.TemplateResponse(
            "loops.html",
            _ctx(request, authenticated=True, loops=loops),
        )

    # ----------------------------------------------------------
    # Artifact download
    # ----------------------------------------------------------

    @app.get("/dashboard/artifacts/{artifact_id}/download")
    async def dashboard_artifact_download(artifact_id: int, request: Request):  # type: ignore[no-untyped-def]
        if redir := await _gate(request):
            return redir
        assert orchestrator.db is not None

        artifact = await queries.get_artifact(orchestrator.db, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Artifact not found")

        file_path = Path(artifact["path"])
        if not file_path.exists():
            raise HTTPException(
                status_code=404,
                detail="Artifact file not found on disk",
            )

        return FileResponse(
            path=str(file_path),
            filename=artifact["filename"],
        )
