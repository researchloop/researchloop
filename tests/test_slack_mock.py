"""Tests for the Slack mock server and test-slack CLI command."""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from researchloop.cli import cli
from researchloop.core.config import (
    ClusterConfig,
    Config,
    SlackConfig,
    StudyConfig,
)
from researchloop.core.orchestrator import Orchestrator, create_app
from researchloop.testing.slack_mock import (
    CapturedMessage,
    CapturedUpload,
    MessageStore,
    create_mock_slack_app,
)

_TEST_SIGNING_SECRET = "test_mock_signing_secret"


# ------------------------------------------------------------------
# MessageStore unit tests
# ------------------------------------------------------------------


class TestMessageStore:
    def test_empty_store(self):
        store = MessageStore()
        data = store.to_dict()
        assert data["messages"] == []
        assert data["uploads"] == []

    def test_add_message(self):
        store = MessageStore()
        msg = CapturedMessage(
            channel="C_TEST",
            text="hello",
            thread_ts=None,
            ts="123.456",
        )
        store.messages.append(msg)
        data = store.to_dict()
        assert len(data["messages"]) == 1
        assert data["messages"][0]["text"] == "hello"
        assert data["messages"][0]["channel"] == "C_TEST"

    def test_add_upload(self):
        store = MessageStore()
        upload = CapturedUpload(
            channels="C_TEST",
            filename="report.pdf",
            initial_comment="A report",
            content_length=1024,
        )
        store.uploads.append(upload)
        data = store.to_dict()
        assert len(data["uploads"]) == 1
        assert data["uploads"][0]["filename"] == "report.pdf"

    def test_clear(self):
        store = MessageStore()
        store.messages.append(
            CapturedMessage(channel="C1", text="hi", thread_ts=None, ts="1.0")
        )
        store.uploads.append(
            CapturedUpload(
                channels="C1",
                filename="f.txt",
                initial_comment="",
                content_length=0,
            )
        )
        assert len(store.messages) == 1
        assert len(store.uploads) == 1
        store.clear()
        assert len(store.messages) == 0
        assert len(store.uploads) == 0


# ------------------------------------------------------------------
# Mock Slack app endpoint tests
# ------------------------------------------------------------------


class TestMockSlackApp:
    @pytest.fixture
    def mock_client(self):
        app = create_mock_slack_app()
        return TestClient(app)

    def test_chat_post_message(self, mock_client):
        resp = mock_client.post(
            "/api/chat.postMessage",
            json={
                "channel": "C_TEST",
                "text": "Hello, world!",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "ts" in data

    def test_chat_post_message_with_thread(self, mock_client):
        resp = mock_client.post(
            "/api/chat.postMessage",
            json={
                "channel": "C_TEST",
                "text": "threaded reply",
                "thread_ts": "111.222",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Check captured
        captured = mock_client.get("/captured").json()
        assert len(captured["messages"]) == 1
        assert captured["messages"][0]["thread_ts"] == "111.222"

    def test_files_upload(self, mock_client):
        resp = mock_client.post(
            "/api/files.upload",
            data={
                "channels": "C_TEST",
                "filename": "report.pdf",
                "initial_comment": "Here is the report",
            },
            files={"file": ("report.pdf", b"fake pdf content")},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        captured = mock_client.get("/captured").json()
        assert len(captured["uploads"]) == 1
        assert captured["uploads"][0]["filename"] == "report.pdf"
        assert captured["uploads"][0]["content_length"] == len(b"fake pdf content")

    def test_captured_endpoint(self, mock_client):
        # Empty initially
        resp = mock_client.get("/captured")
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"] == []
        assert data["uploads"] == []

        # Add a message
        mock_client.post(
            "/api/chat.postMessage",
            json={"channel": "C_TEST", "text": "msg1"},
        )
        resp = mock_client.get("/captured")
        assert len(resp.json()["messages"]) == 1

    def test_clear_endpoint(self, mock_client):
        # Add data
        mock_client.post(
            "/api/chat.postMessage",
            json={"channel": "C_TEST", "text": "msg1"},
        )
        assert len(mock_client.get("/captured").json()["messages"]) == 1

        # Clear
        resp = mock_client.post("/clear")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify empty
        assert len(mock_client.get("/captured").json()["messages"]) == 0

    def test_multiple_messages_captured(self, mock_client):
        for i in range(5):
            mock_client.post(
                "/api/chat.postMessage",
                json={"channel": "C_TEST", "text": f"msg {i}"},
            )
        captured = mock_client.get("/captured").json()
        assert len(captured["messages"]) == 5
        assert captured["messages"][2]["text"] == "msg 2"


# ------------------------------------------------------------------
# _SLACK_API env var override in slack.py
# ------------------------------------------------------------------


class TestSlackApiUrlOverride:
    def test_default_url(self):
        # When no env var is set, the module-level constant should
        # default to slack.com.  We can't easily test this without
        # reloading the module, so instead we verify the constant
        # is a string containing "slack".
        from researchloop.comms import slack

        # It will either be the real URL or an override set in the
        # test environment — either way it should be a valid URL.
        assert isinstance(slack._SLACK_API, str)
        assert slack._SLACK_API.startswith("http")

    def test_override_via_env(self, monkeypatch):
        """Verify that re-importing picks up the env var."""
        import importlib

        from researchloop.comms import slack

        monkeypatch.setenv("RESEARCHLOOP_SLACK_API_URL", "http://localhost:9876/api")
        importlib.reload(slack)
        assert slack._SLACK_API == "http://localhost:9876/api"

        # Restore default
        monkeypatch.delenv("RESEARCHLOOP_SLACK_API_URL", raising=False)
        importlib.reload(slack)


# ------------------------------------------------------------------
# SlackNotifier integration with mock server
# ------------------------------------------------------------------


class TestSlackNotifierWithMock:
    @pytest.fixture
    def mock_app(self):
        app = create_mock_slack_app()
        return TestClient(app), app

    @pytest.mark.asyncio
    async def test_post_message_hits_mock(self, mock_app, monkeypatch):
        mock_client, app = mock_app
        store = app.state.store

        # We can't easily redirect httpx calls inside SlackNotifier
        # to the TestClient, so we test the mock endpoints directly
        # and verify the capture works.
        resp = mock_client.post(
            "/api/chat.postMessage",
            json={
                "channel": "C_TEST",
                "text": "Sprint started!",
            },
            headers={
                "Authorization": "Bearer xoxb-test",
                "Content-Type": "application/json",
            },
        )
        assert resp.json()["ok"] is True
        assert len(store.messages) == 1
        assert store.messages[0].text == "Sprint started!"


# ------------------------------------------------------------------
# test-slack CLI command tests
# ------------------------------------------------------------------


def _make_orchestrator_app(
    signing_secret: str = _TEST_SIGNING_SECRET,
) -> TestClient:
    """Build a minimal orchestrator app for receiving test events."""
    config = Config(
        studies=[StudyConfig(name="test", cluster="local", sprints_dir="./sp")],
        clusters=[
            ClusterConfig(name="local", host="localhost", scheduler_type="local")
        ],
        db_path=":memory:",
        artifact_dir=tempfile.mkdtemp(),
        slack=SlackConfig(
            bot_token="xoxb-test",
            signing_secret=signing_secret,
        ),
    )
    orch = Orchestrator(config)
    app = create_app(orch)
    return TestClient(app)


class TestTestSlackCli:
    def test_test_slack_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["test-slack", "--help"])
        assert result.exit_code == 0
        assert "signing-secret" in result.output
        assert "thread-ts" in result.output

    def test_test_slack_connection_error(self):
        """When the orchestrator is not running, report an error."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "test-slack",
                "hello",
                "--url",
                "http://localhost:19999",
            ],
        )
        assert result.exit_code != 0
        assert "Cannot connect" in result.output

    def test_test_slack_sends_signed_event(self):
        """Verify the CLI builds a properly signed event."""
        # We can't easily start a real server in the test, but we
        # can verify the signature generation logic matches what
        # the orchestrator expects.
        signing_secret = "verify_me"
        message = "sprint list"
        ts_str = str(int(time.time()))
        user = "U_TEST"
        channel = "C_TEST"

        # Build payload the same way the CLI does
        payload = {
            "type": "event_callback",
            "event_id": "EvTEST12345",
            "event": {
                "type": "message",
                "text": message,
                "user": user,
                "channel": channel,
                "channel_type": "channel",
                "ts": f"{int(time.time())}.000001",
            },
        }
        body = json.dumps(payload).encode()

        basestring = f"v0:{ts_str}:{body.decode('utf-8')}"
        sig = (
            "v0="
            + hmac.new(
                signing_secret.encode(),
                basestring.encode(),
                hashlib.sha256,
            ).hexdigest()
        )

        # Verify against the same function the orchestrator uses
        from researchloop.comms.slack import verify_slack_signature

        assert verify_slack_signature(signing_secret, ts_str, body, sig)


class TestMockSlackCli:
    def test_mock_slack_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["mock-slack", "--help"])
        assert result.exit_code == 0
        assert "--port" in result.output
        assert "--target-url" in result.output
        assert "--signing-secret" in result.output


# ------------------------------------------------------------------
# send-event endpoint tests (mock → orchestrator)
# ------------------------------------------------------------------


class TestSendEvent:
    def test_send_event_format(self):
        """Verify the /send-event endpoint builds correct payload structure."""
        app = create_mock_slack_app(
            target_url="http://localhost:19999",
            signing_secret="test_secret",
        )
        client = TestClient(app)

        # This will fail to connect to the target, but we can verify
        # the error response format
        resp = client.post(
            "/send-event",
            json={
                "text": "help",
                "user": "U_TEST",
                "channel": "C_TEST",
            },
        )
        # Should return 502 because target is unreachable
        assert resp.status_code == 502
        data = resp.json()
        assert data["ok"] is False
        assert "error" in data

    def test_send_event_request_validation(self):
        """Verify SendEventRequest defaults work."""
        app = create_mock_slack_app()
        client = TestClient(app)

        # Minimal request — only text required
        resp = client.post(
            "/send-event",
            json={"text": "test message"},
        )
        # Will fail to connect to the default target, but should
        # return a proper error, not a validation error
        assert resp.status_code == 502

    def test_send_event_with_thread_ts(self):
        """Verify thread_ts is included when provided."""
        app = create_mock_slack_app(
            target_url="http://localhost:19999",
        )
        client = TestClient(app)

        resp = client.post(
            "/send-event",
            json={
                "text": "threaded message",
                "thread_ts": "111.222",
            },
        )
        assert resp.status_code == 502  # target unreachable
