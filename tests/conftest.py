"""Shared test fixtures."""

import tempfile
from pathlib import Path

import pytest

from researchloop.core.config import (
    ClusterConfig,
    Config,
    StudyConfig,
)
from researchloop.db.database import Database


@pytest.fixture
def sample_config() -> Config:
    """Minimal Config for testing."""
    return Config(
        studies=[
            StudyConfig(
                name="test-study",
                cluster="local",
                description="A test study",
                sprints_dir="./sprints",
                claude_md_path="./CLAUDE.md",
                red_team_max_rounds=2,
            ),
        ],
        clusters=[
            ClusterConfig(
                name="local",
                host="localhost",
                port=22,
                user="testuser",
                key_path="~/.ssh/id_ed25519",
                scheduler_type="local",
                working_dir="/tmp/researchloop-test",
            ),
        ],
        db_path=":memory:",
        artifact_dir=tempfile.mkdtemp(),
        shared_secret="test-api-key",
        orchestrator_url="http://localhost:8080",
    )


@pytest.fixture
async def db():
    """Connected in-memory database."""
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def db_with_study(db):
    """Database with one study pre-created."""
    from researchloop.db import queries

    await queries.create_study(
        db,
        name="test-study",
        cluster="local",
        description="A test study",
        claude_md_path=None,
        sprints_dir="./sprints",
    )
    return db


@pytest.fixture
def toml_config_file(tmp_path: Path) -> Path:
    """Write a minimal researchloop.toml and return its path."""
    content = """
db_path = "researchloop.db"
artifact_dir = "artifacts"
shared_secret = "test-key"
orchestrator_url = "http://localhost:8080"

[[cluster]]
name = "local"
host = "localhost"
scheduler_type = "local"
working_dir = "/tmp/rl"

[[study]]
name = "my-study"
cluster = "local"
description = "Test study"
sprints_dir = "./sprints"

[ntfy]
url = "https://ntfy.sh"
topic = "test-topic"

[dashboard]
enabled = true
port = 9090
"""
    p = tmp_path / "researchloop.toml"
    p.write_text(content)
    return p
