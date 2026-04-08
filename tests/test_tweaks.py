"""Tests for the quick tweak feature."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

from researchloop.core.config import (
    ClusterConfig,
    Config,
    StudyConfig,
)
from researchloop.core.models import generate_tweak_id
from researchloop.db import queries
from researchloop.sprints.manager import SprintManager
from researchloop.studies.manager import StudyManager


def _tweak_config(tmp_path: Path) -> Config:
    """Config with slurm scheduler (has a job template)."""
    return Config(
        studies=[
            StudyConfig(
                name="test-study",
                cluster="local",
                sprints_dir=str(tmp_path / "sprints"),
            ),
        ],
        clusters=[
            ClusterConfig(
                name="local",
                host="localhost",
                scheduler_type="slurm",
                working_dir=str(tmp_path / "work"),
            ),
        ],
        db_path=":memory:",
        artifact_dir=str(tmp_path / "artifacts"),
        shared_secret="test",
        orchestrator_url="http://localhost:8080",
    )


# ---------------------------------------------------------------------------
# Model helpers
# ---------------------------------------------------------------------------


class TestTweakId:
    def test_generate_tweak_id_format(self):
        tid = generate_tweak_id()
        assert tid.startswith("tw-")
        assert len(tid) == 9  # tw- + 6 hex chars

    def test_generate_tweak_id_unique(self):
        ids = {generate_tweak_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------


class TestTweakQueries:
    async def test_create_tweak(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await queries.update_sprint(db_with_study, sprint.id, status="completed")

        row = await queries.create_tweak(
            db_with_study, "tw-abc123", sprint.id, "fix the plots"
        )
        assert row["id"] == "tw-abc123"
        assert row["sprint_id"] == sprint.id
        assert row["instruction"] == "fix the plots"
        assert row["status"] == "pending"

    async def test_get_tweak(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await queries.create_tweak(db_with_study, "tw-get001", sprint.id, "instruction")
        row = await queries.get_tweak(db_with_study, "tw-get001")
        assert row is not None
        assert row["id"] == "tw-get001"

    async def test_get_tweak_nonexistent(self, db_with_study):
        assert await queries.get_tweak(db_with_study, "tw-nope") is None

    async def test_list_tweaks(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await queries.create_tweak(db_with_study, "tw-list01", sprint.id, "first")
        await queries.create_tweak(db_with_study, "tw-list02", sprint.id, "second")
        tweaks = await queries.list_tweaks(db_with_study, sprint.id)
        assert len(tweaks) == 2

    async def test_update_tweak(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await queries.create_tweak(db_with_study, "tw-upd001", sprint.id, "instruction")
        await queries.update_tweak(
            db_with_study, "tw-upd001", status="completed", job_id="12345"
        )
        row = await queries.get_tweak(db_with_study, "tw-upd001")
        assert row is not None
        assert row["status"] == "completed"
        assert row["job_id"] == "12345"


# ---------------------------------------------------------------------------
# SprintManager tweak methods
# ---------------------------------------------------------------------------


class TestSubmitTweak:
    async def test_submit_tweak_on_completed_sprint(self, db_with_study, tmp_path):
        """Happy path: submit a tweak on a completed sprint."""
        config = _tweak_config(tmp_path)
        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "999"

        study_mgr = StudyManager(db_with_study, config)

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
            study_manager=study_mgr,
        )
        sprint = await mgr.create_sprint("test-study", "original idea")
        await queries.update_sprint(db_with_study, sprint.id, status="completed")

        tweak_id = await mgr.submit_tweak(sprint.id, "fix the axis labels")

        assert tweak_id.startswith("tw-")
        tweak = await queries.get_tweak(db_with_study, tweak_id)
        assert tweak is not None
        assert tweak["status"] == "submitted"
        assert tweak["job_id"] == "999"
        assert tweak["instruction"] == "fix the axis labels"

        # Verify SSH calls were made.
        assert ssh_mock.run.call_count >= 2  # write script + chmod
        scheduler.submit.assert_called_once()

    async def test_submit_tweak_rejects_non_completed_sprint(
        self, db_with_study, sample_config
    ):
        """Should raise ValueError for non-completed sprints."""
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        # Sprint is in "pending" status.
        try:
            await mgr.submit_tweak(sprint.id, "some tweak")
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "not completed" in str(e)

    async def test_submit_tweak_rejects_active_tweak(self, db_with_study, tmp_path):
        """Should reject if there's already an active tweak."""
        config = _tweak_config(tmp_path)
        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "999"

        study_mgr = StudyManager(db_with_study, config)

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
            study_manager=study_mgr,
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await queries.update_sprint(db_with_study, sprint.id, status="completed")

        # First tweak succeeds.
        await mgr.submit_tweak(sprint.id, "first tweak")

        # Second tweak should be rejected.
        try:
            await mgr.submit_tweak(sprint.id, "second tweak")
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "active tweak" in str(e)


class TestHandleTweakCompletion:
    async def test_handle_tweak_completion(self, db_with_study, sample_config):
        """Tweak completion updates the tweak record."""
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await queries.create_tweak(db_with_study, "tw-comp01", sprint.id, "fix plots")

        await mgr.handle_tweak_completion(
            tweak_id="tw-comp01",
            sprint_id=sprint.id,
            status="completed",
        )

        tweak = await queries.get_tweak(db_with_study, "tw-comp01")
        assert tweak is not None
        assert tweak["status"] == "completed"
        assert tweak["completed_at"] is not None

    async def test_handle_tweak_completion_failed(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await queries.create_tweak(db_with_study, "tw-fail01", sprint.id, "bad tweak")

        await mgr.handle_tweak_completion(
            tweak_id="tw-fail01",
            sprint_id=sprint.id,
            status="failed",
            error="Claude crashed",
        )

        tweak = await queries.get_tweak(db_with_study, "tw-fail01")
        assert tweak is not None
        assert tweak["status"] == "failed"
        assert tweak["error"] == "Claude crashed"

    async def test_handle_tweak_completion_fetches_results(
        self, db_with_study, sample_config
    ):
        """Tweak completion should re-fetch sprint results from cluster."""
        ssh_mock = AsyncMock()

        async def fake_run(cmd: str) -> tuple[str, str, int]:
            if "report.md" in cmd:
                return ("# Updated Report", "", 0)
            if "findings.md" in cmd:
                return ("Updated findings", "", 0)
            if "test -f" in cmd and "report.pdf" in cmd:
                return ("", "", 1)
            return ("", "", 0)

        ssh_mock.run = AsyncMock(side_effect=fake_run)
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        study_mgr = StudyManager(db_with_study, sample_config)

        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=ssh_mgr,
            schedulers={},
            study_manager=study_mgr,
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await queries.create_tweak(
            db_with_study, "tw-fetch1", sprint.id, "fix something"
        )

        await mgr.handle_tweak_completion(
            tweak_id="tw-fetch1",
            sprint_id=sprint.id,
            status="completed",
        )

        row = await queries.get_sprint(db_with_study, sprint.id)
        assert row is not None
        assert row["metadata_json"] is not None
        meta = json.loads(row["metadata_json"])
        assert "Updated Report" in meta.get("report", "")


# ---------------------------------------------------------------------------
# Dashboard routes
# ---------------------------------------------------------------------------


class TestTweakDashboard:
    """Test tweak-related dashboard routes."""

    async def test_tweak_form_visible_on_completed_sprint(
        self, db_with_study, sample_config
    ):
        """The tweak form should appear on completed sprint detail pages."""
        import tempfile

        from fastapi.testclient import TestClient

        from researchloop.core.config import DashboardConfig
        from researchloop.core.orchestrator import Orchestrator, create_app

        config = Config(
            studies=sample_config.studies,
            clusters=sample_config.clusters,
            db_path=":memory:",
            artifact_dir=tempfile.mkdtemp(),
            dashboard=DashboardConfig(password_hash=None),
        )
        orch = Orchestrator(config)
        app = create_app(orch)
        client = TestClient(app)

        with client:
            assert orch.db is not None
            # Set up dashboard password and login.
            from researchloop.dashboard.auth import hash_password

            pw_hash = hash_password("testpass123")
            await orch.db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("dashboard_password_hash", pw_hash),
            )
            resp = client.post(
                "/dashboard/login",
                data={"password": "testpass123"},
                follow_redirects=False,
            )
            cookies = dict(resp.cookies)

            # Create a completed sprint.
            await queries.create_sprint(
                orch.db, "sp-twk-vis", "test-study", "some idea"
            )
            await queries.update_sprint(orch.db, "sp-twk-vis", status="completed")

            resp = client.get(
                "/dashboard/sprints/sp-twk-vis",
                cookies=cookies,
            )
            assert resp.status_code == 200
            assert "Quick Tweak" in resp.text
            assert 'name="instruction"' in resp.text

    async def test_tweak_form_hidden_on_running_sprint(
        self, db_with_study, sample_config
    ):
        """The tweak form should NOT appear on non-completed sprints."""
        import tempfile

        from fastapi.testclient import TestClient

        from researchloop.core.config import DashboardConfig
        from researchloop.core.orchestrator import Orchestrator, create_app

        config = Config(
            studies=sample_config.studies,
            clusters=sample_config.clusters,
            db_path=":memory:",
            artifact_dir=tempfile.mkdtemp(),
            dashboard=DashboardConfig(password_hash=None),
        )
        orch = Orchestrator(config)
        app = create_app(orch)
        client = TestClient(app)

        with client:
            assert orch.db is not None
            from researchloop.dashboard.auth import hash_password

            pw_hash = hash_password("testpass123")
            await orch.db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("dashboard_password_hash", pw_hash),
            )
            resp = client.post(
                "/dashboard/login",
                data={"password": "testpass123"},
                follow_redirects=False,
            )
            cookies = dict(resp.cookies)

            # Create a running sprint.
            await queries.create_sprint(
                orch.db, "sp-twk-run", "test-study", "running idea"
            )
            await queries.update_sprint(orch.db, "sp-twk-run", status="running")

            resp = client.get(
                "/dashboard/sprints/sp-twk-run",
                cookies=cookies,
            )
            assert resp.status_code == 200
            assert "Quick Tweak" not in resp.text


# ---------------------------------------------------------------------------
# Webhook routing
# ---------------------------------------------------------------------------


class TestTweakWebhook:
    async def test_tweak_completion_webhook(self, db_with_study, sample_config):
        """Webhook with tweak_id should route to tweak completion."""
        import tempfile

        from fastapi.testclient import TestClient

        from researchloop.core.config import DashboardConfig
        from researchloop.core.orchestrator import Orchestrator, create_app

        config = Config(
            studies=sample_config.studies,
            clusters=sample_config.clusters,
            db_path=":memory:",
            artifact_dir=tempfile.mkdtemp(),
            dashboard=DashboardConfig(password_hash=None),
        )
        orch = Orchestrator(config)
        app = create_app(orch)
        client = TestClient(app)

        with client:
            assert orch.db is not None
            row = await queries.create_sprint(
                orch.db, "sp-twk-wh", "test-study", "idea"
            )
            token = row["webhook_token"]

            await queries.create_tweak(orch.db, "tw-wh-001", "sp-twk-wh", "fix labels")

            resp = client.post(
                "/api/webhook/sprint-complete",
                json={
                    "sprint_id": "sp-twk-wh",
                    "tweak_id": "tw-wh-001",
                    "status": "completed",
                },
                headers={"x-webhook-token": token},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["tweak_id"] == "tw-wh-001"

            tweak = await queries.get_tweak(orch.db, "tw-wh-001")
            assert tweak is not None
            assert tweak["status"] == "completed"
            assert tweak["completed_at"] is not None
