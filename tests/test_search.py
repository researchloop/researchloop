"""Tests for dashboard search."""

from __future__ import annotations

import json
import tempfile
from unittest.mock import AsyncMock

from researchloop.core.config import Config, DashboardConfig
from researchloop.core.orchestrator import Orchestrator, create_app
from researchloop.db import queries
from researchloop.sprints.manager import SprintManager


class TestSearchQuery:
    """Test the search_sprints DB query."""

    async def test_search_matches_idea(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        await mgr.create_sprint("test-study", "investigate feature absorption")
        await mgr.create_sprint("test-study", "unrelated work")

        results = await queries.search_sprints(db_with_study, "absorption")
        assert len(results) == 1
        assert "absorption" in results[0]["idea"]

    async def test_search_matches_summary(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await queries.update_sprint(
            db_with_study, sprint.id, summary="Found significant correlation"
        )

        results = await queries.search_sprints(db_with_study, "correlation")
        assert len(results) == 1

    async def test_search_matches_metadata_json(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        meta = json.dumps({"report": "The hypothesis was confirmed"})
        await queries.update_sprint(db_with_study, sprint.id, metadata_json=meta)

        results = await queries.search_sprints(db_with_study, "hypothesis")
        assert len(results) == 1

    async def test_search_no_match(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        await mgr.create_sprint("test-study", "something else entirely")

        results = await queries.search_sprints(db_with_study, "nonexistent")
        assert len(results) == 0

    async def test_search_case_insensitive(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        await mgr.create_sprint("test-study", "Investigate SAE Features")

        results = await queries.search_sprints(db_with_study, "sae features")
        assert len(results) == 1


class TestSearchRoute:
    """Test the /dashboard/search route."""

    def _make_client(self, sample_config):  # type: ignore[no-untyped-def]
        from fastapi.testclient import TestClient

        config = Config(
            studies=sample_config.studies,
            clusters=sample_config.clusters,
            db_path=":memory:",
            artifact_dir=tempfile.mkdtemp(),
            dashboard=DashboardConfig(password_hash=None),
        )
        orch = Orchestrator(config)
        app = create_app(orch)
        return TestClient(app), orch

    async def test_search_page_renders(self, db_with_study, sample_config):
        client, orch = self._make_client(sample_config)
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

            resp = client.get("/dashboard/search", cookies=cookies)
            assert resp.status_code == 200
            assert "Search" in resp.text

    async def test_search_with_query(self, db_with_study, sample_config):
        client, orch = self._make_client(sample_config)
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

            # Create a sprint to search for.
            await queries.create_sprint(
                orch.db, "sp-srch1", "test-study", "investigate transformers"
            )
            await queries.create_sprint(
                orch.db, "sp-srch2", "test-study", "unrelated work"
            )

            resp = client.get("/dashboard/search?q=transformers", cookies=cookies)
            assert resp.status_code == 200
            assert "sp-srch1" in resp.text
            assert "sp-srch2" not in resp.text
            assert '1 result for "transformers"' in resp.text

    async def test_search_empty_query(self, db_with_study, sample_config):
        client, orch = self._make_client(sample_config)
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

            resp = client.get("/dashboard/search?q=", cookies=cookies)
            assert resp.status_code == 200
            assert "0 result" not in resp.text  # No count shown for empty query
