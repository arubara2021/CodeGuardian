"""Tests for the skill curator prompt.

Two checks: the prompt renders against a realistic variable set, and
the rendered text contains the contract the runtime expects.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.prompts import MissingVariableError, render_prompt

PROMPTS_ROOT = Path(__file__).resolve().parents[3] / "prompts"

SAMPLE_VARS = {
    "directive": "extract the retry logic into a helper",
    "target_symbol": "client.request",
    "target_path": "src/agent/client.py",
    "diff": "--- a/src/agent/client.py\n+++ b/src/agent/client.py\n@@ ...",
    "verifier_verdicts": ("correctness: pass (0.91)\nsecurity: pass (0.88)\ncontract: pass (0.85)"),
    "test_results": "12 characterization tests passed on old and new code",
}


@pytest.fixture(scope="module")
def rendered() -> str:
    return render_prompt("skill_curator", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT).rendered


class TestSkillCuratorRender:
    def test_renders_without_error(self) -> None:
        result = render_prompt("skill_curator", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.role == "skill_curator"
        assert result.version == "1.0.0"

    def test_mode_is_plan(self) -> None:
        result = render_prompt("skill_curator", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.frontmatter.get("mode") == "plan"

    def test_role_declared_in_frontmatter(self) -> None:
        result = render_prompt("skill_curator", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.frontmatter.get("role") == "skill_curator"

    def test_all_variables_substituted(self, rendered: str) -> None:
        assert "{{" not in rendered
        assert "extract the retry logic" in rendered
        assert "client.request" in rendered
        assert "src/agent/client.py" in rendered
        assert "correctness: pass" in rendered
        assert "12 characterization tests" in rendered

    def test_base_section_present(self, rendered: str) -> None:
        assert "Refusal protocol" in rendered
        assert "Output discipline" in rendered

    def test_role_section_present(self, rendered: str) -> None:
        assert "Skill Curator Role" in rendered


class TestSkillCuratorOutputContract:
    def test_json_shape_documented(self, rendered: str) -> None:
        assert '"skill_name"' in rendered
        assert '"skill_markdown"' in rendered
        assert '"confidence"' in rendered

    def test_no_skill_sentinel_documented(self, rendered: str) -> None:
        assert '"(none)"' in rendered

    def test_confidence_range_documented(self, rendered: str) -> None:
        assert "[0.0, 1.0]" in rendered

    def test_skill_markdown_requirements_documented(self, rendered: str) -> None:
        assert "SKILL.md" in rendered
        assert "frontmatter" in rendered.lower()


class TestSkillCuratorNoTools:
    def test_no_tool_list_in_prompt(self, rendered: str) -> None:
        for tool in (
            "code-skeleton",
            "function-body",
            "call-graph-slice",
            "list-symbols",
        ):
            assert tool not in rendered, f"skill_curator should not list {tool}"


class TestSkillCuratorRefusesOnMissingVariables:
    def test_missing_diff_raises(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "diff"}
        with pytest.raises(MissingVariableError):
            render_prompt("skill_curator", "1.0.0", bad, prompts_root=PROMPTS_ROOT)

    def test_missing_verifier_verdicts_raises(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "verifier_verdicts"}
        with pytest.raises(MissingVariableError):
            render_prompt("skill_curator", "1.0.0", bad, prompts_root=PROMPTS_ROOT)


class TestSkillCuratorSchemaCompat:
    def test_output_schema_accepts_valid_proposal(self) -> None:
        from codeguardian.core.prompts import validate_output

        out = validate_output(
            "skill_curator",
            {
                "skill_name": "python-extract-retry-helper",
                "skill_markdown": "---\nname: x\n---\nbody",
                "confidence": 0.75,
            },
        )
        assert out.role == "skill_curator"
        assert out.skill_name == "python-extract-retry-helper"
        assert out.confidence == pytest.approx(0.75)

    def test_output_schema_accepts_none_sentinel(self) -> None:
        from codeguardian.core.prompts import validate_output

        out = validate_output(
            "skill_curator",
            {
                "skill_name": "(none)",
                "skill_markdown": "no reusable pattern in this refactor",
                "confidence": 0.0,
            },
        )
        assert out.confidence == pytest.approx(0.0)


class TestSkillCuratorLoadErrors:
    def test_unknown_version_raises(self) -> None:
        with pytest.raises(CodeGuardianError):
            render_prompt("skill_curator", "9.9.9", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
