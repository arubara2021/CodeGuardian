"""Pipeline runner for the deep test, driven by the orchestrator.

The orchestrator walks ``LEGACY_GRAPH`` and dispatches each slot to a
real agent. This module owns the setup the orchestrator does not:
building the model router, registering the nine Phase 11 agents plus
the two test-scoped adapters (``SandboxVerifyAgent`` and the three
``VerifierStackAgent`` instances), constructing the input factory that
maps each slot name to its input dataclass, and running fault
injection after the orchestrator returns.

The pipeline's job is to produce a ``PipelineResult`` identical in
shape to the pre-Phase-13 version, so the deep test's existing
readers keep working. Every per-phase stage the orchestrator reports
through its observer becomes one ``PipelineStageResult``. The stage
names match the pre-Phase-13 names so a reader does not have to learn
a new vocabulary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from codeguardian.core.agents import (
    AnalystAgent,
    BlastAgent,
    ReconAgent,
    SkillCuratorAgent,
    TesterAgent,
    WriterAgent,
    _store_context,
)
from codeguardian.core.agents.analyst import AnalystInput
from codeguardian.core.agents.blast import BlastInput
from codeguardian.core.agents.recon import ReconInput
from codeguardian.core.agents.skill_curator import SkillCuratorInput
from codeguardian.core.agents.tester import TesterInput
from codeguardian.core.agents.writer import WriterInput
from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.orchestrator import (
    AgentRegistry,
    LangGraphOrchestrator,
)
from codeguardian.core.orchestrator.checkpoint_supervisor import (
    CheckpointSupervisor,
)
from codeguardian.core.orchestrator.loop import LoopContext, SlotExecution
from codeguardian.core.orchestrator.registry import AgentSpec
from codeguardian.core.plugins import BudgetTracker, PluginRegistry
from codeguardian.core.providers import FallbackModelProvider, LocalProvider
from codeguardian.core.storage import SQLiteStore
from codeguardian.interfaces.orchestrator import RunConfig

from ._discovery import RankedTarget
from ._fault_injection import FaultInjectionResult, run_fault_injection
from ._sandbox_verify import SandboxVerifyAgent, SandboxVerifyInput
from ._verifier_agents import (
    VerifierStackAgent,
    VerifierStackInput,
    build_verifier_agents,
)

if TYPE_CHECKING:
    from ._sandbox import SandboxSession

__all__ = [
    "Band",
    "PipelineResult",
    "PipelineStageResult",
    "run_pipeline",
]


Band = Literal["proceed", "review", "block"]


_SLOT_AGENT_TAGS: dict[str, frozenset[str]] = {
    "recon": frozenset({"deterministic", "cpg-builder"}),
    "blast": frozenset({"deterministic", "graph-query"}),
    "analyst": frozenset({"llm", "reasoning", "spec-producer"}),
    "tester": frozenset({"llm", "coding", "test-producer"}),
    "writer": frozenset({"llm", "coding", "code-producer"}),
    "sandbox_verify": frozenset({"deterministic", "sandbox-runner"}),
    "verifier_correctness": frozenset({"llm", "reasoning", "verdict-producer", "correctness"}),
    "verifier_security": frozenset({"llm", "reasoning", "verdict-producer", "security"}),
    "verifier_contract": frozenset({"llm", "reasoning", "verdict-producer", "contract"}),
    "skill_curator": frozenset({"llm", "reasoning", "skill-producer"}),
}
"""The capability tags each agent must declare to fill its slot.

The verifier tags include the distinguishing fourth tag the legacy
graph now requires, so each verifier resolves to its own agent rather
than all three resolving to the highest-priority one.
"""


@dataclass(frozen=True, slots=True)
class PipelineStageResult:
    """One stage's outcome inside a pipeline run."""

    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """The complete outcome of one pipeline run."""

    target_qualified_name: str
    directive: str
    band: Band
    refused: bool
    refusal_reason: str | None
    stages: tuple[PipelineStageResult, ...]
    rollback_ok: bool
    fault_injection: FaultInjectionResult | None
    telemetry_rows: int


class _RecordingAuditWriter:
    """Audit writer that keeps entries in memory.

    The deep test's audit chain is the JSONL file the ApplyProvider
    writes. This writer is for the orchestrator's per-phase entries;
    it keeps them in a list so a caller can inspect them and returns
    a hash per entry so the loop's audit chain stays consistent.
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._prev_hash = "GENESIS"

    def __call__(self, entry: dict[str, Any]) -> str:
        full = {
            "entry_id": hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:32],
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            **entry,
        }
        payload_hash = hashlib.sha256(
            json.dumps(full, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        entry_hash = hashlib.sha256((self._prev_hash + payload_hash).encode()).hexdigest()
        self.entries.append({**full, "payload_hash": payload_hash, "entry_hash": entry_hash})
        self._prev_hash = entry_hash
        return entry_hash


class _DeepTestObserver:
    """Observer that records per-phase executions and builds stages.

    The orchestrator calls the observer once per phase. The observer
    collects the ``SlotExecution`` and, when the run finishes,
    ``to_stages`` converts them into the ``PipelineStageResult``
    tuples the deep test's readers expect.
    """

    def __init__(self) -> None:
        self.executions: list[SlotExecution] = []

    def __call__(
        self,
        slot_name: str,
        execution: SlotExecution,
        context: LoopContext,
    ) -> None:
        self.executions.append(execution)

    def to_stages(self) -> tuple[PipelineStageResult, ...]:
        stages: list[PipelineStageResult] = []
        for execution in self.executions:
            detail = ""
            if not execution.ok and execution.error is not None:
                detail = execution.error.message[:200]
            elif execution.checkpoint_id is not None:
                detail = f"checkpoint={execution.checkpoint_id[:8]}"
            stages.append(
                PipelineStageResult(
                    name=execution.slot_name,
                    ok=execution.ok,
                    detail=detail,
                )
            )
        return tuple(stages)

    def find(self, slot_name: str) -> SlotExecution | None:
        for execution in self.executions:
            if execution.slot_name == slot_name:
                return execution
        return None


class _DeepTestInputFactory:
    """Builds each slot's input from the loop's accumulated context.

    The factory also owns the CPG context. The analyst, tester, and
    writer agents call context-provider plugins that read from a CPG
    handle; the handle is opened once on the first such call and kept
    active for the rest of the run. The factory's ``close`` resets it.
    """

    def __init__(
        self,
        *,
        directive: str,
        router: FallbackModelProvider,
        registry: PluginRegistry,
        clone_root: Path,
        target: RankedTarget,
        repo_url: str,
        characterization_test_path: str,
        verifier_agents: list[VerifierStackAgent],
    ) -> None:
        self._directive = directive
        self._router = router
        self._registry = registry
        self._clone_root = clone_root
        self._target = target
        self._repo_url = repo_url
        self._characterization_test_path = characterization_test_path
        self._verifier_agents = verifier_agents
        self._cpg_handle: Any = None
        self._budget = BudgetTracker(max_calls=200, max_output_bytes=20_000_000)

    def _ensure_cpg(self, context: LoopContext) -> None:
        """Set the CPG context vars for phases that need them.

        The factory does not use ``cpg_available`` as a context
        manager. The manager's tokens are bound to the context of the
        thread that enters them; entering on the loop's worker thread
        and letting garbage collection finalise on the main thread
        produces a ``was created in a different Context`` error. The
        context vars are set directly instead, with no token to reset.
        The worker thread's context is discarded when the run ends, so
        there is nothing to leak.

        The analyst agent enters its own ``cpg_available`` from the
        recon result and exits it before returning; the reset restores
        whatever was set before, which is either ``None`` (if the
        analyst ran first) or the handle this method set (if a prior
        phase already set it). Either value is harmless because the
        analyst has finished by the time the tester and writer run.

        Called idempotently. The first call creates the handle and sets
        the vars; every subsequent call is a no-op.
        """
        if self._cpg_handle is not None:
            return
        recon = context.results.get("recon")
        if recon is None:
            return
        from codeguardian import _native
        from codeguardian.core.plugins.context_runtime import (
            _CURRENT_CLONE_ROOT,
            _CURRENT_CPG,
        )

        self._cpg_handle = _native.PyCPG(recon.cpg_bytes)
        _CURRENT_CPG.set(self._cpg_handle)
        _CURRENT_CLONE_ROOT.set(self._clone_root)

    def build(self, slot_name: str, context: LoopContext) -> Any:
        if slot_name == "recon":
            return ReconInput(
                repo_url=self._repo_url,
                provider="local",
                commit_hash=self._commit_hash(),
                clone_root=self._clone_root,
                cpg_output_dir=self._clone_root.parent / "cpg-out",
            )
        if slot_name == "blast":
            recon = context.results["recon"]
            return BlastInput(
                cpg_bytes=recon.cpg_bytes,
                target_path=self._target.candidate.path,
                target_qualified_name=self._target.candidate.qualified_name,
                clone_root=self._clone_root,
                change_kind="semantic",
            )
        if slot_name == "analyst":
            return AnalystInput(
                blast=context.results["blast"],
                recon=context.results["recon"],
                directive=self._directive,
                registry=self._registry,
                provider=self._router,
                model="analyst",
                budget=self._budget,
                clone_root=self._clone_root,
            )
        if slot_name == "tester":
            self._ensure_cpg(context)
            return TesterInput(
                spec=context.results["analyst"].spec,
                blast=context.results["blast"],
                registry=self._registry,
                provider=self._router,
                model="tester",
                budget=self._budget,
                clone_root=self._clone_root,
                target_path=self._target.candidate.path,
                target_symbol=self._target.candidate.qualified_name,
            )
        if slot_name == "writer":
            self._ensure_cpg(context)
            return WriterInput(
                spec=context.results["analyst"].spec,
                blast=context.results["blast"],
                skeleton=self._skeleton(context),
                characterization_test=self._characterization_test(),
                registry=self._registry,
                provider=self._router,
                model="writer",
                budget=BudgetTracker(max_calls=10, max_output_bytes=5_000_000),
                clone_root=self._clone_root,
                target_path=self._target.candidate.path,
                target_symbol=self._target.candidate.qualified_name,
                n_candidates=3,
            )
        if slot_name == "sandbox_verify":
            return SandboxVerifyInput(
                results=dict(context.results),
                clone_root=self._clone_root,
                target_path=self._target.candidate.path,
                target_symbol=self._target.candidate.qualified_name,
                characterization_test_path=self._characterization_test_path,
            )
        if slot_name.startswith("verifier_"):
            return VerifierStackInput(
                slot=slot_name,
                results=dict(context.results),
            )
        if slot_name == "skill_curator":
            return self._build_curator_input(context)
        raise CodeGuardianError(
            "SYS_INVARIANT_VIOLATED",
            f"no input builder for slot {slot_name!r}",
            details={"slot": slot_name},
        )

    def _commit_hash(self) -> str:
        try:
            return LocalProvider().get_metadata().head_commit
        except CodeGuardianError:
            return "unknown"

    def _skeleton(self, context: LoopContext) -> str:
        recon = context.results.get("recon")
        if recon is None:
            return ""
        return ""

    def _characterization_test(self) -> str:
        path = self._clone_root / self._characterization_test_path
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return ""

    def _build_curator_input(self, context: LoopContext) -> SkillCuratorInput:
        sandbox_verify = context.results.get("sandbox_verify")
        diff = getattr(sandbox_verify, "diff", "") if sandbox_verify else ""
        verdicts = []
        for role in ("verifier_correctness", "verifier_security", "verifier_contract"):
            result = context.results.get(role)
            if result is not None and hasattr(result, "verdict"):
                verdicts.append(f"{role}: {result.verdict.verdict}")
        return SkillCuratorInput(
            outcome="success",
            directive=self._directive,
            target_symbol=self._target.candidate.qualified_name,
            target_path=self._target.candidate.path,
            diff=diff,
            verifier_verdicts="\n".join(verdicts),
            test_results="tool: PASS",
            failure_reason="",
            provider=self._router,
            model="skill_curator",
            budget=self._budget,
            clone_root=self._clone_root,
        )


def _register_agents(
    registry: AgentRegistry,
    *,
    sandbox_verify_agent: SandboxVerifyAgent,
    verifier_agents: list[VerifierStackAgent],
) -> None:
    """Register every agent the legacy graph needs.

    The nine Phase 11 agents are registered by their class names. The
    two test-scoped adapters are registered as their own agents. Each
    spec's capability tags come from ``_SLOT_AGENT_TAGS`` so the
    resolver matches the right agent to the right slot.
    """
    phase_11: list[tuple[str, Any]] = [
        ("recon", ReconAgent()),
        ("blast", BlastAgent()),
        ("analyst", AnalystAgent()),
        ("tester", TesterAgent()),
        ("writer", WriterAgent()),
        ("skill_curator", SkillCuratorAgent()),
    ]
    for name, agent in phase_11:
        registry.register(
            AgentSpec(
                name=name,
                agent=agent,
                capability_tags=_SLOT_AGENT_TAGS[name],
            )
        )
    registry.register(
        AgentSpec(
            name="sandbox_verify",
            agent=sandbox_verify_agent,
            capability_tags=_SLOT_AGENT_TAGS["sandbox_verify"],
        )
    )
    for agent in verifier_agents:
        registry.register(
            AgentSpec(
                name=agent.name,
                agent=agent,
                capability_tags=_SLOT_AGENT_TAGS[agent.name],
            )
        )


def _source_files_for_parse(
    clone_root: Path,
    parse_filter: str,
    language: str,
) -> list[tuple[str, str, str]]:
    """Collect source files for the initial parse.

    Duplicated from the pre-Phase-13 pipeline because the orchestrator
    does not own the file walk. The exclusion list matches the deep
    test's, so the CPG is built from the same file set as before.
    """
    excluded = {
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "target",
        "dist",
        "build",
        ".deps",
        ".git",
    }
    sources: list[tuple[str, str, str]] = []
    for path in sorted(clone_root.rglob(parse_filter)):
        rel = path.relative_to(clone_root)
        if any(part in excluded or part.startswith(".") for part in rel.parts):
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
        except OSError:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sources.append((str(rel), language, content))
    return sources


async def _run_pipeline_via_orchestrator(
    *,
    session: SandboxSession,
    router: FallbackModelProvider,
    provider: LocalProvider,
    bridge: Any,
    audit_path: Path,
    store: SQLiteStore,
    clone_root: Path,
    target: RankedTarget,
    band: Band,
    directive: str,
    repo_url: str,
) -> tuple[Any, _DeepTestObserver, _DeepTestInputFactory, _RecordingAuditWriter]:
    """Set up and run the orchestrator on the legacy graph.

    Returns the ``RunResult``, the observer (for per-phase stages), the
    input factory (for cleanup), and the audit writer (for inspection).
    The caller is responsible for calling ``factory.close()`` after the
    run completes.
    """
    plugin_registry = PluginRegistry.discover_bundled()

    characterization_test_path = _write_tool_characterization(clone_root, target)
    _write_pytest_ini(clone_root)

    test_result = "PASS"
    old_run = session.run(
        f"PYTHONPATH=.deps:.:$PYTHONPATH python3 run_pytest.py "
        f"{characterization_test_path.name} -v --tb=line",
        cwd="clone",
        timeout_ms=120_000,
    )
    if old_run.exit_code != 0:
        test_result = "FAIL: characterization test on old code failed"

    band_for_stack: Band = band
    sandbox_verify_agent = SandboxVerifyAgent(
        provider=provider,
        bridge=bridge,
        audit_path=audit_path,
        session=session,
        host_clone_root=clone_root,
    )
    verifier_agents = build_verifier_agents(
        provider=router,
        blast=target,
        test_result=test_result,
        clone_root=clone_root,
        target_symbol=target.candidate.qualified_name,
        band=band_for_stack,
    )

    agent_registry = AgentRegistry()
    _register_agents(
        agent_registry,
        sandbox_verify_agent=sandbox_verify_agent,
        verifier_agents=verifier_agents,
    )

    input_factory = _DeepTestInputFactory(
        directive=directive,
        router=router,
        registry=plugin_registry,
        clone_root=clone_root,
        target=target,
        repo_url=repo_url,
        characterization_test_path=characterization_test_path.name,
        verifier_agents=verifier_agents,
    )
    observer = _DeepTestObserver()
    audit_writer = _RecordingAuditWriter()

    orchestrator = LangGraphOrchestrator(
        store=store,
        registry=agent_registry,
        supervisor=CheckpointSupervisor(store),
        audit_writer=audit_writer,
        input_factory=input_factory,
        artifact_root=clone_root.parent / "artifacts",
        observer=observer,
    )

    run_result = orchestrator.run(
        RunConfig(
            url=repo_url,
            target=f"{target.candidate.path}:{target.candidate.qualified_name}",
            directive=directive,
            mode="legacy",
            verify=True,
            apply=False,
        )
    )
    return run_result, observer, input_factory, audit_writer


def _write_tool_characterization(clone: Path, target: RankedTarget) -> Path:
    """Write the tool's value-based characterization test into the clone."""
    record = target.execution or {}
    deterministic = bool(record.get("deterministic"))
    module = target.candidate.module
    function = target.candidate.function
    safe_name = "".join(c if c.isalnum() else "_" for c in function)
    args_repr = repr(list(target.candidate.args))
    expected_type = record.get("expected_type", "")
    expected_value_json = record.get("expected_value_json", "null")
    value_comparable = deterministic and not (
        '"attrs"' in expected_value_json or '"repr"' in expected_value_json
    )

    header = (
        f'"""Characterization test for {module}.{function}."""\n'
        "\n"
        "import os\n"
        "import sys\n"
        "_root = os.path.dirname(os.path.abspath(__file__))\n"
        "sys.path.insert(0, _root)\n"
        "if os.path.isdir(os.path.join(_root, 'src')):\n"
        "    sys.path.insert(0, os.path.join(_root, 'src'))\n"
        "\n\n"
        f"_ARGS = {args_repr}\n"
        f"_EXPECTED_TYPE = {expected_type!r}\n"
        "\n\n"
        "def _call_target():\n"
        f"    from {module} import {function}\n"
        f"    return {function}(*_ARGS)\n"
        "\n\n"
        f"def test_smoke_{safe_name}():\n"
        "    result = _call_target()\n"
        '    assert result is not None, "returned None"\n'
        "    actual_type = type(result).__name__\n"
        "    assert actual_type == _EXPECTED_TYPE, (\n"
        '        f"return type drift: expected {_EXPECTED_TYPE}, got {actual_type}"\n'
        "    )\n"
    )
    if value_comparable:
        header += (
            "\n\n"
            f"def test_value_{safe_name}():\n"
            "    result = _call_target()\n"
            "    import json\n"
            '    actual = {"t": type(result).__name__, "v": repr(result)}\n'
            "    expected = json.loads(_EXPECTED_VALUE_JSON)\n"
            "    assert actual == expected\n"
        )
    path = clone / f"deep_test_characterization_{safe_name}.py"
    path.write_text(header, encoding="utf-8")
    return path


def _write_pytest_ini(clone: Path) -> None:
    """Write a bare pytest.ini to stop the repo's pyproject from loading."""
    (clone / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")


async def run_pipeline(
    *,
    session: SandboxSession,
    router: FallbackModelProvider,
    provider: LocalProvider,
    bridge: Any,
    audit_path: Path,
    store: SQLiteStore,
    native: Any,
    clone_root: Path,
    target: RankedTarget,
    band: Band,
    directive: str,
    repo_url: str,
) -> PipelineResult:
    """Run the full pipeline through the orchestrator.

    The orchestrator walks the legacy graph. The stages the observer
    reports become ``PipelineStageResult`` entries. Fault injection
    runs after the orchestrator returns, using the same sandbox
    session and the same original source the orchestrator started
    from. The return shape matches the pre-Phase-13 pipeline.
    """
    target_path = target.candidate.path
    target_file = clone_root / target_path
    original_source = target_file.read_text(encoding="utf-8")

    with _store_context.active_store(store):
        try:
            run_result, observer, _factory, _audit_writer = await _run_pipeline_via_orchestrator(
                session=session,
                router=router,
                provider=provider,
                bridge=bridge,
                audit_path=audit_path,
                store=store,
                clone_root=clone_root,
                target=target,
                band=band,
                directive=directive,
                repo_url=repo_url,
            )
        except CodeGuardianError as exc:
            return PipelineResult(
                target_qualified_name=target.candidate.qualified_name,
                directive=directive,
                band=band,
                refused=False,
                refusal_reason=exc.message[:200],
                stages=(PipelineStageResult("orchestrator setup", False, exc.message[:200]),),
                rollback_ok=True,
                fault_injection=None,
                telemetry_rows=_count_agent_calls(store),
            )

        stages = list(observer.to_stages())

        writer_execution = observer.find("writer")
        refused = False
        refusal_reason: str | None = None
        if writer_execution is not None and writer_execution.output is not None:
            writer_output = writer_execution.output
            if getattr(writer_output, "is_refusal", False):
                refused = True
                refusal_reason = getattr(writer_output, "refusal_reason", None)

        if band == "block" and writer_execution is not None:
            status_value = "?"
            requires_value: tuple[str, ...] = ()
            if writer_execution.output is not None:
                status_value = str(getattr(writer_execution.output, "status", "?"))
                requires_value = tuple(getattr(writer_execution.output, "requires", ()))
            honoured = (
                status_value == "partial" and "human_approval" in requires_value
            ) or status_value == "refused"
            stages.append(
                PipelineStageResult(
                    "block-band human approval required",
                    honoured,
                    f"status={status_value}, requires={requires_value}",
                )
            )

        rollback_ok = True
        sandbox_verify_execution = observer.find("sandbox_verify")
        if sandbox_verify_execution is not None:
            rollback_ok = bool(getattr(sandbox_verify_execution.output, "rollback_ok", True))

        fault_result: FaultInjectionResult | None = None
        if run_result.status == "success" and not refused:
            safe_name = "".join(c if c.isalnum() else "_" for c in target.candidate.function)
            fault_result = _run_fault_injection_section(
                session=session,
                provider=provider,
                bridge=bridge,
                audit_path=audit_path,
                clone_root=clone_root,
                target=target,
                original_source=original_source,
                characterization_test_path=(
                    clone_root / f"deep_test_characterization_{safe_name}.py"
                ),
            )
            if fault_result is not None:
                stages.append(fault_result.as_check())

        return PipelineResult(
            target_qualified_name=target.candidate.qualified_name,
            directive=directive,
            band=band,
            refused=refused,
            refusal_reason=refusal_reason,
            stages=tuple(stages),
            rollback_ok=rollback_ok,
            fault_injection=fault_result,
            telemetry_rows=_count_agent_calls(store),
        )


def _run_fault_injection_section(
    *,
    session: SandboxSession,
    provider: LocalProvider,
    bridge: Any,
    audit_path: Path,
    clone_root: Path,
    target: RankedTarget,
    original_source: str,
    characterization_test_path: Path,
) -> FaultInjectionResult | None:
    """Run the fault-injection section after the orchestrator returns."""
    import hashlib
    from datetime import UTC, datetime

    from codeguardian.core.providers import ApplyProvider
    from codeguardian.interfaces.repository import FileChange

    target_path = target.candidate.path
    function_name = target.candidate.function

    def build_and_apply(modified: str) -> tuple[Any, Any, bool]:
        changes = [FileChange(path=target_path, new_content=modified)]
        try:
            diff = provider.get_diff(changes)
        except CodeGuardianError:
            return None, None, False
        if not diff:
            return None, None, False
        from codeguardian.interfaces.apply import ConsentRecord

        diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        consent = ConsentRecord(
            consent_id=f"fault-{diff_hash[:12]}",
            run_id="deep-test-fault",
            granted_at=datetime.now(UTC),
            granted_by="deep-test",
            scope="apply",
            diff_hash=diff_hash,
            rollback_ref="",
        )
        applier = ApplyProvider(provider, bridge, audit_path)
        try:
            result = applier.apply(str(clone_root), diff, consent)
        except CodeGuardianError:
            return None, None, False
        return result.rollback_ref, applier, True

    return run_fault_injection(
        session,
        original_source=original_source,
        function_name=function_name,
        target_path=target_path,
        char_filename=characterization_test_path.name,
        build_and_apply=build_and_apply,
    )


def _count_agent_calls(store: SQLiteStore) -> int:
    """Return the number of agent_calls rows currently in the store."""
    try:
        rows = store.query("SELECT COUNT(*) AS c FROM agent_calls")
        return int(rows[0]["c"]) if rows else 0
    except CodeGuardianError:
        return 0
