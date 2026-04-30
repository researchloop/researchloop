"""Tests for runner components (template rendering, output parsing)."""

import jinja2

from researchloop.runner.claude import _parse_output, render_template


def _render_job_template(name: str) -> str:
    """Render slurm.sh.j2 / sge.sh.j2 with minimal variables for inspection."""
    from pathlib import Path

    import researchloop.sprints.manager as _m

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(
            str(Path(_m.__file__).resolve().parent.parent / "runner" / "job_templates")
        ),
    )
    return env.get_template(name).render(
        sprint_id="sp-test",
        study_name="study",
        idea="test idea",
        sprint_dirname="sp-test-dir",
        job_name="rl-sp-test",
        working_dir="/work",
        time_limit="8:00:00",
        environment={},
        job_options={},
        claude_command="claude --dangerously-skip-permissions",
        orchestrator_url="http://orch",
        webhook_token="tok",
        red_team_max_rounds=3,
        prompts=[],
    )


class TestParseOutput:
    def test_json_with_result(self):
        raw = '{"result": "Hello world", "session_id": "sess-123"}'
        text, sid = _parse_output(raw)
        assert text == "Hello world"
        assert sid == "sess-123"

    def test_json_with_text_field(self):
        raw = '{"text": "Output text"}'
        text, sid = _parse_output(raw)
        assert text == "Output text"
        assert sid is None

    def test_json_with_content_field(self):
        raw = '{"content": "Some content"}'
        text, sid = _parse_output(raw)
        assert text == "Some content"

    def test_empty_input(self):
        text, sid = _parse_output("")
        assert text == ""
        assert sid is None

    def test_whitespace_only(self):
        text, sid = _parse_output("   \n  ")
        assert text == ""
        assert sid is None

    def test_invalid_json(self):
        text, sid = _parse_output("not json at all")
        assert text == "not json at all"
        assert sid is None

    def test_non_dict_json(self):
        raw = '"just a string"'
        text, sid = _parse_output(raw)
        assert text == "just a string"
        assert sid is None

    def test_result_takes_priority(self):
        raw = '{"result": "primary", "text": "fallback", "session_id": "s1"}'
        text, sid = _parse_output(raw)
        assert text == "primary"
        assert sid == "s1"


class TestRenderTemplate:
    def test_research_template(self):
        output = render_template(
            "research_sprint.md.j2",
            study_context="Study about transformers",
            idea="feature absorption",
            sprint_dir="/tmp/sprint",
        )
        assert "feature absorption" in output
        assert "Study about transformers" in output
        assert "/tmp/sprint" in output

    def test_research_template_contains_progress_md(self):
        """Research template instructs the runner to maintain progress.md."""
        output = render_template(
            "research_sprint.md.j2",
            study_context="Study context",
            idea="test idea",
            sprint_dir="/tmp/sprint",
        )
        assert "progress.md" in output

    def test_red_team_template(self):
        output = render_template(
            "red_team.md.j2",
            idea="test idea",
            round_number=2,
            max_rounds=3,
        )
        assert "Round 2 of 3" in output
        assert "red_team_round_2.md" in output
        assert "NO CRITICAL ISSUES" in output

    def test_fix_issues_template(self):
        output = render_template("fix_issues.md.j2", round_number=1)
        assert "red_team_round_1.md" in output
        assert "fixes_round_1.md" in output

    def test_report_template(self):
        output = render_template("report.md.j2", idea="explore SAEs")
        assert "explore SAEs" in output
        assert "report.md" in output

    def test_summarizer_template(self):
        output = render_template("summarizer.md.j2")
        assert "summary.txt" in output

    def test_idea_generator_template(self):
        output = render_template(
            "idea_generator.md.j2",
            study_context="Study context here",
            previous_sprints=[
                {"id": "sp-001", "summary": "Found X"},
                {"id": "sp-002", "summary": "Confirmed Y"},
            ],
        )
        assert "sp-001" in output
        assert "Found X" in output
        assert "sp-002" in output


class TestJobScriptWatchdog:
    """The heartbeat loop in slurm.sh.j2 / sge.sh.j2 should warn when the
    active step's stream-json output stops growing — signature of the hung
    pipeline class of bug.
    """

    def _assert_watchdog_present(self, script: str) -> None:
        assert "STUCK_PIPE detected" in script
        assert "stuck_threshold_secs=300" in script
        # Watchdog must live inside the heartbeat loop, not in run_step.
        assert "_heartbeat_loop()" in script
        # mtime check uses both Linux + BSD stat fallbacks.
        assert "stat -c %Y" in script
        assert "stat -f %m" in script
        # Once-per-stuck-episode flag, not log-every-heartbeat.
        assert "stuck_warned" in script

    def test_slurm_template_includes_watchdog(self):
        self._assert_watchdog_present(_render_job_template("slurm.sh.j2"))

    def test_sge_template_includes_watchdog(self):
        self._assert_watchdog_present(_render_job_template("sge.sh.j2"))
