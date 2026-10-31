"""Tests for the tester agent."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("codeguardian._native")

from codeguardian import _native
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
