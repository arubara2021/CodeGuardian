"""Tests for the golden-case evaluation harnesses.

Verifies every golden YAML loads, every harness runs all cases with a
valid fake model, and edge-case paths (refusals, the (none) sentinel)
work as expected. Does not assert model quality. That is Phase 14's
real-model evaluation's job. These tests prove the harness and prompt
rendering machinery work.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from codeguardian.core.agents.agent_eval import (
    load_golden as agent_load_golden,
)
from codeguardian.core.agents.agent_eval import (
    run_golden as agent_run_golden,
)
from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.prompts import (
    load_golden as verifier_load_golden,
)
from codeguardian.core.prompts import (
    run_golden as verifier_run_golden,
)

PROMPTS_ROOT = Path(__file__).resolve().parents[3] / "prompts"

_ANALYST_JSON = '{"files": [{"path": "a.py", "reason": "target"}], "reason": "ok"}'
_TESTER_SOURCE = "import pytest\n\ndef test_parse():\n    assert parse('x') == 'x'\n"
_WRITER_SOURCE = "def parse(raw: str) -> str:\n    return raw.strip()\n"
_SKILL_JSON = json.dumps(
    {
        "skill_name": "test-skill",
        "skill_markdown": (
            "---\n"
            "name: test-skill\n"
            "description: test description\n"
            "trigger: test trigger\n"
            "skill_type: refactor_pattern\n"
            "---\n"
            "body text of the skill"
        ),
        "confidence": 0.8,
    }
)
_VERIFIER_JSON = '{"verdict": "pass", "confidence": 0.9, "findings": []}'


def _fixed_fake(text: str) -> Callable[[str], Awaitable[str]]:
    async def fake(prompt: str) -> str:
        return text

    return fake


class TestGoldenLoads:
    def test_analyst_loads_12(self) -> None:
        cases = agent_load_golden("analyst", prompts_root=PROMPTS_ROOT)
        assert len(cases) == 12

    def test_tester_loads_12(self) -> None:
        cases = agent_load_golden("tester", prompts_root=PROMPTS_ROOT)
        assert len(cases) == 12

    def test_writer_loads_12(self) -> None:
        cases = agent_load_golden("writer", prompts_root=PROMPTS_ROOT)
        assert len(cases) == 12

    def test_skill_curator_loads_10(self) -> None:
        cases = agent_load_golden("skill_curator", prompts_root=PROMPTS_ROOT)
        assert len(cases) == 10

    def test_correctness_verifier_loads_10(self) -> None:
        cases = verifier_load_golden("correctness_verifier", prompts_root=PROMPTS_ROOT)
        assert len(cases) == 10

    def test_security_verifier_loads_10(self) -> None:
        cases = verifier_load_golden("security_verifier", prompts_root=PROMPTS_ROOT)
        assert len(cases) == 10

    def test_contract_verifier_loads_10(self) -> None:
        cases = verifier_load_golden("contract_verifier", prompts_root=PROMPTS_ROOT)
        assert len(cases) == 10


class TestHarnessRuns:
    async def test_analyst_harness_runs_all_cases(self) -> None:
        cases = agent_load_golden("analyst", prompts_root=PROMPTS_ROOT)
        report = await agent_run_golden(
            cases,
            _fixed_fake(_ANALYST_JSON),
            "analyst",
            "1.0.0",
            prompts_root=PROMPTS_ROOT,
        )
        assert report.total == len(cases)
        assert report.passed + report.failed == len(cases)

    async def test_tester_harness_runs_all_cases(self) -> None:
        cases = agent_load_golden("tester", prompts_root=PROMPTS_ROOT)
        report = await agent_run_golden(
            cases,
            _fixed_fake(_TESTER_SOURCE),
            "tester",
            "1.0.0",
            prompts_root=PROMPTS_ROOT,
        )
        assert report.total == len(cases)
        assert report.passed + report.failed == len(cases)

    async def test_writer_harness_runs_all_cases(self) -> None:
        cases = agent_load_golden("writer", prompts_root=PROMPTS_ROOT)
        report = await agent_run_golden(
            cases,
            _fixed_fake(_WRITER_SOURCE),
            "writer",
            "1.0.0",
            prompts_root=PROMPTS_ROOT,
        )
        assert report.total == len(cases)
        assert report.passed + report.failed == len(cases)

    async def test_skill_curator_harness_runs_all_cases(self) -> None:
        cases = agent_load_golden("skill_curator", prompts_root=PROMPTS_ROOT)
        report = await agent_run_golden(
            cases,
            _fixed_fake(_SKILL_JSON),
            "skill_curator",
            "1.0.0",
            prompts_root=PROMPTS_ROOT,
        )
        assert report.total == len(cases)
        assert report.passed + report.failed == len(cases)

    async def test_correctness_verifier_harness_runs_all_cases(self) -> None:
        cases = verifier_load_golden("correctness_verifier", prompts_root=PROMPTS_ROOT)
        report = await verifier_run_golden(
            cases,
            _fixed_fake(_VERIFIER_JSON),
            "correctness_verifier",
            "1.0.0",
            prompts_root=PROMPTS_ROOT,
        )
        assert report.total == len(cases)

    async def test_security_verifier_harness_runs_all_cases(self) -> None:
        cases = verifier_load_golden("security_verifier", prompts_root=PROMPTS_ROOT)
        report = await verifier_run_golden(
            cases,
            _fixed_fake(_VERIFIER_JSON),
            "security_verifier",
            "1.0.0",
            prompts_root=PROMPTS_ROOT,
        )
        assert report.total == len(cases)

    async def test_contract_verifier_harness_runs_all_cases(self) -> None:
        cases = verifier_load_golden("contract_verifier", prompts_root=PROMPTS_ROOT)
        report = await verifier_run_golden(
            cases,
            _fixed_fake(_VERIFIER_JSON),
            "contract_verifier",
            "1.0.0",
            prompts_root=PROMPTS_ROOT,
        )
        assert report.total == len(cases)


class TestEdgeCases:
    async def test_analyst_refusal_shape_passes(self) -> None:
        cases = agent_load_golden("analyst", prompts_root=PROMPTS_ROOT)
        refusal_cases = [c for c in cases if c.expected.get("expects_refusal")]
        assert refusal_cases, "no refusal cases found in analyst golden"

        async def refusal_fake(prompt: str) -> str:
            return '{"files": [], "reason": "refused: test refusal reason"}'

        report = await agent_run_golden(
            [refusal_cases[0]],
            refusal_fake,
            "analyst",
            "1.0.0",
            prompts_root=PROMPTS_ROOT,
        )
        assert report.total == 1
        assert report.passed == 1

    async def test_skill_curator_none_sentinel_passes(self) -> None:
        cases = agent_load_golden("skill_curator", prompts_root=PROMPTS_ROOT)
        none_cases = [c for c in cases if c.expected.get("expected_skill_name_or_none") == "(none)"]
        assert none_cases, "no (none) sentinel cases found"

        async def sentinel_fake(prompt: str) -> str:
            return json.dumps(
                {
                    "skill_name": "(none)",
                    "skill_markdown": "no reusable pattern",
                    "confidence": 0.0,
                }
            )

        report = await agent_run_golden(
            [none_cases[0]],
            sentinel_fake,
            "skill_curator",
            "1.0.0",
            prompts_root=PROMPTS_ROOT,
        )
        assert report.total == 1
        assert report.passed == 1


class TestRoleRejection:
    def test_agent_eval_rejects_verifier_roles(self) -> None:
        with pytest.raises(CodeGuardianError):
            agent_load_golden("correctness_verifier")

    def test_verifier_eval_rejects_non_verifier_roles(self) -> None:
        with pytest.raises(CodeGuardianError):
            verifier_load_golden("analyst")
