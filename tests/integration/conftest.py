"""Integration test fixtures -- Docker SLURM container + SSH keys."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from researchloop.clusters.ssh import SSHManager
from researchloop.core.config import ClusterConfig, Config, StudyConfig
from researchloop.db.database import Database
from researchloop.sprints.manager import SprintManager

# Paths.
_DOCKER_DIR = Path(__file__).resolve().parent.parent / "docker" / "slurm"
_SGE_DOCKER_DIR = Path(__file__).resolve().parent.parent / "docker" / "sge"
_IMAGE_NAME = os.environ.get("SLURM_TEST_IMAGE", "researchloop-slurm-test")


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 30) -> None:
    """Block until *host:port* is accepting connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"{host}:{port} not reachable after {timeout}s")


def _wait_for_ssh(
    host: str,
    port: int,
    key_path: str,
    timeout: float = 30,
) -> None:
    """Block until SSH login succeeds."""
    last_err = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "ConnectTimeout=5",
                "-i",
                key_path,
                "-p",
                str(port),
                f"root@{host}",
                "echo OK",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and "OK" in result.stdout:
            return
        last_err = result.stderr
        time.sleep(2)
    raise TimeoutError(
        f"SSH to {host}:{port} not ready after {timeout}s. "
        f"Last stderr: {last_err[-500:]}"
    )


# ------------------------------------------------------------------
# SSH key pair (session-scoped)
# ------------------------------------------------------------------


@pytest.fixture(scope="session")
def ssh_key_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Generate a temporary ed25519 SSH key pair."""
    key_dir = tmp_path_factory.mktemp("ssh")
    priv = key_dir / "id_ed25519"
    pub = key_dir / "id_ed25519.pub"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(priv), "-N", "", "-q"],
        check=True,
    )
    return priv, pub


# ------------------------------------------------------------------
# Docker container (session-scoped)
# ------------------------------------------------------------------


@pytest.fixture(scope="session")
def slurm_container(
    ssh_key_pair: tuple[Path, Path],
) -> Iterator[dict[str, object]]:
    """Build and run the SLURM Docker container.

    Yields a dict with connection info:
    ``{"host": "localhost", "ssh_port": int, "container_id": str}``
    """
    priv_key, pub_key = ssh_key_pair

    # Build the image.
    subprocess.run(
        [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "-t",
            _IMAGE_NAME,
            str(_DOCKER_DIR),
        ],
        check=True,
        capture_output=True,
    )

    ssh_port = _find_free_port()

    # Run the container.
    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--platform",
            "linux/amd64",
            "-p",
            f"{ssh_port}:22",
            "-v",
            f"{pub_key}:/tmp/test_key.pub:ro",
            "--add-host=host.docker.internal:host-gateway",
            _IMAGE_NAME,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = result.stdout.strip()

    try:
        _wait_for_port("localhost", ssh_port, timeout=90)
        # Extra wait for sshd to fully initialize (especially under emulation).
        time.sleep(3)

        # Verify SSH is actually working before yielding.
        _wait_for_ssh(
            "localhost",
            ssh_port,
            str(ssh_key_pair[0]),
            timeout=60,
        )

        yield {
            "host": "localhost",
            "ssh_port": ssh_port,
            "container_id": container_id,
        }
    finally:
        # Dump logs on failure for debugging.
        logs = subprocess.run(
            ["docker", "logs", container_id],
            capture_output=True,
            text=True,
        )
        if logs.stdout:
            print(f"SLURM container logs:\n{logs.stdout}")
        subprocess.run(
            ["docker", "stop", container_id],
            capture_output=True,
        )


# ------------------------------------------------------------------
# Cluster config pointing at Docker container
# ------------------------------------------------------------------


@pytest.fixture(scope="session")
def slurm_cluster_config(
    slurm_container: dict[str, object],
    ssh_key_pair: tuple[Path, Path],
) -> ClusterConfig:
    """ClusterConfig that connects to the Docker SLURM container."""
    return ClusterConfig(
        name="test-slurm",
        host=str(slurm_container["host"]),
        port=int(slurm_container["ssh_port"]),  # type: ignore[arg-type]
        user="root",
        key_path=str(ssh_key_pair[0]),
        scheduler_type="slurm",
        working_dir="/tmp/researchloop",
    )


@pytest.fixture
def integration_config(
    slurm_cluster_config: ClusterConfig,
    tmp_path: Path,
) -> Config:
    """Full Config with a SLURM cluster for integration tests."""
    return Config(
        studies=[
            StudyConfig(
                name="integration-study",
                cluster="test-slurm",
                description="Integration test study",
                sprints_dir="/tmp/researchloop/integration-study",
                red_team_max_rounds=1,
            ),
        ],
        clusters=[slurm_cluster_config],
        db_path=":memory:",
        artifact_dir=str(tmp_path / "artifacts"),
        orchestrator_url="",
        claude_command="claude --dangerously-skip-permissions",
    )


@pytest.fixture
async def integration_db() -> AsyncIterator[Database]:
    """In-memory database for integration tests."""
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def integration_db_with_study(
    integration_db: Database,
) -> Database:
    """Database with the integration study pre-created."""
    from researchloop.db import queries

    await queries.create_study(
        integration_db,
        name="integration-study",
        cluster="test-slurm",
        description="Integration test study",
        claude_md_path=None,
        sprints_dir="/tmp/researchloop/integration-study",
    )
    return integration_db


@pytest.fixture
async def sprint_manager(
    integration_db_with_study: Database,
    integration_config: Config,
) -> AsyncIterator[SprintManager]:
    """SprintManager wired to the Docker SLURM container."""
    from researchloop.schedulers.slurm import SlurmScheduler
    from researchloop.sprints.manager import SprintManager
    from researchloop.studies.manager import StudyManager

    ssh_mgr = SSHManager()
    cluster = integration_config.clusters[0]
    scheduler = SlurmScheduler()
    study_mgr = StudyManager(integration_db_with_study, integration_config)
    mgr = SprintManager(
        db=integration_db_with_study,
        config=integration_config,
        ssh_manager=ssh_mgr,
        schedulers={
            cluster.name: scheduler,
            cluster.scheduler_type: scheduler,
        },
        study_manager=study_mgr,
    )
    yield mgr
    await ssh_mgr.close_all()


# ==================================================================
# SGE container and fixtures
# ==================================================================

_SGE_IMAGE_NAME = os.environ.get("SGE_TEST_IMAGE", "researchloop-sge-test")


@pytest.fixture(scope="session")
def sge_container(
    ssh_key_pair: tuple[Path, Path],
) -> Iterator[dict[str, object]]:
    """Build and run the SGE Docker container."""
    _priv_key, pub_key = ssh_key_pair

    subprocess.run(
        [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "-t",
            _SGE_IMAGE_NAME,
            str(_SGE_DOCKER_DIR),
        ],
        check=True,
        capture_output=True,
    )

    ssh_port = _find_free_port()

    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--platform",
            "linux/amd64",
            "-p",
            f"{ssh_port}:22",
            "-v",
            f"{pub_key}:/tmp/test_key.pub:ro",
            "--add-host=host.docker.internal:host-gateway",
            _SGE_IMAGE_NAME,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = result.stdout.strip()

    try:
        _wait_for_port("localhost", ssh_port, timeout=120)
        time.sleep(10)  # SGE install + init takes longer than SLURM
        _wait_for_ssh(
            "localhost",
            ssh_port,
            str(ssh_key_pair[0]),
            timeout=90,
        )
        yield {
            "host": "localhost",
            "ssh_port": ssh_port,
            "container_id": container_id,
        }
    finally:
        logs = subprocess.run(
            ["docker", "logs", container_id],
            capture_output=True,
            text=True,
        )
        if logs.stdout:
            print(f"SGE container logs:\n{logs.stdout[-500:]}")
        subprocess.run(
            ["docker", "stop", container_id],
            capture_output=True,
        )


@pytest.fixture(scope="session")
def sge_cluster_config(
    sge_container: dict[str, object],
    ssh_key_pair: tuple[Path, Path],
) -> ClusterConfig:
    """ClusterConfig pointing at the Docker SGE container."""
    return ClusterConfig(
        name="test-sge",
        host=str(sge_container["host"]),
        port=int(sge_container["ssh_port"]),  # type: ignore[arg-type]
        user="sgeuser",
        key_path=str(ssh_key_pair[0]),
        scheduler_type="sge",
        working_dir="/tmp/researchloop",
    )
