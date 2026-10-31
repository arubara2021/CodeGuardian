"""Tests for the analyst agent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("codeguardian._native")

from codeguardian import _native
from codeguardian.core.agents import analyst as analyst_module
from codeguardian.core.agents.analyst import (
    AnalystAgent,
    AnalystInput,
)
from codeguardian.core.agents.blast import BlastResult
from codeguardian.core.agents.recon import ReconResult
from codeguardian.core.plugins import BudgetTracker, PluginRegistry
from codeguardian.interfaces.model import ModelOptions, ModelResponse

_SOURCE = "def parse(raw):\n    return raw.strip()\n"


class FakeProvider:
    """Fake ModelProvider returning a fixed sequence of responses."""

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

    def stream(self, *args, **kwargs):
        raise NotImplementedError

    def count_tokens(self, text, model):
        return 0

    def get_available_models(self):
        return []


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    return PluginRegistry.discover_bundled()


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, bytes]:
    (tmp_path / "a.py").write_text(_SOURCE, encoding="utf-8")
    cpg_bytes = _native.build_graph([("a.py", "python", _SOURCE)])
    return tmp_path, cpg_bytes


def _make_blast(target_path: str, target_symbol: str) -> BlastResult:
    return BlastResult(
        target_path=target_path,
        target_qualified_name=target_symbol,
        target_language=None,
        change_kind="semantic",
        direct_callers=(),
        transitive_callers=(),
        contract_violations=(),
        coverage_gaps=(),
        blast_score=0,
        recommendation_raw="proceed",
        recommendation_adjusted="proceed",
        generated_at="2026-10-31T00:00:00+00:00",
        run_id="test-run",
        ranked_callers=(),
        contract_assertions=(),
        unresolved_call_sites=0,
        caller_search_complete=True,
        calibration_reason=None,
    )


def _make_recon(cpg_bytes: bytes, cpg_path: Path) -> ReconResult:
    return ReconResult(artifact={}, cpg_bytes=cpg_bytes, cpg_path=cpg_path)


def _make_input(
    *,
    workspace: tuple[Path, bytes],
    directive: str,
    registry: PluginRegistry,
    provider: FakeProvider,
    budget: BudgetTracker | None = None,
) -> AnalystInput:
    clone_root, cpg_bytes = workspace
    return AnalystInput(
        blast=_make_blast("a.py", "parse"),
        recon=_make_recon(cpg_bytes, clone_root / "abc.cpg"),
        directive=directive,
        registry=registry,
        provider=provider,
        model="fake",
        budget=budget or BudgetTracker(),
        clone_root=clone_root,
    )


class TestRequiresTools:
    def test_exact_declaration(self) -> None:
        assert AnalystAgent.requires_tools == (
            "list-symbols",
            "code-skeleton",
            "function-body",
            "call-graph-slice",
        )


class TestProgressiveDisclosure:
    async def test_rename_does_not_call_function_body(
        self,
        workspace: tuple[Path, bytes],
        registry: PluginRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []
        real = analyst_module.fresh_barrier

        def spy(reg, name, *, mode, budget, allowed_tools):
            calls.append(name)
            return real(reg, name, mode=mode, budget=budget, allowed_tools=allowed_tools)

        monkeypatch.setattr(analyst_module, "fresh_barrier", spy)
        provider = FakeProvider(
            ['{"files": [{"path": "a.py", "reason": "target"}], "reason": "only target"}']
        )
        agent = AnalystAgent()
        await agent.run(
            _make_input(
                workspace=workspace,
                directive="rename raw to data",
                registry=registry,
                provider=provider,
            )
        )
        assert "list-symbols" in calls
        assert "code-skeleton" in calls
        assert "function-body" not in calls

    async def test_type_directive_calls_function_body(
        self,
        workspace: tuple[Path, bytes],
        registry: PluginRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []
        real = analyst_module.fresh_barrier

        def spy(reg, name, *, mode, budget, allowed_tools):
            calls.append(name)
            return real(reg, name, mode=mode, budget=budget, allowed_tools=allowed_tools)

        monkeypatch.setattr(analyst_module, "fresh_barrier", spy)
        provider = FakeProvider(
            ['{"files": [{"path": "a.py", "reason": "target"}], "reason": "only target"}']
        )
        agent = AnalystAgent()
        await agent.run(
            _make_input(
                workspace=workspace,
                directive="add type hints",
                registry=registry,
                provider=provider,
            )
        )
        assert "function-body" in calls


class TestRefactorSpec:
    async def test_spec_built_from_cpg_evidence(
        self,
        workspace: tuple[Path, bytes],
        registry: PluginRegistry,
    ) -> None:
        provider = FakeProvider(
            ['{"files": [{"path": "a.py", "reason": "target"}], "reason": "only target"}']
        )
        agent = AnalystAgent()
        result = await agent.run(
            _make_input(
                workspace=workspace,
                directive="add type hints",
                registry=registry,
                provider=provider,
            )
        )
        kinds = [c.kind for c in result.spec.changes]
        assert "apply_directive" in kinds
        assert "parameter_annotation" in kinds
        assert "return_annotation" in kinds

        param = next(c for c in result.spec.changes if c.kind == "parameter_annotation")
        assert param.name == "raw"
        assert param.inferred_type is None

        ret = next(c for c in result.spec.changes if c.kind == "return_annotation")
        assert ret.inferred_type == "str"

    async def test_spec_present_even_without_body(
        self,
        workspace: tuple[Path, bytes],
        registry: PluginRegistry,
    ) -> None:
        provider = FakeProvider(
            ['{"files": [{"path": "a.py", "reason": "target"}], "reason": "ok"}']
        )
        agent = AnalystAgent()
        result = await agent.run(
            _make_input(
                workspace=workspace,
                directive="rename raw to data",
                registry=registry,
                provider=provider,
            )
        )
        kinds = [c.kind for c in result.spec.changes]
        assert "apply_directive" in kinds
        assert "parameter_annotation" not in kinds
        assert "return_annotation" not in kinds


class TestContextPack:
    async def test_artifact_validates_against_schema(
        self,
        workspace: tuple[Path, bytes],
        registry: PluginRegistry,
    ) -> None:
        import jsonschema

        schema_path = Path(__file__).resolve().parents[3] / "schemas" / "context-pack.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        provider = FakeProvider(
            ['{"files": [{"path": "a.py", "reason": "target"}], "reason": "only target"}']
        )
        agent = AnalystAgent()
        result = await agent.run(
            _make_input(
                workspace=workspace,
                directive="add type hints",
                registry=registry,
                provider=provider,
            )
        )
        assert result.context_pack_artifact is not None
        jsonschema.validate(result.context_pack_artifact, schema)
        assert result.is_refusal is False

    async def test_contradiction_adds_target_file(
        self,
        workspace: tuple[Path, bytes],
        registry: PluginRegistry,
        tmp_path: Path,
    ) -> None:
        clone_root, _ = workspace
        (clone_root / "other.py").write_text("x = 1\n", encoding="utf-8")
        provider = FakeProvider(
            ['{"files": [{"path": "other.py", "reason": "related"}], "reason": "ok"}']
        )
        agent = AnalystAgent()
        result = await agent.run(
            _make_input(
                workspace=workspace,
                directive="add type hints",
                registry=registry,
                provider=provider,
            )
        )
        paths = {f.path for f in result.context_pack.files}
        assert "a.py" in paths
        assert "other.py" in paths
        assert any("target" in w for w in result.warnings)


class TestRefusal:
    async def test_model_refusal(
        self,
        workspace: tuple[Path, bytes],
        registry: PluginRegistry,
    ) -> None:
        provider = FakeProvider(['{"files": [], "reason": "refused: cannot identify target"}'])
        agent = AnalystAgent()
        result = await agent.run(
            _make_input(
                workspace=workspace,
                directive="add type hints",
                registry=registry,
                provider=provider,
            )
        )
        assert result.is_refusal is True
        assert result.context_pack_artifact is None
        assert result.context_pack.reason.startswith("refused:")

    async def test_invalid_json_returns_schema_refusal(
        self,
        workspace: tuple[Path, bytes],
        registry: PluginRegistry,
    ) -> None:
        provider = FakeProvider(["not valid json"])
        agent = AnalystAgent()
        result = await agent.run(
            _make_input(
                workspace=workspace,
                directive="add type hints",
                registry=registry,
                provider=provider,
            )
        )
        assert result.is_refusal is True
        assert any("schema" in w or "json" in w for w in result.warnings)

    async def test_budget_exhaustion_returns_refusal(
        self,
        workspace: tuple[Path, bytes],
        registry: PluginRegistry,
    ) -> None:
        provider = FakeProvider([])
        tight_budget = BudgetTracker(max_calls=1)
        agent = AnalystAgent()
        result = await agent.run(
            _make_input(
                workspace=workspace,
                directive="add type hints",
                registry=registry,
                provider=provider,
                budget=tight_budget,
            )
        )
        assert result.is_refusal is True
        assert result.context_pack_artifact is None
        assert "budget" in result.context_pack.reason.lower()
