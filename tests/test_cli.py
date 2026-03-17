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
        result = runner.invoke(
            cli,
            ["-c", str(toml_config_file), "study", "list"],
        )
        assert result.exit_code == 0
        assert "my-study" in result.output

    def test_study_show(self, toml_config_file):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "-c",
                str(toml_config_file),
                "study",
                "show",
                "my-study",
            ],
        )
        assert result.exit_code == 0
        assert "my-study" in result.output

    def test_study_show_not_found(self, toml_config_file):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "-c",
                str(toml_config_file),
                "study",
                "show",
                "nope",
            ],
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
        result = runner.invoke(
            cli,
            ["-c", str(toml_config_file), "sprint", "list"],
        )
        assert result.exit_code == 0

    @patch("httpx.post")
    def test_sprint_show(self, mock_post, toml_config_file):
        mock_post.return_value = _mock_response(
            {"sprint_id": "sp-abc123", "status": "submitted"},
            status_code=201,
        )
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "-c",
                str(toml_config_file),
                "sprint",
                "run",
                "idea",
                "-s",
                "my-study",
            ],
        )
        result = runner.invoke(
            cli,
            [
                "-c",
                str(toml_config_file),
                "sprint",
                "show",
                "sp-abc123",
            ],
        )
        assert result.exit_code in (0, 1)

    @patch("httpx.post")
    def test_sprint_cancel(self, mock_post, toml_config_file):
        mock_post.return_value = _mock_response({"cancelled": True})
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "-c",
                str(toml_config_file),
                "sprint",
                "cancel",
                "sp-abc123",
            ],
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
            [
                "-c",
                str(toml_config_file),
                "sprint",
                "run",
                "idea",
                "-s",
                "nope",
            ],
        )
        assert result.exit_code != 0
        assert "400" in result.output or "not found" in result.output.lower()


class TestClusterCommands:
    def test_cluster_list(self, toml_config_file):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(toml_config_file), "cluster", "list"],
        )
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
            [
                "-c",
                str(toml_config_file),
                "loop",
                "start",
                "-s",
                "my-study",
                "-n",
                "3",
            ],
        )
        assert result.exit_code == 0
        assert "loop-abc123" in result.output

    def test_loop_status(self, toml_config_file):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(toml_config_file), "loop", "status"],
        )
        assert result.exit_code == 0


_FAKE_CREDS = {
    "url": "http://localhost:9999",
    "token": "old-expired-token",
}


class TestAutoReauth:
    """Auto-reauth on 401 with saved credentials."""

    @patch("researchloop.core.credentials.load_credentials")
    @patch("researchloop.core.credentials.save_credentials")
    @patch("httpx.post")
    def test_reauth_on_401_then_retry_succeeds(self, mock_post, mock_save, mock_load):
        """401 -> prompt password -> new token -> retry ok."""
        mock_load.return_value = _FAKE_CREDS

        resp_401 = _mock_response({"detail": "Unauthorized"}, status_code=401)
        resp_401.text = "Unauthorized"
        resp_auth = _mock_response({"token": "new-token"}, status_code=200)
        resp_ok = _mock_response(
            {
                "sprint_id": "sp-retry1",
                "study_name": "my-study",
                "status": "submitted",
                "job_id": "456",
            },
            status_code=201,
        )
        mock_post.side_effect = [
            resp_401,
            resp_auth,
            resp_ok,
        ]

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["sprint", "run", "idea", "-s", "my-study"],
            input="mypassword\n",
        )
        assert result.exit_code == 0
        assert "sp-retry1" in result.output
        assert "Re-authenticated" in result.output
        # 3 calls: original, auth, retry
        assert mock_post.call_count == 3
        # Token was saved
        mock_save.assert_called_once_with("http://localhost:9999", "new-token")

    @patch("researchloop.core.credentials.load_credentials")
    @patch("httpx.post")
    def test_reauth_wrong_password_fails(self, mock_post, mock_load):
        """401 -> prompt password -> wrong password -> error."""
        mock_load.return_value = _FAKE_CREDS

        resp_401 = _mock_response({"detail": "Unauthorized"}, status_code=401)
        resp_401.text = "Unauthorized"
        resp_auth_fail = _mock_response({"detail": "Invalid password"}, status_code=401)
        resp_auth_fail.text = "Invalid password"
        mock_post.side_effect = [resp_401, resp_auth_fail]

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["sprint", "run", "idea", "-s", "my-study"],
            input="wrongpassword\n",
        )
        assert result.exit_code != 0
        assert "Invalid password" in result.output

    @patch("httpx.post")
    def test_no_reauth_with_shared_secret(self, mock_post, toml_config_file):
        """401 with shared_secret auth should NOT prompt."""
        resp_401 = _mock_response({"detail": "Invalid"}, status_code=401)
        resp_401.text = '{"detail":"Invalid"}'
        mock_post.return_value = resp_401

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "-c",
                str(toml_config_file),
                "sprint",
                "run",
                "idea",
                "-s",
                "my-study",
            ],
        )
        assert result.exit_code != 0
        # Only 1 call — no reauth since we used shared_secret
        assert mock_post.call_count == 1
        # Should not contain password prompt
        assert "Re-authenticated" not in result.output

    @patch("researchloop.core.credentials.load_credentials")
    @patch("researchloop.core.credentials.save_credentials")
    @patch("httpx.post")
    def test_reauth_only_retries_once(self, mock_post, mock_save, mock_load):
        """After reauth, if retry also 401s, don't loop."""
        mock_load.return_value = _FAKE_CREDS

        resp_401 = _mock_response({"detail": "Unauthorized"}, status_code=401)
        resp_401.text = "Unauthorized"
        resp_auth = _mock_response({"token": "new-token"}, status_code=200)
        resp_401_again = _mock_response(
            {"detail": "Still unauthorized"},
            status_code=401,
        )
        resp_401_again.text = "Still unauthorized"
        mock_post.side_effect = [
            resp_401,
            resp_auth,
            resp_401_again,
        ]

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["sprint", "run", "idea", "-s", "my-study"],
            input="mypassword\n",
        )
        assert result.exit_code != 0
        # 3 calls total — no infinite loop
        assert mock_post.call_count == 3
