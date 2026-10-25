"""Tests for the writer prompt.

Verifies the prompt renders against a realistic variable set, declares
the correct mode, lists the four read-only tools, documents the refusal
convention, and validates against the source-output schema. Also checks
that the prompt mentions the preservation contract, which is the entire
point of the writer role.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.prompts import (
    MissingVariableError,
    SourceOutput,
    render_prompt,
    validate_output,
)

PROMPTS_ROOT = Path(__file__).resolve().parents[3] / "prompts"

SAMPLE_VARS = {
    "target_symbol": "Session.send",
    "target_path": "src/requests/sessions.py",
    "language": "python",
    "directive": "add type hints",
    "skeleton": (
        "class Session:\n  def send(self, request): ...\n  def request(self, method, url): ..."
    ),
    "blast_report": (
        '{"direct_callers": [{"qualified_name": "Session.request", '
        '"line": 500}], "contract_violations": [], "blast_score": 34, '
        '"recommendation": "review"}'
    ),
    "characterization_test": (
        "import pytest\n"
        "from src.requests.sessions import Session\n\n"
        "def test_send_returns_response():\n"
        "    s = Session()\n"
        "    assert s.send('req') is not None\n"
    ),
}

SAMPLE_REFACTORED = """\
def send(self, request: object) -> object:
    return self._send(request)
"""


@pytest.fixture(scope="module")
def rendered() -> str:
    return render_prompt("writer", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT).rendered


class TestWriterRender:
    def test_renders_without_error(self) -> None:
        result = render_prompt("writer", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.role == "writer"
        assert result.version == "1.0.0"

    def test_mode_is_auto_edit(self) -> None:
        result = render_prompt("writer", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.frontmatter.get("mode") == "auto-edit"

    def test_role_declared_in_frontmatter(self) -> None:
        result = render_prompt("writer", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.frontmatter.get("role") == "writer"

    def test_all_variables_substituted(self, rendered: str) -> None:
        assert "{{" not in rendered
        assert "Session.send" in rendered
        assert "src/requests/sessions.py" in rendered
        assert "add type hints" in rendered
        assert "test_send_returns_response" in rendered

    def test_base_section_present(self, rendered: str) -> None:
        assert "Refusal protocol" in rendered
        assert "Authoritative source" in rendered

    def test_role_section_present(self, rendered: str) -> None:
        assert "Writer Role" in rendered


class TestWriterToolList:
    def test_all_four_tools_listed(self, rendered: str) -> None:
        for tool in (
            "function-body",
            "call-graph-slice",
            "code-skeleton",
            "list-symbols",
        ):
            assert tool in rendered, f"tool {tool} missing"

    def test_tools_declared_read_only(self, rendered: str) -> None:
        assert "read-only" in rendered.lower()

    def test_call_graph_recommended_for_blast(self, rendered: str) -> None:
        assert "blast report" in rendered.lower()


class TestWriterPreservationContract:
    def test_preservation_language_present(self, rendered: str) -> None:
        assert "preservation" in rendered.lower()

    def test_behavior_language_present(self, rendered: str) -> None:
        assert "behavior" in rendered.lower()

    def test_identical_observable_behavior_stated(self, rendered: str) -> None:
        assert "identical observable behavior" in rendered.lower()

    def test_preservation_wins_over_directive(self, rendered: str) -> None:
        assert "preservation contract wins" in rendered.lower() or (
            "behavior\npreservation wins" in rendered.lower()
        )

    def test_signature_compatibility_stated(self, rendered: str) -> None:
        assert "signature" in rendered.lower()


class TestWriterOutputContract:
    def test_refused_convention_documented(self, rendered: str) -> None:
        assert "REFUSED:" in rendered

    def test_ast_parse_mentioned(self, rendered: str) -> None:
        assert "ast.parse" in rendered

    def test_function_only_output_documented(self, rendered: str) -> None:
        assert "refactored function" in rendered.lower()


class TestWriterRefusesOnMissingVariables:
    def test_missing_target_symbol_raises(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "target_symbol"}
        with pytest.raises(MissingVariableError):
            render_prompt("writer", "1.0.0", bad, prompts_root=PROMPTS_ROOT)

    def test_missing_characterization_test_raises(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "characterization_test"}
        with pytest.raises(MissingVariableError):
            render_prompt("writer", "1.0.0", bad, prompts_root=PROMPTS_ROOT)

    def test_missing_blast_report_raises(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "blast_report"}
        with pytest.raises(MissingVariableError):
            render_prompt("writer", "1.0.0", bad, prompts_root=PROMPTS_ROOT)


class TestWriterSchemaCompat:
    def test_valid_function_accepted_and_parses(self) -> None:
        out = validate_output("writer", SAMPLE_REFACTORED)
        assert isinstance(out, SourceOutput)
        assert out.is_refusal is False
        ast.parse(out.content)

    def test_refused_line_accepted(self) -> None:
        out = validate_output("writer", "REFUSED: directive requires removing a live parameter")
        assert out.is_refusal is True
        assert "live parameter" in out.refusal_reason

    def test_invalid_python_rejected_by_ast(self) -> None:
        bad_output = "def send(self, request:\n    return None\n"
        out = validate_output("writer", bad_output)
        with pytest.raises(SyntaxError):
            ast.parse(out.content)


class TestWriterLoadErrors:
    def test_unknown_version_raises(self) -> None:
        with pytest.raises(CodeGuardianError):
            render_prompt("writer", "9.9.9", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
