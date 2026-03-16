"""Tests for the dashboard web UI routes."""

from __future__ import annotations

import tempfile

from fastapi.testclient import TestClient

from researchloop.core.config import (
    ClusterConfig,
    Config,
    DashboardConfig,
    StudyConfig,
)
from researchloop.core.orchestrator import (
    Orchestrator,
    create_app,
)
from researchloop.dashboard.auth import (
    SESSION_COOKIE,
    hash_password,
)
from researchloop.db import queries


def _make_app(
    password_hash: str | None = None,
) -> tuple[TestClient, Orchestrator]:
    """Create a TestClient with dashboard routes."""
    config = Config(
        studies=[
            StudyConfig(
                name="test",
                cluster="local",
                sprints_dir="./sp",
                description="A test study",
            ),
        ],
        clusters=[
            ClusterConfig(
                name="local",
                host="localhost",
                scheduler_type="local",
            ),
        ],
        db_path=":memory:",
        artifact_dir=tempfile.mkdtemp(),
        shared_secret="test-key",
        dashboard=DashboardConfig(
            password_hash=password_hash,
        ),
    )
    orch = Orchestrator(config)
    app = create_app(orch)
    return TestClient(app), orch


class TestStudiesPage:
    def test_studies_page_returns_200(self):
        client, _ = _make_app()
        with client:
            resp = client.get(
                "/dashboard/",
                follow_redirects=False,
            )
            assert resp.status_code == 200
            assert "test" in resp.text

    def test_studies_page_shows_study(self):
        client, _ = _make_app()
        with client:
            resp = client.get("/dashboard/")
            assert "A test study" in resp.text


class TestSprintsPage:
    def test_sprints_page_returns_200(self):
        client, _ = _make_app()
        with client:
            resp = client.get("/dashboard/sprints")
            assert resp.status_code == 200
            assert "Sprints" in resp.text

    async def test_sprints_page_with_data(self):
        client, orch = _make_app()
        with client:
            await queries.create_sprint(
                orch.db,
                "sp-dash01",
                "test",
                "dashboard test idea",
            )
            resp = client.get("/dashboard/sprints")
            assert resp.status_code == 200
            assert "sp-dash01" in resp.text


class TestSprintDetailPage:
    async def test_sprint_detail_page(self):
        client, orch = _make_app()
        with client:
            await queries.create_sprint(
                orch.db,
                "sp-det01",
                "test",
                "detail test idea",
            )
            resp = client.get("/dashboard/sprints/sp-det01")
            assert resp.status_code == 200
            assert "detail test idea" in resp.text
            assert "sp-det01" in resp.text

    def test_sprint_detail_not_found(self):
        client, _ = _make_app()
        with client:
            resp = client.get("/dashboard/sprints/sp-nope")
            assert resp.status_code == 404


class TestLoginPage:
    def test_login_page_renders(self):
        client, _ = _make_app(password_hash=hash_password("secret"))
        with client:
            resp = client.get("/dashboard/login")
            assert resp.status_code == 200
            assert "Login" in resp.text
            assert "Sign in" in resp.text

    def test_login_correct_password(self):
        pw_hash = hash_password("secret")
        client, _ = _make_app(password_hash=pw_hash)
        with client:
            resp = client.post(
                "/dashboard/login",
                data={"password": "secret"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/dashboard/"
            assert SESSION_COOKIE in resp.cookies

    def test_login_incorrect_password(self):
        pw_hash = hash_password("secret")
        client, _ = _make_app(password_hash=pw_hash)
        with client:
            resp = client.post(
                "/dashboard/login",
                data={"password": "wrong"},
            )
            assert resp.status_code == 401
            assert "Invalid password" in resp.text


class TestAuthRedirect:
    def test_redirect_when_password_required(self):
        pw_hash = hash_password("secret")
        client, _ = _make_app(password_hash=pw_hash)
        with client:
            resp = client.get(
                "/dashboard/",
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "/dashboard/login" in (resp.headers.get("location", ""))

    def test_no_redirect_without_password(self):
        client, _ = _make_app()
        with client:
            resp = client.get(
                "/dashboard/",
                follow_redirects=False,
            )
            assert resp.status_code == 200

    def test_authenticated_access(self):
        pw_hash = hash_password("secret")
        client, _ = _make_app(password_hash=pw_hash)
        with client:
            # Login first
            login_resp = client.post(
                "/dashboard/login",
                data={"password": "secret"},
                follow_redirects=False,
            )
            cookie = login_resp.cookies.get(SESSION_COOKIE)
            assert cookie is not None

            # Access protected page with cookie
            resp = client.get(
                "/dashboard/",
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 200


class TestLogout:
    def test_logout_clears_cookie(self):
        pw_hash = hash_password("secret")
        client, _ = _make_app(password_hash=pw_hash)
        with client:
            resp = client.get(
                "/dashboard/logout",
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "/dashboard/login" in (resp.headers.get("location", ""))


class TestLoopsPage:
    def test_loops_page_returns_200(self):
        client, _ = _make_app()
        with client:
            resp = client.get("/dashboard/loops")
            assert resp.status_code == 200
            assert "Auto-Loops" in resp.text
