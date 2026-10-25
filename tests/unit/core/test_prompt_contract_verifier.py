"""Tests for the contract verifier prompt and its golden cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
        "--- a/src/config.py\n"
        "+++ b/src/config.py\n"
        "@@ -1,2 +1,4 @@\n"
        " def get_config():\n"
        "     data = load()\n"
        "+    if not data:\n"
        "+        return None\n"
        "     return data\n"
    ),
    "change_kind": "contract",
    "blast_score": "55",
    "contract_assertions": (
        "- caller: src/api.py:handler\n"
        "  assumption: get_config() returns a dict, never None\n"
        "  severity: critical\n"
    ),
}


@pytest.fixture(scope="module")
def rendered() -> str:
    return render_prompt(
        "contract_verifier", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT
    ).rendered


class TestRender:
    def test_renders_without_error(self) -> None:
        result = render_prompt("contract_verifier", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.role == "contract_verifier"

    def test_mode_is_plan(self) -> None:
        result = render_prompt("contract_verifier", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.frontmatter.get("mode") == "plan"

    def test_role_in_frontmatter(self) -> None:
        result = render_prompt("contract_verifier", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.frontmatter.get("role") == "contract_verifier"

    def test_variables_substituted(self, rendered: str) -> None:
        assert "{{" not in rendered
        assert "contract" in rendered
        assert "get_config" in rendered

    def test_assertions_substituted(self, rendered: str) -> None:
        assert "returns a dict, never None" in rendered

    def test_base_sections_present(self, rendered: str) -> None:
        assert "Refusal protocol" in rendered

    def test_role_section_present(self, rendered: str) -> None:
        assert "Contract Verifier Role" in rendered

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

    def test_broken_assertion_critical(self, rendered: str) -> None:
        assert "critical" in rendered

    def test_uncertain_for_undecidable(self, rendered: str) -> None:
        assert "uncertain" in rendered


class TestRefusesOnMissing:
    def test_missing_diff(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "diff"}
        with pytest.raises(MissingVariableError):
            render_prompt("contract_verifier", "1.0.0", bad, prompts_root=PROMPTS_ROOT)

    def test_missing_contract_assertions(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "contract_assertions"}
        with pytest.raises(MissingVariableError):
            render_prompt("contract_verifier", "1.0.0", bad, prompts_root=PROMPTS_ROOT)


class TestSchemaCompat:
    def test_fail_with_broken_assertion_accepted(self) -> None:
        out = validate_output(
            "contract_verifier",
            {
                "verdict": "fail",
                "confidence": 0.9,
                "findings": [
                    {
                        "severity": "critical",
                        "message": "assertion broken: returns None",
                        "location": "src/config.py:3",
                    }
                ],
            },
        )
        assert isinstance(out, VerdictOutput)
        assert out.findings[0].severity == "critical"


class TestGoldenCases:
    def test_ten_cases_present(self) -> None:
        cases = load_golden("contract_verifier", PROMPTS_ROOT)
        assert len(cases) == 10

    def test_cases_have_required_fields(self) -> None:
        cases = load_golden("contract_verifier", PROMPTS_ROOT)
        for case in cases:
            assert case.id
            assert case.expected_verdict in ("pass", "fail", "uncertain", "refused")

    def test_cases_cover_all_verdicts(self) -> None:
        cases = load_golden("contract_verifier", PROMPTS_ROOT)
        verdicts = {case.expected_verdict for case in cases}
        assert "pass" in verdicts
        assert "fail" in verdicts
        assert "refused" in verdicts

    async def test_run_golden_records_every_case(self) -> None:
        cases = load_golden("contract_verifier", PROMPTS_ROOT)

        async def model(prompt: str) -> str:
            return json.dumps({"verdict": "pass", "confidence": 0.8, "findings": []})

        report = await run_golden(cases, model, "contract_verifier", "1.0.0", PROMPTS_ROOT)
        assert report.total == 10
        assert len(report.results) == 10
        assert all(result.case_id for result in report.results)

    async def test_run_golden_handles_invalid_json(self) -> None:
        cases = load_golden("contract_verifier", PROMPTS_ROOT)[:1]

        async def model(prompt: str) -> str:
            return "prose not json"

        report = await run_golden(cases, model, "contract_verifier", "1.0.0", PROMPTS_ROOT)
        assert report.total == 1
        assert report.failed == 1
        assert "invalid JSON" in report.results[0].error
