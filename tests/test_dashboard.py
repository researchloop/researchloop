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


def _make_app_with_password(
    password: str = "secret",
) -> tuple[TestClient, Orchestrator, str]:
    """Create app with a pre-set password, return client + hash."""
    pw_hash = hash_password(password)
    client, orch = _make_app(password_hash=pw_hash)
    return client, orch, pw_hash


def _login_and_csrf(
    client: TestClient,
    password: str = "secret",
) -> tuple[str, str]:
    """Log in, return (session_cookie, csrf_token)."""
    resp = client.post(
        "/dashboard/login",
        data={"password": password},
        follow_redirects=False,
    )
    cookie = resp.cookies.get(SESSION_COOKIE, "")
    # The session manager is created lazily; we need its secret_key.
    # Since we can't access it directly, compute the CSRF token from the
    # cookie by hitting a page and extracting from the HTML.  But it's
    # simpler to use the generate_csrf_token helper with the signing key
    # that was stored in the DB.  The signing key is auto-generated on
    # first access.  We can read it from the rendered page instead.
    # Actually, the easiest approach: fetch a page and parse the csrf_token.
    page = client.get(
        "/dashboard/sprints",
        cookies={SESSION_COOKIE: cookie},
    )
    import re

    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    csrf = m.group(1) if m else ""
    return cookie, csrf


class TestFirstRunSetup:
    def test_redirects_to_setup_when_no_password(self):
        client, _ = _make_app()
        with client:
            resp = client.get("/dashboard/", follow_redirects=False)
            assert resp.status_code == 303
            assert "/dashboard/setup" in resp.headers["location"]

    def test_setup_page_renders(self):
        client, _ = _make_app()
        with client:
            resp = client.get("/dashboard/setup")
            assert resp.status_code == 200
            assert "Set password" in resp.text

    def test_setup_sets_password_and_logs_in(self):
        client, _ = _make_app()
        with client:
            resp = client.post(
                "/dashboard/setup",
                data={
                    "password": "mypassword",
                    "confirm": "mypassword",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/dashboard/"
            assert SESSION_COOKIE in resp.cookies

    def test_setup_rejects_short_password(self):
        client, _ = _make_app()
        with client:
            resp = client.post(
                "/dashboard/setup",
                data={"password": "short", "confirm": "short"},
            )
            assert resp.status_code == 400
            assert "at least 8" in resp.text

    def test_setup_rejects_mismatched_passwords(self):
        client, _ = _make_app()
        with client:
            resp = client.post(
                "/dashboard/setup",
                data={
                    "password": "mypassword",
                    "confirm": "different",
                },
            )
            assert resp.status_code == 400
            assert "do not match" in resp.text

    def test_setup_blocked_after_password_set(self):
        client, _, _ = _make_app_with_password()
        with client:
            resp = client.get("/dashboard/setup", follow_redirects=False)
            assert resp.status_code == 303
            assert "/dashboard/" in resp.headers["location"]


class TestLoginPage:
    def test_login_page_renders(self):
        client, _, _ = _make_app_with_password()
        with client:
            resp = client.get("/dashboard/login")
            assert resp.status_code == 200
            assert "Sign in" in resp.text

    def test_login_redirects_to_setup_if_no_password(self):
        client, _ = _make_app()
        with client:
            resp = client.get("/dashboard/login", follow_redirects=False)
            assert resp.status_code == 303
            assert "/dashboard/setup" in resp.headers["location"]

    def test_login_correct_password(self):
        client, _, _ = _make_app_with_password("secret")
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
        client, _, _ = _make_app_with_password("secret")
        with client:
            resp = client.post(
                "/dashboard/login",
                data={"password": "wrong"},
            )
            assert resp.status_code == 401
            assert "Invalid password" in resp.text


class TestAuthRedirect:
    def test_redirect_when_password_required(self):
        client, _, _ = _make_app_with_password()
        with client:
            resp = client.get("/dashboard/", follow_redirects=False)
            assert resp.status_code == 303
            assert "/dashboard/login" in resp.headers["location"]

    def test_authenticated_access(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            login_resp = client.post(
                "/dashboard/login",
                data={"password": "secret"},
                follow_redirects=False,
            )
            cookie = login_resp.cookies.get(SESSION_COOKIE)
            assert cookie is not None

            resp = client.get(
                "/dashboard/",
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 200


class TestStudiesPage:
    def test_studies_page_shows_study(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            login_resp = client.post(
                "/dashboard/login",
                data={"password": "secret"},
                follow_redirects=False,
            )
            cookie = login_resp.cookies.get(SESSION_COOKIE)
            resp = client.get(
                "/dashboard/",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 200
            assert "A test study" in resp.text


class TestSprintsPage:
    async def test_sprints_page_with_data(self):
        client, orch, _ = _make_app_with_password("secret")
        with client:
            login_resp = client.post(
                "/dashboard/login",
                data={"password": "secret"},
                follow_redirects=False,
            )
            cookie = login_resp.cookies.get(SESSION_COOKIE)
            await queries.create_sprint(
                orch.db,
                "sp-dash01",
                "test",
                "dashboard test idea",
            )
            resp = client.get(
                "/dashboard/sprints",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 200
            assert "sp-dash01" in resp.text

    async def test_new_sprint_form_defaults_to_last_submitted_study(self):
        client, orch, _ = _make_app_with_password("secret")
        with client:
            cookie, _ = _login_and_csrf(client, "secret")
            # Add a second study so there's more than one option.
            await queries.create_study(
                orch.db,
                name="other",
                cluster="local",
                description=None,
                claude_md_path=None,
                sprints_dir="./sp-other",
            )
            # Most recent sprint is against "other".
            await queries.create_sprint(orch.db, "sp-earlier", "test", "idea1")
            await queries.create_sprint(orch.db, "sp-latest", "other", "idea2")
            await queries.update_sprint(
                orch.db, "sp-earlier", created_at="2026-01-01 00:00:00"
            )
            await queries.update_sprint(
                orch.db, "sp-latest", created_at="2026-04-19 12:00:00"
            )

            resp = client.get(
                "/dashboard/sprints",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 200
            assert '<option value="other" selected>other</option>' in resp.text
            assert '<option value="test">test</option>' in resp.text

    async def test_new_sprint_form_no_default_when_no_sprints(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            cookie, _ = _login_and_csrf(client, "secret")
            resp = client.get(
                "/dashboard/sprints",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 200
            assert " selected>" not in resp.text


class TestSprintDetailPage:
    async def test_sprint_detail_page(self):
        client, orch, _ = _make_app_with_password("secret")
        with client:
            login_resp = client.post(
                "/dashboard/login",
                data={"password": "secret"},
                follow_redirects=False,
            )
            cookie = login_resp.cookies.get(SESSION_COOKIE)
            await queries.create_sprint(
                orch.db,
                "sp-det01",
                "test",
                "detail test idea",
            )
            resp = client.get(
                "/dashboard/sprints/sp-det01",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 200
            assert "detail test idea" in resp.text

    def test_sprint_detail_not_found(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            login_resp = client.post(
                "/dashboard/login",
                data={"password": "secret"},
                follow_redirects=False,
            )
            cookie = login_resp.cookies.get(SESSION_COOKIE)
            resp = client.get(
                "/dashboard/sprints/sp-nope",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 404


class TestLogout:
    def test_logout_clears_cookie(self):
        client, _, _ = _make_app_with_password()
        with client:
            resp = client.get("/dashboard/logout", follow_redirects=False)
            assert resp.status_code == 303
            assert "/dashboard/login" in resp.headers["location"]


class TestSprintCancel:
    """POST /dashboard/sprints/{id}/cancel redirects."""

    async def test_cancel_sprint_redirects(self):
        from unittest.mock import AsyncMock, patch

        client, orch, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")

            await queries.create_sprint(
                orch.db,
                "sp-cancel01",
                "test",
                "cancel me",
            )

            with patch.object(
                orch.sprint_manager,
                "cancel_sprint",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_cancel:
                resp = client.post(
                    "/dashboard/sprints/sp-cancel01/cancel",
                    data={"csrf_token": csrf},
                    cookies={SESSION_COOKIE: cookie},
                    follow_redirects=False,
                )
                assert resp.status_code == 303
                loc = resp.headers["location"]
                assert "sp-cancel01" in loc
                mock_cancel.assert_called_once_with("sp-cancel01")

    def test_cancel_unauthenticated_redirects(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            resp = client.post(
                "/dashboard/sprints/sp-x/cancel",
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "/login" in resp.headers["location"]


class TestSprintDelete:
    """POST /dashboard/sprints/{id}/delete."""

    async def test_delete_sprint_redirects(self):
        client, orch, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")

            await queries.create_sprint(
                orch.db,
                "sp-del01",
                "test",
                "delete me",
            )

            resp = client.post(
                "/dashboard/sprints/sp-del01/delete",
                data={"csrf_token": csrf},
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "/dashboard/sprints" in resp.headers["location"]

            # Sprint should be gone.
            sprint = await queries.get_sprint(orch.db, "sp-del01")
            assert sprint is None

    def test_delete_unauthenticated_redirects(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            resp = client.post(
                "/dashboard/sprints/sp-x/delete",
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "/login" in resp.headers["location"]


class TestStudyManagementUI:
    """CRUD routes for studies in the dashboard."""

    def _base_form(self, **overrides) -> dict[str, str]:
        data = {
            "name": "ui-one",
            "cluster": "local",
            "description": "Built in UI",
            "sprints_dir": "./sp",
            "claude_md_path": "",
            "context": "",
            "claude_command": "",
            "gpu": "",
            "mem": "",
            "cpus": "",
            "job_options_json": "",
            "max_sprint_duration_hours": "8",
            "red_team_max_rounds": "3",
            "allow_loop": "on",
        }
        data.update(overrides)
        return data

    def test_new_study_form_renders(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            cookie, _ = _login_and_csrf(client, "secret")
            resp = client.get(
                "/dashboard/studies/new",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 200
            assert "New study" in resp.text
            assert 'name="name"' in resp.text
            assert "local" in resp.text  # cluster dropdown

    def test_create_ui_study_success(self):
        client, orch, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            form = self._base_form(csrf_token=csrf)
            resp = client.post(
                "/dashboard/studies",
                data=form,
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/dashboard/studies/ui-one"
            names = {s.name for s in orch.config.studies}
            assert "ui-one" in names

    def test_create_ui_study_rejects_unknown_cluster(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            form = self._base_form(csrf_token=csrf, cluster="nope")
            resp = client.post(
                "/dashboard/studies",
                data=form,
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 400
            assert "Cluster" in resp.text

    def test_create_ui_study_rejects_bad_name(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            form = self._base_form(csrf_token=csrf, name="Bad Name")
            resp = client.post(
                "/dashboard/studies",
                data=form,
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 400
            assert "Name" in resp.text

    def test_create_ui_study_rejects_duplicate(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            # Attempt to create a study with the same name as the YAML study.
            form = self._base_form(csrf_token=csrf, name="test")
            resp = client.post(
                "/dashboard/studies",
                data=form,
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 400
            assert "already exists" in resp.text

    def test_create_ui_study_rejects_bad_json(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            form = self._base_form(
                csrf_token=csrf,
                job_options_json="not json",
            )
            resp = client.post(
                "/dashboard/studies",
                data=form,
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 400
            assert "JSON" in resp.text

    def test_create_ui_study_with_advanced_job_options(self):
        client, orch, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            form = self._base_form(
                csrf_token=csrf,
                gpu="gpu:a100:1",
                mem="128G",
                job_options_json='{"time": "4:00:00"}',
            )
            resp = client.post(
                "/dashboard/studies",
                data=form,
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            study = next(s for s in orch.config.studies if s.name == "ui-one")
            assert study.job_options["gres"] == "gpu:a100:1"
            assert study.job_options["mem"] == "128G"
            assert study.job_options["time"] == "4:00:00"

    def test_edit_form_renders(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            cookie, _ = _login_and_csrf(client, "secret")
            resp = client.get(
                "/dashboard/studies/test/edit",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 200
            assert "readonly" in resp.text  # name is readonly on edit
            assert "A test study" in resp.text

    def test_edit_form_populates_all_yaml_fields(self):
        """Every YAML-defined field should appear as an input value so users
        can make small tweaks without consulting the TOML."""
        config = Config(
            studies=[
                StudyConfig(
                    name="rich",
                    cluster="local",
                    description="Rich study",
                    claude_md_path="./studies/rich/CLAUDE.md",
                    context="# Multi-line\ncontext value\n",
                    sprints_dir="/scratch/rich",
                    claude_command="claude --custom",
                    job_options={
                        "gres": "gpu:a100:2",
                        "mem": "128G",
                        "cpus-per-task": "16",
                        "time": "12:00:00",
                    },
                    max_sprint_duration_hours=12,
                    red_team_max_rounds=5,
                    allow_loop=False,
                ),
            ],
            clusters=[
                ClusterConfig(name="local", host="localhost", scheduler_type="local"),
            ],
            db_path=":memory:",
            artifact_dir=tempfile.mkdtemp(),
            shared_secret="test-key",
            dashboard=DashboardConfig(password_hash=hash_password("secret")),
        )
        orch = Orchestrator(config)
        app = create_app(orch)
        client = TestClient(app)
        with client:
            cookie, _ = _login_and_csrf(client, "secret")
            resp = client.get(
                "/dashboard/studies/rich/edit",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 200
            text = resp.text
            # Friendly fields prefilled from job_options
            assert 'value="gpu:a100:2"' in text
            assert 'value="128G"' in text
            assert 'value="16"' in text
            # Other TOML values
            assert 'value="Rich study"' in text
            assert 'value="./studies/rich/CLAUDE.md"' in text
            assert 'value="/scratch/rich"' in text
            assert 'value="claude --custom"' in text
            # Multi-line context rendered inside textarea
            assert "# Multi-line" in text
            assert "context value" in text
            # Numeric + boolean fields
            assert 'value="12"' in text  # max_sprint_duration_hours
            assert 'value="5"' in text  # red_team_max_rounds
            # allow_loop=False → checkbox NOT checked
            assert 'name="allow_loop"' in text
            # 'time' key from advanced job_options is surfaced in JSON textarea
            assert "12:00:00" in text

    def test_edit_form_handles_yaml_study_without_sprints_dir(self):
        """Server studies often omit sprints_dir; editing must still work."""
        config = Config(
            studies=[
                StudyConfig(
                    name="no-dir",
                    cluster="local",
                    description="No sprints dir",
                    sprints_dir="",
                ),
            ],
            clusters=[
                ClusterConfig(name="local", host="localhost", scheduler_type="local"),
            ],
            db_path=":memory:",
            artifact_dir=tempfile.mkdtemp(),
            shared_secret="test-key",
            dashboard=DashboardConfig(password_hash=hash_password("secret")),
        )
        orch = Orchestrator(config)
        app = create_app(orch)
        client = TestClient(app)
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            # Editing without setting sprints_dir should succeed.
            form = self._base_form(
                csrf_token=csrf,
                name="no-dir",
                sprints_dir="",
                description="Tweaked",
            )
            resp = client.post(
                "/dashboard/studies/no-dir/edit",
                data=form,
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            study = next(s for s in orch.config.studies if s.name == "no-dir")
            assert study.description == "Tweaked"
            assert study.sprints_dir == ""

    def test_edit_study_updates_fields(self):
        client, orch, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            form = self._base_form(
                csrf_token=csrf,
                name="test",
                description="Edited via UI",
            )
            resp = client.post(
                "/dashboard/studies/test/edit",
                data=form,
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            study = next(s for s in orch.config.studies if s.name == "test")
            assert study.description == "Edited via UI"

    def test_edit_rejects_rename(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            form = self._base_form(csrf_token=csrf, name="renamed")
            resp = client.post(
                "/dashboard/studies/test/edit",
                data=form,
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 400
            assert "Renaming" in resp.text

    def test_edit_shows_revert_button_for_edited_yaml_study(self):
        client, orch, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            form = self._base_form(
                csrf_token=csrf,
                name="test",
                description="Edited",
            )
            client.post(
                "/dashboard/studies/test/edit",
                data=form,
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            resp = client.get(
                "/dashboard/studies/test",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 200
            assert "Revert to YAML" in resp.text
            assert "(edited)" in resp.text

    def test_revert_restores_yaml(self):
        client, orch, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            form = self._base_form(
                csrf_token=csrf,
                name="test",
                description="Edited",
            )
            client.post(
                "/dashboard/studies/test/edit",
                data=form,
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            # Now revert.
            resp = client.post(
                "/dashboard/studies/test/revert",
                data={"csrf_token": csrf},
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            study = next(s for s in orch.config.studies if s.name == "test")
            assert study.description == "A test study"

    def test_revert_rejected_for_ui_only_study(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            # Create a UI-only study first.
            client.post(
                "/dashboard/studies",
                data=self._base_form(csrf_token=csrf),
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            resp = client.post(
                "/dashboard/studies/ui-one/revert",
                data={"csrf_token": csrf},
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=" in resp.headers["location"]

    def test_delete_ui_study_success(self):
        client, orch, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            client.post(
                "/dashboard/studies",
                data=self._base_form(csrf_token=csrf),
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            resp = client.post(
                "/dashboard/studies/ui-one/delete",
                data={"csrf_token": csrf},
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/dashboard/"
            assert await_get_study_none(orch, "ui-one")

    def test_delete_yaml_study_rejected(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            resp = client.post(
                "/dashboard/studies/test/delete",
                data={"csrf_token": csrf},
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=" in resp.headers["location"]
            assert "/dashboard/studies/test" in resp.headers["location"]

    async def test_delete_rejected_when_sprints_exist(self):
        client, orch, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            client.post(
                "/dashboard/studies",
                data=self._base_form(csrf_token=csrf),
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            await queries.create_sprint(orch.db, "sp-blk", "ui-one", "blk")
            resp = client.post(
                "/dashboard/studies/ui-one/delete",
                data={"csrf_token": csrf},
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=" in resp.headers["location"]

    def test_csrf_required_on_create(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            cookie, _ = _login_and_csrf(client, "secret")
            resp = client.post(
                "/dashboard/studies",
                data=self._base_form(),
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 403

    def test_csrf_required_on_delete(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            cookie, _ = _login_and_csrf(client, "secret")
            resp = client.post(
                "/dashboard/studies/test/delete",
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            assert resp.status_code == 403

    def test_ui_study_visible_on_studies_page(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            cookie, csrf = _login_and_csrf(client, "secret")
            client.post(
                "/dashboard/studies",
                data=self._base_form(csrf_token=csrf),
                cookies={SESSION_COOKIE: cookie},
                follow_redirects=False,
            )
            resp = client.get(
                "/dashboard/",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 200
            assert "ui-one" in resp.text
            assert "Built in UI" in resp.text

    def test_unauthenticated_new_study_form_redirects(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            resp = client.get("/dashboard/studies/new", follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers["location"]


def await_get_study_none(orch, name: str) -> bool:
    """Helper: synchronously check that a study is gone from in-memory config."""
    return all(s.name != name for s in orch.config.studies)


class TestLoopDetailPage:
    """GET /dashboard/loops/{id}."""

    async def test_loop_detail_page(self):
        client, orch, _ = _make_app_with_password("secret")
        with client:
            login_resp = client.post(
                "/dashboard/login",
                data={"password": "secret"},
                follow_redirects=False,
            )
            cookie = login_resp.cookies.get(SESSION_COOKIE)

            await queries.create_auto_loop(
                orch.db,
                "loop-detail01",
                "test",
                5,
            )

            resp = client.get(
                "/dashboard/loops/loop-detail01",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 200
            assert "loop-detail01" in resp.text
            assert "test" in resp.text

    def test_loop_detail_not_found(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            login_resp = client.post(
                "/dashboard/login",
                data={"password": "secret"},
                follow_redirects=False,
            )
            cookie = login_resp.cookies.get(SESSION_COOKIE)
            resp = client.get(
                "/dashboard/loops/loop-nope",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 404

    def test_loop_detail_unauthenticated(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            resp = client.get(
                "/dashboard/loops/loop-x",
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "/login" in resp.headers["location"]

    async def test_loop_detail_renders_inplace_refresh_wiring(self):
        """Sprint rows on loop detail must carry the data-cell markers and
        button hook used by the in-place refresh JS, so the idea/status/summary
        cells get updated without a full page reload.
        """
        client, orch, _ = _make_app_with_password("secret")
        with client:
            login_resp = client.post(
                "/dashboard/login",
                data={"password": "secret"},
                follow_redirects=False,
            )
            cookie = login_resp.cookies.get(SESSION_COOKIE)

            await queries.create_auto_loop(orch.db, "loop-rfr01", "test", 5)
            sp = await queries.create_sprint(orch.db, "sp-rfr01", "test", idea=None)
            await queries.update_sprint(
                orch.db,
                sp["id"],
                status="running",
                loop_id="loop-rfr01",
            )

            resp = client.get(
                "/dashboard/loops/loop-rfr01",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 200
            html = resp.text
            assert 'id="row-sp-rfr01"' in html
            assert 'data-cell="status"' in html
            assert 'data-cell="idea"' in html
            assert 'data-cell="summary"' in html
            assert "refreshSprintRow('sp-rfr01', this)" in html
            assert "function refreshSprintRow(" in html


class TestLoopsPage:
    def test_loops_page_returns_200(self):
        client, _, _ = _make_app_with_password("secret")
        with client:
            login_resp = client.post(
                "/dashboard/login",
                data={"password": "secret"},
                follow_redirects=False,
            )
            cookie = login_resp.cookies.get(SESSION_COOKIE)
            resp = client.get(
                "/dashboard/loops",
                cookies={SESSION_COOKIE: cookie},
            )
            assert resp.status_code == 200
            assert "Auto-Loops" in resp.text
