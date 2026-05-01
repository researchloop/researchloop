"""Tests for researchloop.db.queries."""

import json

from researchloop.db import queries


class TestStudyQueries:
    async def test_create_and_get(self, db):
        study = await queries.create_study(
            db,
            name="my-study",
            cluster="hpc",
            description="A study",
            claude_md_path="/path/CLAUDE.md",
            sprints_dir="./sprints",
        )
        assert study["name"] == "my-study"
        assert study["cluster"] == "hpc"
        assert study["description"] == "A study"

        fetched = await queries.get_study(db, "my-study")
        assert fetched is not None
        assert fetched["name"] == "my-study"

    async def test_get_nonexistent(self, db):
        assert await queries.get_study(db, "nope") is None

    async def test_list_studies(self, db):
        await queries.create_study(db, "s1", "c1", None, None, "./sp1")
        await queries.create_study(db, "s2", "c2", None, None, "./sp2")
        studies = await queries.list_studies(db)
        assert len(studies) == 2

    async def test_update_study(self, db):
        await queries.create_study(db, "s1", "c1", "old desc", None, "./sp")
        updated = await queries.update_study(db, "s1", description="new desc")
        assert updated["description"] == "new desc"

    async def test_update_no_kwargs(self, db):
        await queries.create_study(db, "s1", "c1", None, None, "./sp")
        result = await queries.update_study(db, "s1")
        assert result["name"] == "s1"

    async def test_create_study_with_source_and_yaml_snapshot(self, db):
        study = await queries.create_study(
            db,
            name="s1",
            cluster="c1",
            description=None,
            claude_md_path=None,
            sprints_dir="./sp",
            config_json='{"name":"s1"}',
            source="ui",
            yaml_config_json=None,
        )
        assert study["source"] == "ui"
        assert study["yaml_config_json"] is None
        assert study["config_json"] == '{"name":"s1"}'

    async def test_create_study_default_source_is_yaml(self, db):
        study = await queries.create_study(
            db,
            name="s1",
            cluster="c1",
            description=None,
            claude_md_path=None,
            sprints_dir="./sp",
            config_json="{}",
            yaml_config_json="{}",
        )
        assert study["source"] == "yaml"
        assert study["yaml_config_json"] == "{}"

    async def test_delete_study(self, db):
        await queries.create_study(db, "s1", "c1", None, None, "./sp")
        await queries.delete_study(db, "s1")
        assert await queries.get_study(db, "s1") is None

    async def test_count_sprints_for_study(self, db):
        await queries.create_study(db, "s1", "c1", None, None, "./sp")
        assert await queries.count_sprints_for_study(db, "s1") == 0
        await queries.create_sprint(db, "sp-1", "s1", "idea 1")
        await queries.create_sprint(db, "sp-2", "s1", "idea 2")
        assert await queries.count_sprints_for_study(db, "s1") == 2
        assert await queries.count_sprints_for_study(db, "other") == 0

    async def test_update_study_yaml_config_json_to_null(self, db):
        await queries.create_study(
            db,
            "s1",
            "c1",
            None,
            None,
            "./sp",
            config_json="{}",
            source="yaml",
            yaml_config_json='{"v": 1}',
        )
        updated = await queries.update_study(db, "s1", yaml_config_json=None)
        assert updated["yaml_config_json"] is None

    async def test_update_study_rejects_unknown_column(self, db):
        import pytest

        await queries.create_study(db, "s1", "c1", None, None, "./sp")
        with pytest.raises(ValueError, match="Invalid column"):
            await queries.update_study(db, "s1", not_a_column="x")

    async def test_serialized_config_roundtrip(self, db):
        payload = {"name": "s1", "cluster": "c1", "extra": [1, 2, 3]}
        await queries.create_study(
            db,
            "s1",
            "c1",
            None,
            None,
            "./sp",
            config_json=json.dumps(payload),
            yaml_config_json=json.dumps(payload),
        )
        row = await queries.get_study(db, "s1")
        assert json.loads(row["config_json"]) == payload
        assert json.loads(row["yaml_config_json"]) == payload


class TestSprintQueries:
    async def test_create_and_get(self, db_with_study):
        sprint = await queries.create_sprint(
            db_with_study,
            id="sp-abc123",
            study_name="test-study",
            idea="test idea",
            directory="/tmp/sprint",
        )
        assert sprint["id"] == "sp-abc123"
        assert sprint["status"] == "pending"
        assert sprint["idea"] == "test idea"

    async def test_list_sprints(self, db_with_study):
        await queries.create_sprint(db_with_study, "sp-001", "test-study", "idea 1")
        await queries.create_sprint(db_with_study, "sp-002", "test-study", "idea 2")
        sprints = await queries.list_sprints(db_with_study)
        assert len(sprints) == 2

    async def test_list_sprints_filter_by_study(self, db_with_study):
        await queries.create_sprint(db_with_study, "sp-001", "test-study", "idea 1")
        sprints = await queries.list_sprints(db_with_study, study_name="test-study")
        assert len(sprints) == 1
        sprints = await queries.list_sprints(db_with_study, study_name="other")
        assert len(sprints) == 0

    async def test_list_sprints_filter_by_status(self, db_with_study):
        await queries.create_sprint(db_with_study, "sp-001", "test-study", "idea 1")
        await queries.update_sprint(db_with_study, "sp-001", status="running")
        sprints = await queries.list_sprints(db_with_study, status="running")
        assert len(sprints) == 1
        sprints = await queries.list_sprints(db_with_study, status="completed")
        assert len(sprints) == 0

    async def test_list_sprints_limit(self, db_with_study):
        for i in range(5):
            await queries.create_sprint(
                db_with_study, f"sp-{i:03d}", "test-study", f"idea {i}"
            )
        sprints = await queries.list_sprints(db_with_study, limit=3)
        assert len(sprints) == 3

    async def test_update_sprint(self, db_with_study):
        await queries.create_sprint(db_with_study, "sp-001", "test-study", "idea")
        updated = await queries.update_sprint(
            db_with_study,
            "sp-001",
            status="running",
            job_id="12345",
        )
        assert updated["status"] == "running"
        assert updated["job_id"] == "12345"

    async def test_get_active_sprints(self, db_with_study):
        await queries.create_sprint(db_with_study, "sp-001", "test-study", "idea 1")
        await queries.create_sprint(db_with_study, "sp-002", "test-study", "idea 2")
        await queries.update_sprint(db_with_study, "sp-001", status="running")
        active = await queries.get_active_sprints(db_with_study)
        assert len(active) == 1
        assert active[0]["id"] == "sp-001"


class TestDeleteSprint:
    async def test_delete_removes_sprint(self, db_with_study):
        await queries.create_sprint(db_with_study, "sp-del", "test-study", "idea")
        await queries.delete_sprint(db_with_study, "sp-del")
        assert await queries.get_sprint(db_with_study, "sp-del") is None

    async def test_delete_removes_related_artifacts_and_events(self, db_with_study):
        await queries.create_sprint(db_with_study, "sp-del", "test-study", "idea")
        await queries.create_artifact(db_with_study, "sp-del", "f.pdf", "/f.pdf")
        await queries.create_event(db_with_study, "sp-del", "test_event")
        await queries.delete_sprint(db_with_study, "sp-del")
        assert await queries.list_artifacts(db_with_study, "sp-del") == []
        assert await queries.list_events(db_with_study, sprint_id="sp-del") == []


class TestAutoLoopQueries:
    async def test_create_and_get(self, db_with_study):
        loop = await queries.create_auto_loop(
            db_with_study,
            id="loop-abc",
            study_name="test-study",
            total_count=5,
        )
        assert loop["id"] == "loop-abc"
        assert loop["total_count"] == 5
        assert loop["completed_count"] == 0
        assert loop["status"] == "running"

    async def test_update_auto_loop(self, db_with_study):
        await queries.create_auto_loop(db_with_study, "loop-1", "test-study", 3)
        updated = await queries.update_auto_loop(
            db_with_study,
            "loop-1",
            completed_count=1,
            current_sprint_id="sp-001",
        )
        assert updated["completed_count"] == 1
        assert updated["current_sprint_id"] == "sp-001"

    async def test_list_auto_loops(self, db_with_study):
        await queries.create_auto_loop(db_with_study, "loop-1", "test-study", 3)
        await queries.create_auto_loop(db_with_study, "loop-2", "test-study", 5)
        loops = await queries.list_auto_loops(db_with_study)
        assert len(loops) == 2

    async def test_list_auto_loops_filter(self, db_with_study):
        await queries.create_auto_loop(db_with_study, "loop-1", "test-study", 3)
        loops = await queries.list_auto_loops(db_with_study, study_name="test-study")
        assert len(loops) == 1
        loops = await queries.list_auto_loops(db_with_study, study_name="other")
        assert len(loops) == 0


class TestArtifactQueries:
    async def test_create_and_list(self, db_with_study):
        await queries.create_sprint(db_with_study, "sp-001", "test-study", "idea")
        art = await queries.create_artifact(
            db_with_study,
            sprint_id="sp-001",
            filename="report.md",
            path="/tmp/report.md",
            size=1024,
            content_type="text/markdown",
        )
        assert art["filename"] == "report.md"
        assert art["size"] == 1024

        arts = await queries.list_artifacts(db_with_study, "sp-001")
        assert len(arts) == 1

    async def test_get_artifact(self, db_with_study):
        await queries.create_sprint(db_with_study, "sp-001", "test-study", "idea")
        art = await queries.create_artifact(
            db_with_study,
            "sp-001",
            "file.pdf",
            "/tmp/file.pdf",
            2048,
        )
        fetched = await queries.get_artifact(db_with_study, art["id"])
        assert fetched is not None
        assert fetched["filename"] == "file.pdf"


class TestEventQueries:
    async def test_create_and_list(self, db_with_study):
        await queries.create_sprint(db_with_study, "sp-001", "test-study", "idea")
        evt = await queries.create_event(
            db_with_study,
            sprint_id="sp-001",
            event_type="status_change",
            data_json=json.dumps({"from": "pending", "to": "running"}),
        )
        assert evt["event_type"] == "status_change"

        events = await queries.list_events(db_with_study, sprint_id="sp-001")
        assert len(events) == 1

    async def test_list_events_limit(self, db_with_study):
        await queries.create_sprint(db_with_study, "sp-001", "test-study", "idea")
        for i in range(5):
            await queries.create_event(db_with_study, "sp-001", f"evt_{i}")
        events = await queries.list_events(db_with_study, limit=3)
        assert len(events) == 3

    async def test_list_all_events(self, db_with_study):
        await queries.create_sprint(db_with_study, "sp-001", "test-study", "idea")
        await queries.create_event(db_with_study, "sp-001", "evt_1")
        await queries.create_event(db_with_study, None, "system_evt")
        events = await queries.list_events(db_with_study)
        assert len(events) == 2
