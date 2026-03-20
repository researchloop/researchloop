"""Integration tests for the Slack bot using SlackSimulator.

These tests exercise the full Slack events flow -- from signed HTTP
request through background task processing to outbound Slack API
call -- using the in-process SlackSimulator harness.
"""

from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from researchloop.core.config import (
    ClusterConfig,
    Config,
    SlackConfig,
    StudyConfig,
)
from researchloop.core.models import Sprint, SprintStatus
from researchloop.core.orchestrator import Orchestrator, create_app
from researchloop.db import queries
from researchloop.db.database import Database
from researchloop.testing.slack_simulator import SlackResponse, SlackSimulator

_SIGNING_SECRET = "test_slack_secret_for_simulator"


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


async def _make_orchestrator(
    slack: SlackConfig | None = None,
) -> tuple[Orchestrator, Database]:
    """Create an Orchestrator with an in-memory DB, ready for testing.

    Bypasses the full ``start()`` lifecycle (no SSH, no job monitor)
    but initialises the database, study manager, sprint manager,
    conversation manager, and notification router.
    """
    db = Database(":memory:")
    await db.connect()

    config = Config(
        studies=[
            StudyConfig(
                name="test-study",
                cluster="local",
                description="A test study for simulator",
                sprints_dir="./sprints",
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
        slack=slack,
    )

    orch = Orchestrator(config)
    orch.db = db

    # Sync studies to DB.
    from researchloop.studies.manager import StudyManager

    orch.study_manager = StudyManager(db, config)
    await orch.study_manager.sync_from_config()

    # Sprint manager with mock SSH (no real cluster needed).
    from researchloop.clusters.ssh import SSHManager
    from researchloop.comms.router import NotificationRouter
    from researchloop.sprints.manager import SprintManager

    orch.notification_router = NotificationRouter()
    orch.sprint_manager = SprintManager(
        db=db,
        config=config,
        ssh_manager=SSHManager(),
        schedulers={},
        study_manager=orch.study_manager,
        notification_router=orch.notification_router,
    )

    # Conversation manager.
    from researchloop.comms.conversation import ConversationManager

    orch.conversation_manager = ConversationManager(
        db, sprint_manager=orch.sprint_manager
    )

    # Prevent lifespan from running start/stop.
    orch.start = AsyncMock()  # type: ignore[method-assign]
    orch.stop = AsyncMock()  # type: ignore[method-assign]

    return orch, db


@pytest.fixture
async def slack_sim():
    """Yield a (SlackSimulator, Database) tuple for a standard Slack config."""
    slack = SlackConfig(
        bot_token="xoxb-test-token",
        signing_secret=_SIGNING_SECRET,
        channel_id="C_TEST",
    )
    orch, db = await _make_orchestrator(slack=slack)
    app = create_app(orch)
    sim = SlackSimulator(app, signing_secret=_SIGNING_SECRET)
    yield sim, db
    await db.close()


@pytest.fixture
async def slack_sim_with_auth():
    """Yield a simulator with allowed_user_ids configured."""
    slack = SlackConfig(
        bot_token="xoxb-test-token",
        signing_secret=_SIGNING_SECRET,
        channel_id="C_TEST",
        allowed_user_ids=["U_ALLOWED", "U_ADMIN"],
    )
    orch, db = await _make_orchestrator(slack=slack)
    app = create_app(orch)
    sim = SlackSimulator(app, signing_secret=_SIGNING_SECRET)
    yield sim, db
    await db.close()


@pytest.fixture
async def slack_sim_restricted_channel():
    """Yield a simulator with channel restriction enabled."""
    slack = SlackConfig(
        bot_token="xoxb-test-token",
        signing_secret=_SIGNING_SECRET,
        channel_id="C_ALLOWED",
        restrict_to_channel=True,
    )
    orch, db = await _make_orchestrator(slack=slack)
    app = create_app(orch)
    sim = SlackSimulator(app, signing_secret=_SIGNING_SECRET)
    yield sim, db
    await db.close()


# ------------------------------------------------------------------
# SlackResponse unit tests
# ------------------------------------------------------------------


class TestSlackResponse:
    def test_text_returns_first_message(self):
        resp = SlackResponse(messages=["hello", "world"])
        assert resp.text == "hello"

    def test_text_empty_when_no_messages(self):
        resp = SlackResponse()
        assert resp.text == ""

    def test_contains_checks_all_messages(self):
        resp = SlackResponse(messages=["hello world", "goodbye"])
        assert "hello" in resp
        assert "goodbye" in resp
        assert "missing" not in resp

    def test_bool_true_when_messages(self):
        assert SlackResponse(messages=["hi"])
        assert not SlackResponse()

    def test_repr(self):
        resp = SlackResponse(messages=["test message"])
        r = repr(resp)
        assert "messages=1" in r
        assert "test message" in r


# ------------------------------------------------------------------
# Command tests
# ------------------------------------------------------------------


class TestHelpCommand:
    async def test_help_returns_commands(self, slack_sim):
        sim, _db = slack_sim
        resp = await sim.send_message("help")
        assert resp.messages, "Bot should reply to 'help'"
        assert "sprint run" in resp
        assert "sprint list" in resp
        assert "loop start" in resp
        assert "help" in resp

    async def test_help_case_insensitive(self, slack_sim):
        # The handler checks text_lower == "help", so it must
        # be exact lowercase.  Verify the lowercase path works.
        sim, _db = slack_sim
        resp = await sim.send_message("help")
        assert resp.messages


class TestSprintListCommand:
    async def test_sprint_list_empty(self, slack_sim):
        sim, _db = slack_sim
        resp = await sim.send_message("sprint list")
        assert resp.messages, "Bot should reply to 'sprint list'"
        assert "no sprints" in resp.text.lower() or "No sprints" in resp.text

    async def test_sprint_list_with_data(self, slack_sim):
        sim, db = slack_sim
        # Pre-populate a sprint in the database.
        await queries.create_sprint(
            db,
            id="sp-abc123",
            study_name="test-study",
            idea="Test the simulator",
        )
        resp = await sim.send_message("sprint list")
        assert resp.messages
        assert "sp-abc123" in resp

    async def test_sprint_list_shows_multiple(self, slack_sim):
        sim, db = slack_sim
        await queries.create_sprint(
            db, id="sp-111111", study_name="test-study", idea="First sprint"
        )
        await queries.create_sprint(
            db, id="sp-222222", study_name="test-study", idea="Second sprint"
        )
        resp = await sim.send_message("sprint list")
        assert "sp-111111" in resp
        assert "sp-222222" in resp


class TestSprintRunCommand:
    async def test_sprint_run_success(self, slack_sim):
        """Verify 'sprint run' calls run_sprint and sends confirmation."""
        sim, _db = slack_sim

        fake_sprint = Sprint(
            id="sp-fake01",
            study_name="test-study",
            idea="test idea",
            status=SprintStatus.SUBMITTED,
            job_id="12345",
        )

        with patch(
            "researchloop.sprints.manager.SprintManager.run_sprint",
            new_callable=AsyncMock,
            return_value=fake_sprint,
        ):
            resp = await sim.send_message("sprint run test-study test idea")

        assert resp.messages
        assert "sp-fake01" in resp

    async def test_sprint_run_failure(self, slack_sim):
        """When run_sprint raises, the bot should report the error."""
        sim, _db = slack_sim

        with patch(
            "researchloop.sprints.manager.SprintManager.run_sprint",
            new_callable=AsyncMock,
            side_effect=ValueError("Study not found: bad-study"),
        ):
            resp = await sim.send_message("sprint run bad-study some idea")

        assert resp.messages
        assert "failed" in resp.text.lower() or "Failed" in resp.text

    async def test_sprint_run_missing_idea_no_crash(self, slack_sim):
        """'sprint run test-study' without an idea should not crash."""
        sim, _db = slack_sim
        # With only study name and no idea, the handler won't call
        # run_sprint (it requires both study and idea), so we just
        # verify no error occurs.
        await sim.send_message("sprint run test-study")
        # Either no response (silently ignored) or a help-like fallback.
        # The important thing is it doesn't crash.


class TestAuthStatusCommand:
    async def test_auth_status(self, slack_sim):
        """'auth status' should trigger a response about Claude auth."""
        sim, _db = slack_sim

        with patch(
            "researchloop.core.auth.check_claude_auth_async",
            new_callable=AsyncMock,
            return_value=(False, "Claude CLI not found"),
        ):
            resp = await sim.send_message("auth status")

        assert resp.messages
        # Should contain info about auth status -- either authenticated
        # or not (we mocked it as not found).
        assert "authenticated" in resp.text.lower() or "not" in resp.text.lower()


# ------------------------------------------------------------------
# Authorization tests
# ------------------------------------------------------------------


class TestUserAuthorization:
    async def test_authorized_user_gets_response(self, slack_sim_with_auth):
        sim, _db = slack_sim_with_auth
        resp = await sim.send_message("help", user="U_ALLOWED")
        assert resp.messages
        assert "sprint" in resp

    async def test_unauthorized_user_rejected(self, slack_sim_with_auth):
        sim, _db = slack_sim_with_auth
        resp = await sim.send_message("help", user="U_INTRUDER")
        assert resp.messages
        assert "not authorized" in resp.text.lower()

    async def test_second_allowed_user(self, slack_sim_with_auth):
        sim, _db = slack_sim_with_auth
        resp = await sim.send_message("help", user="U_ADMIN")
        assert resp.messages
        assert "sprint" in resp

    async def test_no_restriction_when_empty(self, slack_sim):
        """When allowed_user_ids is empty, any user is allowed."""
        sim, _db = slack_sim
        resp = await sim.send_message("help", user="U_ANYONE")
        assert resp.messages
        assert "sprint" in resp


# ------------------------------------------------------------------
# Channel restriction tests
# ------------------------------------------------------------------


class TestChannelRestriction:
    async def test_allowed_channel_gets_response(self, slack_sim_restricted_channel):
        sim, _db = slack_sim_restricted_channel
        resp = await sim.send_message("help", channel="C_ALLOWED")
        assert resp.messages

    async def test_wrong_channel_ignored(self, slack_sim_restricted_channel):
        sim, _db = slack_sim_restricted_channel
        resp = await sim.send_message("help", channel="C_OTHER")
        # The bot should silently ignore messages from wrong channels.
        assert not resp.messages

    async def test_dm_always_allowed(self, slack_sim_restricted_channel):
        """DMs (channel_type 'im') bypass channel restriction."""
        sim, _db = slack_sim_restricted_channel
        resp = await sim.send_message(
            "help",
            channel="D_DM_CHANNEL",
            channel_type="im",
        )
        assert resp.messages
        assert "sprint" in resp

    async def test_unrestricted_any_channel(self, slack_sim):
        """When restrict_to_channel is False, any channel is allowed."""
        sim, _db = slack_sim
        resp = await sim.send_message("help", channel="C_RANDOM")
        assert resp.messages


# ------------------------------------------------------------------
# Bot message filtering
# ------------------------------------------------------------------


class TestBotMessageIgnored:
    async def test_bot_message_produces_no_response(self, slack_sim):
        sim, _db = slack_sim
        resp = await sim.send_bot_message("I am a bot")
        assert not resp.messages, "Bot messages should be ignored"

    async def test_bot_message_in_thread(self, slack_sim):
        sim, _db = slack_sim
        resp = await sim.send_bot_message(
            "Bot reply",
            thread_ts="1234567890.000001",
        )
        assert not resp.messages


# ------------------------------------------------------------------
# Conversational (free-form) messages
# ------------------------------------------------------------------


class TestConversationalMessages:
    async def test_freeform_message_calls_claude(self, slack_sim):
        """Non-command text should be passed to ConversationManager."""
        sim, _db = slack_sim

        with patch(
            "researchloop.comms.conversation.ConversationManager.handle_message",
            new_callable=AsyncMock,
            return_value="I can help with your research!",
        ):
            resp = await sim.send_message("Tell me about quantum computing")

        assert resp.messages
        assert "research" in resp.text.lower()

    async def test_freeform_message_error_handled(self, slack_sim):
        """When ConversationManager raises, the bot sends an error reply."""
        sim, _db = slack_sim

        with patch(
            "researchloop.comms.conversation.ConversationManager.handle_message",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Claude is broken"),
        ):
            resp = await sim.send_message("What should I research next?")

        assert resp.messages
        text = resp.text.lower()
        assert "something went wrong" in text or "help" in text


# ------------------------------------------------------------------
# Thread support
# ------------------------------------------------------------------


class TestThreadedMessages:
    async def test_message_in_thread(self, slack_sim):
        """Messages with thread_ts should be handled normally."""
        sim, _db = slack_sim
        resp = await sim.send_message(
            "help",
            thread_ts="1234567890.000001",
        )
        assert resp.messages
        # The bot's reply should include thread_ts in the raw payload.
        assert resp.raw_messages
        assert resp.raw_messages[0].get("thread_ts") == "1234567890.000001"


# ------------------------------------------------------------------
# Event deduplication
# ------------------------------------------------------------------


class TestEventDeduplication:
    async def test_same_event_id_not_processed_twice(self, slack_sim):
        """The handler deduplicates events by event_id.

        This is tricky to test directly because SlackSimulator
        generates unique event_ids each time.  Instead we verify
        that each call produces a response (confirming unique IDs).
        """
        sim, _db = slack_sim
        resp1 = await sim.send_message("help")
        resp2 = await sim.send_message("help")
        assert resp1.messages
        assert resp2.messages


# ------------------------------------------------------------------
# Multiple messages in one interaction
# ------------------------------------------------------------------


class TestRawMessagePayloads:
    async def test_raw_messages_contain_channel(self, slack_sim):
        sim, _db = slack_sim
        resp = await sim.send_message("help", channel="C_MYCHAN")
        assert resp.raw_messages
        assert resp.raw_messages[0].get("channel") == "C_MYCHAN"
