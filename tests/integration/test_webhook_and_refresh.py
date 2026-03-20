"""Integration tests for webhook handling, dashboard refresh,
and notification routing."""

from __future__ import annotations

import asyncio
import json
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from researchloop.clusters.ssh import SSHManager
from researchloop.comms.base import BaseNotifier
from researchloop.comms.router import NotificationRouter
from researchloop.comms.slack import SlackNotifier
from researchloop.core.config import (
    ClusterConfig,
    Config,
    DashboardConfig,
    StudyConfig,
)
from researchloop.core.orchestrator import Orchestrator, create_app
from researchloop.db import queries
from researchloop.db.database import Database
from researchloop.schedulers.slurm import SlurmScheduler
from researchloop.sprints.manager import SprintManager
from researchloop.studies.manager import StudyManager

# ======================================================================
# Fixtures for webhook/heartbeat tests (no Docker, in-memory DB)
# ======================================================================


def _make_app(
    shared_secret: str | None = None,
) -> tuple[TestClient, Orchestrator]:
    """Create a TestClient with in-memory orchestrator, no auth gate."""
    config = Config(
        studies=[
            StudyConfig(
                name="test-study",
                cluster="local",
                sprints_dir="./sp",
                description="Integration test study",
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
        shared_secret=shared_secret,
        dashboard=DashboardConfig(password_hash=None),
    )
    orch = Orchestrator(config)
    app = create_app(orch)
    return TestClient(app), orch


# ======================================================================
# 1. Webhook Token Validation
# ======================================================================


class TestWebhookTokenValidation:
    """Verify that webhook endpoints enforce per-sprint tokens."""

    async def test_webhook_rejects_invalid_token(self) -> None:
        """POST with wrong token returns 401, DB unchanged."""
        client, orch = _make_app()
        with client:
            assert orch.db is not None
            row = await queries.create_sprint(
                orch.db, "sp-bad-token", "test-study", "some idea"
            )
            correct_token = row["webhook_token"]

            resp = client.post(
                "/api/webhook/sprint-complete",
                json={
                    "sprint_id": "sp-bad-token",
                    "status": "completed",
                    "summary": "should not work",
                },
                headers={"x-webhook-token": "wrong-token-value"},
            )
            assert resp.status_code == 401

            # Verify DB was NOT updated.
            sprint = await queries.get_sprint(orch.db, "sp-bad-token")
            assert sprint is not None
            assert sprint["status"] == "pending"
            assert sprint["summary"] is None
            assert correct_token != "wrong-token-value"

    async def test_webhook_accepts_valid_token(self) -> None:
        """POST with correct token + status=completed + summary returns 200
        and updates the DB."""
        client, orch = _make_app()
        with client:
            assert orch.db is not None
            row = await queries.create_sprint(
                orch.db, "sp-good-token", "test-study", "an idea"
            )
            token = row["webhook_token"]

            resp = client.post(
                "/api/webhook/sprint-complete",
                json={
                    "sprint_id": "sp-good-token",
                    "status": "completed",
                    "summary": "Everything went great",
                },
                headers={"x-webhook-token": token},
            )
            assert resp.status_code == 200
            assert resp.json()["ok"] is True

            sprint = await queries.get_sprint(orch.db, "sp-good-token")
            assert sprint is not None
            assert sprint["status"] == "completed"
            assert sprint["summary"] == "Everything went great"
            assert sprint["completed_at"] is not None

    async def test_webhook_updates_idea_for_loop_sprint(self) -> None:
        """When sprint has idea=None (loop sprint), webhook idea is stored."""
        client, orch = _make_app()
        with client:
            assert orch.db is not None
            # Create sprint with no idea (auto-loop sprint).
            row = await queries.create_sprint(
                orch.db, "sp-loop-idea", "test-study", None
            )
            token = row["webhook_token"]
            # Set loop_id to simulate a loop sprint.
            await queries.update_sprint(orch.db, "sp-loop-idea", loop_id="loop-abc")

            resp = client.post(
                "/api/webhook/sprint-complete",
                json={
                    "sprint_id": "sp-loop-idea",
                    "status": "completed",
                    "summary": "Done",
                    "idea": "generated idea from cluster",
                },
                headers={"x-webhook-token": token},
            )
            assert resp.status_code == 200

            sprint = await queries.get_sprint(orch.db, "sp-loop-idea")
            assert sprint is not None
            assert sprint["idea"] == "generated idea from cluster"

    async def test_webhook_preserves_existing_idea(self) -> None:
        """When sprint already has an idea, webhook idea is NOT overwritten."""
        client, orch = _make_app()
        with client:
            assert orch.db is not None
            row = await queries.create_sprint(
                orch.db, "sp-keep-idea", "test-study", "original idea"
            )
            token = row["webhook_token"]

            resp = client.post(
                "/api/webhook/sprint-complete",
                json={
                    "sprint_id": "sp-keep-idea",
                    "status": "completed",
                    "summary": "Done",
                    "idea": "different idea",
                },
                headers={"x-webhook-token": token},
            )
            assert resp.status_code == 200

            sprint = await queries.get_sprint(orch.db, "sp-keep-idea")
            assert sprint is not None
            # The original idea is preserved because handle_completion
            # only updates idea when the sprint had idea=None.
            assert sprint["idea"] == "original idea"


# ======================================================================
# 2. Heartbeat Endpoint
# ======================================================================


class TestHeartbeatEndpoint:
    """Verify heartbeat updates sprint status and metadata."""

    async def test_heartbeat_updates_status_and_log(self) -> None:
        """Heartbeat with phase + log_tail + progress updates status/error."""
        client, orch = _make_app()
        with client:
            assert orch.db is not None
            row = await queries.create_sprint(
                orch.db, "sp-hb-01", "test-study", "test idea"
            )
            token = row["webhook_token"]

            resp = client.post(
                "/api/webhook/heartbeat",
                json={
                    "sprint_id": "sp-hb-01",
                    "phase": "running (research)",
                    "log_tail": ">>> Starting step: research\nRunning...",
                    "progress": "## Plan\n1. Do research",
                },
                headers={"x-webhook-token": token},
            )
            assert resp.status_code == 200

            sprint = await queries.get_sprint(orch.db, "sp-hb-01")
            assert sprint is not None
            # Phase updates the status field.
            assert sprint["status"] == "running (research)"
            # Error field contains progress + log.
            assert sprint["error"] is not None
            assert "## Plan" in sprint["error"]
            assert "--- Tool log ---" in sprint["error"]
            assert ">>> Starting step: research" in sprint["error"]

    async def test_heartbeat_preserves_report_metadata(self) -> None:
        """Pre-set report/has_pdf in metadata_json survives heartbeat."""
        client, orch = _make_app()
        with client:
            assert orch.db is not None
            row = await queries.create_sprint(
                orch.db, "sp-hb-meta", "test-study", "test idea"
            )
            token = row["webhook_token"]

            # Pre-populate metadata with report and has_pdf.
            existing_meta = {
                "report": "# Full Report\nDetailed findings here.",
                "has_pdf": True,
            }
            await queries.update_sprint(
                orch.db,
                "sp-hb-meta",
                metadata_json=json.dumps(existing_meta),
            )

            # Send heartbeat.
            resp = client.post(
                "/api/webhook/heartbeat",
                json={
                    "sprint_id": "sp-hb-meta",
                    "phase": "running (red_team_1)",
                    "log_tail": "Red team running",
                },
                headers={"x-webhook-token": token},
            )
            assert resp.status_code == 200

            sprint = await queries.get_sprint(orch.db, "sp-hb-meta")
            assert sprint is not None
            meta = json.loads(sprint["metadata_json"])
            # report and has_pdf preserved from the original metadata.
            assert meta["report"] == "# Full Report\nDetailed findings here."
            assert meta["has_pdf"] is True
            # heartbeat fields added.
            assert "last_heartbeat" in meta
            assert meta["phase"] == "running (red_team_1)"


# ======================================================================
# 3. Dashboard Refresh with Real SLURM
# ======================================================================

pytestmark_integration = pytest.mark.integration


async def _setup_dashboard_auth(
    client: httpx.AsyncClient,
) -> None:
    """Set up dashboard password and authenticate the async client."""
    resp = await client.post(
        "/dashboard/setup",
        data={"password": "testpass123", "confirm": "testpass123"},
        follow_redirects=False,
    )
    # Extract session cookie and apply it.
    assert resp.status_code == 303
    for cookie_header in resp.headers.get_list("set-cookie"):
        if "rl_session=" in cookie_header:
            token = cookie_header.split("rl_session=")[1].split(";")[0]
            client.cookies.set("rl_session", token)
            return


def _make_orch_and_app(
    db: Database,
    config: Config,
    ssh_mgr: SSHManager,
) -> tuple[Orchestrator, object]:
    """Build an Orchestrator + FastAPI app.

    Patches start/stop to avoid re-creating an in-memory DB
    (the lifespan would connect to a fresh :memory: DB otherwise).
    """
    orch = Orchestrator(config)
    orch.db = db
    cluster = config.clusters[0]
    scheduler = SlurmScheduler()
    study_mgr = StudyManager(db, config)
    orch.study_manager = study_mgr
    orch.sprint_manager = SprintManager(
        db=db,
        config=config,
        ssh_manager=ssh_mgr,
        schedulers={
            cluster.name: scheduler,
            cluster.scheduler_type: scheduler,
        },
        study_manager=study_mgr,
    )

    # Prevent lifespan from re-creating the DB.
    async def _noop() -> None:
        pass

    orch.start = _noop  # type: ignore[assignment]
    orch.stop = _noop  # type: ignore[assignment]

    app = create_app(orch)
    return orch, app


async def _get_ssh_and_sprint_path(
    sprint_id: str,
    db: Database,
    config: Config,
) -> tuple[SSHManager, object, str]:
    """Get SSH conn + sprint path on cluster."""
    cluster = config.clusters[0]
    ssh_mgr = SSHManager()
    conn = await ssh_mgr.get_connection(
        {
            "host": cluster.host,
            "port": cluster.port,
            "user": cluster.user,
            "key_path": cluster.key_path,
        }
    )
    row = await queries.get_sprint(db, sprint_id)
    assert row is not None
    base = config.studies[0].sprints_dir
    sprint_path = f"{base}/{row['directory']}"
    return ssh_mgr, conn, sprint_path


async def _refresh_sprint(
    app: object,
    sprint_id: str,
    accept_json: bool = True,
) -> httpx.Response:
    """Set up auth and call refresh via httpx.AsyncClient."""
    import re

    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _setup_dashboard_auth(client)

        # Extract CSRF token from a dashboard page.
        page = await client.get(
            f"/dashboard/sprints/{sprint_id}",
            follow_redirects=True,
        )
        csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
        csrf_token = csrf_match.group(1) if csrf_match else ""

        headers: dict[str, str] = {}
        if accept_json:
            headers["accept"] = "application/json"
        if csrf_token:
            headers["X-CSRF-Token"] = csrf_token
        return await client.post(
            f"/dashboard/sprints/{sprint_id}/refresh",
            headers=headers,
            follow_redirects=False,
        )


@pytest.mark.integration
class TestDashboardRefreshSLURM:
    """Test the dashboard refresh endpoint reading files from cluster."""

    async def test_refresh_reads_idea_from_cluster(
        self,
        integration_db_with_study: Database,
        integration_config: Config,
        sprint_manager: SprintManager,
    ) -> None:
        """Submit loop sprint, write idea.txt, refresh syncs it."""
        sprint = await sprint_manager.create_sprint("integration-study", None)
        await sprint_manager.submit_sprint(sprint.id)

        ssh_mgr, conn, sprint_path = await _get_ssh_and_sprint_path(
            sprint.id, integration_db_with_study, integration_config
        )
        try:
            await conn.run(
                f"echo 'Auto-generated research idea' > {sprint_path}/idea.txt"
            )
            _, app = _make_orch_and_app(
                integration_db_with_study, integration_config, ssh_mgr
            )
            resp = await _refresh_sprint(app, sprint.id)
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("idea") is not None
            assert "Auto-generated research idea" in data["idea"]
        finally:
            await ssh_mgr.close_all()

    async def test_refresh_reads_summary_from_cluster(
        self,
        integration_db_with_study: Database,
        integration_config: Config,
        sprint_manager: SprintManager,
    ) -> None:
        """Write summary.txt, refresh syncs it."""
        sprint = await sprint_manager.create_sprint("integration-study", "summary test")
        await sprint_manager.submit_sprint(sprint.id)

        ssh_mgr, conn, sprint_path = await _get_ssh_and_sprint_path(
            sprint.id, integration_db_with_study, integration_config
        )
        try:
            await conn.run(
                f"echo 'Important results found' > {sprint_path}/summary.txt"
            )
            _, app = _make_orch_and_app(
                integration_db_with_study, integration_config, ssh_mgr
            )
            resp = await _refresh_sprint(app, sprint.id)
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("summary") is not None
            assert "Important results found" in data["summary"]
        finally:
            await ssh_mgr.close_all()

    async def test_refresh_reads_sprint_log(
        self,
        integration_db_with_study: Database,
        integration_config: Config,
        sprint_manager: SprintManager,
    ) -> None:
        """Write sprint_log.txt, refresh stores in error field."""
        sprint = await sprint_manager.create_sprint("integration-study", "log test")
        await sprint_manager.submit_sprint(sprint.id)

        ssh_mgr, conn, sprint_path = await _get_ssh_and_sprint_path(
            sprint.id, integration_db_with_study, integration_config
        )
        try:
            await conn.run(
                f"printf '>>> Starting step: research\\n"
                f"Running research phase' > "
                f"{sprint_path}/sprint_log.txt"
            )
            _, app = _make_orch_and_app(
                integration_db_with_study, integration_config, ssh_mgr
            )
            await _refresh_sprint(app, sprint.id, accept_json=False)

            updated = await queries.get_sprint(integration_db_with_study, sprint.id)
            assert updated is not None
            assert updated.get("error") is not None
            assert "research" in updated["error"].lower()
        finally:
            await ssh_mgr.close_all()

    async def test_refresh_returns_json(
        self,
        integration_db_with_study: Database,
        integration_config: Config,
        sprint_manager: SprintManager,
    ) -> None:
        """Refresh with Accept: application/json returns expected fields."""
        sprint = await sprint_manager.create_sprint("integration-study", "json test")
        await sprint_manager.submit_sprint(sprint.id)

        ssh_mgr = SSHManager()
        try:
            _, app = _make_orch_and_app(
                integration_db_with_study, integration_config, ssh_mgr
            )
            resp = await _refresh_sprint(app, sprint.id)
            assert resp.status_code == 200
            data = resp.json()
            assert "status" in data
            assert "idea" in data
            assert "summary" in data
            assert "completed_at" in data
        finally:
            await ssh_mgr.close_all()

    async def test_refresh_reads_progress_md(
        self,
        integration_db_with_study: Database,
        integration_config: Config,
        sprint_manager: SprintManager,
    ) -> None:
        """Write progress.md on cluster, refresh shows it."""
        sprint = await sprint_manager.create_sprint(
            "integration-study", "progress test"
        )
        await sprint_manager.submit_sprint(sprint.id)

        row = await queries.get_sprint(integration_db_with_study, sprint.id)
        assert row is not None
        ssh_mgr, conn, sprint_path = await _get_ssh_and_sprint_path(
            sprint.id, integration_db_with_study, integration_config
        )
        try:
            # Wait for the job to complete so mock claude doesn't
            # overwrite our progress.md.
            for _ in range(30):
                stdout, _, _ = await conn.run(
                    f"scontrol show job {row['job_id']} -o 2>/dev/null"
                )
                if "COMPLETED" in stdout or "FAILED" in stdout:
                    break
                await asyncio.sleep(1)

            await conn.run(
                f"printf '## Research Progress\\n"
                f"- Step 1 complete' > "
                f"{sprint_path}/progress.md"
            )
            _, app = _make_orch_and_app(
                integration_db_with_study, integration_config, ssh_mgr
            )
            await _refresh_sprint(app, sprint.id, accept_json=False)

            updated = await queries.get_sprint(integration_db_with_study, sprint.id)
            assert updated is not None
            assert updated.get("error") is not None
            assert "Research Progress" in updated["error"]
        finally:
            await ssh_mgr.close_all()


# ======================================================================
# 4. Notification Routing
# ======================================================================


class _MockNotifier(BaseNotifier):
    """In-memory notifier that records calls for assertion."""

    def __init__(self, *, fail: bool = False) -> None:
        self.started_calls: list[tuple[str, str, str]] = []
        self.completed_calls: list[tuple[str, str, str, str | None]] = []
        self.failed_calls: list[tuple[str, str, str]] = []
        self._fail = fail

    async def notify_sprint_started(
        self, sprint_id: str, study_name: str, idea: str
    ) -> None:
        if self._fail:
            raise RuntimeError("Notifier failed!")
        self.started_calls.append((sprint_id, study_name, idea))

    async def notify_sprint_completed(
        self,
        sprint_id: str,
        study_name: str,
        summary: str,
        pdf_path: str | None = None,
    ) -> None:
        if self._fail:
            raise RuntimeError("Notifier failed!")
        self.completed_calls.append((sprint_id, study_name, summary, pdf_path))

    async def notify_sprint_failed(
        self, sprint_id: str, study_name: str, error: str
    ) -> None:
        if self._fail:
            raise RuntimeError("Notifier failed!")
        self.failed_calls.append((sprint_id, study_name, error))


class TestNotificationRouter:
    """Test NotificationRouter fan-out and error handling."""

    async def test_notification_router_calls_all_notifiers(self) -> None:
        """Two registered notifiers both get called on notify_sprint_completed."""
        router = NotificationRouter()
        n1 = _MockNotifier()
        n2 = _MockNotifier()
        router.add_notifier(n1)
        router.add_notifier(n2)

        await router.notify_sprint_completed(
            sprint_id="sp-001",
            study_name="my-study",
            summary="Everything passed",
        )

        assert len(n1.completed_calls) == 1
        assert len(n2.completed_calls) == 1
        assert n1.completed_calls[0][0] == "sp-001"
        assert n1.completed_calls[0][2] == "Everything passed"
        assert n2.completed_calls[0][0] == "sp-001"

    async def test_notification_router_continues_on_failure(self) -> None:
        """First notifier raises, second still gets called."""
        router = NotificationRouter()
        n_fail = _MockNotifier(fail=True)
        n_ok = _MockNotifier()
        router.add_notifier(n_fail)
        router.add_notifier(n_ok)

        # Should not raise despite n_fail throwing.
        await router.notify_sprint_completed(
            sprint_id="sp-err",
            study_name="my-study",
            summary="Summary text",
        )

        # The failing notifier recorded nothing.
        assert len(n_fail.completed_calls) == 0
        # The second notifier was still called.
        assert len(n_ok.completed_calls) == 1
        assert n_ok.completed_calls[0][0] == "sp-err"

    async def test_notification_router_fan_out_all_methods(self) -> None:
        """All three notification methods fan out to all notifiers."""
        router = NotificationRouter()
        n1 = _MockNotifier()
        n2 = _MockNotifier()
        router.add_notifier(n1)
        router.add_notifier(n2)

        await router.notify_sprint_started("sp-a", "study-a", "idea-a")
        await router.notify_sprint_completed("sp-b", "study-b", "summary-b")
        await router.notify_sprint_failed("sp-c", "study-c", "error-c")

        assert len(n1.started_calls) == 1
        assert len(n1.completed_calls) == 1
        assert len(n1.failed_calls) == 1
        assert len(n2.started_calls) == 1
        assert len(n2.completed_calls) == 1
        assert len(n2.failed_calls) == 1

    async def test_slack_notifier_truncates_long_summary(self) -> None:
        """SlackNotifier truncates summary to 500 chars in the message."""
        long_summary = "A" * 1000

        # Mock httpx to capture the posted message.
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "ts": "123.456"}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            notifier = SlackNotifier(
                bot_token="xoxb-fake-token",
                channel_id="C12345",
            )
            await notifier.notify_sprint_completed(
                sprint_id="sp-long",
                study_name="study-x",
                summary=long_summary,
            )

            # Verify the post was called.
            mock_client.post.assert_called_once()
            call_kwargs = mock_client.post.call_args
            posted_json = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            posted_text = posted_json["text"]

            # Summary in the message should be truncated at 500 + ellipsis.
            # The full 1000-char string should NOT appear.
            assert long_summary not in posted_text
            assert "A" * 500 in posted_text
            # The truncation marker should be present.
            assert "\u2026" in posted_text  # ellipsis character
