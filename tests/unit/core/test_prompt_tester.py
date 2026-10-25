"""Tests for the tester prompt.

Verifies the prompt renders against a realistic variable set, declares
the correct mode, lists the four read-only tools, documents the refusal
convention, and validates against the source-output schema (both a
valid Python test file and a REFUSED: line).
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
}

SAMPLE_TEST_FILE = """\
import pytest

from src.requests.sessions import Session


def test_send_returns_response():
    s = Session()
    result = s.send("request")
    assert result is not None


def test_send_empty_request_raises():
    s = Session()
    with pytest.raises(ValueError):
        s.send("")
"""


@pytest.fixture(scope="module")
def rendered() -> str:
    return render_prompt("tester", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT).rendered


class TestTesterRender:
    def test_renders_without_error(self) -> None:
        result = render_prompt("tester", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.role == "tester"
        assert result.version == "1.0.0"

    def test_mode_is_auto_edit(self) -> None:
        result = render_prompt("tester", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.frontmatter.get("mode") == "auto-edit"

    def test_role_declared_in_frontmatter(self) -> None:
        result = render_prompt("tester", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.frontmatter.get("role") == "tester"

    def test_all_variables_substituted(self, rendered: str) -> None:
        assert "{{" not in rendered
        assert "Session.send" in rendered
        assert "src/requests/sessions.py" in rendered
        assert "add type hints" in rendered
        assert "python" in rendered

    def test_base_section_present(self, rendered: str) -> None:
        assert "Refusal protocol" in rendered
        assert "Authoritative source" in rendered
        assert "Output discipline" in rendered

    def test_role_section_present(self, rendered: str) -> None:
        assert "Tester Role" in rendered


class TestTesterToolList:
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

    def test_function_body_recommended_first(self, rendered: str) -> None:
        assert "Use this\n  first" in rendered or "function-body — the full" in rendered


class TestTesterOutputContract:
    def test_pytest_named(self, rendered: str) -> None:
        assert "pytest" in rendered

    def test_test_file_named(self, rendered: str) -> None:
        assert "test file" in rendered.lower()

    def test_refused_convention_documented(self, rendered: str) -> None:
        assert "REFUSED:" in rendered

    def test_mocking_forbidden(self, rendered: str) -> None:
        assert "mock" in rendered.lower()

    def test_ast_parse_mentioned(self, rendered: str) -> None:
        assert "ast.parse" in rendered


class TestTesterRefusesOnMissingVariables:
    def test_missing_target_symbol_raises(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "target_symbol"}
        with pytest.raises(MissingVariableError):
            render_prompt("tester", "1.0.0", bad, prompts_root=PROMPTS_ROOT)

    def test_missing_blast_report_raises(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "blast_report"}
        with pytest.raises(MissingVariableError):
            render_prompt("tester", "1.0.0", bad, prompts_root=PROMPTS_ROOT)


class TestTesterSchemaCompat:
    def test_valid_python_test_file_accepted_and_parses(self) -> None:
        out = validate_output("tester", SAMPLE_TEST_FILE)
        assert isinstance(out, SourceOutput)
        assert out.is_refusal is False
        ast.parse(out.content)

    def test_refused_line_accepted(self) -> None:
        out = validate_output("tester", "REFUSED: target body unreadable")
        assert out.is_refusal is True
        assert out.refusal_reason == "target body unreadable"

    def test_invalid_python_rejected_by_ast(self) -> None:
        bad_output = "def test_broken(:\n    pass\n"
        out = validate_output("tester", bad_output)
        with pytest.raises(SyntaxError):
            ast.parse(out.content)


class TestTesterLoadErrors:
    def test_unknown_version_raises(self) -> None:
        with pytest.raises(CodeGuardianError):
            render_prompt("tester", "9.9.9", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
