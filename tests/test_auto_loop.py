"""Tests for researchloop.sprints.auto_loop."""

from __future__ import annotations

from unittest.mock import AsyncMock

from researchloop.core.models import Sprint, SprintStatus
from researchloop.db import queries
from researchloop.sprints.auto_loop import AutoLoopController

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_sprint(
    sid: str = "sp-aaa111",
    study: str = "test-study",
    idea: str | None = "test idea",
) -> Sprint:
    return Sprint(
        id=sid,
        study_name=study,
        idea=idea,
        status=SprintStatus.SUBMITTED,
    )


def _make_controller(
    db,
    config,
    create_sprint_return=None,
    submit_sprint_return="job-123",
) -> AutoLoopController:
    sprint_mgr = AsyncMock()
    sprint_mgr.create_sprint.return_value = create_sprint_return or _make_sprint()
    sprint_mgr.submit_sprint.return_value = submit_sprint_return
    # Keep run_sprint for tests that don't need the split
    sprint_mgr.run_sprint.return_value = create_sprint_return or _make_sprint()
    return AutoLoopController(
        db=db,
        sprint_manager=sprint_mgr,
        config=config,
    )


# ------------------------------------------------------------------
# on_sprint_complete
# ------------------------------------------------------------------


class TestOnSprintCompleteIncrement:
    """completed_count is incremented when a loop sprint finishes."""

    async def test_increments_completed_count(
        self,
        db_with_study,
        sample_config,
    ):
        ctrl = _make_controller(db_with_study, sample_config)

        # Manually create an auto-loop with 3 total sprints.
        await queries.create_auto_loop(
            db_with_study,
            id="loop-aaa",
            study_name="test-study",
            total_count=3,
        )
        await queries.update_auto_loop(
            db_with_study,
            "loop-aaa",
            current_sprint_id="sp-first",
            status="running",
        )

        await ctrl.on_sprint_complete("sp-first")

        loop = await queries.get_auto_loop(
            db_with_study,
            "loop-aaa",
        )
        assert loop is not None
        assert loop["completed_count"] == 1
        assert loop["status"] == "running"


class TestOnSprintCompleteStopsOnFailure:
    """Loop stops when a sprint fails."""

    async def test_stops_loop_on_failed_sprint(
        self,
        db_with_study,
        sample_config,
    ):
        ctrl = _make_controller(db_with_study, sample_config)

        await queries.create_auto_loop(
            db_with_study,
            id="loop-fail",
            study_name="test-study",
            total_count=3,
        )
        await queries.update_auto_loop(
            db_with_study,
            "loop-fail",
            current_sprint_id="sp-broken",
            status="running",
            completed_count=0,
        )

        # Create a sprint marked as failed.
        await queries.create_sprint(
            db_with_study, "sp-broken", "test-study", "bad idea"
        )
        await queries.update_sprint(db_with_study, "sp-broken", status="failed")

        await ctrl.on_sprint_complete("sp-broken")

        loop = await queries.get_auto_loop(db_with_study, "loop-fail")
        assert loop is not None
        assert loop["status"] == "failed"
        assert loop["stopped_at"] is not None

        # No new sprint should have been created.
        ctrl.sprint_manager.create_sprint.assert_not_called()


class TestOnSprintCompleteMarksCompleted:
    """Loop is marked completed when all sprints are done."""

    async def test_marks_loop_completed(
        self,
        db_with_study,
        sample_config,
    ):
        ctrl = _make_controller(db_with_study, sample_config)

        await queries.create_auto_loop(
            db_with_study,
            id="loop-bbb",
            study_name="test-study",
            total_count=2,
        )
        await queries.update_auto_loop(
            db_with_study,
            "loop-bbb",
            current_sprint_id="sp-last",
            status="running",
            completed_count=1,
        )

        await ctrl.on_sprint_complete("sp-last")

        loop = await queries.get_auto_loop(
            db_with_study,
            "loop-bbb",
        )
        assert loop is not None
        assert loop["completed_count"] == 2
        assert loop["status"] == "completed"
        assert loop["stopped_at"] is not None


class TestOnSprintCompleteStartsNext:
    """A new sprint is started when more remain in the loop."""

    async def test_starts_next_sprint(
        self,
        db_with_study,
        sample_config,
    ):
        next_sprint = _make_sprint(sid="sp-next", idea=None)

        ctrl = _make_controller(
            db_with_study,
            sample_config,
            create_sprint_return=next_sprint,
        )

        await queries.create_auto_loop(
            db_with_study,
            id="loop-ccc",
            study_name="test-study",
            total_count=3,
        )
        await queries.update_auto_loop(
            db_with_study,
            "loop-ccc",
            current_sprint_id="sp-done",
            status="running",
            completed_count=0,
        )

        await ctrl.on_sprint_complete("sp-done")

        # create_sprint was called with None idea.
        ctrl.sprint_manager.create_sprint.assert_called_once_with("test-study", None)
        # submit_sprint was called after loop_id was set.
        ctrl.sprint_manager.submit_sprint.assert_called_once_with(
            "sp-next", extra_job_options=None
        )

        # current_sprint_id updated.
        loop = await queries.get_auto_loop(
            db_with_study,
            "loop-ccc",
        )
        assert loop is not None
        assert loop["current_sprint_id"] == "sp-next"


class TestOnSprintCompleteIgnoresNonLoop:
    """Sprints not belonging to any loop are silently ignored."""

    async def test_ignores_non_loop_sprint(
        self,
        db_with_study,
        sample_config,
    ):
        ctrl = _make_controller(db_with_study, sample_config)

        # No auto-loop exists, so this should be a no-op.
        await ctrl.on_sprint_complete("sp-orphan")

        ctrl.sprint_manager.create_sprint.assert_not_called()
        ctrl.sprint_manager.submit_sprint.assert_not_called()


# ------------------------------------------------------------------
# start — loop_id set before submission
# ------------------------------------------------------------------


class TestStartSetsLoopIdBeforeSubmit:
    """loop_id must be set on the sprint BEFORE submit_sprint is called."""

    async def test_loop_id_set_before_submit(
        self,
        db_with_study,
        sample_config,
    ):
        """Verify that loop_id is set in DB before submit_sprint runs.

        This is the root cause of the 'auto-generating idea...' bug:
        if loop_id isn't set before submission, submit_sprint won't
        include the idea generator prompt in the job script.
        """
        sprint = _make_sprint(sid="sp-loop1", idea=None)
        ctrl = _make_controller(
            db_with_study,
            sample_config,
            create_sprint_return=sprint,
        )

        # Create the sprint in DB first (as create_sprint would).
        await queries.create_sprint(
            db_with_study,
            id="sp-loop1",
            study_name="test-study",
            idea=None,
        )

        # Track what loop_id was at submit time.
        loop_id_at_submit: list[str | None] = []

        async def tracking_submit(sprint_id, **_kw):
            row = await queries.get_sprint(db_with_study, sprint_id)
            loop_id_at_submit.append(row.get("loop_id") if row else None)
            return "job-123"

        ctrl.sprint_manager.submit_sprint.side_effect = tracking_submit

        loop_id = await ctrl.start("test-study", 3)

        assert loop_id.startswith("loop-")
        assert len(loop_id_at_submit) == 1
        assert loop_id_at_submit[0] is not None, (
            "loop_id must be set BEFORE submit_sprint is called"
        )

    async def test_on_sprint_complete_sets_loop_id_before_submit(
        self,
        db_with_study,
        sample_config,
    ):
        """Verify loop_id is set before submit on subsequent sprints too."""
        next_sprint = _make_sprint(sid="sp-next2", idea=None)
        ctrl = _make_controller(
            db_with_study,
            sample_config,
            create_sprint_return=next_sprint,
        )

        # Set up loop and create the next sprint in DB.
        await queries.create_auto_loop(
            db_with_study,
            id="loop-order",
            study_name="test-study",
            total_count=3,
        )
        await queries.update_auto_loop(
            db_with_study,
            "loop-order",
            current_sprint_id="sp-prev",
            status="running",
            completed_count=0,
        )
        await queries.create_sprint(
            db_with_study,
            id="sp-next2",
            study_name="test-study",
            idea=None,
        )

        loop_id_at_submit: list[str | None] = []

        async def check_loop_id_submit(sprint_id, **_kw):
            row = await queries.get_sprint(db_with_study, sprint_id)
            loop_id_at_submit.append(row.get("loop_id") if row else None)
            return "job-456"

        ctrl.sprint_manager.submit_sprint.side_effect = check_loop_id_submit

        await ctrl.on_sprint_complete("sp-prev")

        assert len(loop_id_at_submit) == 1
        assert loop_id_at_submit[0] == "loop-order", (
            f"Expected loop_id='loop-order', got {loop_id_at_submit[0]!r}"
        )


# ------------------------------------------------------------------
# stop
# ------------------------------------------------------------------


class TestStopCancelsCurrentSprint:
    """Stopping a loop cancels the current sprint."""

    async def test_stop_cancels_sprint(
        self,
        db_with_study,
        sample_config,
    ):
        ctrl = _make_controller(db_with_study, sample_config)

        await queries.create_auto_loop(
            db_with_study,
            id="loop-ddd",
            study_name="test-study",
            total_count=5,
        )
        await queries.update_auto_loop(
            db_with_study,
            "loop-ddd",
            current_sprint_id="sp-running",
            status="running",
        )

        await ctrl.stop("loop-ddd")

        ctrl.sprint_manager.cancel_sprint.assert_called_once_with(
            "sp-running",
        )

        loop = await queries.get_auto_loop(
            db_with_study,
            "loop-ddd",
        )
        assert loop is not None
        assert loop["status"] == "stopped"
        assert loop["stopped_at"] is not None


# ------------------------------------------------------------------
# resume
# ------------------------------------------------------------------


class TestResume:
    """Resuming a stopped/failed loop."""

    async def test_resume_stopped_loop(self, db_with_study, sample_config):
        """Resume submits a new sprint and sets status back to running."""
        ctrl = _make_controller(db_with_study, sample_config)

        await queries.create_auto_loop(
            db_with_study,
            id="loop-resume",
            study_name="test-study",
            total_count=3,
        )
        await queries.update_auto_loop(
            db_with_study,
            "loop-resume",
            status="stopped",
            completed_count=1,
            stopped_at="2026-01-01T00:00:00Z",
        )

        sprint_id = await ctrl.resume("loop-resume")
        assert sprint_id is not None

        loop = await queries.get_auto_loop(db_with_study, "loop-resume")
        assert loop is not None
        assert loop["status"] == "running"
        assert loop["stopped_at"] is None
        assert loop["current_sprint_id"] == sprint_id

    async def test_resume_failed_loop(self, db_with_study, sample_config):
        """Can resume a failed loop too."""
        ctrl = _make_controller(db_with_study, sample_config)

        await queries.create_auto_loop(
            db_with_study,
            id="loop-rfail",
            study_name="test-study",
            total_count=3,
        )
        await queries.update_auto_loop(
            db_with_study,
            "loop-rfail",
            status="failed",
            completed_count=1,
        )

        sprint_id = await ctrl.resume("loop-rfail")
        assert sprint_id is not None

        loop = await queries.get_auto_loop(db_with_study, "loop-rfail")
        assert loop is not None
        assert loop["status"] == "running"

    async def test_resume_completed_loop_raises(self, db_with_study, sample_config):
        """Cannot resume a loop that already completed all sprints."""
        import pytest

        ctrl = _make_controller(db_with_study, sample_config)

        await queries.create_auto_loop(
            db_with_study,
            id="loop-done",
            study_name="test-study",
            total_count=2,
        )
        await queries.update_auto_loop(
            db_with_study,
            "loop-done",
            status="stopped",
            completed_count=2,
        )

        with pytest.raises(ValueError, match="already completed"):
            await ctrl.resume("loop-done")

    async def test_resume_running_loop_raises(self, db_with_study, sample_config):
        """Cannot resume an already-running loop."""
        import pytest

        ctrl = _make_controller(db_with_study, sample_config)

        await queries.create_auto_loop(
            db_with_study,
            id="loop-run",
            study_name="test-study",
            total_count=3,
        )
        await queries.update_auto_loop(
            db_with_study,
            "loop-run",
            status="running",
        )

        with pytest.raises(ValueError, match="Cannot resume"):
            await ctrl.resume("loop-run")


# ------------------------------------------------------------------
# job_options pass-through
# ------------------------------------------------------------------


class TestJobOptionsPassthrough:
    """Loop job_options stored in metadata and passed to sprints."""

    async def test_start_stores_job_options(self, db_with_study, sample_config):
        """start() with job_options stores them in metadata_json."""
        ctrl = _make_controller(db_with_study, sample_config)

        loop_id = await ctrl.start(
            "test-study",
            count=2,
            job_options={"gres": "gpu:l40:1", "mem": "64G"},
        )

        loop = await queries.get_auto_loop(db_with_study, loop_id)
        assert loop is not None
        import json

        meta = json.loads(loop["metadata_json"])
        assert meta["job_options"] == {"gres": "gpu:l40:1", "mem": "64G"}

    async def test_start_passes_job_options_to_submit(
        self, db_with_study, sample_config
    ):
        """start() passes job_options to submit_sprint."""
        ctrl = _make_controller(db_with_study, sample_config)

        await ctrl.start(
            "test-study",
            count=2,
            job_options={"gres": "gpu:l40:2"},
        )

        ctrl.sprint_manager.submit_sprint.assert_called_once()
        call_kw = ctrl.sprint_manager.submit_sprint.call_args.kwargs
        assert call_kw.get("extra_job_options") == {"gres": "gpu:l40:2"}


# ------------------------------------------------------------------
# allow_loop guard
# ------------------------------------------------------------------


class TestAllowLoopGuard:
    """Studies with allow_loop=false reject auto-loops."""

    async def test_start_blocked(self, db_with_study):
        import pytest

        from researchloop.core.config import (
            ClusterConfig,
            Config,
            StudyConfig,
        )

        config = Config(
            studies=[
                StudyConfig(
                    name="test-study",
                    cluster="local",
                    sprints_dir="./sp",
                    allow_loop=False,
                ),
            ],
            clusters=[
                ClusterConfig(
                    name="local",
                    host="localhost",
                ),
            ],
        )
        ctrl = _make_controller(db_with_study, config)

        with pytest.raises(ValueError, match="allow_loop"):
            await ctrl.start("test-study", 5)

    async def test_start_allowed_by_default(self, db_with_study, sample_config):
        ctrl = _make_controller(db_with_study, sample_config)
        loop_id = await ctrl.start("test-study", 2)
        assert loop_id.startswith("loop-")
