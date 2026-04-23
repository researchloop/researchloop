"""Tests for researchloop.sprints.manager."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

from researchloop.core.config import (
    ClusterConfig,
    Config,
    StudyConfig,
)
from researchloop.core.models import SprintStatus
from researchloop.db import queries
from researchloop.sprints.manager import SprintManager, _merge_job_options
from researchloop.studies.manager import StudyManager


class TestSprintManagerCreate:
    async def test_create_sprint(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", "explore SAE features")
        assert sprint.id.startswith("sp-")
        assert sprint.study_name == "test-study"
        assert sprint.idea == "explore SAE features"
        assert sprint.status == SprintStatus.PENDING
        assert sprint.directory is not None

        # Verify in DB
        row = await queries.get_sprint(db_with_study, sprint.id)
        assert row is not None
        assert row["idea"] == "explore SAE features"


class TestSprintManagerQuery:
    async def test_get_sprint(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        result = await mgr.get_sprint(sprint.id)
        assert result is not None
        assert result["id"] == sprint.id

    async def test_get_sprint_nonexistent(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        assert await mgr.get_sprint("sp-nonexistent") is None

    async def test_list_sprints(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        await mgr.create_sprint("test-study", "idea 1")
        await mgr.create_sprint("test-study", "idea 2")
        sprints = await mgr.list_sprints()
        assert len(sprints) == 2

    async def test_list_sprints_filter(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        await mgr.create_sprint("test-study", "idea")
        sprints = await mgr.list_sprints(study_name="test-study")
        assert len(sprints) == 1
        sprints = await mgr.list_sprints(study_name="other")
        assert len(sprints) == 0


class TestSprintManagerCompletion:
    async def test_handle_completion_completed(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", "idea")

        await mgr.handle_completion(
            sprint.id, status="completed", summary="Great results!"
        )

        row = await queries.get_sprint(db_with_study, sprint.id)
        assert row["status"] == "completed"
        assert row["summary"] == "Great results!"
        assert row["completed_at"] is not None

        # Check event was created
        events = await queries.list_events(db_with_study, sprint_id=sprint.id)
        assert len(events) == 1
        assert events[0]["event_type"] == "sprint_completed"

    async def test_handle_completion_failed(self, db_with_study, sample_config):
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", "idea")

        await mgr.handle_completion(sprint.id, status="failed", error="OOM on GPU")

        row = await queries.get_sprint(db_with_study, sprint.id)
        assert row["status"] == "failed"
        assert row["error"] == "OOM on GPU"

    async def test_handle_completion_updates_idea(self, db_with_study, sample_config):
        """When a sprint has no idea (auto-loop), completion should update it."""
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", idea=None)
        row = await queries.get_sprint(db_with_study, sprint.id)
        assert row["idea"] is None

        await mgr.handle_completion(
            sprint.id,
            status="completed",
            summary="Results",
            idea="Investigate feature absorption in SAEs",
        )

        row = await queries.get_sprint(db_with_study, sprint.id)
        assert row["idea"] == "Investigate feature absorption in SAEs"
        assert row["status"] == "completed"

    async def test_handle_completion_preserves_existing_idea(
        self, db_with_study, sample_config
    ):
        """When a sprint already has an idea, completion should not overwrite it."""
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
        )
        sprint = await mgr.create_sprint("test-study", idea="original idea")

        await mgr.handle_completion(
            sprint.id,
            status="completed",
            summary="Results",
            idea="different idea from webhook",
        )

        row = await queries.get_sprint(db_with_study, sprint.id)
        assert row["idea"] == "original idea"

    async def test_cancel_sprint_stops_parent_loop(self, db_with_study, sample_config):
        """Cancelling a loop sprint should stop the parent loop."""
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={"local": AsyncMock()},
        )
        sprint = await mgr.create_sprint("test-study", "loop idea")

        # Assign to a loop.
        await queries.create_auto_loop(
            db_with_study, id="loop-cancel", study_name="test-study", total_count=3
        )
        await queries.update_auto_loop(
            db_with_study,
            "loop-cancel",
            current_sprint_id=sprint.id,
            status="running",
        )
        await queries.update_sprint(db_with_study, sprint.id, loop_id="loop-cancel")

        await mgr.cancel_sprint(sprint.id)

        # Sprint should be cancelled.
        row = await queries.get_sprint(db_with_study, sprint.id)
        assert row["status"] == "cancelled"

        # Loop should be stopped.
        loop = await queries.get_auto_loop(db_with_study, "loop-cancel")
        assert loop["status"] == "stopped"
        assert loop["stopped_at"] is not None

    async def test_cancel_sprint_sends_notification(self, db_with_study, sample_config):
        """Cancelling a sprint should notify via the notification router."""
        from researchloop.comms.router import NotificationRouter

        router = NotificationRouter()
        mock_notifier = AsyncMock()
        router.add_notifier(mock_notifier)

        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={"local": AsyncMock()},
            notification_router=router,
        )
        sprint = await mgr.create_sprint("test-study", "cancel notify test")
        await mgr.cancel_sprint(sprint.id)

        mock_notifier.notify_sprint_failed.assert_called_once()
        call_args = mock_notifier.notify_sprint_failed.call_args
        # Could be positional or keyword args.
        kwargs = call_args.kwargs if call_args.kwargs else {}
        args = call_args.args if call_args.args else ()
        sid = kwargs.get("sprint_id") or (args[0] if args else None)
        err = kwargs.get("error") or (args[2] if len(args) > 2 else "")
        assert sid == sprint.id
        assert "cancelled" in err.lower()

    async def test_handle_completion_fetches_results(
        self, db_with_study, sample_config
    ):
        """Completion should fetch result files from cluster into metadata_json."""
        ssh_mock = AsyncMock()

        # Mock SSH run to return result files.
        async def fake_run(cmd: str) -> tuple[str, str, int]:
            if "report.md" in cmd:
                return ("# Final Report\nGreat findings", "", 0)
            if "findings.md" in cmd:
                return ("## Key Findings\nFound X", "", 0)
            if "progress.md" in cmd:
                return ("Step 1 done", "", 0)
            if "red_team_round_1.md" in cmd:
                return ("## Issues\nNone critical", "", 0)
            if "fixes_round_1.md" in cmd:
                return ("No fixes needed", "", 0)
            if "test -f" in cmd and "report.pdf" in cmd:
                return ("", "", 1)  # No PDF
            if "idea.txt" in cmd:
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
        await mgr.handle_completion(sprint.id, status="completed", summary="Done!")

        row = await queries.get_sprint(db_with_study, sprint.id)
        assert row["status"] == "completed"
        assert row["metadata_json"] is not None
        meta = json.loads(row["metadata_json"])
        assert "# Final Report" in meta["report"]
        assert "Key Findings" in meta["findings"]
        assert "Step 1 done" in meta["progress"]
        assert "Issues" in meta["red_team"]
        assert "No fixes needed" in meta["fixes"]
        assert meta.get("has_pdf") is not True

    async def test_handle_completion_no_study_manager_skips_fetch(
        self, db_with_study, sample_config
    ):
        """Without study_manager, results fetch is gracefully skipped."""
        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
            study_manager=None,
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.handle_completion(sprint.id, status="completed", summary="Done!")

        row = await queries.get_sprint(db_with_study, sprint.id)
        assert row["status"] == "completed"
        assert row["metadata_json"] is None

    async def test_handle_completion_with_notifier(self, db_with_study, sample_config):
        from researchloop.comms.router import NotificationRouter

        router = NotificationRouter()
        mock_notifier = AsyncMock()
        router.add_notifier(mock_notifier)

        mgr = SprintManager(
            db=db_with_study,
            config=sample_config,
            ssh_manager=AsyncMock(),
            schedulers={},
            notification_router=router,
        )
        sprint = await mgr.create_sprint("test-study", "idea")

        await mgr.handle_completion(sprint.id, status="completed", summary="Done!")
        mock_notifier.notify_sprint_completed.assert_called_once()


def _make_config(
    tmp_path: Path,
    global_context: str = "",
    global_context_paths: list[str] | None = None,
    cluster_context: str = "",
    cluster_context_paths: list[str] | None = None,
    study_context: str = "",
    study_claude_md_path: str = "",
) -> Config:
    """Build a Config with context fields set."""
    return Config(
        studies=[
            StudyConfig(
                name="test-study",
                cluster="local",
                sprints_dir=str(tmp_path / "sprints"),
                context=study_context,
                claude_md_path=study_claude_md_path,
            ),
        ],
        clusters=[
            ClusterConfig(
                name="local",
                host="localhost",
                scheduler_type="slurm",
                working_dir=str(tmp_path / "work"),
                context=cluster_context,
                context_paths=cluster_context_paths or [],
            ),
        ],
        context=global_context,
        context_paths=global_context_paths or [],
        db_path=":memory:",
        artifact_dir=str(tmp_path / "artifacts"),
        shared_secret="test",
        orchestrator_url="http://localhost:8080",
    )


def _extract_context(ssh_mock: AsyncMock) -> str | None:
    """Extract the study context from the embedded research prompt.

    The script is written via base64. Inside it, prompt files are
    also written via base64. We decode the script, find the
    research prompt's base64, and decode that.
    """
    import base64

    for call in ssh_mock.run.call_args_list:
        cmd = call.args[0] if call.args else ""
        if "run_sprint.sh" in cmd and "base64 -d" in cmd:
            # Decode the job script.
            start = cmd.index("'") + 1
            end = cmd.index("'", start)
            script = base64.b64decode(cmd[start:end]).decode("utf-8")

            # Find the research prompt base64 line.
            for line in script.split("\n"):
                if "prompt_research.md" in line:
                    # Line: echo '<b64>' | base64 -d > ...
                    b_start = line.index("'") + 1
                    b_end = line.index("'", b_start)
                    prompt = base64.b64decode(line[b_start:b_end]).decode("utf-8")
                    # The context is in the "Study Context"
                    # section of the prompt.
                    return prompt
    return None


class TestContextMerging:
    """Test that cluster + study context is merged correctly."""

    async def test_cluster_inline_only(self, db_with_study, tmp_path):
        config = _make_config(tmp_path, cluster_context="Cluster info here")
        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "123"

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.submit_sprint(sprint.id)

        content = _extract_context(ssh_mock)
        assert content is not None
        assert "Cluster info here" in content

    async def test_study_inline_only(self, db_with_study, tmp_path):
        config = _make_config(tmp_path, study_context="Study about SAEs")
        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "123"

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.submit_sprint(sprint.id)

        content = _extract_context(ssh_mock)
        assert content is not None
        assert "Study about SAEs" in content

    async def test_cluster_and_study_merged(self, db_with_study, tmp_path):
        config = _make_config(
            tmp_path,
            cluster_context="Cluster: 4x A100",
            study_context="Study: transformers",
        )
        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "123"

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.submit_sprint(sprint.id)

        content = _extract_context(ssh_mock)
        assert content is not None
        assert "Cluster: 4x A100" in content
        assert "Study: transformers" in content
        # Cluster context comes first.
        assert content.index("Cluster:") < content.index("Study:")

    async def test_context_file_loaded(self, db_with_study, tmp_path):
        ctx_file = tmp_path / "cluster_info.md"
        ctx_file.write_text("From cluster file", encoding="utf-8")

        config = _make_config(
            tmp_path,
            cluster_context_paths=[str(ctx_file)],
        )
        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "123"

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.submit_sprint(sprint.id)

        content = _extract_context(ssh_mock)
        assert content is not None
        assert "From cluster file" in content

    async def test_study_claude_md_file(self, db_with_study, tmp_path):
        md_file = tmp_path / "study_claude.md"
        md_file.write_text("Study file content", encoding="utf-8")

        config = _make_config(
            tmp_path,
            study_claude_md_path=str(md_file),
        )
        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "123"

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.submit_sprint(sprint.id)

        content = _extract_context(ssh_mock)
        assert content is not None
        assert "Study file content" in content

    async def test_all_four_sources_merged_in_order(self, db_with_study, tmp_path):
        """cluster inline → cluster file → study inline → study file."""
        cluster_file = tmp_path / "cluster.md"
        cluster_file.write_text("2-cluster-file", encoding="utf-8")
        study_file = tmp_path / "study.md"
        study_file.write_text("4-study-file", encoding="utf-8")

        config = _make_config(
            tmp_path,
            cluster_context="1-cluster-inline",
            cluster_context_paths=[str(cluster_file)],
            study_context="3-study-inline",
            study_claude_md_path=str(study_file),
        )
        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "123"

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.submit_sprint(sprint.id)

        content = _extract_context(ssh_mock)
        assert content is not None
        assert content.index("1-cluster-inline") < content.index("2-cluster-file")
        assert content.index("2-cluster-file") < content.index("3-study-inline")
        assert content.index("3-study-inline") < content.index("4-study-file")

    async def test_no_context_empty_study_context(self, db_with_study, tmp_path):
        """No context → prompt has empty study context section."""
        config = _make_config(tmp_path)
        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "123"

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.submit_sprint(sprint.id)

        content = _extract_context(ssh_mock)
        assert content is not None
        # Study context section should be empty.
        assert "## Study Context\n\n\n" in content

    async def test_missing_file_skipped(self, db_with_study, tmp_path):
        """Non-existent context files are silently skipped."""
        config = _make_config(
            tmp_path,
            cluster_context="present",
            cluster_context_paths=[str(tmp_path / "nope.md")],
        )
        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "123"

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.submit_sprint(sprint.id)

        content = _extract_context(ssh_mock)
        assert content is not None
        assert "present" in content
        assert "nope" not in content

    async def test_global_context_comes_first(self, db_with_study, tmp_path):
        config = _make_config(
            tmp_path,
            global_context="0-global",
            cluster_context="1-cluster",
            study_context="2-study",
        )
        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "123"

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.submit_sprint(sprint.id)

        content = _extract_context(ssh_mock)
        assert content is not None
        assert content.index("0-global") < content.index("1-cluster")
        assert content.index("1-cluster") < content.index("2-study")

    async def test_global_context_file(self, db_with_study, tmp_path):
        gfile = tmp_path / "global.md"
        gfile.write_text("Global file content", encoding="utf-8")

        config = _make_config(
            tmp_path,
            global_context_paths=[str(gfile)],
            study_context="study stuff",
        )
        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "123"

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.submit_sprint(sprint.id)

        content = _extract_context(ssh_mock)
        assert content is not None
        assert "Global file content" in content
        assert content.index("Global file") < content.index("study stuff")

    async def test_global_only(self, db_with_study, tmp_path):
        """Global context alone still uploads CLAUDE.md."""
        config = _make_config(tmp_path, global_context="SAELens docs at ...")
        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "123"

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.submit_sprint(sprint.id)

        content = _extract_context(ssh_mock)
        assert content is not None
        assert "SAELens docs at ..." in content


def _extract_job_script(ssh_mock: AsyncMock) -> str | None:
    """Decode the uploaded base64 job script from SSH mock calls."""
    import base64

    for call in ssh_mock.run.call_args_list:
        cmd = call.args[0] if call.args else ""
        if "run_sprint.sh" in cmd and "base64 -d" in cmd:
            start = cmd.index("'") + 1
            end = cmd.index("'", start)
            return base64.b64decode(cmd[start:end]).decode("utf-8")
    return None


class TestMergeJobOptions:
    def test_later_overrides_earlier(self):
        merged = _merge_job_options(
            {"gres": "gpu:1", "mem": "32G"},
            {"mem": "64G"},
        )
        assert merged == {"gres": "gpu:1", "mem": "64G"}

    def test_empty_value_clears_inherited_key(self):
        merged = _merge_job_options(
            {"gres": "gpu:1", "mem": "32G"},
            {"gres": ""},
        )
        assert merged == {"mem": "32G"}

    def test_empty_in_base_layer_is_dropped(self):
        merged = _merge_job_options({"gres": ""}, {"mem": "32G"})
        assert merged == {"mem": "32G"}

    def test_all_three_layers(self):
        merged = _merge_job_options(
            {"gres": "gpu:1", "mem": "32G", "cpus-per-task": "4"},
            {"mem": "64G"},
            {"gres": "", "cpus-per-task": "8"},
        )
        assert merged == {"mem": "64G", "cpus-per-task": "8"}


class TestSubmitSprintJobOptions:
    """Verify empty overrides clear upstream defaults in the rendered script."""

    async def test_empty_gres_override_omits_gres_directive(
        self, db_with_study, tmp_path
    ):
        config = Config(
            studies=[
                StudyConfig(
                    name="test-study",
                    cluster="local",
                    sprints_dir=str(tmp_path / "sprints"),
                    job_options={"gres": "gpu:1", "mem": "32G"},
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

        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "123"

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.submit_sprint(sprint.id, extra_job_options={"gres": ""})

        script = _extract_job_script(ssh_mock)
        assert script is not None
        assert "#SBATCH --gres" not in script
        assert "#SBATCH --mem=32G" in script

    async def test_non_empty_override_replaces_default(self, db_with_study, tmp_path):
        config = Config(
            studies=[
                StudyConfig(
                    name="test-study",
                    cluster="local",
                    sprints_dir=str(tmp_path / "sprints"),
                    job_options={"gres": "gpu:1"},
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

        ssh_mock = AsyncMock()
        ssh_mgr = AsyncMock()
        ssh_mgr.get_connection.return_value = ssh_mock

        scheduler = AsyncMock()
        scheduler.submit.return_value = "123"

        mgr = SprintManager(
            db=db_with_study,
            config=config,
            ssh_manager=ssh_mgr,
            schedulers={"slurm": scheduler},
        )
        sprint = await mgr.create_sprint("test-study", "idea")
        await mgr.submit_sprint(sprint.id, extra_job_options={"gres": "gpu:a100:2"})

        script = _extract_job_script(ssh_mock)
        assert script is not None
        assert "#SBATCH --gres=gpu:a100:2" in script
        assert "#SBATCH --gres=gpu:1" not in script
