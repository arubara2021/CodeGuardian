"""Tests for the security verifier prompt and its golden cases."""

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
        "--- a/src/db.py\n"
        "+++ b/src/db.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-    cursor.execute('SELECT * FROM users WHERE name = ?', (username,))\n"
        "+    cursor.execute(f\"SELECT * FROM users WHERE name = '{username}'\")\n"
    ),
    "change_kind": "semantic",
    "blast_score": "60",
    "contract_assertions": "",
}


@pytest.fixture(scope="module")
def rendered() -> str:
    return render_prompt(
        "security_verifier", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT
    ).rendered


class TestRender:
    def test_renders_without_error(self) -> None:
        result = render_prompt("security_verifier", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.role == "security_verifier"

    def test_mode_is_plan(self) -> None:
        result = render_prompt("security_verifier", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.frontmatter.get("mode") == "plan"

    def test_role_in_frontmatter(self) -> None:
        result = render_prompt("security_verifier", "1.0.0", SAMPLE_VARS, prompts_root=PROMPTS_ROOT)
        assert result.frontmatter.get("role") == "security_verifier"

    def test_variables_substituted(self, rendered: str) -> None:
        assert "{{" not in rendered
        assert "semantic" in rendered

    def test_base_sections_present(self, rendered: str) -> None:
        assert "Refusal protocol" in rendered

    def test_role_section_present(self, rendered: str) -> None:
        assert "Security Verifier Role" in rendered

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


class TestFocusAreas:
    def test_injection_mentioned(self, rendered: str) -> None:
        assert "injection" in rendered.lower()

    def test_path_traversal_mentioned(self, rendered: str) -> None:
        assert "path traversal" in rendered.lower()

    def test_credential_exposure_mentioned(self, rendered: str) -> None:
        assert "credential" in rendered.lower()

    def test_crypto_downgrade_mentioned(self, rendered: str) -> None:
        assert "crypto" in rendered.lower() or "sha-1" in rendered.lower()


class TestOutputContract:
    def test_verdict_values_documented(self, rendered: str) -> None:
        for value in ("pass", "fail", "uncertain", "refused"):
            assert value in rendered

    def test_severity_levels_documented(self, rendered: str) -> None:
        for level in ("critical", "warning", "info"):
            assert level in rendered


class TestRefusesOnMissing:
    def test_missing_diff(self) -> None:
        bad = {k: v for k, v in SAMPLE_VARS.items() if k != "diff"}
        with pytest.raises(MissingVariableError):
            render_prompt("security_verifier", "1.0.0", bad, prompts_root=PROMPTS_ROOT)


class TestSchemaCompat:
    def test_fail_with_critical_accepted(self) -> None:
        out = validate_output(
            "security_verifier",
            {
                "verdict": "fail",
                "confidence": 0.88,
                "findings": [
                    {"severity": "critical", "message": "SQL injection", "location": "src/db.py:2"}
                ],
            },
        )
        assert isinstance(out, VerdictOutput)
        assert out.findings[0].severity == "critical"


class TestGoldenCases:
    def test_ten_cases_present(self) -> None:
        cases = load_golden("security_verifier", PROMPTS_ROOT)
        assert len(cases) == 10

    def test_cases_have_required_fields(self) -> None:
        cases = load_golden("security_verifier", PROMPTS_ROOT)
        for case in cases:
            assert case.id
            assert case.expected_verdict in ("pass", "fail", "uncertain", "refused")

    def test_cases_cover_pass_fail_uncertain(self) -> None:
        cases = load_golden("security_verifier", PROMPTS_ROOT)
        verdicts = {case.expected_verdict for case in cases}
        assert "pass" in verdicts
        assert "fail" in verdicts
        assert "uncertain" in verdicts

    async def test_run_golden_with_refusing_model(self) -> None:
        cases = load_golden("security_verifier", PROMPTS_ROOT)

        async def refusing_model(prompt: str) -> str:
            return json.dumps({"verdict": "refused", "reason": "cannot assess"})

        report = await run_golden(cases, refusing_model, "security_verifier", "1.0.0", PROMPTS_ROOT)
        assert report.total == 10
        # Only cases expecting refused can pass; expect <= count of refused.
        assert report.passed <= sum(1 for case in cases if case.expected_verdict == "refused")

    async def test_run_golden_with_perfect_model_returns_valid_report(self) -> None:
        cases = load_golden("security_verifier", PROMPTS_ROOT)

        async def model(prompt: str) -> str:
            return json.dumps({"verdict": "pass", "confidence": 0.9, "findings": []})

        report = await run_golden(cases, model, "security_verifier", "1.0.0", PROMPTS_ROOT)
        assert report.total == 10
        assert isinstance(report.passed, int)
        assert report.passed + report.failed == report.total
