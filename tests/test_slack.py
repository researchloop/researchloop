"""Tests for Slack integration -- notifier and signature verification."""

from __future__ import annotations

import hashlib
import hmac
import time
from unittest.mock import AsyncMock, MagicMock, patch

from researchloop.comms.slack import (
    SlackNotifier,
    verify_slack_signature,
)

# ------------------------------------------------------------------
# SlackNotifier
# ------------------------------------------------------------------


class TestSlackNotifier:
    async def test_notify_sprint_started(self):
        """Notifier POSTs to Slack chat.postMessage."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "researchloop.comms.slack.httpx.AsyncClient", return_value=mock_client
        ):
            notifier = SlackNotifier(bot_token="xoxb-test", channel_id="C123")
            await notifier.notify_sprint_started("sp-001", "my-study", "test idea")

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert "chat.postMessage" in call_kwargs[0][0]
        payload = call_kwargs[1]["json"]
        assert payload["channel"] == "C123"
        assert "sp-001" in payload["text"]

    async def test_notify_sprint_completed(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "researchloop.comms.slack.httpx.AsyncClient", return_value=mock_client
        ):
            notifier = SlackNotifier(bot_token="xoxb-test", channel_id="C123")
            await notifier.notify_sprint_completed(
                "sp-002", "study-x", "All tests passed"
            )

        mock_client.post.assert_called_once()
        payload = mock_client.post.call_args[1]["json"]
        assert "sp-002" in payload["text"]
        assert "All tests passed" in payload["text"]

    async def test_notify_sprint_failed(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "researchloop.comms.slack.httpx.AsyncClient", return_value=mock_client
        ):
            notifier = SlackNotifier(bot_token="xoxb-test", channel_id="C123")
            await notifier.notify_sprint_failed("sp-003", "study-y", "OOM error")

        mock_client.post.assert_called_once()
        payload = mock_client.post.call_args[1]["json"]
        assert "sp-003" in payload["text"]
        assert "OOM error" in payload["text"]

    async def test_no_channel_returns_empty(self):
        """When no channel is configured, return empty dict."""
        notifier = SlackNotifier(bot_token="xoxb-test")
        await notifier.notify_sprint_started("sp-001", "study", "idea")
        # No exception raised, no API call made


# ------------------------------------------------------------------
# verify_slack_signature
# ------------------------------------------------------------------


class TestVerifySlackSignature:
    def _make_signature(self, secret: str, timestamp: str, body: bytes) -> str:
        basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        return (
            "v0="
            + hmac.new(
                secret.encode(),
                basestring.encode(),
                hashlib.sha256,
            ).hexdigest()
        )

    def test_valid_signature(self):
        secret = "test-signing-secret"
        ts = str(int(time.time()))
        body = b'{"type":"url_verification"}'
        sig = self._make_signature(secret, ts, body)

        assert verify_slack_signature(secret, ts, body, sig)

    def test_invalid_signature(self):
        secret = "test-signing-secret"
        ts = str(int(time.time()))
        body = b'{"type":"url_verification"}'

        assert not verify_slack_signature(secret, ts, body, "v0=invalid")

    def test_expired_timestamp(self):
        secret = "test-signing-secret"
        # Timestamp older than 5 minutes
        ts = str(int(time.time()) - 400)
        body = b'{"type":"url_verification"}'
        sig = self._make_signature(secret, ts, body)

        assert not verify_slack_signature(secret, ts, body, sig)
