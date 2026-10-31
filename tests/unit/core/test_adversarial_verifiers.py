"""Tests for the adversarial verifier cases.

Each trap in the adversarial YAMLs exercises a specific verifier
failure mode. The primary test provides a fake model that returns the
correct verdict (fail with a specific finding). The verifier agent
must propagate the verdict faithfully.

A companion test per verifier class uses a "fooled" fake that returns
pass despite the trap. This documents that the pipeline does NOT
magically fix a fooled model. The security and contract verifiers can
still surface the mismatch via their deterministic pre-scan; the
correctness verifier cannot, because it has no signal to work with
when test_result is PASS. This asymmetry is honest: the correctness
verifier depends on the model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from codeguardian.core.agents.contract_verifier import (
    ContractVerifier,
    ContractVerifierInput,
)
from codeguardian.core.agents.correctness_verifier import (
    CorrectnessVerifier,
    CorrectnessVerifierInput,
)
from codeguardian.core.agents.security_verifier import (
    SecurityVerifier,
    SecurityVerifierInput,
)
from codeguardian.core.plugins import BudgetTracker
from codeguardian.interfaces.model import ModelOptions, ModelResponse
from codeguardian.interfaces.verifier import ContractAssertion

PROMPTS_ROOT = Path(__file__).resolve().parents[3] / "prompts"

_PASS_JSON = '{"verdict": "pass", "confidence": 0.9, "findings": []}'


def _load_adversarial(role: str) -> list[dict[str, Any]]:
    path = PROMPTS_ROOT / role / "eval" / "adversarial.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["cases"]


class FakeProvider:
    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[tuple[str, str, ModelOptions]] = []

    async def complete(self, prompt: str, model: str, options: ModelOptions) -> ModelResponse:
        self.calls.append((prompt, model, options))
        return ModelResponse(
            text=self._response,
            tokens_in=10,
            tokens_out=10,
            model_id="fake",
            finish_reason="stop",
        )

    def stream(self, *a: object, **kw: object) -> None:
        raise NotImplementedError

    def count_tokens(self, text: str, model: str) -> int:
        return 0

    def get_available_models(self) -> list[object]:
        return []


def _fail_fake(phrase: str) -> FakeProvider:
    body = json.dumps(
        {
            "verdict": "fail",
            "confidence": 0.9,
            "findings": [
                {
                    "severity": "critical",
                    "message": f"Detected: {phrase}",
                }
            ],
        }
    )
    return FakeProvider(body)


def _correctness_input(case: dict[str, Any], provider: FakeProvider) -> CorrectnessVerifierInput:
    return CorrectnessVerifierInput(
        diff=case["diff"],
        change_kind=case.get("change_kind", "semantic"),
        blast_score=int(case.get("blast_score", 30)),
        test_result="PASS",
        provider=provider,
        model="correctness_verifier",
        budget=BudgetTracker(),
        clone_root=Path("/tmp"),
    )


def _security_input(case: dict[str, Any], provider: FakeProvider) -> SecurityVerifierInput:
    return SecurityVerifierInput(
        diff=case["diff"],
        change_kind=case.get("change_kind", "semantic"),
        blast_score=int(case.get("blast_score", 30)),
        original_security_signals=(),
        provider=provider,
        model="security_verifier",
        budget=BudgetTracker(),
        clone_root=Path("/tmp"),
    )


def _contract_input(case: dict[str, Any], provider: FakeProvider) -> ContractVerifierInput:
    assertion = ContractAssertion(
        caller="test_caller",
        assumption="test assumption",
        severity="warning",
        test_expression="assert result is not None",
    )
    return ContractVerifierInput(
        diff=case["diff"],
        change_kind=case.get("change_kind", "contract"),
        blast_score=int(case.get("blast_score", 30)),
        contract_assertions=(assertion,),
        provider=provider,
        model="contract_verifier",
        budget=BudgetTracker(),
        clone_root=Path("/tmp"),
    )


class TestCorrectnessAdversarial:
    @pytest.mark.parametrize(
        "case",
        _load_adversarial("correctness_verifier"),
        ids=lambda c: c["id"],
    )
    async def test_trap_produces_fail_verdict(self, case: dict[str, Any]) -> None:
        phrase = case["expected_finding_contains"][0]
        provider = _fail_fake(phrase)
        result = await CorrectnessVerifier().run(_correctness_input(case, provider))
        assert result.verdict.verdict == "fail"
        assert result.verdict_reason == "behavior_drift"
        messages = [f.message for f in result.verdict.findings]
        assert any(phrase in msg for msg in messages), (
            case["id"],
            messages,
        )

    async def test_pipeline_does_not_fix_fooled_model(self) -> None:
        cases = _load_adversarial("correctness_verifier")
        case = cases[0]
        provider = FakeProvider(_PASS_JSON)
        result = await CorrectnessVerifier().run(_correctness_input(case, provider))
        assert result.verdict.verdict == "pass"
        # No pre-scan signal (test_result=PASS) so no conflict
        assert result.pre_scan_conflict is False


class TestSecurityAdversarial:
    @pytest.mark.parametrize(
        "case",
        _load_adversarial("security_verifier"),
        ids=lambda c: c["id"],
    )
    async def test_trap_produces_fail_verdict(self, case: dict[str, Any]) -> None:
        phrase = case["expected_finding_contains"][0]
        provider = _fail_fake(phrase)
        result = await SecurityVerifier().run(_security_input(case, provider))
        assert result.verdict.verdict == "fail"
        assert result.verdict_reason == "vulnerability_introduced"
        messages = [f.message for f in result.verdict.findings]
        assert any(phrase in msg for msg in messages), (
            case["id"],
            messages,
        )

    async def test_pipeline_flags_fooled_model_via_prescan(self) -> None:
        cases = _load_adversarial("security_verifier")
        case = cases[0]
        provider = FakeProvider(_PASS_JSON)
        result = await SecurityVerifier().run(_security_input(case, provider))
        assert result.verdict.verdict == "pass"
        # Security pre-scan regex matched; conflict surfaces even when
        # the model is fooled
        assert result.pre_scan_conflict is True


class TestContractAdversarial:
    @pytest.mark.parametrize(
        "case",
        _load_adversarial("contract_verifier"),
        ids=lambda c: c["id"],
    )
    async def test_trap_produces_fail_verdict(self, case: dict[str, Any]) -> None:
        phrase = case["expected_finding_contains"][0]
        provider = _fail_fake(phrase)
        result = await ContractVerifier().run(_contract_input(case, provider))
        assert result.verdict.verdict == "fail"
        assert result.verdict_reason == "contract_broken"
        messages = [f.message for f in result.verdict.findings]
        assert any(phrase in msg for msg in messages), (
            case["id"],
            messages,
        )

    async def test_pipeline_flags_fooled_model_via_prescan(self) -> None:
        cases = _load_adversarial("contract_verifier")
        case = cases[0]
        provider = FakeProvider(_PASS_JSON)
        result = await ContractVerifier().run(_contract_input(case, provider))
        assert result.verdict.verdict == "pass"
        # Assertion caller/test_expression not in diff → conflict
        assert result.pre_scan_conflict is True
