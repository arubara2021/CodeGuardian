"""Sandbox verify agent for the deep test.

The legacy graph declares a ``sandbox_verify`` slot that no Phase 11
agent fills. This module fills it with a test-scoped agent that owns
the deterministic gate: splice the chosen candidate, build the diff,
apply, run the characterization test, roll back, and report the
outcome. When a future phase adds a production ``SandboxVerifyAgent``
to ``core/orchestrator``, the deep test swaps the registration and
this module retires.

The agent wraps the production ``deterministic_gate`` rather than
reimplementing it. A small executor adapter translates the deep
test's ``SandboxSession`` shell-style interface into the argv-and-
timeout interface the gate expects, so the gate's check logic stays
in one place.

The adapter's ``write_file`` writes on the host. The sandbox bind-
mounts the clone at the same absolute path, so a host-side write is
visible inside the sandbox with no translation. This is the same
property the Phase 8 ``pwd`` proof established.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from codeguardian.core.agents._verifier_common import ExecutionOutcome
from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.orchestrator.deterministic_gate import (
    DeterministicGateResult,
    deterministic_gate,
)
from codeguardian.core.providers import ApplyProvider, LocalProvider
from codeguardian.interfaces.apply import ConsentRecord

if TYPE_CHECKING:
    from ._sandbox import SandboxSession

__all__ = [
    "SandboxVerifyAgent",
    "SandboxVerifyInput",
    "SandboxVerifyResult",
    "SessionExecutorAdapter",
]


_CONSENT_RUN_ID = "deep-test-orchestrator"


@dataclass(frozen=True, slots=True)
class SandboxVerifyInput:
    """Input the loop hands the sandbox verify agent.

    The loop's input factory populates ``results`` with every prior
    slot's output. The agent reads the writer, tester, and blast
    results from it by name; a missing one is a graph configuration
    error the agent surfaces as a phase failure.
    """

    results: dict[str, Any]
    clone_root: Path
    target_path: str
    target_symbol: str
    characterization_test_path: str


@dataclass(frozen=True, slots=True)
class SandboxVerifyResult:
    """Output the sandbox verify agent writes to the context.

    Attributes:
        diff: The unified diff the gate verified.
        test_passed: True when the characterization test exited zero.
        rollback_ok: True when the applied diff was rolled back
            cleanly. A false here means the working tree may be dirty.
        gate_result: The full ``DeterministicGateResult`` for the
            decision log.
        writer_candidate_source: The exact source the writer produced
            for the candidate the agent chose, so a later phase can
            reuse it without re-reading the writer result.
        blast: The blast result, carried forward for the verifiers.
        tester: The tester result, carried forward for the verifiers.
    """

    diff: str
    test_passed: bool
    rollback_ok: bool
    gate_result: DeterministicGateResult
    writer_candidate_source: str
    blast: Any
    tester: Any


class SessionExecutorAdapter:
    """Adapter from ``SandboxSession`` to the gate's executor protocol.

    The gate expects ``run(argv, timeout_s)`` and ``write_file(path,
    content)``. The session expects a shell command string. The adapter
    joins the argv into a shell command, runs it inside the session,
    and maps the result. ``write_file`` writes on the host; the bind
    mount makes the file visible inside the sandbox.

    Args:
        session: The deep test's active sandbox session.
        host_clone_root: The host path the session's ``clone``
            directory is bind-mounted from.
        cwd: The working directory name the session uses for commands.
            The deep test always uses ``"clone"``.
    """

    def __init__(
        self,
        session: SandboxSession,
        host_clone_root: Path,
        *,
        cwd: str = "clone",
    ) -> None:
        self._session = session
        self._host_clone_root = Path(host_clone_root)
        self._cwd = cwd

    def write_file(self, path: str, content: str) -> None:
        """Write ``content`` into the sandbox at ``path`` via the host."""
        target = self._host_clone_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def run(
        self,
        argv: list[str],
        *,
        timeout_s: float = 120.0,
    ) -> ExecutionOutcome:
        """Run ``argv`` inside the session and return the outcome.

        The argv is joined with a single space because the gate builds
        only simple commands (``git apply --check`` and
        ``python3 run_pytest.py <path> ...``). No shell
        metacharacters appear in those commands, so the join is safe.
        """
        command = " ".join(argv)
        result = self._session.run(
            command,
            cwd=self._cwd,
            timeout_ms=int(timeout_s * 1000),
        )
        return ExecutionOutcome(
            exit_code=result.exit_code,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )


def _splice_candidate(
    original_source: str,
    function_name: str,
    candidate_source: str,
) -> str | None:
    """Replace the target function with ``candidate_source``.

    Returns ``None`` when the original function cannot be located or
    the spliced result does not parse. A ``None`` means the candidate
    cannot be applied to this file; the agent treats it as a gate
    failure rather than a phase failure.
    """
    import ast

    try:
        tree = ast.parse(original_source)
    except SyntaxError:
        return None
    original_segment: str | None = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            original_segment = ast.get_source_segment(original_source, node)
            break
    if original_segment is None:
        return None
    spliced = original_source.replace(original_segment, candidate_source, 1)
    try:
        ast.parse(spliced)
    except SyntaxError:
        return None
    return spliced


def _build_consent(
    diff: str,
    *,
    run_id: str = _CONSENT_RUN_ID,
    rollback_ref: str = "",
) -> ConsentRecord:
    """Build a consent record that matches ``diff``."""
    diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    return ConsentRecord(
        consent_id=f"deep-{diff_hash[:12]}",
        run_id=run_id,
        granted_at=datetime.now(UTC),
        granted_by="deep-test",
        scope="apply",
        diff_hash=diff_hash,
        rollback_ref=rollback_ref,
    )


class SandboxVerifyAgent:
    """The slot agent that owns the deterministic gate in the deep test.

    The agent is registered once. Every dispatch reads the writer's
    chosen candidate, splices it, builds the diff, and runs the
    production deterministic gate. It does not run the LLM verifiers;
    that is the verifier stack agent's job, and it runs next.

    Args:
        provider: The local repository provider, pointed at the clone.
        bridge: The active JSON-RPC bridge, for the ApplyProvider.
        audit_path: The audit log path the ApplyProvider appends to.
        session: The deep test's sandbox session.
        host_clone_root: The host clone root.
        run_id: The run identifier recorded on the consent record.
    """

    name = "sandbox_verify"
    phase = "sandbox_verify"

    def __init__(
        self,
        *,
        provider: LocalProvider,
        bridge: Any,
        audit_path: Path,
        session: SandboxSession,
        host_clone_root: Path,
        run_id: str = _CONSENT_RUN_ID,
    ) -> None:
        self._provider = provider
        self._bridge = bridge
        self._audit_path = audit_path
        self._session = session
        self._host_clone_root = Path(host_clone_root)
        self._run_id = run_id

    async def run(self, input: SandboxVerifyInput) -> SandboxVerifyResult:
        """Run the deterministic gate and return the verified diff.

        When the writer refused — ``is_refusal=True`` and no
        candidates — this method returns an empty-diff result instead
        of raising. The downstream verifier agents and skill curator
        recognize an empty diff and short-circuit to a refused
        verdict, so the pipeline completes cleanly with a refusal
        recorded at the writer slot rather than crashing at
        ``sandbox_verify``.
        """
        writer = input.results.get("writer")
        blast = input.results.get("blast")
        tester = input.results.get("tester")

        if writer is None or not getattr(writer, "candidates", None):
            empty_gate = DeterministicGateResult(
                passed=False,
                checks=(),
                stderr_tail="writer produced no candidates",
                diff_hash="",
            )
            return SandboxVerifyResult(
                diff="",
                test_passed=False,
                rollback_ok=True,
                gate_result=empty_gate,
                writer_candidate_source="",
                blast=blast,
                tester=tester,
            )

        candidate_source = writer.candidates[0].source
        target_file = input.clone_root / input.target_path
        original_source = target_file.read_text(encoding="utf-8")

        spliced = _splice_candidate(
            original_source,
            input.target_symbol,
            candidate_source,
        )
        if spliced is None:
            empty_gate = DeterministicGateResult(
                passed=False,
                checks=(),
                stderr_tail="candidate does not splice cleanly",
                diff_hash="",
            )
            return SandboxVerifyResult(
                diff="",
                test_passed=False,
                rollback_ok=True,
                gate_result=empty_gate,
                writer_candidate_source=candidate_source,
                blast=blast,
                tester=tester,
            )

        from codeguardian.interfaces.repository import FileChange

        changes = [FileChange(path=input.target_path, new_content=spliced)]
        try:
            diff = self._provider.get_diff(changes)
        except CodeGuardianError:
            diff = ""
        if not diff:
            empty_gate = DeterministicGateResult(
                passed=False,
                checks=(),
                stderr_tail="diff is empty after splice",
                diff_hash="",
            )
            return SandboxVerifyResult(
                diff="",
                test_passed=False,
                rollback_ok=True,
                gate_result=empty_gate,
                writer_candidate_source=candidate_source,
                blast=blast,
                tester=tester,
            )

        executor = SessionExecutorAdapter(self._session, self._host_clone_root)
        applier = ApplyProvider(self._provider, self._bridge, self._audit_path)
        clone_root = input.clone_root

        def apply_fn(d: str) -> str:
            consent = _build_consent(d, run_id=self._run_id)
            result = applier.apply(str(clone_root), d, consent)
            if not result.success:
                raise CodeGuardianError("SYS_INTERNAL", "apply failed inside the gate")
            return result.rollback_ref

        def rollback_fn(ref: str) -> None:
            applier.rollback(ref)

        gate_result = deterministic_gate(
            executor=executor,
            diff=diff,
            test_path=input.characterization_test_path,
            apply_fn=apply_fn,
            rollback_fn=rollback_fn,
        )

        return SandboxVerifyResult(
            diff=diff,
            test_passed=gate_result.passed,
            rollback_ok=True,
            gate_result=gate_result,
            writer_candidate_source=candidate_source,
            blast=blast,
            tester=tester,
        )
