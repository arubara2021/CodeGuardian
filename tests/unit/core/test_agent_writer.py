"""Tests for the writer agent."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("codeguardian._native")

from codeguardian import _native
from codeguardian.core.agents.analyst import RefactorChange, RefactorSpec
from codeguardian.core.agents.blast import BlastResult
from codeguardian.core.agents.writer import (
    WriterAgent,
    WriterInput,
    _check_directive_compliance,
    _check_signature_drift,
    _compute_diff_size,
)
from codeguardian.core.plugins import BudgetTracker, PluginRegistry
from codeguardian.core.plugins.context_runtime import cpg_available
from codeguardian.interfaces.model import ModelOptions, ModelResponse

_SOURCE = "def parse(raw):\n    return raw.strip()\n"
_VALID_CANDIDATE = "def parse(raw: str) -> str:\n    return raw.strip()\n"


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


def _make_blast() -> BlastResult:
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
        contract_assertions=(),
        unresolved_call_sites=0,
        caller_search_complete=True,
        calibration_reason=None,
    )


def _make_input(
    workspace,
    registry: PluginRegistry,
    provider: FakeProvider,
    *,
    previous_attempt: str = "",
    test_failure: str = "",
    previous_diff: str = "",
) -> WriterInput:
    root, _ = workspace
    return WriterInput(
        spec=_make_spec(),
        blast=_make_blast(),
        skeleton="def parse(raw): ...",
        characterization_test="def test_parse():\n    assert parse('x') == 'x'\n",
        registry=registry,
        provider=provider,
        model="fake",
        budget=BudgetTracker(),
        clone_root=root,
        target_path="a.py",
        target_symbol="parse",
        previous_attempt=previous_attempt,
        test_failure=test_failure,
        previous_diff=previous_diff,
    )


async def test_three_candidates_generated(workspace, registry) -> None:
    provider = FakeProvider([_VALID_CANDIDATE, _VALID_CANDIDATE, _VALID_CANDIDATE])
    root, cpg = workspace
    with cpg_available(cpg, root):
        result = await WriterAgent().run(_make_input(workspace, registry, provider))
    assert result.is_refusal is False
    assert len(result.candidates) == 3
    assert result.total_attempted == 3
    assert len(provider.calls) == 3


async def test_signature_drift_drops_candidate(workspace, registry) -> None:
    drifted = "def parse(data: str) -> str:\n    return data.strip()\n"
    provider = FakeProvider([drifted, _VALID_CANDIDATE, _VALID_CANDIDATE])
    root, cpg = workspace
    with cpg_available(cpg, root):
        result = await WriterAgent().run(_make_input(workspace, registry, provider))
    assert result.is_refusal is False
    assert len(result.candidates) == 2
    for cand in result.candidates:
        assert "raw" in cand.source


async def test_ast_parse_failure_drops_candidate(workspace, registry) -> None:
    invalid = "def parse(raw:\n    return None\n"
    provider = FakeProvider([invalid, _VALID_CANDIDATE, _VALID_CANDIDATE])
    root, cpg = workspace
    with cpg_available(cpg, root):
        result = await WriterAgent().run(_make_input(workspace, registry, provider))
    assert result.is_refusal is False
    assert len(result.candidates) == 2


async def test_directive_compliance_prunes(workspace, registry) -> None:
    non_compliant = "def parse(raw):\n    return raw.strip()\n"
    provider = FakeProvider([non_compliant, _VALID_CANDIDATE, _VALID_CANDIDATE])
    root, cpg = workspace
    with cpg_available(cpg, root):
        result = await WriterAgent().run(_make_input(workspace, registry, provider))
    assert result.is_refusal is False
    assert len(result.candidates) == 2


async def test_ranking_fully_compliant_above_partial(workspace, registry) -> None:
    partial = "def parse(raw: str):\n    return raw.strip()\n"
    full = _VALID_CANDIDATE
    provider = FakeProvider([partial, full, full])
    root, cpg = workspace
    with cpg_available(cpg, root):
        result = await WriterAgent().run(_make_input(workspace, registry, provider))
    assert result.is_refusal is False
    assert result.candidates[0].compliance_score == 1.0
    assert all(
        result.candidates[i].compliance_score >= result.candidates[i + 1].compliance_score
        for i in range(len(result.candidates) - 1)
    )


async def test_ranked_output_carries_scores(workspace, registry) -> None:
    provider = FakeProvider([_VALID_CANDIDATE, _VALID_CANDIDATE, _VALID_CANDIDATE])
    root, cpg = workspace
    with cpg_available(cpg, root):
        result = await WriterAgent().run(_make_input(workspace, registry, provider))
    for cand in result.candidates:
        assert isinstance(cand.compliance_score, float)
        assert isinstance(cand.diff_size, int)
        assert cand.compliance_score > 0
        assert cand.diff_size >= 0


async def test_retry_uses_v110_prompt(workspace, registry) -> None:
    provider = FakeProvider([_VALID_CANDIDATE, _VALID_CANDIDATE, _VALID_CANDIDATE])
    root, cpg = workspace
    with cpg_available(cpg, root):
        result = await WriterAgent().run(
            _make_input(
                workspace,
                registry,
                provider,
                previous_attempt="def parse(raw): return raw\n",
                test_failure="assert parse('x') == 'x' failed",
                previous_diff="-def parse(raw): pass\n+def parse(raw): return raw\n",
            )
        )
    assert result.is_refusal is False
    for prompt, _, _ in provider.calls:
        assert "Previous attempt feedback" in prompt
        assert "assert parse('x') == 'x' failed" in prompt


async def test_refusal_when_all_candidates_fail(workspace, registry) -> None:
    invalid = "def parse(raw:\n    return None\n"
    provider = FakeProvider([invalid, invalid, invalid])
    root, cpg = workspace
    with cpg_available(cpg, root):
        result = await WriterAgent().run(_make_input(workspace, registry, provider))
    assert result.is_refusal is True
    assert result.candidates == ()
    assert result.total_attempted == 3


class TestPreCheckFunctions:
    def test_signature_drift_clean(self) -> None:
        clean, _ = _check_signature_drift(_SOURCE, _VALID_CANDIDATE)
        assert clean is True

    def test_signature_drift_detected(self) -> None:
        drifted = "def parse(data: str) -> str:\n    return data.strip()\n"
        clean, reason = _check_signature_drift(_SOURCE, drifted)
        assert clean is False
        assert reason is not None
        assert "parameter" in reason

    def test_return_type_change_detected(self) -> None:
        changed = "def parse(raw: str) -> int:\n    return 0\n"
        original_with_str = "def parse(raw: str) -> str:\n    return raw\n"
        clean, reason = _check_signature_drift(original_with_str, changed)
        assert clean is False
        assert reason is not None
        assert "return type" in reason

    def test_compliance_full(self) -> None:
        score = _check_directive_compliance(_VALID_CANDIDATE, _make_spec())
        assert score == 1.0

    def test_compliance_partial(self) -> None:
        partial = "def parse(raw: str):\n    return raw.strip()\n"
        score = _check_directive_compliance(partial, _make_spec())
        assert 0.0 < score < 1.0

    def test_compliance_zero(self) -> None:
        non = "def parse(raw):\n    return raw.strip()\n"
        score = _check_directive_compliance(non, _make_spec())
        assert score == 0.0

    def test_diff_size_smaller_for_similar(self) -> None:
        small = _compute_diff_size(_SOURCE, _VALID_CANDIDATE)
        large = _compute_diff_size(
            _SOURCE, "def parse(a, b, c):\n    return a\n    x = 1\n    y = 2\n"
        )
        assert small < large
