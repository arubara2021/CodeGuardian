"""Tests for the three verifier agents."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from codeguardian.core.agents import _store_context
from codeguardian.core.agents._verifier_common import ExecutionOutcome
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


class FakeSandboxExecutor:
    """SandboxExecutor that returns a canned outcome."""

    def __init__(
        self,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self.calls: list[list[str]] = []

    def run(
        self,
        argv: list[str],
        *,
        timeout_s: float = 120.0,
    ) -> ExecutionOutcome:
        self.calls.append(list(argv))
        return ExecutionOutcome(
            exit_code=self._exit_code,
            stdout=self._stdout,
            stderr=self._stderr,
        )


class TestExecutableCorrectnessVerifier:
    async def test_no_executor_keeps_model_verdict(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await CorrectnessVerifier().run(_corr_input(provider, test_result="PASS"))
        assert result.verdict.verdict == "pass"
        assert result.evidence_receipt is None
        assert result.counterexample is None
        assert result.evidence_conflict is False

    async def test_executor_pass_confirms_pass(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        executor = FakeSandboxExecutor(exit_code=0, stdout="1 passed")
        inp = _corr_input(provider, test_result="PASS")
        inp = replace(
            inp,
            sandbox_executor=executor,
            characterization_test_path="test_foo.py",
        )
        result = await CorrectnessVerifier().run(inp)
        assert result.verdict.verdict == "pass"
        assert result.evidence_receipt is not None
        assert result.evidence_receipt["commands_run"]
        assert "test_foo.py" in result.evidence_receipt["commands_run"][0]
        assert result.counterexample is None
        assert result.evidence_conflict is False
        assert len(executor.calls) == 1

    async def test_executor_fail_overrides_model_pass(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        executor = FakeSandboxExecutor(
            exit_code=1,
            stderr="AssertionError: assert parse('x') == 'y'",
        )
        inp = _corr_input(provider, test_result="PASS")
        inp = replace(
            inp,
            sandbox_executor=executor,
            characterization_test_path="test_foo.py",
        )
        result = await CorrectnessVerifier().run(inp)
        assert result.verdict.verdict == "fail"
        assert result.verdict_reason == "behavior_drift"
        assert result.evidence_conflict is True
        assert result.counterexample is not None
        assert "AssertionError" in result.counterexample or "assert" in result.counterexample

    async def test_executor_fail_confirms_model_fail(self) -> None:
        provider = FakeProvider([_FAIL_JSON])
        executor = FakeSandboxExecutor(
            exit_code=1,
            stderr="AssertionError: assert 1 == 2",
        )
        inp = _corr_input(provider, test_result="FAIL")
        inp = replace(
            inp,
            sandbox_executor=executor,
            characterization_test_path="test_foo.py",
        )
        result = await CorrectnessVerifier().run(inp)
        assert result.verdict.verdict == "fail"
        assert result.evidence_conflict is False
        assert result.counterexample is not None

    async def test_executor_pass_overrides_model_fail(self) -> None:
        provider = FakeProvider([_FAIL_JSON])
        executor = FakeSandboxExecutor(exit_code=0, stdout="1 passed")
        inp = _corr_input(provider)
        inp = replace(
            inp,
            sandbox_executor=executor,
            characterization_test_path="test_foo.py",
        )
        result = await CorrectnessVerifier().run(inp)
        assert result.verdict.verdict == "pass"
        assert result.evidence_conflict is True

    async def test_executor_other_exit_code_is_inconclusive(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        executor = FakeSandboxExecutor(exit_code=2, stderr="collection error")
        inp = _corr_input(provider, test_result="PASS")
        inp = replace(
            inp,
            sandbox_executor=executor,
            characterization_test_path="test_foo.py",
        )
        result = await CorrectnessVerifier().run(inp)
        assert result.verdict.verdict == "pass"
        assert result.evidence_receipt is not None
        assert result.counterexample is None
        assert result.evidence_conflict is False


class TestExecutableContractVerifier:
    async def test_no_test_path_keeps_model_verdict(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await ContractVerifier().run(_con_input(provider))
        assert result.verdict.verdict == "pass"
        assert result.evidence_receipt is None
        assert result.counterexample is None
        assert result.evidence_conflict is False

    async def test_test_path_pass_confirms_pass(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        executor = FakeSandboxExecutor(exit_code=0, stdout="1 passed")
        inp = _con_input(provider)
        inp = replace(
            inp,
            sandbox_executor=executor,
            contract_test_path="test_contract.py",
        )
        result = await ContractVerifier().run(inp)
        assert result.verdict.verdict == "pass"
        assert result.evidence_receipt is not None
        assert result.evidence_receipt["commands_run"]
        assert "test_contract.py" in result.evidence_receipt["commands_run"][0]

    async def test_test_path_fail_overrides_model_pass(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        executor = FakeSandboxExecutor(
            exit_code=1,
            stderr="AssertionError: assert result is not None",
        )
        inp = _con_input(provider)
        inp = replace(
            inp,
            sandbox_executor=executor,
            contract_test_path="test_contract.py",
        )
        result = await ContractVerifier().run(inp)
        assert result.verdict.verdict == "fail"
        assert result.verdict_reason == "contract_broken"
        assert result.evidence_conflict is True
        assert result.counterexample is not None


_SEMGREP_SQL_JSON = json.dumps(
    {
        "results": [
            {
                "check_id": "python.lang.security.audit.sql-string-format",
                "path": "target.py",
                "start": {"line": 12, "col": 5},
                "extra": {
                    "severity": "ERROR",
                    "message": "SQL query built with string formatting",
                    "lines": '    cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")',
                    "metadata": {
                        "cwe": ["CWE-89: Improper Neutralization of Special Elements in SQL"]
                    },
                },
            }
        ],
        "errors": [],
    }
)

_BANDIT_EVAL_JSON = json.dumps(
    {
        "results": [
            {
                "test_id": "B307",
                "issue_severity": "HIGH",
                "issue_cwe": {"id": 95},
                "issue_text": "Use of eval detected",
                "line_number": 18,
                "code": "    return eval(expression)\n",
            }
        ],
        "errors": [],
    }
)


class FakeScannerSandbox:
    """SandboxExecutor that returns canned output keyed by the argv prefix."""

    def __init__(
        self,
        *,
        has_semgrep: bool = False,
        has_bandit: bool = False,
        semgrep_stdout: str = "",
        bandit_stdout: str = "",
    ) -> None:
        self._has_semgrep = has_semgrep
        self._has_bandit = has_bandit
        self._semgrep_stdout = semgrep_stdout
        self._bandit_stdout = bandit_stdout
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, timeout_s: float = 120.0) -> ExecutionOutcome:
        self.calls.append(list(argv))
        if argv[:1] == ["which"]:
            binary = argv[1]
            present = (binary == "semgrep" and self._has_semgrep) or (
                binary == "bandit" and self._has_bandit
            )
            return ExecutionOutcome(exit_code=0 if present else 1, stdout="", stderr="")
        if argv[:1] == ["semgrep"]:
            return ExecutionOutcome(exit_code=0, stdout=self._semgrep_stdout, stderr="")
        if argv[:1] == ["bandit"]:
            return ExecutionOutcome(exit_code=0, stdout=self._bandit_stdout, stderr="")
        return ExecutionOutcome(exit_code=0, stdout="", stderr="")


class TestExternalScanners:
    async def test_semgrep_finding_becomes_external_finding(self) -> None:
        executor = FakeScannerSandbox(has_semgrep=True, semgrep_stdout=_SEMGREP_SQL_JSON)
        provider = FakeProvider([_FAIL_JSON])
        inp = replace(
            _sec_input(provider),
            sandbox_executor=executor,
            target_file_path="target.py",
        )
        result = await SecurityVerifier().run(inp)
        assert len(result.external_findings) == 1
        finding = result.external_findings[0]
        assert finding.cwe_id == "CWE-89"
        assert finding.severity == "high"
        assert finding.source_tool == "semgrep"
        assert finding.line == 12

    async def test_bandit_finding_becomes_external_finding(self) -> None:
        executor = FakeScannerSandbox(has_bandit=True, bandit_stdout=_BANDIT_EVAL_JSON)
        provider = FakeProvider([_FAIL_JSON])
        inp = replace(
            _sec_input(provider),
            sandbox_executor=executor,
            target_file_path="target.py",
        )
        result = await SecurityVerifier().run(inp)
        assert len(result.external_findings) == 1
        finding = result.external_findings[0]
        assert finding.cwe_id == "CWE-95"
        assert finding.severity == "high"
        assert finding.source_tool == "bandit"

    async def test_semgrep_absent_is_silent(self) -> None:
        executor = FakeScannerSandbox(has_semgrep=False)
        provider = FakeProvider([_PASS_JSON])
        inp = replace(
            _sec_input(provider),
            sandbox_executor=executor,
            target_file_path="target.py",
        )
        result = await SecurityVerifier().run(inp)
        assert result.external_findings == ()


class TestTaintPreScan:
    async def test_sql_fstring_detected(self) -> None:
        diff = (
            "--- a/db.py\n"
            "+++ b/db.py\n"
            "@@ -1,3 +1,3 @@\n"
            " def get_user(cursor, user_id):\n"
            "-    cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))\n"
            "+    cursor.execute(f'SELECT * FROM users WHERE id = {user_id}')\n"
            "     return cursor.fetchone()\n"
        )
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider, diff=diff))
        assert any(f.cwe_id == "CWE-89" for f in result.taint_findings)
        assert any(f.severity == "high" for f in result.taint_findings)

    async def test_command_injection_detected(self) -> None:
        diff = (
            "--- a/io.py\n+++ b/io.py\n@@ -1,2 +1,3 @@\n"
            " def run(name):\n"
            "+    os.system('ls ' + name)\n"
            "     return None\n"
        )
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider, diff=diff))
        assert any(f.cwe_id == "CWE-78" for f in result.taint_findings)

    async def test_pickle_loads_detected(self) -> None:
        diff = (
            "--- a/sess.py\n+++ b/sess.py\n@@ -1,3 +1,5 @@\n"
            "+import pickle\n"
            " def load_session(token):\n"
            "-    return json.loads(token)\n"
            "+    return pickle.loads(token)\n"
        )
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider, diff=diff))
        assert any(f.cwe_id == "CWE-502" for f in result.taint_findings)
        assert any(f.severity == "critical" for f in result.taint_findings)

    async def test_path_traversal_detected(self) -> None:
        diff = (
            "--- a/io.py\n+++ b/io.py\n@@ -1,2 +1,3 @@\n"
            " def read_attachment(root, name):\n"
            "+    return open(root + '/' + name).read()\n"
        )
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider, diff=diff))
        assert any(f.cwe_id == "CWE-22" for f in result.taint_findings)

    async def test_clean_diff_has_no_taint_findings(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider))
        assert result.taint_findings == ()


class TestEvidenceOverride:
    async def test_critical_taint_overrides_model_pass(self) -> None:
        diff = (
            "--- a/sess.py\n+++ b/sess.py\n@@ -1,3 +1,5 @@\n"
            "+import pickle\n"
            " def load_session(token):\n"
            "-    return json.loads(token)\n"
            "+    return pickle.loads(token)\n"
        )
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider, diff=diff))
        assert result.verdict.verdict == "fail"
        assert result.verdict_reason == "vulnerability_introduced"
        assert result.evidence_conflict is True

    async def test_high_semgrep_overrides_model_pass(self) -> None:
        executor = FakeScannerSandbox(has_semgrep=True, semgrep_stdout=_SEMGREP_SQL_JSON)
        provider = FakeProvider([_PASS_JSON])
        inp = replace(
            _sec_input(provider),
            sandbox_executor=executor,
            target_file_path="target.py",
        )
        result = await SecurityVerifier().run(inp)
        assert result.verdict.verdict == "fail"
        assert result.evidence_conflict is True

    async def test_no_findings_keeps_model_verdict(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider))
        assert result.verdict.verdict == "pass"
        assert result.evidence_conflict is False


class TestPoCGeneration:
    async def test_poc_generated_for_sqli(self) -> None:
        diff = (
            "--- a/db.py\n+++ b/db.py\n@@ -1,3 +1,3 @@\n"
            " def get_user(cursor, user_id):\n"
            "-    cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))\n"
            "+    cursor.execute(f'SELECT * FROM users WHERE id = {user_id}')\n"
            "     return cursor.fetchone()\n"
        )
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider, diff=diff))
        assert result.poc is not None
        assert result.poc.cwe_id == "CWE-89"
        assert "def test_sql_injection_payload" in result.poc.test_source
        assert "OR '1'='1" in result.poc.test_source

    async def test_poc_generated_for_command_injection(self) -> None:
        diff = (
            "--- a/io.py\n+++ b/io.py\n@@ -1,2 +1,3 @@\n"
            " def run(name):\n"
            "+    os.system('ls ' + name)\n"
            "     return None\n"
        )
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider, diff=diff))
        assert result.poc is not None
        assert result.poc.cwe_id == "CWE-78"
        assert "CODE_GUARDIAN_POC" in result.poc.test_source

    async def test_no_poc_for_clean_diff(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider))
        assert result.poc is None


class TestEvidenceReceipt:
    async def test_receipt_present_when_sandbox_used(self) -> None:
        executor = FakeScannerSandbox(has_semgrep=True, has_bandit=True)
        provider = FakeProvider([_PASS_JSON])
        inp = replace(
            _sec_input(provider),
            sandbox_executor=executor,
            target_file_path="target.py",
        )
        result = await SecurityVerifier().run(inp)
        assert result.evidence_receipt is not None
        assert "target.py" in result.evidence_receipt["artifacts_checked"]
        assert any("semgrep" in cmd for cmd in result.evidence_receipt["commands_run"])

    async def test_receipt_absent_without_sandbox(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider))
        assert result.evidence_receipt is None


class TestIndependenceProtocol:
    async def test_correctness_blind_prediction_pass_with_passing_test(
        self,
    ) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await CorrectnessVerifier().run(_corr_input(provider, test_result="PASS"))
        assert result.blind_prediction == "pass"
        assert result.informed_verdict == "pass"
        assert result.disagreement is False

    async def test_correctness_blind_prediction_fail_with_failing_test(
        self,
    ) -> None:
        provider = FakeProvider([_FAIL_JSON])
        result = await CorrectnessVerifier().run(
            _corr_input(provider, test_result="FAIL: assert x == 1")
        )
        assert result.blind_prediction == "fail"
        assert result.informed_verdict == "fail"
        assert result.disagreement is False

    async def test_correctness_disagreement_detected(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await CorrectnessVerifier().run(
            _corr_input(provider, test_result="FAIL: assert x == 1")
        )
        assert result.blind_prediction == "fail"
        assert result.informed_verdict == "pass"
        assert result.disagreement is True

    async def test_security_blind_prediction_pass_with_no_signals(
        self,
    ) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider))
        assert result.blind_prediction == "pass"
        assert result.informed_verdict == "pass"
        assert result.disagreement is False

    async def test_security_blind_prediction_fail_with_signals(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(
            _sec_input(provider, signals=("eval found in original",))
        )
        assert result.blind_prediction == "fail"
        assert result.informed_verdict == "pass"
        assert result.disagreement is True

    async def test_contract_blind_prediction_uncertain_with_warning(
        self,
    ) -> None:
        provider = FakeProvider([_PASS_JSON])
        assertion = ContractAssertion(
            caller="c",
            assumption="returns non-None",
            severity="warning",
            test_expression="assert result is not None",
        )
        result = await ContractVerifier().run(_con_input(provider, assertions=(assertion,)))
        assert result.blind_prediction == "uncertain"
        assert result.informed_verdict == "pass"
        assert result.disagreement is True

    async def test_contract_blind_prediction_fail_with_critical(self) -> None:
        provider = FakeProvider([_FAIL_JSON])
        assertion = ContractAssertion(
            caller="c",
            assumption="returns dict, never None",
            severity="critical",
            test_expression="assert result is not None",
        )
        result = await ContractVerifier().run(_con_input(provider, assertions=(assertion,)))
        assert result.blind_prediction == "fail"
        assert result.disagreement is False


class TestVeto:
    async def test_security_veto_flips_pass_to_uncertain_when_prescan_flags(
        self,
    ) -> None:
        diff_with_eval = _DIFF + "+    return eval(raw)\n"
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider, diff=diff_with_eval))
        assert result.informed_verdict == "pass"
        assert result.pre_scan_conflict is True
        assert result.verdict.verdict == "uncertain"

    async def test_security_veto_does_not_fire_without_prescan_match(
        self,
    ) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await SecurityVerifier().run(_sec_input(provider))
        assert result.informed_verdict == "pass"
        assert result.pre_scan_conflict is False
        assert result.verdict.verdict == "pass"

    async def test_security_veto_does_not_override_fail_verdict(self) -> None:
        diff_with_eval = _DIFF + "+    return eval(raw)\n"
        provider = FakeProvider([_FAIL_JSON])
        result = await SecurityVerifier().run(_sec_input(provider, diff=diff_with_eval))
        assert result.informed_verdict == "fail"
        assert result.verdict.verdict == "fail"
        assert result.pre_scan_conflict is False

    async def test_correctness_veto_on_failed_test_result(self) -> None:
        provider = FakeProvider([_PASS_JSON])
        result = await CorrectnessVerifier().run(
            _corr_input(provider, test_result="FAIL: assert x == 1")
        )
        assert result.informed_verdict == "pass"
        assert result.pre_scan_conflict is True
        assert result.verdict.verdict == "uncertain"

    async def test_contract_veto_on_assertion_not_in_diff(self) -> None:
        assertion = ContractAssertion(
            caller="other_module_fn",
            assumption="returns non-None",
            severity="warning",
            test_expression="assert other_fn_result is not None",
        )
        provider = FakeProvider([_PASS_JSON])
        result = await ContractVerifier().run(_con_input(provider, assertions=(assertion,)))
        assert result.informed_verdict == "pass"
        assert result.pre_scan_conflict is True
        assert result.verdict.verdict == "uncertain"
