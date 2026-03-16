"""Tests for researchloop.core.config."""

import pytest

from researchloop.core.config import load_config


class TestLoadConfig:
    def test_loads_from_path(self, toml_config_file):
        config = load_config(str(toml_config_file))
        assert len(config.clusters) == 1
        assert config.clusters[0].name == "local"
        assert config.clusters[0].scheduler_type == "local"
        assert len(config.studies) == 1
        assert config.studies[0].name == "my-study"
        assert config.studies[0].cluster == "local"

    def test_ntfy_parsed(self, toml_config_file):
        config = load_config(str(toml_config_file))
        assert config.ntfy is not None
        assert config.ntfy.topic == "test-topic"

    def test_dashboard_parsed(self, toml_config_file):
        config = load_config(str(toml_config_file))
        assert config.dashboard.port == 9090
        assert config.dashboard.enabled is True

    def test_shared_secret_parsed(self, toml_config_file):
        config = load_config(str(toml_config_file))
        assert config.shared_secret == "test-key"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(str(tmp_path / "nonexistent.toml"))

    def test_defaults(self, tmp_path):
        # Minimal valid TOML
        p = tmp_path / "researchloop.toml"
        p.write_text(
            '[[cluster]]\nname = "c1"\nhost = "h1"\n\n'
            '[[study]]\nname = "s1"\n'
            'cluster = "c1"\nsprints_dir = "./sp"\n'
        )
        config = load_config(str(p))
        assert config.db_path == "researchloop.db"
        assert config.artifact_dir == "artifacts"
        assert config.dashboard.host == "0.0.0.0"
        assert config.dashboard.port == 8080
        assert config.shared_secret is None
        assert config.slack is None


class TestEnvOverrides:
    """Env vars with RESEARCHLOOP_ prefix override TOML values."""

    def test_shared_secret(self, toml_config_file, monkeypatch):
        monkeypatch.setenv("RESEARCHLOOP_SHARED_SECRET", "from-env")
        config = load_config(str(toml_config_file))
        assert config.shared_secret == "from-env"

    def test_orchestrator_url(self, toml_config_file, monkeypatch):
        monkeypatch.setenv("RESEARCHLOOP_ORCHESTRATOR_URL", "https://x.io")
        config = load_config(str(toml_config_file))
        assert config.orchestrator_url == "https://x.io"

    def test_db_path(self, toml_config_file, monkeypatch):
        monkeypatch.setenv("RESEARCHLOOP_DB_PATH", "/tmp/rl.db")
        config = load_config(str(toml_config_file))
        assert config.db_path == "/tmp/rl.db"

    def test_slack_from_env_only(self, tmp_path, monkeypatch):
        """Slack config created even if not in TOML."""
        p = tmp_path / "researchloop.toml"
        p.write_text(
            '[[cluster]]\nname = "c"\nhost = "h"\n\n'
            '[[study]]\nname = "s"\n'
            'cluster = "c"\nsprints_dir = "."\n'
        )
        monkeypatch.setenv("RESEARCHLOOP_SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("RESEARCHLOOP_SLACK_CHANNEL_ID", "C123")
        config = load_config(str(p))
        assert config.slack is not None
        assert config.slack.bot_token == "xoxb-test"
        assert config.slack.channel_id == "C123"

    def test_slack_overrides_toml(self, tmp_path, monkeypatch):
        p = tmp_path / "researchloop.toml"
        p.write_text(
            '[[cluster]]\nname = "c"\nhost = "h"\n\n'
            '[[study]]\nname = "s"\n'
            'cluster = "c"\nsprints_dir = "."\n\n'
            '[slack]\nbot_token = "old"\n'
        )
        monkeypatch.setenv("RESEARCHLOOP_SLACK_BOT_TOKEN", "new-token")
        config = load_config(str(p))
        assert config.slack.bot_token == "new-token"

    def test_ntfy_from_env(self, tmp_path, monkeypatch):
        p = tmp_path / "researchloop.toml"
        p.write_text(
            '[[cluster]]\nname = "c"\nhost = "h"\n\n'
            '[[study]]\nname = "s"\n'
            'cluster = "c"\nsprints_dir = "."\n'
        )
        monkeypatch.setenv("RESEARCHLOOP_NTFY_TOPIC", "my-topic")
        config = load_config(str(p))
        assert config.ntfy is not None
        assert config.ntfy.topic == "my-topic"

    def test_dashboard_port(self, toml_config_file, monkeypatch):
        monkeypatch.setenv("RESEARCHLOOP_DASHBOARD_PORT", "3000")
        config = load_config(str(toml_config_file))
        assert config.dashboard.port == 3000

    def test_dashboard_password_hash(self, toml_config_file, monkeypatch):
        monkeypatch.setenv("RESEARCHLOOP_DASHBOARD_PASSWORD_HASH", "$2b$12$xxx")
        config = load_config(str(toml_config_file))
        assert config.dashboard.password_hash == "$2b$12$xxx"

    def test_env_does_not_override_when_unset(self, toml_config_file):
        """Without env vars, TOML values are preserved."""
        config = load_config(str(toml_config_file))
        assert config.shared_secret == "test-key"  # from TOML
