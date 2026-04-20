"""Tests for researchloop.studies.manager."""

import json

import pytest

from researchloop.core.config import ClusterConfig, Config, StudyConfig
from researchloop.db import queries
from researchloop.studies.manager import StudyManager


class TestStudyManager:
    async def test_sync_creates_studies(self, db, sample_config):
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()

        study = await queries.get_study(db, "test-study")
        assert study is not None
        assert study["cluster"] == "local"
        assert study["description"] == "A test study"

    async def test_sync_updates_existing(self, db, sample_config):
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()

        # Modify config and re-sync
        sample_config.studies[0].description = "Updated description"
        await mgr.sync_from_config()

        study = await queries.get_study(db, "test-study")
        assert study["description"] == "Updated description"

    async def test_get(self, db, sample_config):
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()

        study = await mgr.get("test-study")
        assert study is not None
        assert study["name"] == "test-study"

    async def test_get_nonexistent(self, db, sample_config):
        mgr = StudyManager(db, sample_config)
        assert await mgr.get("nonexistent") is None

    async def test_list_all(self, db, sample_config):
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()

        studies = await mgr.list_all()
        assert len(studies) == 1

    async def test_get_cluster_config(self, db, sample_config):
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()

        cluster = await mgr.get_cluster_config("test-study")
        assert cluster.name == "local"
        assert cluster.host == "localhost"

    async def test_get_cluster_config_missing_study(self, db, sample_config):
        import pytest

        mgr = StudyManager(db, sample_config)
        with pytest.raises(ValueError, match="Study not found"):
            await mgr.get_cluster_config("nonexistent")

    async def test_get_cluster_config_missing_cluster(self, db):
        config = Config(
            studies=[
                StudyConfig(
                    name="orphan", cluster="missing-cluster", sprints_dir="./sp"
                )
            ],
            clusters=[],
        )
        mgr = StudyManager(db, config)
        await mgr.sync_from_config()
        with pytest.raises(ValueError, match="not found in config"):
            await mgr.get_cluster_config("orphan")

    async def test_sync_populates_yaml_config_json(self, db, sample_config):
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()

        row = await queries.get_study(db, "test-study")
        assert row["source"] == "yaml"
        assert row["yaml_config_json"] is not None
        assert row["yaml_config_json"] == row["config_json"]

    async def test_sync_rebuilds_in_memory_studies(self, db, sample_config):
        original_id = id(sample_config.studies)
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()
        # List reference preserved (in-place mutation).
        assert id(sample_config.studies) == original_id
        assert len(sample_config.studies) == 1
        assert sample_config.studies[0].name == "test-study"


class TestStudyManagerSyncStateMachine:
    async def test_sync_adopts_new_yaml_when_no_ui_edits(self, db, sample_config):
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()

        sample_config.studies[0].description = "Updated via YAML"
        await mgr.sync_from_config()

        row = await queries.get_study(db, "test-study")
        cfg = json.loads(row["config_json"])
        assert cfg["description"] == "Updated via YAML"
        # yaml_config_json keeps tracking current YAML
        assert row["yaml_config_json"] == row["config_json"]

    async def test_sync_preserves_ui_edits(self, db, sample_config):
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()

        edited = StudyConfig(
            name="test-study",
            cluster="local",
            description="UI edit",
            sprints_dir="./sprints",
            claude_md_path="./CLAUDE.md",
            red_team_max_rounds=2,
        )
        await mgr.update_study_from_ui("test-study", edited)

        sample_config.studies[0].description = "YAML changed"
        await mgr.sync_from_config()

        row = await queries.get_study(db, "test-study")
        cfg = json.loads(row["config_json"])
        # UI edit is preserved despite YAML change
        assert cfg["description"] == "UI edit"
        # YAML snapshot updated to latest TOML value for revert target
        yaml_cfg = json.loads(row["yaml_config_json"])
        assert yaml_cfg["description"] == "YAML changed"

    async def test_sync_clears_snapshot_when_study_removed_from_yaml(
        self, db, sample_config
    ):
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()

        # Remove the study from YAML; DB row should survive as UI-only.
        sample_config.studies.clear()
        await mgr.sync_from_config()

        row = await queries.get_study(db, "test-study")
        assert row is not None
        assert row["yaml_config_json"] is None
        assert row["source"] == "ui"


class TestStudyManagerUIMutations:
    def _config(self) -> Config:
        return Config(
            studies=[],
            clusters=[
                ClusterConfig(
                    name="local",
                    host="localhost",
                    scheduler_type="local",
                ),
            ],
        )

    async def test_create_study_from_ui_success(self, db):
        mgr = StudyManager(db, self._config())
        await mgr.sync_from_config()
        cfg = StudyConfig(
            name="ui-one",
            cluster="local",
            sprints_dir="./sp",
            description="Made in UI",
        )
        await mgr.create_study_from_ui(cfg)

        row = await queries.get_study(db, "ui-one")
        assert row["source"] == "ui"
        assert row["yaml_config_json"] is None
        assert {s.name for s in mgr.config.studies} == {"ui-one"}

    async def test_create_study_from_ui_rejects_duplicate(self, db):
        mgr = StudyManager(db, self._config())
        await mgr.sync_from_config()
        cfg = StudyConfig(name="dup", cluster="local", sprints_dir="./sp")
        await mgr.create_study_from_ui(cfg)
        with pytest.raises(ValueError, match="already exists"):
            await mgr.create_study_from_ui(cfg)

    async def test_create_study_from_ui_rejects_bad_name(self, db):
        mgr = StudyManager(db, self._config())
        await mgr.sync_from_config()
        bad = StudyConfig(name="Not Ok", cluster="local", sprints_dir="./sp")
        with pytest.raises(ValueError, match="Name"):
            await mgr.create_study_from_ui(bad)

    async def test_create_study_from_ui_rejects_unknown_cluster(self, db):
        mgr = StudyManager(db, self._config())
        await mgr.sync_from_config()
        bad = StudyConfig(name="x", cluster="nope", sprints_dir="./sp")
        with pytest.raises(ValueError, match="Cluster"):
            await mgr.create_study_from_ui(bad)

    async def test_create_study_from_ui_allows_empty_sprints_dir(self, db):
        """sprints_dir is optional; falls back to cluster.working_dir/<name>."""
        mgr = StudyManager(db, self._config())
        await mgr.sync_from_config()
        cfg = StudyConfig(name="x", cluster="local", sprints_dir="")
        await mgr.create_study_from_ui(cfg)
        row = await queries.get_study(db, "x")
        assert row is not None

    async def test_update_study_from_ui_rejects_rename(self, db, sample_config):
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()
        renamed = StudyConfig(name="different", cluster="local", sprints_dir="./sp")
        with pytest.raises(ValueError, match="Renaming"):
            await mgr.update_study_from_ui("test-study", renamed)

    async def test_update_study_from_ui_updates_in_memory(self, db, sample_config):
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()
        edited = StudyConfig(
            name="test-study",
            cluster="local",
            description="new",
            sprints_dir="./sprints",
        )
        await mgr.update_study_from_ui("test-study", edited)
        assert sample_config.studies[0].description == "new"

    async def test_revert_restores_yaml(self, db, sample_config):
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()

        edited = StudyConfig(
            name="test-study",
            cluster="local",
            description="UI change",
            sprints_dir="./sprints",
        )
        await mgr.update_study_from_ui("test-study", edited)
        await mgr.revert_study_to_yaml("test-study")

        row = await queries.get_study(db, "test-study")
        cfg = json.loads(row["config_json"])
        assert cfg["description"] == "A test study"
        assert sample_config.studies[0].description == "A test study"

    async def test_revert_rejected_when_no_snapshot(self, db):
        mgr = StudyManager(db, self._config())
        await mgr.sync_from_config()
        await mgr.create_study_from_ui(
            StudyConfig(name="ui-only", cluster="local", sprints_dir="./sp")
        )
        with pytest.raises(ValueError, match="No YAML version"):
            await mgr.revert_study_to_yaml("ui-only")

    async def test_delete_ui_study_success(self, db):
        mgr = StudyManager(db, self._config())
        await mgr.sync_from_config()
        await mgr.create_study_from_ui(
            StudyConfig(name="ui-del", cluster="local", sprints_dir="./sp")
        )
        await mgr.delete_ui_study("ui-del")
        assert await queries.get_study(db, "ui-del") is None
        assert {s.name for s in mgr.config.studies} == set()

    async def test_delete_ui_rejects_yaml_study(self, db, sample_config):
        mgr = StudyManager(db, sample_config)
        await mgr.sync_from_config()
        with pytest.raises(ValueError, match="Only UI-only studies"):
            await mgr.delete_ui_study("test-study")

    async def test_delete_ui_rejects_when_sprints_exist(self, db):
        mgr = StudyManager(db, self._config())
        await mgr.sync_from_config()
        await mgr.create_study_from_ui(
            StudyConfig(name="ui-sp", cluster="local", sprints_dir="./sp")
        )
        await queries.create_sprint(db, "sp-1", "ui-sp", "idea")
        with pytest.raises(ValueError, match="sprint"):
            await mgr.delete_ui_study("ui-sp")
