"""Determinism tests for the seven LLM agents.

Each agent runs twice with the same input, same fake model, and
temperature 0. Results must be identical. This proves our side is
deterministic given the same model output. It does not prove the
hosted model itself is deterministic.

Recon and blast are trivially deterministic (byte-equality tested in
their own test modules) and are excluded here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("codeguardian._native")

from codeguardian import _native
from codeguardian.core.agents.analyst import (
    AnalystAgent,
    AnalystInput,
    RefactorChange,
    RefactorSpec,
)
from codeguardian.core.agents.blast import BlastResult
from codeguardian.core.agents.contract_verifier import (
    ContractVerifier,
    ContractVerifierInput,
)
from codeguardian.core.agents.correctness_verifier import (
    CorrectnessVerifier,
    CorrectnessVerifierInput,
)
from codeguardian.core.agents.recon import ReconResult
from codeguardian.core.agents.security_verifier import (
    SecurityVerifier,
    SecurityVerifierInput,
)
from codeguardian.core.agents.skill_curator import (
    SkillCuratorAgent,
    SkillCuratorInput,
)
from codeguardian.core.agents.tester import TesterAgent, TesterInput
from codeguardian.core.agents.writer import WriterAgent, WriterInput
from codeguardian.core.plugins import BudgetTracker, PluginRegistry
from codeguardian.core.plugins.context_runtime import cpg_available
from codeguardian.interfaces.model import ModelOptions, ModelResponse
from codeguardian.interfaces.verifier import ContractAssertion

_SOURCE = "def parse(raw):\n    return raw.strip()\n"


class FakeProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, prompt: str, model: str, options: ModelOptions) -> ModelResponse:
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

    def stream(self, *a: object, **kw: object) -> None:
        raise NotImplementedError

    def count_tokens(self, text: str, model: str) -> int:
        return 0

    def get_available_models(self) -> list[object]:
        return []


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    return PluginRegistry.discover_bundled()


@pytest.fixture
def workspace(tmp_path: Path):
    (tmp_path / "a.py").write_text(_SOURCE, encoding="utf-8")
    cpg_bytes = _native.build_graph([("a.py", "python", _SOURCE)])
    return tmp_path, cpg_bytes, _native.PyCPG(cpg_bytes)


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


def _make_recon(cpg_bytes: bytes, cpg_path: Path) -> ReconResult:
    return ReconResult(artifact={}, cpg_bytes=cpg_bytes, cpg_path=cpg_path)


def _make_spec() -> RefactorSpec:
    return RefactorSpec(
        directive="add type hints",
        target="a.py:parse",
        changes=(RefactorChange(kind="apply_directive", reason="add type hints"),),
        constraints=(),
    )


_ANALYST_JSON = '{"files": [{"path": "a.py", "reason": "target"}], "reason": "ok"}'
_TESTER_SOURCE = "import pytest\n\ndef test_parse():\n    assert parse('x') == 'x'\n"
_WRITER_SOURCE = "def parse(raw: str) -> str:\n    return raw.strip()\n"
_VERIFIER_JSON = '{"verdict": "pass", "confidence": 0.9, "findings": []}'
_SKILL_JSON = json.dumps(
    {
        "skill_name": "test-skill",
        "skill_markdown": (
            "---\n"
            "name: test-skill\n"
            "description: test\n"
            "trigger: test\n"
            "skill_type: refactor_pattern\n"
            "---\n"
            "body"
        ),
        "confidence": 0.8,
    }
)


async def test_analyst_deterministic(workspace, registry) -> None:
    root, cpg_bytes, cpg = workspace

    async def run_once() -> object:
        provider = FakeProvider([_ANALYST_JSON])
        inp = AnalystInput(
            blast=_make_blast(),
            recon=_make_recon(cpg_bytes, root / "abc.cpg"),
            directive="add type hints",
            registry=registry,
            provider=provider,
            model="fake",
            budget=BudgetTracker(),
            clone_root=root,
        )
        with cpg_available(cpg, root):
            return await AnalystAgent().run(inp)

    r1 = await run_once()
    r2 = await run_once()
    assert r1.spec.to_dict() == r2.spec.to_dict()
    assert r1.context_pack == r2.context_pack
    assert r1.is_refusal == r2.is_refusal
    assert r1.warnings == r2.warnings


async def test_tester_deterministic(workspace, registry) -> None:
    root, _cpg_bytes, cpg = workspace

    async def run_once() -> object:
        provider = FakeProvider([_TESTER_SOURCE])
        inp = TesterInput(
            spec=_make_spec(),
            blast=_make_blast(),
            registry=registry,
            provider=provider,
            model="fake",
            budget=BudgetTracker(),
            clone_root=root,
            target_path="a.py",
            target_symbol="parse",
        )
        with cpg_available(cpg, root):
            return await TesterAgent().run(inp)

    r1 = await run_once()
    r2 = await run_once()
    assert r1.test_file == r2.test_file
    assert r1.is_refusal == r2.is_refusal
    assert r1.is_valid == r2.is_valid
    assert r1.attempts == r2.attempts


async def test_writer_deterministic(workspace, registry) -> None:
    root, _cpg_bytes, cpg = workspace

    async def run_once() -> object:
        provider = FakeProvider([_WRITER_SOURCE] * 3)
        inp = WriterInput(
            spec=_make_spec(),
            blast=_make_blast(),
            skeleton="def parse(raw): ...",
            characterization_test=("def test_parse():\n    assert parse('x') == 'x'\n"),
            registry=registry,
            provider=provider,
            model="fake",
            budget=BudgetTracker(),
            clone_root=root,
            target_path="a.py",
            target_symbol="parse",
        )
        with cpg_available(cpg, root):
            return await WriterAgent().run(inp)

    r1 = await run_once()
    r2 = await run_once()
    assert len(r1.candidates) == len(r2.candidates)
    for c1, c2 in zip(r1.candidates, r2.candidates, strict=False):
        assert c1.source == c2.source
        assert c1.compliance_score == c2.compliance_score
        assert c1.diff_size == c2.diff_size
    assert r1.is_refusal == r2.is_refusal


async def test_correctness_verifier_deterministic() -> None:
    async def run_once() -> object:
        provider = FakeProvider([_VERIFIER_JSON])
        inp = CorrectnessVerifierInput(
            diff="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n",
            change_kind="semantic",
            blast_score=10,
            test_result="PASS",
            provider=provider,
            model="fake",
            budget=BudgetTracker(),
            clone_root=Path("/tmp"),
        )
        return await CorrectnessVerifier().run(inp)

    r1 = await run_once()
    r2 = await run_once()
    assert r1.verdict == r2.verdict
    assert r1.verdict_reason == r2.verdict_reason
    assert r1.pre_scan_conflict == r2.pre_scan_conflict


async def test_security_verifier_deterministic() -> None:
    async def run_once() -> object:
        provider = FakeProvider([_VERIFIER_JSON])
        inp = SecurityVerifierInput(
            diff="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n",
            change_kind="semantic",
            blast_score=10,
            original_security_signals=(),
            provider=provider,
            model="fake",
            budget=BudgetTracker(),
            clone_root=Path("/tmp"),
        )
        return await SecurityVerifier().run(inp)

    r1 = await run_once()
    r2 = await run_once()
    assert r1.verdict == r2.verdict
    assert r1.pre_scan == r2.pre_scan


async def test_contract_verifier_deterministic() -> None:
    async def run_once() -> object:
        provider = FakeProvider([_VERIFIER_JSON])
        inp = ContractVerifierInput(
            diff="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n",
            change_kind="semantic",
            blast_score=10,
            contract_assertions=(
                ContractAssertion(
                    caller="c",
                    assumption="a",
                    severity="warning",
                    test_expression="assert result is not None",
                ),
            ),
            provider=provider,
            model="fake",
            budget=BudgetTracker(),
            clone_root=Path("/tmp"),
        )
        return await ContractVerifier().run(inp)

    r1 = await run_once()
    r2 = await run_once()
    assert r1.verdict == r2.verdict
    assert r1.pre_scan == r2.pre_scan


async def test_skill_curator_deterministic() -> None:
    async def run_once() -> object:
        provider = FakeProvider([_SKILL_JSON])
        inp = SkillCuratorInput(
            outcome="success",
            directive="add type hints",
            target_symbol="parse",
            target_path="a.py",
            diff="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n",
            verifier_verdicts="correctness: pass",
            test_results="1 passed",
            failure_reason="",
            provider=provider,
            model="fake",
            budget=BudgetTracker(),
            clone_root=Path("/tmp"),
        )
        return await SkillCuratorAgent().run(inp)

    r1 = await run_once()
    r2 = await run_once()
    assert r1.proposal == r2.proposal
    assert r1.polarity == r2.polarity
    assert r1.state == r2.state
    assert r1.has_proposal == r2.has_proposal
