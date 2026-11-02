"""Tests for the tester agent."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("codeguardian._native")
from dataclasses import replace

from codeguardian import _native
from codeguardian.core.agents._verifier_common import ExecutionOutcome
from codeguardian.core.agents.analyst import RefactorChange, RefactorSpec
from codeguardian.core.agents.blast import BlastResult
from codeguardian.core.agents.tester import (
    TesterAgent,
    TesterInput,
    _validate_test_file,
)
from codeguardian.core.plugins import BudgetTracker, PluginRegistry
from codeguardian.core.plugins.context_runtime import cpg_available
from codeguardian.interfaces.model import ModelOptions, ModelResponse
from codeguardian.interfaces.verifier import ContractAssertion

_SOURCE = "def parse(raw):\n    return raw.strip()\n"

_VALID_TEST = (
    "import pytest\n"
    "from a import parse\n"
    "\n"
    "def test_parse_returns_str():\n"
    "    assert parse('x') == 'x'\n"
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


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    return PluginRegistry.discover_bundled()


@pytest.fixture
def workspace(tmp_path: Path):
    (tmp_path / "a.py").write_text(_SOURCE, encoding="utf-8")
    cpg_bytes = _native.build_graph([("a.py", "python", _SOURCE)])
    return tmp_path, _native.PyCPG(cpg_bytes)


def _make_spec() -> RefactorSpec:
    return RefactorSpec(
        directive="add type hints",
        target="a.py:parse",
        changes=(
            RefactorChange(kind="apply_directive", reason="add type hints"),
            RefactorChange(kind="parameter_annotation", name="raw", inferred_type=None),
            RefactorChange(kind="return_annotation", inferred_type="str"),
        ),
        constraints=(),
    )


def _make_blast(contract_assertions: tuple[ContractAssertion, ...] = ()) -> BlastResult:
    return BlastResult(
        target_path="a.py",
        target_qualified_name="parse",
        target_language="python",
        change_kind="semantic",
        direct_callers=(),
        transitive_callers=(),
        contract_violations=(),
        coverage_gaps=(),
        blast_score=0,
        recommendation_raw="proceed",
        recommendation_adjusted="proceed",
        generated_at="2026-10-31T00:00:00+00:00",
        run_id="r",
        ranked_callers=(),
        contract_assertions=contract_assertions,
        unresolved_call_sites=0,
        caller_search_complete=True,
        calibration_reason=None,
    )


def _make_input(
    workspace,
    registry: PluginRegistry,
    provider: FakeProvider,
    *,
    contract_assertions: tuple[ContractAssertion, ...] = (),
    previous_test_attempt: str = "",
    parse_error: str = "",
    old_code_failure: str = "",
) -> TesterInput:
    root, _ = workspace
    return TesterInput(
        spec=_make_spec(),
        blast=_make_blast(contract_assertions),
        registry=registry,
        provider=provider,
        model="fake",
        budget=BudgetTracker(),
        clone_root=root,
        target_path="a.py",
        target_symbol="parse",
        previous_test_attempt=previous_test_attempt,
        parse_error=parse_error,
        old_code_failure=old_code_failure,
    )


async def test_valid_output_returns_success(workspace, registry) -> None:
    provider = FakeProvider([_VALID_TEST])
    root, cpg = workspace
    with cpg_available(cpg, root):
        result = await TesterAgent().run(_make_input(workspace, registry, provider))
    assert result.is_valid is True
    assert result.is_refusal is False
    assert result.test_file == _VALID_TEST
    assert result.attempts == 1


async def test_parse_failure_self_retries(workspace, registry) -> None:
    invalid = "def test_parse(\n    # unbalanced\n"
    provider = FakeProvider([invalid, _VALID_TEST])
    root, cpg = workspace
    with cpg_available(cpg, root):
        result = await TesterAgent().run(_make_input(workspace, registry, provider))
    assert result.is_valid is True
    assert result.attempts == 2
    assert len(provider.calls) == 2
    # Second attempt's prompt names the parse failure
    second_prompt = provider.calls[1][0]
    assert "SyntaxError" in second_prompt or "previous_test_attempt" in second_prompt


async def test_empty_assertion_test_self_retries(workspace, registry) -> None:
    no_assert = "def test_parse():\n    x = 1\n"
    provider = FakeProvider([no_assert, _VALID_TEST])
    root, cpg = workspace
    with cpg_available(cpg, root):
        result = await TesterAgent().run(_make_input(workspace, registry, provider))
    assert result.is_valid is True
    assert result.attempts == 2


async def test_refusal_shape(workspace, registry) -> None:
    provider = FakeProvider(["REFUSED: cannot test without environment access"])
    root, cpg = workspace
    with cpg_available(cpg, root):
        result = await TesterAgent().run(_make_input(workspace, registry, provider))
    assert result.is_refusal is True
    assert result.is_valid is False
    assert result.test_file == ""
    assert "environment" in (result.refusal_reason or "")


async def test_missing_target_returns_refusal_before_model(workspace, registry) -> None:
    provider = FakeProvider([])  # no responses expected
    root, cpg = workspace
    inp = TesterInput(
        spec=_make_spec(),
        blast=_make_blast(),
        registry=registry,
        provider=provider,
        model="fake",
        budget=BudgetTracker(),
        clone_root=root,
        target_path="a.py",
        target_symbol="nonexistent",
    )
    with cpg_available(cpg, root):
        result = await TesterAgent().run(inp)
    assert result.is_refusal is True
    assert result.refusal_code == "target_missing"
    assert result.attempts == 0
    assert len(provider.calls) == 0


class TestValidateTestFile:
    def test_valid_passes(self) -> None:
        valid, reason = _validate_test_file(_VALID_TEST)
        assert valid is True
        assert reason is None

    def test_parse_error_detected(self) -> None:
        valid, reason = _validate_test_file("def test_x(:\n")
        assert valid is False
        assert reason is not None
        assert "SyntaxError" in reason

    def test_no_assert_detected(self) -> None:
        valid, reason = _validate_test_file("def test_x():\n    pass\n")
        assert valid is False
        assert reason is not None
        assert "no assertions" in reason

    def test_pytest_raises_counts(self) -> None:
        source = (
            "import pytest\n"
            "def test_x():\n"
            "    with pytest.raises(ValueError):\n"
            "        raise ValueError()\n"
        )
        valid, reason = _validate_test_file(source)
        assert valid is True
        assert reason is None

    def test_missing_contract_assertion_detected(self) -> None:
        assertion = ContractAssertion(
            caller="caller",
            assumption="result is never None",
            severity="warning",
            test_expression="assert result is not None",
        )
        # _VALID_TEST has no "is not None", so it should fail
        valid, reason = _validate_test_file(_VALID_TEST, (assertion,))
        assert valid is False
        assert reason is not None
        assert "is not None" in reason

    def test_contract_assertion_present_passes(self) -> None:
        assertion = ContractAssertion(
            caller="caller",
            assumption="result is never None",
            severity="warning",
            test_expression="assert result is not None",
        )
        source = "def test_parse_not_none():\n    result = 1\n    assert result is not None\n"
        valid, reason = _validate_test_file(source, (assertion,))
        assert valid is True
        assert reason is None


class FakeExecutableSandbox:
    """SandboxExecutor with write_file that returns queued outcomes."""

    def __init__(self) -> None:
        self._files: dict[str, str] = {}
        self._queue: list[tuple[int, str, str]] = []
        self.calls: list[list[str]] = []

    def write_file(self, path: str, content: str) -> None:
        self._files[path] = content

    def run(self, argv: list[str], *, timeout_s: float = 120.0) -> ExecutionOutcome:
        self.calls.append(list(argv))
        if self._queue:
            exit_code, stdout, stderr = self._queue.pop(0)
        else:
            exit_code, stdout, stderr = 0, "", ""
        return ExecutionOutcome(exit_code=exit_code, stdout=stdout, stderr=stderr)

    def queue_result(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        self._queue.append((exit_code, stdout, stderr))

    def files(self) -> dict[str, str]:
        return dict(self._files)


class TestExecutableTester:
    async def test_sandbox_pass_returns_valid(self, workspace, registry) -> None:
        provider = FakeProvider([_VALID_TEST])
        executor = FakeExecutableSandbox()
        executor.queue_result(exit_code=0, stdout="1 passed")
        inp = replace(
            _make_input(workspace, registry, provider),
            sandbox_executor=executor,
        )
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await TesterAgent().run(inp)
        assert result.is_valid is True
        assert result.attempts == 1
        assert result.test_quality is not None
        assert result.test_quality.assertion_count >= 1
        assert len(executor.calls) == 1

    async def test_sandbox_fail_triggers_retry(self, workspace, registry) -> None:
        provider = FakeProvider([_VALID_TEST, _VALID_TEST])
        executor = FakeExecutableSandbox()
        executor.queue_result(exit_code=1, stderr="AssertionError: 1 != 2")
        executor.queue_result(exit_code=0, stdout="1 passed")
        inp = replace(
            _make_input(workspace, registry, provider),
            sandbox_executor=executor,
        )
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await TesterAgent().run(inp)
        assert result.is_valid is True
        assert result.attempts == 2
        assert len(provider.calls) == 2
        assert "AssertionError" in provider.calls[1][0]

    async def test_sandbox_fail_both_attempts_returns_invalid(self, workspace, registry) -> None:
        provider = FakeProvider([_VALID_TEST, _VALID_TEST])
        executor = FakeExecutableSandbox()
        executor.queue_result(exit_code=1, stderr="AssertionError: first")
        executor.queue_result(exit_code=1, stderr="AssertionError: second")
        inp = replace(
            _make_input(workspace, registry, provider),
            sandbox_executor=executor,
        )
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await TesterAgent().run(inp)
        assert result.is_valid is False
        assert result.attempts == 2
        assert "AssertionError" in (result.failure_reason or "")

    async def test_no_sandbox_keeps_existing_behavior(self, workspace, registry) -> None:
        provider = FakeProvider([_VALID_TEST])
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await TesterAgent().run(_make_input(workspace, registry, provider))
        assert result.is_valid is True
        assert result.attempts == 1
        assert result.test_quality is not None
        assert result.test_quality.mutation_score is None
        assert result.test_quality.branch_coverage is None


class TestTestQuality:
    async def test_assertion_count_populated_without_extras(self, workspace, registry) -> None:
        provider = FakeProvider([_VALID_TEST])
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await TesterAgent().run(_make_input(workspace, registry, provider))
        assert result.test_quality is not None
        assert result.test_quality.assertion_count >= 1
        assert result.test_quality.mutation_score is None
        assert result.test_quality.branch_coverage is None
        assert result.test_quality.flaky is False

    async def test_mutation_score_requires_flag(self, workspace, registry) -> None:
        provider = FakeProvider([_VALID_TEST])
        executor = FakeExecutableSandbox()
        executor.queue_result(exit_code=0)
        inp = replace(
            _make_input(workspace, registry, provider),
            sandbox_executor=executor,
            enable_mutation_gate=False,
        )
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await TesterAgent().run(inp)
        assert result.test_quality is not None
        assert result.test_quality.mutation_score is None


class TestCountAssertions:
    def test_empty_source(self) -> None:
        from codeguardian.core.agents.tester import _count_assertions

        assert _count_assertions("") == 0

    def test_single_assert(self) -> None:
        from codeguardian.core.agents.tester import _count_assertions

        assert _count_assertions("def test_x():\n    assert 1 == 1\n") == 1

    def test_pytest_raises_counts(self) -> None:
        from codeguardian.core.agents.tester import _count_assertions

        source = (
            "import pytest\n"
            "def test_x():\n"
            "    with pytest.raises(ValueError):\n"
            "        raise ValueError()\n"
        )
        assert _count_assertions(source) == 1

    def test_multiple_asserts(self) -> None:
        from codeguardian.core.agents.tester import _count_assertions

        source = "def test_x():\n    assert 1 == 1\n    assert 2 == 2\n"
        assert _count_assertions(source) == 2

    def test_invalid_source(self) -> None:
        from codeguardian.core.agents.tester import _count_assertions

        assert _count_assertions("def test_x(:\n") == 0


class TestGenerateMutations:
    def test_returns_empty_for_missing_function(self) -> None:
        from codeguardian.core.agents._mutation import generate_mutations

        assert generate_mutations("def other():\n    return 1\n", "missing") == []

    def test_returns_empty_for_invalid_source(self) -> None:
        from codeguardian.core.agents._mutation import generate_mutations

        assert generate_mutations("def broken(:\n", "broken") == []

    def test_generates_compare_mutation(self) -> None:
        from codeguardian.core.agents._mutation import generate_mutations

        source = "def f(x):\n    if x < 10:\n        return True\n    return False\n"
        mutations = generate_mutations(source, "f")
        assert len(mutations) >= 1
        descriptions = {m.description for m in mutations}
        assert any("Lt" in d or "<" in d for d in descriptions)

    def test_respects_limit(self) -> None:
        from codeguardian.core.agents._mutation import generate_mutations

        source = (
            "def f(x, y):\n"
            "    if x < 10 and y > 5:\n"
            "        return 1\n"
            "    if x == 0:\n"
            "        return 0\n"
            "    return 2\n"
        )
        mutations = generate_mutations(source, "f", limit=2)
        assert len(mutations) <= 2

    def test_mutated_source_parses(self) -> None:
        import ast

        from codeguardian.core.agents._mutation import generate_mutations

        source = "def f(x):\n    return x + 1\n"
        mutations = generate_mutations(source, "f")
        for mutation in mutations:
            ast.parse(mutation.mutated_source)
