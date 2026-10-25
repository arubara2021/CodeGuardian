"""Tests for the analyst prompt.

Two checks: the prompt renders without error against a realistic
variable set, and the rendered text contains the contract the runtime
expects: read-only tool list, correct mode, the input variables, and
the JSON output shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.prompts import MissingVariableError, render_prompt

PROMPTS_ROOT = Path(__file__).resolve().parents[3] / "prompts"

SAMPLE_VARS = {
    "target_symbol": "Session.send",
    "target_path": "src/requests/sessions.py",
    "directive": "add type hints",
    "blast_report": '{"direct_callers": [{"qualified_name": "Session.request"}]}',
    "skeleton": "class Session:\n  def send(self): ...\n  def request(self): ...",
}


@pytest.fixture(scope="module")
def rendered() -> str:
    return render_prompt("analyst", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT).rendered


class TestAnalystRender:
    def test_renders_without_error(self) -> None:
        result = render_prompt("analyst", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.role == "analyst"
        assert result.version == "1.0.0"

    def test_mode_is_plan(self) -> None:
        result = render_prompt("analyst", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.frontmatter.get("mode") == "plan"

    def test_role_declared_in_frontmatter(self) -> None:
        result = render_prompt("analyst", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.frontmatter.get("role") == "analyst"

    def test_all_variables_substituted(self, rendered: str) -> None:
        assert "{{" not in rendered
        assert "Session.send" in rendered
        assert "src/requests/sessions.py" in rendered
        assert "add type hints" in rendered

    def test_base_section_present(self, rendered: str) -> None:
        assert "Refusal protocol" in rendered
        assert "Authoritative source" in rendered

    def test_role_section_present(self, rendered: str) -> None:
        assert "Analyst Role" in rendered


class TestAnalystToolList:
    def test_all_four_tools_listed(self, rendered: str) -> None:
        for tool in (
            "code-skeleton",
            "function-body",
            "call-graph-slice",
            "list-symbols",
        ):
            assert tool in rendered, f"tool {tool} missing"

    def test_tools_declared_read_only(self, rendered: str) -> None:
        assert "read-only" in rendered.lower()


class TestAnalystOutputContract:
    def test_json_shape_documented(self, rendered: str) -> None:
        assert '"files"' in rendered
        assert '"path"' in rendered
        assert '"reason"' in rendered

    def test_refusal_mechanism_documented(self, rendered: str) -> None:
        assert "refused:" in rendered


class TestAnalystRefusesOnMissingVariables:
    def test_missing_target_symbol_raises(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "target_symbol"}
        with pytest.raises(MissingVariableError):
            render_prompt("analyst", "1.0.0", bad, prompts_root=PROMPTS_ROOT)

    def test_missing_blast_report_raises(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "blast_report"}
        with pytest.raises(MissingVariableError):
            render_prompt("analyst", "1.0.0", bad, prompts_root=PROMPTS_ROOT)

    def test_missing_skeleton_raises(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "skeleton"}
        with pytest.raises(MissingVariableError):
            render_prompt("analyst", "1.0.0", bad, prompts_root=PROMPTS_ROOT)


class TestAnalystSchemaCompat:
    def test_output_schema_accepts_valid_response(self) -> None:
        from codeguardian.core.prompts import validate_output

        out = validate_output(
            "analyst",
            {
                "files": [{"path": "src/a.py", "reason": "target"}],
                "reason": "target only",
            },
        )
        assert out.role == "analyst"
        assert len(out.files) == 1

    def test_output_schema_accepts_refusal_shape(self) -> None:
        from codeguardian.core.prompts import validate_output

        out = validate_output(
            "analyst",
            {"files": [], "reason": "refused: target file missing"},
        )
        assert out.files == ()
        assert out.reason.startswith("refused:")


class TestAnalystLoadErrors:
    def test_unknown_version_raises(self) -> None:
        with pytest.raises(CodeGuardianError):
            render_prompt("analyst", "9.9.9", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
