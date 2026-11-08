"""Tests for the writer agent."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("codeguardian._native")
from dataclasses import replace

from codeguardian import _native
from codeguardian.core.agents._verifier_common import ExecutionOutcome
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

    def test_signature_drift_rejects_invalid_original(self) -> None:
        """Phase 11 §2.2: invalid original source rejects the candidate."""
        invalid_original = "def parse(raw:\n    return raw\n"
        clean, reason = _check_signature_drift(invalid_original, _VALID_CANDIDATE)
        assert clean is False
        assert reason is not None
        assert "original source did not parse" in reason

    def test_signature_drift_rejects_invalid_candidate(self) -> None:
        """Phase 11 §2.2: invalid candidate source rejects the candidate."""
        invalid_candidate = "def parse(raw: str:\n    return raw\n"
        clean, reason = _check_signature_drift(_SOURCE, invalid_candidate)
        assert clean is False
        assert reason is not None
        assert "candidate source did not parse" in reason


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
            return ExecutionOutcome(exit_code=exit_code, stdout=stdout, stderr=stderr)
        return ExecutionOutcome(exit_code=0, stdout="", stderr="")

    def queue_result(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        self._queue.append((exit_code, stdout, stderr))

    def files(self) -> dict[str, str]:
        return dict(self._files)


def _block_blast() -> BlastResult:
    """Return a blast result with recommendation_adjusted set to block."""
    return replace(_make_blast(), recommendation_adjusted="block")


class TestWriterStatus:
    async def test_success_status_and_empty_requires(self, workspace, registry) -> None:
        provider = FakeProvider([_VALID_CANDIDATE, _VALID_CANDIDATE, _VALID_CANDIDATE])
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await WriterAgent().run(_make_input(workspace, registry, provider))
        assert result.status == "success"
        assert result.is_refusal is False
        assert result.requires == ()

    async def test_partial_status_and_human_approval_requires(self, workspace, registry) -> None:
        provider = FakeProvider([_VALID_CANDIDATE, _VALID_CANDIDATE, _VALID_CANDIDATE])
        inp = replace(
            _make_input(workspace, registry, provider),
            blast=_block_blast(),
        )
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await WriterAgent().run(inp)
        assert result.status == "partial"
        assert result.is_refusal is False
        assert result.requires == ("human_approval",)
        assert len(result.candidates) == 3

    async def test_refused_status_when_no_survivors(self, workspace, registry) -> None:
        invalid = "def parse(raw:\n    return None\n"
        provider = FakeProvider([invalid, invalid, invalid])
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await WriterAgent().run(_make_input(workspace, registry, provider))
        assert result.status == "refused"
        assert result.is_refusal is True
        assert result.requires == ()
        assert result.candidates == ()


class TestWriterSandboxVerification:
    async def test_passing_test_sets_tests_pass_true(self, workspace, registry) -> None:
        provider = FakeProvider([_VALID_CANDIDATE])
        executor = FakeExecutableSandbox()
        executor.queue_result(exit_code=0, stdout="1 passed")
        inp = replace(
            _make_input(workspace, registry, provider),
            n_candidates=1,
            sandbox_executor=executor,
            characterization_test_path="test_parse.py",
        )
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await WriterAgent().run(inp)
        assert result.candidates[0].verification is not None
        assert result.candidates[0].verification["tests_pass"] is True
        assert result.candidates[0].verification["splices"] is True
        assert result.candidates[0].verification["parses"] is True
        assert len(executor.calls) == 1

    async def test_failing_test_sets_tests_pass_false(self, workspace, registry) -> None:
        provider = FakeProvider([_VALID_CANDIDATE])
        executor = FakeExecutableSandbox()
        executor.queue_result(exit_code=1, stderr="AssertionError: 1 != 2")
        inp = replace(
            _make_input(workspace, registry, provider),
            n_candidates=1,
            sandbox_executor=executor,
            characterization_test_path="test_parse.py",
        )
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await WriterAgent().run(inp)
        assert result.candidates[0].verification is not None
        assert result.candidates[0].verification["tests_pass"] is False

    async def test_no_sandbox_leaves_verification_defaults(self, workspace, registry) -> None:
        provider = FakeProvider([_VALID_CANDIDATE])
        inp = replace(
            _make_input(workspace, registry, provider),
            n_candidates=1,
        )
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await WriterAgent().run(inp)
        assert result.candidates[0].verification is not None
        assert result.candidates[0].verification["tests_pass"] is None
        assert result.candidates[0].verification["verifier_score"] is None

    async def test_verifier_gated_ranking_prefers_passing(self, workspace, registry) -> None:
        partial = "def parse(raw: str):\n    return raw.strip()\n"
        full = _VALID_CANDIDATE
        provider = FakeProvider([partial, full, full])
        executor = FakeExecutableSandbox()
        executor.queue_result(exit_code=0)
        executor.queue_result(exit_code=1)
        executor.queue_result(exit_code=1)
        inp = replace(
            _make_input(workspace, registry, provider),
            sandbox_executor=executor,
            characterization_test_path="test_parse.py",
        )
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await WriterAgent().run(inp)
        assert len(result.candidates) == 3
        assert result.candidates[0].verification["tests_pass"] is True
        assert result.candidates[0].source == partial


class TestWriterVerifierScore:
    async def test_verifier_score_influences_ranking(self, workspace, registry) -> None:
        partial = "def parse(raw: str):\n    return raw.strip()\n"
        full = _VALID_CANDIDATE

        def score_fn(source: str) -> float:
            if source == partial:
                return 90.0
            return 50.0

        provider = FakeProvider([partial, full])
        inp = replace(
            _make_input(workspace, registry, provider),
            n_candidates=2,
            verifier_score_fn=score_fn,
        )
        root, cpg = workspace
        with cpg_available(cpg, root):
            result = await WriterAgent().run(inp)
        assert len(result.candidates) == 2
        assert result.candidates[0].source == partial
        assert result.candidates[0].verification["verifier_score"] == 90.0
        assert result.candidates[1].verification["verifier_score"] == 50.0
