"""Tests for the Slack Events API endpoint (/api/slack/events)."""

from __future__ import annotations

import tempfile

from fastapi.testclient import TestClient

from researchloop.core.config import (
    ClusterConfig,
    Config,
    SlackConfig,
    StudyConfig,
)
from researchloop.core.orchestrator import (
    Orchestrator,
    create_app,
)


def _make_app(
    slack: SlackConfig | None = None,
) -> tuple[TestClient, Orchestrator]:
    config = Config(
        studies=[
            StudyConfig(
                name="test",
                cluster="local",
                sprints_dir="./sp",
            )
        ],
        clusters=[
            ClusterConfig(
                name="local",
                host="localhost",
                scheduler_type="local",
            )
        ],
        db_path=":memory:",
        artifact_dir=tempfile.mkdtemp(),
        slack=slack,
    )
    orch = Orchestrator(config)
    app = create_app(orch)
    return TestClient(app), orch


def _event_payload(
    text: str,
    user: str = "U_ALLOWED",
    channel: str = "C_TEST",
    channel_type: str = "channel",
) -> dict:
    return {
        "type": "event_callback",
        "event": {
            "type": "message",
            "text": text,
            "user": user,
            "channel": channel,
            "channel_type": channel_type,
            "ts": "1234.5678",
        },
    }


class TestUrlVerification:
    def test_challenge(self):
        client, _ = _make_app()
        with client:
            resp = client.post(
                "/api/slack/events",
                json={
                    "type": "url_verification",
                    "challenge": "abc123",
                },
            )
            assert resp.status_code == 200
            assert resp.json()["challenge"] == "abc123"


class TestAllowedUsers:
    def test_allowed_user_gets_response(self):
        slack = SlackConfig(
            bot_token="xoxb-test",
            allowed_user_ids=["U_ALLOWED"],
        )
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json=_event_payload("help", user="U_ALLOWED"),
            )
            assert resp.status_code == 200

    def test_unauthorized_user_rejected(self):
        slack = SlackConfig(
            bot_token="xoxb-test",
            allowed_user_ids=["U_ALLOWED"],
        )
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json=_event_payload("help", user="U_INTRUDER"),
            )
            assert resp.status_code == 200
            # The bot responds but with "not authorized"
            # (we can't check the Slack message from here,
            # but we verify it doesn't crash)

    def test_no_restriction_when_empty(self):
        slack = SlackConfig(
            bot_token="xoxb-test",
            allowed_user_ids=[],
        )
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json=_event_payload("help", user="U_ANYONE"),
            )
            assert resp.status_code == 200


class TestChannelRestriction:
    def test_allowed_channel(self):
        slack = SlackConfig(
            bot_token="xoxb-test",
            channel_id="C_ALLOWED",
            restrict_to_channel=True,
        )
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json=_event_payload("help", channel="C_ALLOWED"),
            )
            assert resp.status_code == 200

    def test_wrong_channel_ignored(self):
        slack = SlackConfig(
            bot_token="xoxb-test",
            channel_id="C_ALLOWED",
            restrict_to_channel=True,
        )
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json=_event_payload("help", channel="C_OTHER"),
            )
            # Should succeed but silently ignore.
            assert resp.status_code == 200

    def test_dm_always_allowed(self):
        slack = SlackConfig(
            bot_token="xoxb-test",
            channel_id="C_ALLOWED",
            restrict_to_channel=True,
        )
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json=_event_payload(
                    "help",
                    channel="D_DM_CHANNEL",
                    channel_type="im",
                ),
            )
            assert resp.status_code == 200

    def test_no_restriction_when_disabled(self):
        slack = SlackConfig(
            bot_token="xoxb-test",
            channel_id="C_ALLOWED",
            restrict_to_channel=False,
        )
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json=_event_payload("help", channel="C_RANDOM"),
            )
            assert resp.status_code == 200


class TestBotMessageIgnored:
    def test_bot_messages_ignored(self):
        slack = SlackConfig(bot_token="xoxb-test")
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json={
                    "type": "event_callback",
                    "event": {
                        "type": "message",
                        "text": "I am a bot",
                        "bot_id": "B_BOT",
                        "channel": "C_TEST",
                        "ts": "1111.2222",
                    },
                },
            )
            assert resp.status_code == 200


class TestCommandRouting:
    def test_help_command(self):
        slack = SlackConfig(bot_token="xoxb-test")
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json=_event_payload("help"),
            )
            assert resp.status_code == 200

    def test_auth_status_command(self):
        slack = SlackConfig(bot_token="xoxb-test")
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json=_event_payload("auth status"),
            )
            assert resp.status_code == 200

    def test_sprint_list_command(self):
        slack = SlackConfig(bot_token="xoxb-test")
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json=_event_payload("sprint list"),
            )
            assert resp.status_code == 200

    def test_non_event_callback_ignored(self):
        client, _ = _make_app()
        with client:
            resp = client.post(
                "/api/slack/events",
                json={"type": "something_else"},
            )
            assert resp.status_code == 200


class TestSprintRunRouting:
    """Verify 'sprint run' returns 200 (processed in background)."""

    def test_sprint_run_returns_ok(self):
        slack = SlackConfig(bot_token="xoxb-test")
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json=_event_payload("sprint run test my idea"),
            )
            assert resp.status_code == 200

    def test_sprint_run_missing_idea_no_crash(self):
        slack = SlackConfig(bot_token="xoxb-test")
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json=_event_payload("sprint run test"),
            )
            assert resp.status_code == 200


class TestHelpResponse:
    """Verify help returns 200 (processed in background)."""

    def test_help_returns_ok(self):
        slack = SlackConfig(bot_token="xoxb-test")
        client, _ = _make_app(slack=slack)
        with client:
            resp = client.post(
                "/api/slack/events",
                json=_event_payload("help"),
            )
            assert resp.status_code == 200


class TestConfigParsing:
    def test_allowed_user_ids_parsed(self, tmp_path):
        from researchloop.core.config import load_config

        p = tmp_path / "researchloop.toml"
        p.write_text(
            '[[cluster]]\nname = "c"\nhost = "h"\n\n'
            '[[study]]\nname = "s"\n'
            'cluster = "c"\nsprints_dir = "."\n\n'
            "[slack]\n"
            'bot_token = "xoxb-test"\n'
            'allowed_user_ids = ["U1", "U2"]\n'
            "restrict_to_channel = true\n"
            'channel_id = "C123"\n'
        )
        config = load_config(str(p))
        assert config.slack is not None
        assert config.slack.allowed_user_ids == ["U1", "U2"]
        assert config.slack.restrict_to_channel is True
        assert config.slack.channel_id == "C123"

    def test_allowed_user_ids_from_env(self, tmp_path, monkeypatch):
        from researchloop.core.config import load_config

        p = tmp_path / "researchloop.toml"
        p.write_text(
            '[[cluster]]\nname = "c"\nhost = "h"\n\n'
            '[[study]]\nname = "s"\n'
            'cluster = "c"\nsprints_dir = "."\n'
        )
        monkeypatch.setenv("RESEARCHLOOP_SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv(
            "RESEARCHLOOP_SLACK_ALLOWED_USER_IDS",
            "U1,U2,U3",
        )
        config = load_config(str(p))
        assert config.slack is not None
        assert config.slack.allowed_user_ids == [
            "U1",
            "U2",
            "U3",
        ]
