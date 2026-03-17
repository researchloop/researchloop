"""Tests for scheduler implementations."""

import asyncio
import tempfile
from unittest.mock import AsyncMock

import pytest

from researchloop.schedulers.local import LocalScheduler
from researchloop.schedulers.slurm import SlurmScheduler


class TestSlurmScheduler:
    """Test SlurmScheduler with a mock SSH connection."""

    def setup_method(self):
        self.scheduler = SlurmScheduler()
        self.ssh = AsyncMock()

    async def test_submit_success(self):
        self.ssh.run = AsyncMock(
            return_value=("Submitted batch job 12345\n", "", 0),
        )
        job_id = await self.scheduler.submit(
            self.ssh,
            "/tmp/work/run_sprint.sh",
            "test-job",
            "/tmp/work",
        )
        assert job_id == "12345"

    async def test_submit_failure(self):
        self.ssh.run = AsyncMock(
            return_value=("", "error: invalid", 1),
        )
        with pytest.raises(RuntimeError, match="sbatch failed"):
            await self.scheduler.submit(
                self.ssh,
                "/tmp/work/run_sprint.sh",
                "test-job",
                "/tmp/work",
            )

    async def test_status_squeue_running(self):
        self.ssh.run = AsyncMock(return_value=("RUNNING\n", "", 0))
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "running"

    async def test_status_squeue_pending(self):
        self.ssh.run = AsyncMock(return_value=("PENDING\n", "", 0))
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "pending"

    async def test_status_squeue_empty_falls_back_to_sacct(self):
        self.ssh.run = AsyncMock(
            side_effect=[
                ("", "", 0),  # squeue returns empty (job done)
                ("COMPLETED\n", "", 0),  # sacct returns final state
            ]
        )
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "completed"

    async def test_status_sacct_failed(self):
        self.ssh.run = AsyncMock(
            side_effect=[
                ("", "", 0),  # squeue empty
                ("FAILED\n", "", 0),  # sacct
            ]
        )
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "failed"

    async def test_status_sacct_cancelled_with_qualifier(self):
        self.ssh.run = AsyncMock(
            side_effect=[
                ("", "", 0),  # squeue empty
                ("CANCELLED by 1234\n", "", 0),  # sacct with qualifier
            ]
        )
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "failed"

    async def test_status_unknown(self):
        self.ssh.run = AsyncMock(
            side_effect=[
                ("", "error", 1),  # squeue fails
                ("", "error", 1),  # sacct fails
            ]
        )
        status = await self.scheduler.status(self.ssh, "12345")
        assert status == "unknown"

    async def test_cancel_success(self):
        self.ssh.run = AsyncMock(return_value=("", "", 0))
        result = await self.scheduler.cancel(self.ssh, "12345")
        assert result is True

    async def test_cancel_failure(self):
        self.ssh.run = AsyncMock(return_value=("", "error", 1))
        result = await self.scheduler.cancel(self.ssh, "12345")
        assert result is False

    def test_generate_script(self):
        script = self.scheduler.generate_script(
            command="echo hello",
            job_name="test-job",
            working_dir="/tmp/work",
            time_limit="4:00:00",
            env={"FOO": "bar"},
        )
        assert "#SBATCH --job-name=test-job" in script
        assert "#SBATCH --time=4:00:00" in script
        assert "echo hello" in script
        assert "export FOO='bar'" in script


class TestLocalScheduler:
    async def test_submit_and_status(self):
        scheduler = LocalScheduler()
        with tempfile.TemporaryDirectory() as tmpdir:
            job_id = await scheduler.submit(
                ssh=None,
                script="#!/bin/bash\nsleep 0.1\necho done",
                job_name="test",
                working_dir=tmpdir,
            )
            assert job_id.isdigit()

            # Should be running briefly
            status = await scheduler.status(None, job_id)
            assert status in ("running", "completed")

            # Wait for it to complete
            await asyncio.sleep(0.3)
            status = await scheduler.status(None, job_id)
            assert status == "completed"

    async def test_submit_and_cancel(self):
        scheduler = LocalScheduler()
        with tempfile.TemporaryDirectory() as tmpdir:
            job_id = await scheduler.submit(
                ssh=None,
                script="#!/bin/bash\nsleep 10",
                job_name="long-job",
                working_dir=tmpdir,
            )
            result = await scheduler.cancel(None, job_id)
            assert result is True
            await asyncio.sleep(0.1)
            status = await scheduler.status(None, job_id)
            assert status in ("failed", "unknown")

    async def test_status_unknown_pid(self):
        scheduler = LocalScheduler()
        status = await scheduler.status(None, "999999999")
        assert status == "unknown"

    def test_generate_script(self):
        scheduler = LocalScheduler()
        script = scheduler.generate_script(
            command="python run.py",
            job_name="my-job",
            working_dir="/tmp",
        )
        assert "python run.py" in script
        assert "set -euo pipefail" in script
