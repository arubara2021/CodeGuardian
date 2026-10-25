"""Tests for the prompt renderer, versioning, and output schemas.

The renderer tests cover the strict-substitution contract: missing
variables, unbalanced braces, injection attempts, and the happy path.
The versioning tests use ``set_prompts_root`` to point at a temporary
directory so they do not depend on the real prompt files being written
yet. The schema tests exercise every branch of ``validate_output`` for
every role.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.prompts import (
    ContextPackOutput,
    InjectionError,
    MissingVariableError,
    PromptBundle,
    SkillProposalOutput,
    SourceOutput,
    VerdictOutput,
    find_variables,
    load_prompt,
    render,
    render_prompt,
    set_prompts_root,
    validate_output,
)

# -- Renderer -----------------------------------------------------------


class TestRenderer:
    def test_empty_template_renders_empty(self):
        assert render("", {}) == ""

    def test_template_without_variables_unchanged(self):
        assert render("hello world", {}) == "hello world"

    def test_single_variable(self):
        assert render("hello {{name}}", {"name": "arun"}) == "hello arun"

    def test_multiple_variables(self):
        out = render("{{a}} + {{b}}", {"a": "1", "b": "2"})
        assert out == "1 + 2"

    def test_same_variable_twice(self):
        out = render("{{x}} and {{x}}", {"x": "y"})
        assert out == "y and y"

    def test_extra_keys_ignored(self):
        out = render("hi {{name}}", {"name": "a", "extra": "b"})
        assert out == "hi a"

    def test_missing_variable_raises(self):
        with pytest.raises(MissingVariableError) as exc_info:
            render("hi {{name}}", {})
        assert "name" in exc_info.value.details["missing"]

    def test_multiple_missing_reported(self):
        with pytest.raises(MissingVariableError) as exc_info:
            render("{{a}} and {{b}}", {})
        assert set(exc_info.value.details["missing"]) == {"a", "b"}

    def test_injection_via_value_raises(self):
        with pytest.raises(InjectionError):
            render("{{outer}}", {"outer": "before {{inner}} after", "inner": "x"})

    def test_injection_only_fires_for_existing_variable(self):
        out = render("{{outer}}", {"outer": "before {{unknown}} after"})
        assert out == "before {{unknown}} after"

    def test_unclosed_braces_raise(self):
        with pytest.raises(CodeGuardianError) as exc_info:
            render("hi {{name", {})
        assert exc_info.value.code is ErrorCode.SYS_INTERNAL

    def test_stray_close_raises(self):
        with pytest.raises(CodeGuardianError):
            render("hi name}}", {})

    def test_invalid_variable_name_not_substituted(self):
        out = render("keep {{123}} literal", {})
        assert out == "keep {{123}} literal"

    def test_find_variables_order_and_dedup(self):
        names = find_variables("{{b}} {{a}} {{b}} {{c}}")
        assert names == ("b", "a", "c")

    def test_find_variables_empty(self):
        assert find_variables("no vars here") == ()

    def test_value_with_newline_preserved(self):
        out = render("{{code}}", {"code": "line1\nline2\n"})
        assert out == "line1\nline2\n"


# -- Versioning ---------------------------------------------------------


@pytest.fixture
def prompts_root(tmp_path: Path):
    (tmp_path / "_base").mkdir()
    (tmp_path / "analyst").mkdir()
    (tmp_path / "_base" / "v1.0.0.md").write_text("BASE BODY\n", encoding="utf-8")
    (tmp_path / "analyst" / "v1.0.0.md").write_text(
        "---\nrole: analyst\nversion: 1.0.0\nmode: plan\n---\nANALYST BODY\n",
        encoding="utf-8",
    )
    yield tmp_path
    set_prompts_root(None)


class TestVersioning:
    def test_load_prompt_returns_bundle(self, prompts_root: Path):
        set_prompts_root(prompts_root)
        bundle = load_prompt("analyst", "1.0.0")
        assert isinstance(bundle, PromptBundle)
        assert bundle.role == "analyst"
        assert bundle.version == "1.0.0"
        assert "BASE BODY" in bundle.base_text
        assert "ANALYST BODY" in bundle.role_text
        assert bundle.frontmatter["mode"] == "plan"

    def test_load_prompt_strips_frontmatter_from_body(self, prompts_root: Path):
        set_prompts_root(prompts_root)
        bundle = load_prompt("analyst", "1.0.0")
        assert "---" not in bundle.role_text
        assert "mode: plan" not in bundle.role_text

    def test_unknown_role_raises(self, prompts_root: Path):
        set_prompts_root(prompts_root)
        with pytest.raises(CodeGuardianError):
            load_prompt("not-a-role", "1.0.0")

    def test_malformed_version_raises(self, prompts_root: Path):
        set_prompts_root(prompts_root)
        with pytest.raises(CodeGuardianError):
            load_prompt("analyst", "1.0")

    def test_missing_file_raises(self, prompts_root: Path):
        set_prompts_root(prompts_root)
        with pytest.raises(CodeGuardianError):
            load_prompt("analyst", "9.9.9")


# -- render_prompt integration -----------------------------------------


class TestRenderPrompt:
    def test_combines_base_and_role(self, prompts_root: Path):
        (prompts_root / "_base" / "v1.0.0.md").write_text("BASE {{role_name}}", encoding="utf-8")
        (prompts_root / "analyst" / "v1.0.0.md").write_text(
            "---\nrole: analyst\nmode: plan\n---\nROLE {{role_name}}",
            encoding="utf-8",
        )
        set_prompts_root(prompts_root)
        result = render_prompt("analyst", "1.0.0", {"role_name": "analyst"})
        assert "BASE analyst" in result.rendered
        assert "ROLE analyst" in result.rendered
        assert result.frontmatter["mode"] == "plan"
        assert result.variables_used == ("role_name",)


# -- Schema validation --------------------------------------------------


class TestVerdictSchema:
    def test_valid_pass(self):
        out = validate_output(
            "correctness_verifier",
            {"verdict": "pass", "confidence": 0.95, "findings": []},
        )
        assert isinstance(out, VerdictOutput)
        assert out.verdict == "pass"
        assert out.confidence == pytest.approx(0.95)

    def test_valid_fail_with_finding(self):
        out = validate_output(
            "security_verifier",
            {
                "verdict": "fail",
                "confidence": 0.8,
                "findings": [
                    {"severity": "critical", "message": "injection via eval", "location": "x.py:10"}
                ],
            },
        )
        assert out.verdict == "fail"
        assert len(out.findings) == 1
        assert out.findings[0].severity == "critical"

    def test_fail_without_finding_raises(self):
        with pytest.raises(CodeGuardianError):
            validate_output(
                "contract_verifier",
                {"verdict": "fail", "confidence": 0.9, "findings": []},
            )

    def test_valid_refused(self):
        out = validate_output(
            "correctness_verifier",
            {"verdict": "refused", "reason": "diff malformed"},
        )
        assert out.verdict == "refused"
        assert out.reason == "diff malformed"
        assert out.findings == ()

    def test_refused_without_reason_raises(self):
        with pytest.raises(CodeGuardianError):
            validate_output("security_verifier", {"verdict": "refused"})

    def test_invalid_verdict_raises(self):
        with pytest.raises(CodeGuardianError):
            validate_output(
                "correctness_verifier",
                {"verdict": "maybe", "confidence": 0.5},
            )

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(CodeGuardianError):
            validate_output(
                "correctness_verifier",
                {"verdict": "pass", "confidence": 1.5, "findings": []},
            )

    def test_non_mapping_raises(self):
        with pytest.raises(CodeGuardianError):
            validate_output("correctness_verifier", "not a mapping")


class TestSourceSchema:
    def test_valid_source(self):
        out = validate_output("writer", "def f():\n    return 1\n")
        assert isinstance(out, SourceOutput)
        assert out.is_refusal is False
        assert "def f" in out.content

    def test_refusal_convention(self):
        out = validate_output("tester", "REFUSED: target file unreadable")
        assert out.is_refusal is True
        assert out.refusal_reason == "target file unreadable"
        assert out.content == ""

    def test_refusal_without_reason_raises(self):
        with pytest.raises(CodeGuardianError):
            validate_output("writer", "REFUSED:")

    def test_non_string_raises(self):
        with pytest.raises(CodeGuardianError):
            validate_output("writer", {"source": "x"})


class TestContextPackSchema:
    def test_valid(self):
        out = validate_output(
            "analyst",
            {
                "files": [
                    {"path": "src/a.py", "reason": "target"},
                    {"path": "src/b.py", "reason": "direct caller"},
                ],
                "reason": "target and one caller",
            },
        )
        assert isinstance(out, ContextPackOutput)
        assert len(out.files) == 2
        assert out.files[0].path == "src/a.py"

    def test_missing_files_raises(self):
        with pytest.raises(CodeGuardianError):
            validate_output("analyst", {"reason": "x"})

    def test_file_missing_reason_raises(self):
        with pytest.raises(CodeGuardianError):
            validate_output("analyst", {"files": [{"path": "src/a.py"}]})

    def test_empty_files_allowed(self):
        out = validate_output("analyst", {"files": []})
        assert out.files == ()


class TestSkillSchema:
    def test_valid(self):
        out = validate_output(
            "skill_curator",
            {
                "skill_name": "python-add-types",
                "skill_markdown": "---\nname: x\n---\nbody",
                "confidence": 0.7,
            },
        )
        assert isinstance(out, SkillProposalOutput)
        assert out.skill_name == "python-add-types"

    def test_missing_name_raises(self):
        with pytest.raises(CodeGuardianError):
            validate_output(
                "skill_curator",
                {"skill_markdown": "x", "confidence": 0.5},
            )

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(CodeGuardianError):
            validate_output(
                "skill_curator",
                {"skill_name": "x", "skill_markdown": "y", "confidence": 2.0},
            )


class TestUnknownRole:
    def test_validate_output_unknown_role_raises(self):
        with pytest.raises(CodeGuardianError):
            validate_output("not-a-role", {})
