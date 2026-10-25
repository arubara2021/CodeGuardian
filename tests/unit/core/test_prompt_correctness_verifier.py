"""Tests for the correctness verifier prompt and its golden cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.prompts import (
    MissingVariableError,
    VerdictOutput,
    load_golden,
    render_prompt,
    run_golden,
    validate_output,
)

PROMPTS_ROOT = Path(__file__).resolve().parents[3] / "prompts"

SAMPLE_VARS = {
    "diff": (
        "--- a/src/util.py\n"
        "+++ b/src/util.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-def add(a, b):\n"
        "+def add(a: int, b: int) -> int:\n"
        "     return a + b\n"
    ),
    "change_kind": "syntactic",
    "blast_score": "12",
    "contract_assertions": "",
}


@pytest.fixture(scope="module")
def rendered() -> str:
    return render_prompt(
        "correctness_verifier", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT
    ).rendered


class TestRender:
    def test_renders_without_error(self) -> None:
        result = render_prompt(
            "correctness_verifier", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT
        )
        assert result.role == "correctness_verifier"
        assert result.version == "1.0.0"

    def test_mode_is_plan(self) -> None:
        result = render_prompt(
            "correctness_verifier", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT
        )
        assert result.frontmatter.get("mode") == "plan"

    def test_role_in_frontmatter(self) -> None:
        result = render_prompt(
            "correctness_verifier", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT
        )
        assert result.frontmatter.get("role") == "correctness_verifier"

    def test_variables_substituted(self, rendered: str) -> None:
        assert "{{" not in rendered
        assert "syntactic" in rendered
        assert "12" in rendered

    def test_base_sections_present(self, rendered: str) -> None:
        assert "Refusal protocol" in rendered
        assert "Authoritative source" in rendered

    def test_role_section_present(self, rendered: str) -> None:
        assert "Correctness Verifier Role" in rendered

    def test_independence_stated(self, rendered: str) -> None:
        assert "did not write" in rendered.lower()


class TestNoTools:
    def test_no_tool_names_listed(self, rendered: str) -> None:
        for tool in (
            "function-body",
            "call-graph-slice",
            "code-skeleton",
            "list-symbols",
        ):
            assert tool not in rendered


class TestOutputContract:
    def test_verdict_values_documented(self, rendered: str) -> None:
        for value in ("pass", "fail", "uncertain", "refused"):
            assert value in rendered

    def test_fail_requires_finding(self, rendered: str) -> None:
        assert "fail" in rendered and "finding" in rendered.lower()

    def test_refusal_shape_documented(self, rendered: str) -> None:
        assert "reason" in rendered


class TestRefusesOnMissing:
    def test_missing_diff(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "diff"}
        with pytest.raises(MissingVariableError):
            render_prompt("correctness_verifier", "1.0.0", bad, prompts_root=PROMPTS_ROOT)


class TestSchemaCompat:
    def test_pass_verdict_accepted(self) -> None:
        out = validate_output(
            "correctness_verifier",
            {"verdict": "pass", "confidence": 0.92, "findings": []},
        )
        assert isinstance(out, VerdictOutput)

    def test_fail_without_finding_rejected(self) -> None:
        with pytest.raises(CodeGuardianError):
            validate_output(
                "correctness_verifier",
                {"verdict": "fail", "confidence": 0.9, "findings": []},
            )

    def test_refused_without_reason_rejected(self) -> None:
        with pytest.raises(CodeGuardianError):
            validate_output("correctness_verifier", {"verdict": "refused"})


class TestGoldenCases:
    def test_ten_cases_present(self) -> None:
        cases = load_golden("correctness_verifier", PROMPTS_ROOT)
        assert len(cases) == 10

    def test_cases_have_required_fields(self) -> None:
        cases = load_golden("correctness_verifier", PROMPTS_ROOT)
        for case in cases:
            assert case.id
            assert case.expected_verdict in ("pass", "fail", "uncertain", "refused")
            assert "diff" in case.variables

    def test_case_verdicts_varied(self) -> None:
        cases = load_golden("correctness_verifier", PROMPTS_ROOT)
        verdicts = {case.expected_verdict for case in cases}
        assert "pass" in verdicts
        assert "fail" in verdicts
        assert "refused" in verdicts

    async def test_run_golden_with_perfect_model(self) -> None:
        cases = load_golden("correctness_verifier", PROMPTS_ROOT)

        async def perfect_model(prompt: str) -> str:
            for case in cases:
                if case.id in prompt:
                    pass
            if "truncated-diff" in prompt or "replacement omitted" in prompt:
                return json.dumps({"verdict": "refused", "reason": "diff truncated"})
            if "none" in prompt.lower() and "greet" in prompt:
                return json.dumps(
                    {
                        "verdict": "fail",
                        "confidence": 0.9,
                        "findings": [
                            {"severity": "warning", "message": "behavior changed on None"}
                        ],
                    }
                )
            return json.dumps({"verdict": "pass", "confidence": 0.9, "findings": []})

        report = await run_golden(
            cases, perfect_model, "correctness_verifier", "1.0.0", PROMPTS_ROOT
        )
        assert report.total == 10
        assert report.role == "correctness_verifier"

    async def test_run_golden_handles_malformed_json(self) -> None:
        cases = load_golden("correctness_verifier", PROMPTS_ROOT)[:2]

        async def bad_model(prompt: str) -> str:
            return "not json"

        report = await run_golden(cases, bad_model, "correctness_verifier", "1.0.0", PROMPTS_ROOT)
        assert report.total == 2
        assert report.failed == 2
        assert all(result.error for result in report.results)


class TestLoadErrors:
    def test_unknown_role(self) -> None:
        with pytest.raises(CodeGuardianError):
            load_golden("not-a-verifier", PROMPTS_ROOT)
