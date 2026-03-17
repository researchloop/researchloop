"""Tests for the SGE scheduler implementation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from researchloop.schedulers.sge import SGEScheduler


class TestSGEScheduler:
    """Test SGEScheduler with a mock SSH connection."""

    def setup_method(self) -> None:
        self.scheduler = SGEScheduler()
        self.ssh = AsyncMock()

    # ----- submit -----

    async def test_submit_success(self) -> None:
        self.ssh.run = AsyncMock(
            return_value=(
                'Your job 12345 ("test") has been submitted\n',
                "",
                0,
            ),
        )
        job_id = await self.scheduler.submit(
            self.ssh,
            "/tmp/work/run_sprint.sh",
            "test-job",
            "/tmp/work",
        )
        assert job_id == "12345"

    async def test_submit_parses_large_job_id(self) -> None:
        self.ssh.run = AsyncMock(
            return_value=(
                'Your job 9876543 ("big") has been submitted\n',
                "",
                0,
            ),
        )
        job_id = await self.scheduler.submit(
            self.ssh,
            "/tmp/work/run_sprint.sh",
            "big-job",
            "/tmp/work",
        )
        assert job_id == "9876543"

    async def test_submit_qsub_failure(self) -> None:
        self.ssh.run = AsyncMock(
            return_value=("", "error: invalid", 1),
        )
        with pytest.raises(RuntimeError, match="qsub failed"):
            await self.scheduler.submit(
                self.ssh,
                "/tmp/work/run_sprint.sh",
                "test-job",
                "/tmp/work",
            )

    async def test_submit_unparseable_output(self) -> None:
        self.ssh.run = AsyncMock(
            return_value=("unexpected output\n", "", 0),
        )
        with pytest.raises(
            RuntimeError,
            match="Could not parse job ID",
        ):
            await self.scheduler.submit(
                self.ssh,
                "script",
                "test-job",
                "/tmp/work",
            )

    # ----- status -----

    async def test_status_qstat_running(self) -> None:
        self.ssh.run = AsyncMock(
            return_value=(
                "job_number: 12345\njob_state   1: r\n",
                "",
                0,
            )
        )
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "running"

    async def test_status_qstat_pending(self) -> None:
        self.ssh.run = AsyncMock(
            return_value=(
                "job_number: 12345\njob_state   1: qw\n",
                "",
                0,
            )
        )
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "pending"

    async def test_status_qstat_error(self) -> None:
        self.ssh.run = AsyncMock(
            return_value=(
                "job_number: 12345\njob_state   1: Eqw\n",
                "",
                0,
            )
        )
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "failed"

    async def test_status_qstat_exists_no_state(
        self,
    ) -> None:
        """Job exists but no job_state line => running."""
        self.ssh.run = AsyncMock(
            return_value=(
                "job_number: 12345\nowner: user\n",
                "",
                0,
            )
        )
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "running"

    async def test_status_qstat_listing_format(
        self,
    ) -> None:
        """Fall back to qstat listing grep."""
        self.ssh.run = AsyncMock(
            side_effect=[
                ("", "", 1),  # qstat -j fails
                (
                    "12345 0.5 test user r 03/16 node\n",
                    "",
                    0,
                ),  # qstat | grep
            ]
        )
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "running"

    async def test_status_qacct_completed(self) -> None:
        self.ssh.run = AsyncMock(
            side_effect=[
                ("", "", 1),  # qstat -j fails
                ("", "", 1),  # qstat | grep fails
                (
                    "exit_status  0\n",
                    "",
                    0,
                ),  # qacct
            ]
        )
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "completed"

    async def test_status_qacct_failed(self) -> None:
        self.ssh.run = AsyncMock(
            side_effect=[
                ("", "", 1),  # qstat -j fails
                ("", "", 1),  # qstat | grep fails
                (
                    "exit_status  137\n",
                    "",
                    0,
                ),  # qacct non-zero
            ]
        )
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "failed"

    async def test_status_qacct_no_exit_status(
        self,
    ) -> None:
        """qacct returns data but no exit_status line."""
        self.ssh.run = AsyncMock(
            side_effect=[
                ("", "", 1),
                ("", "", 1),
                ("hostname node1\n", "", 0),
            ]
        )
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "completed"

    async def test_status_unknown(self) -> None:
        self.ssh.run = AsyncMock(
            side_effect=[
                ("", "error", 1),  # qstat -j
                ("", "error", 1),  # qstat | grep
                ("", "error", 1),  # qacct
            ]
        )
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "unknown"

    # ----- cancel -----

    async def test_cancel_success(self) -> None:
        self.ssh.run = AsyncMock(return_value=("", "", 0))
        result = await self.scheduler.cancel(self.ssh, "12345")
        assert result is True

    async def test_cancel_failure(self) -> None:
        self.ssh.run = AsyncMock(return_value=("", "error", 1))
        result = await self.scheduler.cancel(self.ssh, "12345")
        assert result is False

    # ----- generate_script -----

    def test_generate_script_basic(self) -> None:
        script = self.scheduler.generate_script(
            command="echo hello",
            job_name="test-job",
            working_dir="/tmp/work",
            time_limit="4:00:00",
        )
        assert "#$ -N test-job" in script
        assert "#$ -l h_rt=4:00:00" in script
        assert "#$ -cwd" in script
        assert "#$ -S /bin/bash" in script
        assert "echo hello" in script
        assert "set -euo pipefail" in script

    def test_generate_script_with_env(self) -> None:
        script = self.scheduler.generate_script(
            command="python run.py",
            job_name="env-job",
            working_dir="/home/user/work",
            env={"FOO": "bar", "BAZ": "qux"},
        )
        assert "export FOO='bar'" in script
        assert "export BAZ='qux'" in script
        assert "python run.py" in script

    def test_generate_script_env_quoting(self) -> None:
        script = self.scheduler.generate_script(
            command="echo test",
            job_name="quote-job",
            working_dir="/tmp",
            env={"VAL": "it's a test"},
        )
        assert "export VAL='it'\\''s a test'" in script

    def test_generate_script_default_time(self) -> None:
        script = self.scheduler.generate_script(
            command="echo hi",
            job_name="default-job",
            working_dir="/tmp",
        )
        assert "#$ -l h_rt=8:00:00" in script
