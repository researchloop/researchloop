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
        tweak_id="tw-test",
        study_name="study",
        idea="test idea",
        sprint_dirname="sp-test-dir",
        sprint_dir="/work/sp-test-dir",
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

    def test_is_error_result_raises(self):
        import pytest

        raw = '{"result": "API overloaded", "is_error": true, "subtype": "success"}'
        with pytest.raises(RuntimeError, match="error"):
            _parse_output(raw)

    def test_error_subtype_raises(self):
        import pytest

        raw = '{"result": "hit the cap", "subtype": "error_max_turns"}'
        with pytest.raises(RuntimeError, match="error_max_turns"):
            _parse_output(raw)

    def test_success_result_with_explicit_false_is_ok(self):
        raw = '{"result": "all good", "is_error": false, "subtype": "success"}'
        text, sid = _parse_output(raw)
        assert text == "all good"


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

    def test_long_running_guidance_shared_across_every_prompt(self):
        """The long-running-command guidance lives in one partial
        (_long_running_commands.md.j2) that EVERY pipeline prompt includes, so
        the rules can't drift between steps. Each rendered prompt must inline the
        partial (no raw Jinja include tag left behind) and carry its key rules:
        raised 24h timeout, foreground-or-joined-background, and the shell-level
        detach ban."""
        rendered = {
            "research_sprint.md.j2": render_template(
                "research_sprint.md.j2",
                study_context="Study context",
                idea="test idea",
                sprint_dir="/tmp/sprint",
            ),
            "fix_issues.md.j2": render_template("fix_issues.md.j2", round_number=1),
            "red_team.md.j2": render_template(
                "red_team.md.j2", idea="test idea", round_number=1, max_rounds=3
            ),
            "report.md.j2": render_template("report.md.j2", idea="test idea"),
            "tweak.md.j2": render_template(
                "tweak.md.j2", instruction="make the plot bigger"
            ),
            "summarizer.md.j2": render_template("summarizer.md.j2"),
            "idea_generator.md.j2": render_template(
                "idea_generator.md.j2", study_context="ctx", previous_sprints=[]
            ),
        }
        for name, output in rendered.items():
            # The include must have resolved — no raw Jinja tag left behind.
            assert "{% include" not in output, name
            # Sentinel content unique to the partial.
            assert "one-shot" in output, name
            assert "86400000" in output, name
            assert "24 hours" in output, name
            assert "Option A" in output, name
            assert "Option B" in output, name
            # Background is allowed but requires a mandatory blocking join.
            assert "run_in_background" in output, name
            assert "wait" in output, name
            assert "results/<job>.done" in output, name
            # Shell-level detach stays banned outright.
            assert "nohup" in output, name
            assert "setsid" in output, name
            assert "disown" in output, name

    def test_idea_generator_keeps_output_only_instruction_last(self):
        """idea_generator's output is captured verbatim into idea.txt, so the
        long-running partial must sit ABOVE the instructions — the dominant
        'output ONLY the idea text' line has to remain the final instruction."""
        output = render_template(
            "idea_generator.md.j2", study_context="ctx", previous_sprints=[]
        )
        assert output.index("86400000") < output.index("Output ONLY the idea text")

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

    def _assert_heartbeat_loop_disables_strict_mode(self, script: str) -> None:
        # The parent script has set -euo pipefail. The backgrounded
        # _heartbeat_loop inherits it — without explicitly disabling errexit
        # and pipefail, a single command-substitution failure (e.g. an
        # ls|head SIGPIPE race) can silently kill the watchdog and the
        # sprint loses all heartbeat + STUCK_PIPE detection for the rest of
        # its wall-clock. The first two `set` lines inside the function
        # must disable both modes.
        loop_idx = script.index("_heartbeat_loop() {")
        body_idx = script.index("\n", loop_idx) + 1
        # Check the disabling lines are at the top of the function body.
        head = script[body_idx : body_idx + 800]
        assert "set +e" in head
        assert "set +o pipefail" in head

    def _assert_hung_pipeline_recovery_present(self, script: str) -> None:
        # claude must run in its own session so the watchdog can SIGTERM the
        # whole group (claude + any leaked Bash-tool subprocesses) by pgid.
        assert "setsid " in script
        # Per-step pgid + result sentinel files used by the watchdog.
        assert "_pgid" in script
        assert "_result_seen" in script
        # Stream filter writes the sentinel on the terminal `result` event.
        assert "open('$result_sentinel'" in script
        # The watchdog escalates SIGTERM → SIGKILL against the pgid (negative
        # number argument to kill targets a process group).
        assert "kill -TERM -" in script
        assert "kill -KILL -" in script
        # Grace period between result event and pgid kill.
        assert "result_grace_secs=60" in script
        # Recovery messaging in the log so operators can tell when a kill ran.
        assert "STUCK_PIPE recovery" in script
        # run_step treats nonzero exit as success iff the sentinel exists.
        assert 'if [ ! -f "$result_sentinel" ]; then' in script
        # active_step file replaces the old log-grep step detection.
        assert "ACTIVE_STEP_FILE=" in script

    def test_slurm_template_includes_watchdog(self):
        self._assert_watchdog_present(_render_job_template("slurm.sh.j2"))

    def test_sge_template_includes_watchdog(self):
        self._assert_watchdog_present(_render_job_template("sge.sh.j2"))

    def test_slurm_template_includes_hung_pipeline_recovery(self):
        self._assert_hung_pipeline_recovery_present(_render_job_template("slurm.sh.j2"))

    def test_sge_template_includes_hung_pipeline_recovery(self):
        self._assert_hung_pipeline_recovery_present(_render_job_template("sge.sh.j2"))

    def _assert_error_result_handling_present(self, script: str) -> None:
        # The stream filter must detect a terminal result that carries an
        # error (is_error / subtype error*) and exit nonzero rather than
        # logging "Done" and letting the result sentinel mark it complete.
        assert "evt.get('is_error')" in script
        assert "subtype.startswith('error')" in script
        assert "open('$error_marker'" in script
        assert "sys.exit(1)" in script
        # run_step must fail the step when the error marker exists, ahead of
        # the result-sentinel "treat as complete" path.
        assert 'if [ -f "$error_marker" ]; then' in script
        error_idx = script.index('if [ -f "$error_marker" ]; then')
        sentinel_idx = script.index('if [ ! -f "$result_sentinel" ]; then')
        assert error_idx < sentinel_idx, (
            "error_marker check must come before the result_sentinel "
            "'treat as complete' path"
        )
        # The error marker is reset at the start of each step.
        assert 'rm -f "$pgid_file" "$result_sentinel" "$error_marker"' in script

    def test_slurm_template_fails_on_error_result(self):
        self._assert_error_result_handling_present(_render_job_template("slurm.sh.j2"))

    def test_sge_template_fails_on_error_result(self):
        self._assert_error_result_handling_present(_render_job_template("sge.sh.j2"))

    def test_slurm_heartbeat_loop_disables_strict_mode(self):
        self._assert_heartbeat_loop_disables_strict_mode(
            _render_job_template("slurm.sh.j2")
        )

    def test_sge_heartbeat_loop_disables_strict_mode(self):
        self._assert_heartbeat_loop_disables_strict_mode(
            _render_job_template("sge.sh.j2")
        )

    def _assert_bash_timeout_raised(self, script: str) -> None:
        # Claude's Bash tool defaults to a 10m timeout ceiling, which kills
        # long-running research commands mid-execution. The job script must
        # raise it to 24h before any claude invocation.
        assert "export BASH_MAX_TIMEOUT_MS=86400000" in script
        # The export must come before the first claude invocation so every
        # step (including the inline generate_idea step) inherits it.
        timeout_idx = script.index("BASH_MAX_TIMEOUT_MS")
        claude_idx = script.index("$CLAUDE_CMD")
        assert timeout_idx < claude_idx, (
            "BASH_MAX_TIMEOUT_MS must be exported before claude is run"
        )

    def test_slurm_template_raises_bash_timeout(self):
        self._assert_bash_timeout_raised(_render_job_template("slurm.sh.j2"))

    def test_sge_template_raises_bash_timeout(self):
        self._assert_bash_timeout_raised(_render_job_template("sge.sh.j2"))

    def test_slurm_tweak_template_raises_bash_timeout(self):
        self._assert_bash_timeout_raised(_render_job_template("slurm_tweak.sh.j2"))

    def test_sge_tweak_template_raises_bash_timeout(self):
        self._assert_bash_timeout_raised(_render_job_template("sge_tweak.sh.j2"))
