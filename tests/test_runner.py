"""Tests for runner components (template rendering, output parsing)."""

from researchloop.runner.claude import _parse_output, render_template


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
