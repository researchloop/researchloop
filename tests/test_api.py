"""Tests for the FastAPI application (orchestrator routes)."""

from __future__ import annotations

import tempfile

from fastapi.testclient import TestClient

from researchloop.core.config import (
    ClusterConfig,
    Config,
    StudyConfig,
)
from researchloop.core.orchestrator import Orchestrator, create_app
from researchloop.db import queries


def _make_app(
    shared_secret: str | None = "test-key",
) -> tuple[TestClient, Orchestrator]:
    """Create a TestClient with an in-memory orchestrator."""
    config = Config(
        studies=[StudyConfig(name="test", cluster="local", sprints_dir="./sp")],
        clusters=[
            ClusterConfig(name="local", host="localhost", scheduler_type="local")
        ],
        db_path=":memory:",
        artifact_dir=tempfile.mkdtemp(),
        shared_secret=shared_secret,
    )
    orch = Orchestrator(config)
    app = create_app(orch)
    return TestClient(app), orch


class TestStudiesAPI:
    def test_list_studies(self):
        client, _ = _make_app()
        with client:
            resp = client.get("/api/studies")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["studies"]) == 1
            assert data["studies"][0]["name"] == "test"


class TestSprintsAPI:
    def test_list_sprints_empty(self):
        client, _ = _make_app()
        with client:
            resp = client.get("/api/sprints")
            assert resp.status_code == 200
            assert resp.json()["sprints"] == []

    def test_get_sprint_not_found(self):
        client, _ = _make_app()
        with client:
            resp = client.get("/api/sprints/sp-nonexistent")
            assert resp.status_code == 404

    async def test_list_sprints_with_data(self):
        client, orch = _make_app()
        with client:
            # Insert a sprint directly into DB
            await queries.create_sprint(orch.db, "sp-test01", "test", "idea 1")
            resp = client.get("/api/sprints")
            assert resp.status_code == 200
            assert len(resp.json()["sprints"]) == 1

    async def test_get_sprint(self):
        client, orch = _make_app()
        with client:
            await queries.create_sprint(orch.db, "sp-test01", "test", "idea 1")
            resp = client.get("/api/sprints/sp-test01")
            assert resp.status_code == 200
            assert resp.json()["sprint"]["idea"] == "idea 1"

    async def test_list_sprints_filter(self):
        client, orch = _make_app()
        with client:
            await queries.create_sprint(orch.db, "sp-001", "test", "idea 1")
            resp = client.get("/api/sprints?study_name=test")
            assert len(resp.json()["sprints"]) == 1
            resp = client.get("/api/sprints?study_name=other")
            assert len(resp.json()["sprints"]) == 0


class TestWebhookAuth:
    def test_webhook_rejects_no_key(self):
        client, _ = _make_app(shared_secret="secret")
        with client:
            resp = client.post(
                "/api/webhook/sprint-complete",
                json={
                    "sprint_id": "sp-001",
                    "status": "completed",
                },
            )
            assert resp.status_code == 401

    def test_webhook_rejects_wrong_key(self):
        client, _ = _make_app(shared_secret="secret")
        with client:
            resp = client.post(
                "/api/webhook/sprint-complete",
                json={"sprint_id": "sp-001", "status": "completed"},
                headers={"x-shared-secret": "wrong"},
            )
            assert resp.status_code == 401

    async def test_no_auth_when_no_key_configured(self):
        client, orch = _make_app(shared_secret=None)
        with client:
            await queries.create_sprint(orch.db, "sp-001", "test", "idea")
            resp = client.post(
                "/api/webhook/sprint-complete",
                json={
                    "sprint_id": "sp-001",
                    "status": "completed",
                },
            )
            assert resp.status_code == 200


class TestWebhookSprintComplete:
    async def test_updates_sprint(self):
        client, orch = _make_app()
        with client:
            await queries.create_sprint(orch.db, "sp-001", "test", "idea")
            resp = client.post(
                "/api/webhook/sprint-complete",
                json={
                    "sprint_id": "sp-001",
                    "status": "completed",
                    "summary": "All good",
                },
                headers={"x-shared-secret": "test-key"},
            )
            assert resp.status_code == 200
            assert resp.json()["ok"] is True

            sprint = await queries.get_sprint(orch.db, "sp-001")
            assert sprint["status"] == "completed"
            assert sprint["summary"] == "All good"

    async def test_missing_sprint_id(self):
        client, _ = _make_app()
        with client:
            resp = client.post(
                "/api/webhook/sprint-complete",
                json={"status": "completed"},
                headers={"x-shared-secret": "test-key"},
            )
            assert resp.status_code == 400


class TestWebhookHeartbeat:
    async def test_heartbeat(self):
        client, orch = _make_app()
        with client:
            await queries.create_sprint(orch.db, "sp-001", "test", "idea")
            resp = client.post(
                "/api/webhook/heartbeat",
                json={"sprint_id": "sp-001", "phase": "research"},
                headers={"x-shared-secret": "test-key"},
            )
            assert resp.status_code == 200
            sprint = await queries.get_sprint(orch.db, "sp-001")
            assert sprint["status"] == "research"


class TestArtifactUpload:
    async def test_upload(self):
        client, orch = _make_app()
        with client:
            await queries.create_sprint(orch.db, "sp-001", "test", "idea")
            resp = client.post(
                "/api/artifacts/sp-001",
                files={
                    "file": ("report.md", b"# Report\nContent here.", "text/markdown")
                },
                headers={"x-shared-secret": "test-key"},
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["filename"] == "report.md"
            assert data["size"] > 0

            arts = await queries.list_artifacts(orch.db, "sp-001")
            assert len(arts) == 1

    async def test_upload_sprint_not_found(self):
        client, _ = _make_app()
        with client:
            resp = client.post(
                "/api/artifacts/sp-nonexistent",
                files={"file": ("f.txt", b"data", "text/plain")},
                headers={"x-shared-secret": "test-key"},
            )
            assert resp.status_code == 404
