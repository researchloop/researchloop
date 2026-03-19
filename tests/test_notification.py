"""Tests for researchloop.comms.router."""

from unittest.mock import AsyncMock

from researchloop.comms.router import NotificationRouter


class TestNotificationRouter:
    async def test_fan_out_started(self):
        router = NotificationRouter()
        n1 = AsyncMock()
        n2 = AsyncMock()
        router.add_notifier(n1)
        router.add_notifier(n2)

        await router.notify_sprint_started("sp-001", "study", "idea")
        n1.notify_sprint_started.assert_called_once_with("sp-001", "study", "idea")
        n2.notify_sprint_started.assert_called_once_with("sp-001", "study", "idea")

    async def test_fan_out_completed(self):
        router = NotificationRouter()
        n1 = AsyncMock()
        router.add_notifier(n1)

        await router.notify_sprint_completed("sp-001", "study", "summary")
        n1.notify_sprint_completed.assert_called_once_with(
            "sp-001", "study", "summary", pdf_path=None
        )

    async def test_fan_out_failed(self):
        router = NotificationRouter()
        n1 = AsyncMock()
        router.add_notifier(n1)

        await router.notify_sprint_failed("sp-001", "study", "error msg")
        n1.notify_sprint_failed.assert_called_once_with("sp-001", "study", "error msg")

    async def test_error_isolation(self):
        """One notifier failing should not prevent others from firing."""
        router = NotificationRouter()
        failing = AsyncMock()
        failing.notify_sprint_started.side_effect = RuntimeError("boom")
        succeeding = AsyncMock()

        router.add_notifier(failing)
        router.add_notifier(succeeding)

        await router.notify_sprint_started("sp-001", "study", "idea")

        # The succeeding notifier should still have been called
        succeeding.notify_sprint_started.assert_called_once()

    async def test_empty_router(self):
        router = NotificationRouter()
        # Should not raise
        await router.notify_sprint_started("sp-001", "study", "idea")
        await router.notify_sprint_completed("sp-001", "study", "summary")
        await router.notify_sprint_failed("sp-001", "study", "error")
