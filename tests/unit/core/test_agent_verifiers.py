"""Tests for the three verifier agents."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from codeguardian.core.agents import _store_context
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
from codeguardian.core.storage import MigrationRunner, SQLiteStore
from codeguardian.interfaces.model import ModelOptions, ModelResponse
from codeguardian.interfaces.verifier import ContractAssertion

_DIFF = (
    "--- a/a.py\n"
    "+++ b/a.py\n"
    "@@ -1,3 +1,3 @@\n"
    "-def parse(raw):\n"
    "-    return raw.strip()\n"
    "+def parse(raw: str) -> str:\n"
    "+    return raw.strip()\n"
)


_PASS_JSON = '{"verdict": "pass", "confidence": 0.9, "findings": []}'
_FAIL_JSON = json.dumps(
    {
        "verdict": "fail",
        "confidence": 0.9,
        "findings": [{"severity": "critical", "message": "behavior changed"}],
    }
)
_REFUSED_WITH_CODE = json.dumps(
    {
        "verdict": "refused",
        "reason": "cannot determine without more evidence",
        "reason_code": "target_missing",
    }
)


class FakeProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, ModelOptions]] = []

    async def complete(self, prompt: str, model: str, options: ModelOptions) -> ModelResponse:
        self.calls.append((prompt, model, options))
        if not self._responses:
            raise RuntimeError("fake provider out of responses")
        text = self._responses.pop(0)
        return ModelResponse(
            text=text,
            tokens_in=10,
            tokens_out=10,
            model_id="fake",
            finish_reason="stop",
        )

    def stream(self, *a, **kw):
        raise NotImplementedError

    def count_tokens(self, t, m):
        return 0

    def get_available_models(self):
        return []


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    instance = SQLiteStore(tmp_path / "t.db")
    instance.open()
    MigrationRunner(instance).run()
    try:
        yield instance
    finally:
        instance.close()


def _corr_input(
    provider: FakeProvider,
    *,
    test_result: str = "PASS",
    model: str = "correctness_verifier",
) -> CorrectnessVerifierInput:
    return CorrectnessVerifierInput(
        diff=_DIFF,
        change_kind="semantic",
        blast_score=30,
        test_result=test_result,
        provider=provider,
        model=model,
        budget=BudgetTracker(),
        clone_root=Path("/tmp"),
    )


def _sec_input(
    provider: FakeProvider,
    *,
    diff: str = _DIFF,
    signals: tuple[str, ...] = (),
    model: str = "security_verifier",
) -> SecurityVerifierInput:
    return SecurityVerifierInput(
        diff=diff,
        change_kind="semantic",
        blast_score=30,
        original_security_signals=signals,
        provider=provider,
        model=model,
        budget=BudgetTracker(),
        clone_root=Path("/tmp"),
    )


def _con_input(
    provider: FakeProvider,
    *,
    assertions: tuple[ContractAssertion, ...] = (),
    model: str = "contract_verifier",
) -> ContractVerifierInput:
    return ContractVerifierInput(
        diff=_DIFF,
        change_kind="semantic",
        blast_score=30,
        contract_assertions=assertions,
        provider=provider,
        model=model,
        budget=BudgetTracker(),
        clone_root=Path("/tmp"),
    )


class TestEvidenceSlices:
    async def test_correctness_sees_test_result_not_security_matches(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        await CorrectnessVerifier().run(_corr_input(provider, test_result="FAIL: x"))
        prompt = provider.calls[0][0]
        assert "test_result: FAIL" in prompt
        assert "# match:" not in prompt

    async def test_security_sees_matches_not_test_result(self) -> None:
        diff_with_eval = _DIFF + "+    return eval(raw)\n"
        provider = FakeProvider([_PASS_JSON])
        await SecurityVerifier().run(_sec_input(provider, diff=diff_with_eval))
        prompt = provider.calls[0][0]
        assert "# match: eval_call" in prompt
        assert "test_result:" not in prompt

    async def test_contract_sees_assertions_not_test_result(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        assertion = ContractAssertion(
            caller="caller_fn",
            assumption="returns non-None",
            severity="warning",
            test_expression="assert result is not None",
        )
        await ContractVerifier().run(_con_input(provider, assertions=(assertion,)))
        prompt = provider.calls[0][0]
        assert "# assertion: caller=caller_fn" in prompt
        assert "test_result:" not in prompt
        assert "# match:" not in prompt


class TestParallelExecution:
    async def test_all_three_run_and_return(self) -> None:
        corr_provider = FakeProvider([_PASS_JSON])
        sec_provider = FakeProvider([_PASS_JSON])
        con_provider = FakeProvider([_PASS_JSON])

        results = await asyncio.gather(
            CorrectnessVerifier().run(_corr_input(corr_provider)),
            SecurityVerifier().run(_sec_input(sec_provider)),
            ContractVerifier().run(_con_input(con_provider)),
        )
        assert len(results) == 3
        for r in results:
            assert r.verdict.verdict == "pass"
            assert r.is_refusal is False


class TestTelemetry:
    async def test_fail_verdict_has_null_failure_category(self, store: SQLiteStore) -> None:
        provider = FakeProvider([_FAIL_JSON])
        with _store_context.active_store(store):
            await CorrectnessVerifier().run(_corr_input(provider))
        rows = store.query("SELECT * FROM agent_calls WHERE role='correctness_verifier'")
        assert len(rows) == 1
        row = rows[0]
        assert row["outcome"] == "success"
        assert row["failure_category"] is None
        assert row["verdict_reason"] == "behavior_drift"

    async def test_security_verdict_reason_on_fail(self, store: SQLiteStore) -> None:
        provider = FakeProvider([_FAIL_JSON])
        with _store_context.active_store(store):
            await SecurityVerifier().run(_sec_input(provider))
        rows = store.query("SELECT * FROM agent_calls WHERE role='security_verifier'")
        assert rows[0]["verdict_reason"] == "vulnerability_introduced"
        assert rows[0]["failure_category"] is None

    async def test_contract_verdict_reason_on_fail(self, store: SQLiteStore) -> None:
        provider = FakeProvider([_FAIL_JSON])
        with _store_context.active_store(store):
            await ContractVerifier().run(_con_input(provider))
        rows = store.query("SELECT * FROM agent_calls WHERE role='contract_verifier'")
        assert rows[0]["verdict_reason"] == "contract_broken"
        assert rows[0]["failure_category"] is None

    async def test_pass_verdict_has_null_verdict_reason(self, store: SQLiteStore) -> None:
        provider = FakeProvider([_PASS_JSON])
        with _store_context.active_store(store):
            await CorrectnessVerifier().run(_corr_input(provider))
        row = store.query("SELECT * FROM agent_calls WHERE role='correctness_verifier'")[0]
        assert row["verdict_reason"] is None
        assert row["failure_category"] is None


class TestReasonCodeHandling:
    async def test_reason_code_validated_and_exposed(self) -> None:
        provider = FakeProvider([_REFUSED_WITH_CODE])
        result = await CorrectnessVerifier().run(_corr_input(provider))
        assert result.is_refusal is True
        assert result.refusal_code == "target_missing"

    async def test_invalid_reason_code_rejected(self) -> None:
        bad = json.dumps(
            {
                "verdict": "refused",
                "reason": "x",
                "reason_code": "not_a_real_code",
            }
        )
        provider = FakeProvider([bad])
        result = await CorrectnessVerifier().run(_corr_input(provider))
        assert result.is_refusal is True
        assert result.refusal_code is None


class TestPreScanConflict:
    async def test_correctness_pass_with_failed_test_is_conflict(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await CorrectnessVerifier().run(
            _corr_input(provider, test_result="FAIL: assert x == 1")
        )
        assert result.pre_scan_conflict is True

    async def test_correctness_pass_with_passed_test_is_clean(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await CorrectnessVerifier().run(_corr_input(provider, test_result="PASS"))
        assert result.pre_scan_conflict is False

    async def test_security_pass_with_match_is_conflict(self) -> None:
        diff_with_eval = _DIFF + "+    return eval(raw)\n"
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider, diff=diff_with_eval))
        assert result.pre_scan_conflict is True

    async def test_contract_pass_with_assertion_not_in_diff_is_conflict(
        self,
    ) -> None:
        assertion = ContractAssertion(
            caller="other_module_fn",
            assumption="returns non-None",
            severity="warning",
            test_expression="assert other_fn_result is not None",
        )
        provider = FakeProvider([_PASS_JSON])
        result = await ContractVerifier().run(_con_input(provider, assertions=(assertion,)))
        assert result.pre_scan_conflict is True


class TestRefusalShape:
    async def test_refused_without_reason_returns_schema_refusal(
        self,
    ) -> None:
        provider = FakeProvider(
            ['{"verdict": "refused"}']  # no reason
        )
        result = await CorrectnessVerifier().run(_corr_input(provider))
        assert result.is_refusal is True
        assert result.verdict.verdict == "refused"
        assert result.pre_scan_conflict is False


class TestFamilyExclusionRoleName:
    async def test_correctness_passes_role_name_to_provider(
        self,
    ) -> None:
        provider = FakeProvider([_PASS_JSON])
        await CorrectnessVerifier().run(_corr_input(provider))
        assert provider.calls[0][1] == "correctness_verifier"

    async def test_security_passes_role_name_to_provider(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        await SecurityVerifier().run(_sec_input(provider))
        assert provider.calls[0][1] == "security_verifier"

    async def test_contract_passes_role_name_to_provider(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        await ContractVerifier().run(_con_input(provider))
        assert provider.calls[0][1] == "contract_verifier"
