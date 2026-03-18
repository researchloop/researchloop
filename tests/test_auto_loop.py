"""Tests for researchloop.sprints.auto_loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from researchloop.core.models import Sprint, SprintStatus
from researchloop.db import queries
from researchloop.sprints.auto_loop import AutoLoopController

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_sprint(
    sid: str = "sp-aaa111",
    study: str = "test-study",
    idea: str = "test idea",
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
    run_sprint_side_effect=None,
) -> AutoLoopController:
    sprint_mgr = AsyncMock()
    if run_sprint_side_effect is not None:
        sprint_mgr.run_sprint.side_effect = run_sprint_side_effect
    else:
        sprint_mgr.run_sprint.return_value = _make_sprint()
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

        with patch("researchloop.sprints.auto_loop.shutil") as mock_shutil:
            mock_shutil.which.return_value = None
            await ctrl.on_sprint_complete("sp-first")

        loop = await queries.get_auto_loop(
            db_with_study,
            "loop-aaa",
        )
        assert loop is not None
        assert loop["completed_count"] == 1
        assert loop["status"] == "running"


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
        next_sprint = _make_sprint(sid="sp-next")

        ctrl = _make_controller(
            db_with_study,
            sample_config,
            run_sprint_side_effect=[next_sprint],
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

        # run_sprint was called with auto-loop marker.
        ctrl.sprint_manager.run_sprint.assert_called_once()
        call_args = ctrl.sprint_manager.run_sprint.call_args
        assert call_args[0][0] == "test-study"
        idea_text = call_args[0][1]
        assert idea_text == "(auto-generating idea...)"

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

        ctrl.sprint_manager.run_sprint.assert_not_called()


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
# _generate_next_idea
# ------------------------------------------------------------------


class TestGenerateNextIdea:
    """The idea generator falls back gracefully."""

    async def test_fallback_when_no_claude(
        self,
        db_with_study,
        sample_config,
    ):
        ctrl = _make_controller(db_with_study, sample_config)

        with patch("researchloop.sprints.auto_loop.shutil") as mock_shutil:
            mock_shutil.which.return_value = None
            idea = await ctrl._generate_next_idea(
                loop_id="loop-eee",
                study_name="test-study",
                sprint_number=2,
                total=4,
            )

        assert "auto-loop loop-eee" in idea
        assert "sprint 2/4" in idea

    async def test_uses_claude_output(
        self,
        db_with_study,
        sample_config,
    ):
        ctrl = _make_controller(db_with_study, sample_config)

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (
            b"Investigate feature absorption",
            b"",
        )
        mock_proc.returncode = 0

        async def _wait_for(coro, **kw):
            return await coro

        with (
            patch("researchloop.sprints.auto_loop.shutil") as mock_shutil,
            patch(
                "researchloop.sprints.auto_loop.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch(
                "researchloop.sprints.auto_loop.asyncio.wait_for",
                side_effect=_wait_for,
            ),
        ):
            mock_shutil.which.return_value = "/usr/bin/claude"
            idea = await ctrl._generate_next_idea(
                loop_id="loop-fff",
                study_name="test-study",
                sprint_number=3,
                total=5,
            )

        assert idea == "Investigate feature absorption"

    async def test_fallback_on_claude_failure(
        self,
        db_with_study,
        sample_config,
    ):
        ctrl = _make_controller(db_with_study, sample_config)

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (
            b"",
            b"error occurred",
        )
        mock_proc.returncode = 1

        async def _wait_for(coro, **kw):
            return await coro

        with (
            patch("researchloop.sprints.auto_loop.shutil") as mock_shutil,
            patch(
                "researchloop.sprints.auto_loop.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch(
                "researchloop.sprints.auto_loop.asyncio.wait_for",
                side_effect=_wait_for,
            ),
        ):
            mock_shutil.which.return_value = "/usr/bin/claude"
            idea = await ctrl._generate_next_idea(
                loop_id="loop-ggg",
                study_name="test-study",
                sprint_number=2,
                total=3,
            )

        assert "auto-loop loop-ggg" in idea
        assert "sprint 2/3" in idea


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
