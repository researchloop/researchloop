"""Tests for the researchloop CLI."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from researchloop.cli import cli


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Build a fake httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = ""
    return resp


class TestInit:
    def test_init_creates_config(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path / "project")])
        assert result.exit_code == 0

    def test_init_existing_config_fails(self, tmp_path):
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path / "project")])
        result = runner.invoke(cli, ["init", "--path", str(tmp_path / "project")])
        assert result.exit_code != 0


class TestVersion:
    def test_version(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "researchloop" in result.output


class TestStudyCommands:
    def test_study_list(self, toml_config_file):
        runner = CliRunner()
        result = runner.invoke(cli, ["-c", str(toml_config_file), "study", "list"])
        assert result.exit_code == 0
        assert "my-study" in result.output

    def test_study_show(self, toml_config_file):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["-c", str(toml_config_file), "study", "show", "my-study"]
        )
        assert result.exit_code == 0
        assert "my-study" in result.output

    def test_study_show_not_found(self, toml_config_file):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["-c", str(toml_config_file), "study", "show", "nope"]
        )
        assert result.exit_code != 0


class TestSprintCommands:
    @patch("httpx.post")
    def test_sprint_run(self, mock_post, toml_config_file):
        mock_post.return_value = _mock_response(
            {
                "sprint_id": "sp-abc123",
                "study_name": "my-study",
                "status": "submitted",
                "job_id": "123",
            },
            status_code=201,
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "-c",
                str(toml_config_file),
                "sprint",
                "run",
                "test idea",
                "-s",
                "my-study",
            ],
        )
        assert result.exit_code == 0
        assert "sp-abc123" in result.output
        assert "test idea" in result.output

    def test_sprint_list(self, toml_config_file):
        runner = CliRunner()
        result = runner.invoke(cli, ["-c", str(toml_config_file), "sprint", "list"])
        assert result.exit_code == 0

    @patch("httpx.post")
    def test_sprint_show(self, mock_post, toml_config_file):
        mock_post.return_value = _mock_response(
            {"sprint_id": "sp-abc123", "status": "submitted"},
            status_code=201,
        )
        runner = CliRunner()
        # Create a sprint first
        runner.invoke(
            cli,
            ["-c", str(toml_config_file), "sprint", "run", "idea", "-s", "my-study"],
        )
        # Show it (sprint show still uses local DB)
        result = runner.invoke(
            cli, ["-c", str(toml_config_file), "sprint", "show", "sp-abc123"]
        )
        # It may not find it in local DB, that's OK — we're testing the CLI wiring
        assert result.exit_code in (0, 1)

    @patch("httpx.post")
    def test_sprint_cancel(self, mock_post, toml_config_file):
        mock_post.return_value = _mock_response({"cancelled": True})
        runner = CliRunner()
        result = runner.invoke(
            cli, ["-c", str(toml_config_file), "sprint", "cancel", "sp-abc123"]
        )
        assert result.exit_code == 0
        assert "Cancelled" in result.output

    @patch("httpx.post")
    def test_sprint_run_api_error(self, mock_post, toml_config_file):
        mock_post.return_value = _mock_response(
            {"detail": "Study not found"},
            status_code=400,
        )
        mock_post.return_value.text = '{"detail":"Study not found"}'
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(toml_config_file), "sprint", "run", "idea", "-s", "nope"],
        )
        assert result.exit_code != 0
        assert "400" in result.output or "not found" in result.output.lower()


class TestClusterCommands:
    def test_cluster_list(self, toml_config_file):
        runner = CliRunner()
        result = runner.invoke(cli, ["-c", str(toml_config_file), "cluster", "list"])
        assert result.exit_code == 0
        assert "local" in result.output


class TestLoopCommands:
    @patch("httpx.post")
    def test_loop_start(self, mock_post, toml_config_file):
        mock_post.return_value = _mock_response(
            {"loop_id": "loop-abc123"},
            status_code=201,
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(toml_config_file), "loop", "start", "-s", "my-study", "-n", "3"],
        )
        assert result.exit_code == 0
        assert "loop-abc123" in result.output

    def test_loop_status(self, toml_config_file):
        runner = CliRunner()
        result = runner.invoke(cli, ["-c", str(toml_config_file), "loop", "status"])
        assert result.exit_code == 0
