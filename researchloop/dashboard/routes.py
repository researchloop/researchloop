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

    password_hash = orchestrator.config.dashboard.password_hash
    session_mgr = SessionManager()

    # ----------------------------------------------------------
    # Auth helpers
    # ----------------------------------------------------------

    def _is_authenticated(request: Request) -> bool:
        """Return True if the session cookie is valid."""
        if not password_hash:
            return True
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return False
        return session_mgr.verify_token(token)

    def _require_auth(request: Request) -> bool:
        """Redirect to login if not authenticated."""
        return _is_authenticated(request)

    def _ctx(request: Request, **kwargs: object) -> dict:
        """Build a template context dict."""
        return {
            "request": request,
            "authenticated": _is_authenticated(request),
            **kwargs,
        }

    # ----------------------------------------------------------
    # Login / Logout
    # ----------------------------------------------------------

    @app.get("/dashboard/login")
    async def dashboard_login(request: Request):  # type: ignore[no-untyped-def]
        return templates.TemplateResponse(
            "login.html",
            _ctx(request, error=None),
        )

    @app.post("/dashboard/login")
    async def dashboard_login_post(request: Request):  # type: ignore[no-untyped-def]
        form = await request.form()
        pwd = form.get("password", "")

        if not password_hash or check_password(str(pwd), password_hash):
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
    # Studies
    # ----------------------------------------------------------

    @app.get("/dashboard/")
    async def dashboard_studies(request: Request):  # type: ignore[no-untyped-def]
        if not _require_auth(request):
            return RedirectResponse("/dashboard/login", status_code=303)
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
            _ctx(request, studies=study_list),
        )

    @app.get("/dashboard/studies/{name}")
    async def dashboard_study_detail(name: str, request: Request):  # type: ignore[no-untyped-def]
        if not _require_auth(request):
            return RedirectResponse("/dashboard/login", status_code=303)
        assert orchestrator.db is not None

        study = await queries.get_study(orchestrator.db, name)
        if study is None:
            raise HTTPException(status_code=404, detail="Study not found")

        sprints = await queries.list_sprints(orchestrator.db, study_name=name, limit=50)
        return templates.TemplateResponse(
            "study_detail.html",
            _ctx(request, study=study, sprints=sprints),
        )

    # ----------------------------------------------------------
    # Sprints
    # ----------------------------------------------------------

    @app.get("/dashboard/sprints")
    async def dashboard_sprints(request: Request):  # type: ignore[no-untyped-def]
        if not _require_auth(request):
            return RedirectResponse("/dashboard/login", status_code=303)
        assert orchestrator.db is not None

        sprints = await queries.list_sprints(orchestrator.db, limit=100)
        return templates.TemplateResponse(
            "sprints.html",
            _ctx(request, sprints=sprints),
        )

    @app.get("/dashboard/sprints/{sprint_id}")
    async def dashboard_sprint_detail(sprint_id: str, request: Request):  # type: ignore[no-untyped-def]
        if not _require_auth(request):
            return RedirectResponse("/dashboard/login", status_code=303)
        assert orchestrator.db is not None

        sprint = await queries.get_sprint(orchestrator.db, sprint_id)
        if sprint is None:
            raise HTTPException(
                status_code=404,
                detail="Sprint not found",
            )

        artifacts = await queries.list_artifacts(orchestrator.db, sprint_id)
        return templates.TemplateResponse(
            "sprint_detail.html",
            _ctx(
                request,
                sprint=sprint,
                artifacts=artifacts,
            ),
        )

    # ----------------------------------------------------------
    # Auto-Loops
    # ----------------------------------------------------------

    @app.get("/dashboard/loops")
    async def dashboard_loops(request: Request):  # type: ignore[no-untyped-def]
        if not _require_auth(request):
            return RedirectResponse("/dashboard/login", status_code=303)
        assert orchestrator.db is not None

        loops = await queries.list_auto_loops(orchestrator.db)
        return templates.TemplateResponse(
            "loops.html",
            _ctx(request, loops=loops),
        )

    # ----------------------------------------------------------
    # Artifact download
    # ----------------------------------------------------------

    @app.get("/dashboard/artifacts/{artifact_id}/download")
    async def dashboard_artifact_download(artifact_id: int, request: Request):  # type: ignore[no-untyped-def]
        if not _require_auth(request):
            return RedirectResponse("/dashboard/login", status_code=303)
        assert orchestrator.db is not None

        artifact = await queries.get_artifact(orchestrator.db, artifact_id)
        if artifact is None:
            raise HTTPException(
                status_code=404,
                detail="Artifact not found",
            )

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
