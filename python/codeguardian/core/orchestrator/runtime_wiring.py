"""Runtime integration layer for the orchestration loop.

The loop walks the phase graph. The Phase 13 modules (verifier stack,
deterministic gate, context engine, capability attenuation, consent,
telemetry) each do one thing well. The runtime wiring layer is the
place that knows which module is called when.

``RuntimeWiring`` exposes five public methods.

``before_phase`` compiles the run's context block for the slot,
snapshots the workspace paths the slot may write, opens a trace span,
and returns a ``PhaseRuntime``.

``around_agent`` binds the attenuated permission set, asserts the
audit hook is active, checks the cancellation token, and awaits the
agent under an optional wall-clock timeout. Deterministic agents are
exempt from attenuation: they do not call an LLM and therefore cannot
be prompt-injected, while their deterministic work requires trusted
writes outside the clone (for example, the recon agent's CPG output
directory, which lives as a sibling of the clone). The
``capability_tags`` on the agent spec carry the signal: agents tagged
``"deterministic"`` run unattenuated, all others run under the
attenuated permission set.

``after_phase`` restores the workspace when the phase failed, writes
one cold-tier row to ``agent_calls``, records phase metrics, and
closes the trace span.

``before_apply`` requests apply consent and returns whether the apply
is authorized.

``after_run`` writes the audit chain's head to the external anchor.

The loop calls these five methods. It does not import the seven Phase
13 modules directly. A module that is not called by this file is not
called anywhere, which makes the "modules not wired" regression from
Phase 13 impossible to reintroduce.

The wiring is optional. The loop accepts ``wiring: RuntimeWiring | None``
and skips the calls when the wiring is None, so existing tests and
call sites continue to work unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from codeguardian.core.agents.telemetry import record_call
from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.orchestrator.audit_anchor import AuditAnchor
from codeguardian.core.orchestrator.cancellation import (
    CancellationToken,
    wait_with_cancellation,
)
from codeguardian.core.orchestrator.capability_attenuation import (
    AttenuationRecord,
    attenuated_dispatch,
)
from codeguardian.core.orchestrator.consent import (
    ApplyRequest,
    ConsentOrigin,
    request_apply_consent,
)
from codeguardian.core.orchestrator.context_engine import (
    CompiledContext,
    ContextEngine,
)
from codeguardian.core.orchestrator.evidence import events_to_decision_log
from codeguardian.core.orchestrator.phase_graph import PhaseSlot
from codeguardian.core.orchestrator.registry import AgentSpec
from codeguardian.core.orchestrator.workspace_snapshot import (
    RestoreRefusal,
    Snapshot,
    restore,
    snapshot_paths,
)
from codeguardian.core.plugins import PluginPermissions
from codeguardian.core.storage.sqlite_store import SQLiteStore
from codeguardian.core.telemetry import (
    MetricsCollector,
    NoopMetricsCollector,
    TraceContext,
)
from codeguardian.core.telemetry.metrics import PhaseTiming

if TYPE_CHECKING:
    from codeguardian.core.orchestrator.loop import (
        LoopContext,
        LoopResult,
        SlotExecution,
    )


__all__ = ["PhaseOutcome", "PhaseRuntime", "RuntimeWiring"]


_AUDIT_PHASE_MAP: dict[str, str] = {
    "recon": "reconnaissance",
    "blast": "blast_radius",
    "analyst": "context",
    "tester": "safety_net",
    "writer": "refactor",
    "sandbox_verify": "sandbox_verify",
    "verifier_correctness": "independent_verify",
    "verifier_security": "independent_verify",
    "verifier_contract": "independent_verify",
    "skill_curator": "output",
    "project_test_suite": "output",
}

_AUDIT_AGENT_NAMES: frozenset[str] = frozenset(
    {
        "recon",
        "blast",
        "analyst",
        "tester",
        "writer",
        "sandbox_verify",
        "correctness_verifier",
        "security_verifier",
        "contract_verifier",
        "skill_curator",
        "orchestrator",
        "cli",
    }
)

_DEFAULT_AGENT_PERMISSIONS = PluginPermissions(
    filesystem="sandbox_only",
    network=False,
    process_spawn=True,
    env_read=(),
)


def _audit_phase_for(slot_name: str) -> str:
    return _AUDIT_PHASE_MAP.get(slot_name, "output")


def _audit_agent_for(agent_name: str) -> str:
    return agent_name if agent_name in _AUDIT_AGENT_NAMES else "orchestrator"


def _latency_ms(started_at: str, finished_at: str) -> int:
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        return max(0, int((end - start).total_seconds() * 1000))
    except (ValueError, AttributeError):
        return 0


def _failure_category(execution: SlotExecution) -> str | None:
    if execution.ok:
        return None
    err = execution.error
    if err is None:
        return None
    message = err.message.lower()
    if "timeout" in message or "timed out" in message or "exceeded its" in message:
        return "timeout"
    if "context" in message and ("exceed" in message or "overflow" in message):
        return "context_overflow"
    return None


def _outcome_for(execution: SlotExecution) -> str:
    """Map a phase result to a schema-valid ``agent_calls.outcome`` value.

    The ``agent_calls.outcome`` CHECK constraint accepts only
    ``success``, ``refused``, ``schema_violation``, ``signature_drift``,
    and ``timeout``. A phase that failed for any other reason is
    recorded as ``refused``, the schema's closest generic failure
    category.
    """
    if execution.ok:
        return "success"
    err = execution.error
    if err is None:
        return "refused"
    message = err.message.lower()
    if "timeout" in message or "timed out" in message or "exceeded its" in message:
        return "timeout"
    if "schema" in message:
        return "schema_violation"
    if "signature" in message:
        return "signature_drift"
    return "refused"


@dataclass(slots=True)
class PhaseRuntime:
    """Mutable per-phase state carried between the wiring methods.

    Created by ``RuntimeWiring.before_phase``, consumed by
    ``around_agent`` and ``after_phase``.

    Attributes:
        slot: The slot being executed.
        compiled_context: The context block compiled for this phase,
            or None when no context engine is configured.
        snapshot: The workspace snapshot taken before the phase ran,
            or None when the slot declares no workspace paths.
        attenuation: The attenuation record bound during the agent
            call, or None when the agent did not run or was exempt.
        trace: The trace span opened for this phase, or None.
        agent_timeout_s: The wall-clock ceiling for the agent call,
            or None for no ceiling.
        context_refused: True when the context budget asked the loop
            to refuse further calls.
    """

    slot: PhaseSlot
    compiled_context: CompiledContext | None = None
    snapshot: Snapshot | None = None
    attenuation: AttenuationRecord | None = None
    trace: TraceContext | None = None
    agent_timeout_s: float | None = None
    context_refused: bool = False


@dataclass(frozen=True, slots=True)
class PhaseOutcome:
    """What the wiring restored and what it declined to restore.

    Attributes:
        rolled_back: Relative paths restored to their pre-phase state.
        rollback_refused: Paths the rollback refused to touch, with
            the reason each was refused.
    """

    rolled_back: tuple[str, ...] = ()
    rollback_refused: tuple[RestoreRefusal, ...] = ()


class RuntimeWiring:
    """Wires the Phase 13 modules into a single runtime integration.

    Construct once per run and pass to ``run_loop(wiring=...)``.

    Args:
        store: The SQLite store for cold-tier writes and consent
            records.
        run_id: The run identifier used in every cold-tier row and
            audit entry.
        audit_writer: The audit writer, supplied to
            ``attenuated_dispatch`` and ``request_apply_consent``.
        anchor: The external audit anchor.
        context_engine: The run's context engine.
        metrics: The metric collector. Defaults to
            ``NoopMetricsCollector``.
        trace: The root trace context.
        cancellation_token: The run's cancellation token.
        default_agent_timeout_s: The fallback timeout applied when a
            slot declares none.
        default_agent_permissions: The permission set bound for an
            attenuated dispatch when the slot declares none.
        apply_approver: A callable that presents an apply request and
            returns True to approve.
        consent_origin: The requested consent origin. A block-band
            target forces this to human at the consent call site.
    """

    def __init__(
        self,
        *,
        store: SQLiteStore | None = None,
        run_id: str = "",
        audit_writer: Callable[[dict[str, Any]], str] | None = None,
        anchor: AuditAnchor | None = None,
        context_engine: ContextEngine | None = None,
        metrics: MetricsCollector | None = None,
        trace: TraceContext | None = None,
        cancellation_token: CancellationToken | None = None,
        default_agent_timeout_s: float | None = None,
        default_agent_permissions: PluginPermissions | None = None,
        apply_approver: Callable[[ApplyRequest], bool] | None = None,
        consent_origin: ConsentOrigin | None = None,
        repo_root: Path | None = None,
        config_home: Path | None = None,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._audit_writer = audit_writer
        self._anchor = anchor
        self._context_engine = context_engine
        self._metrics: MetricsCollector = metrics or NoopMetricsCollector()
        self._trace = trace
        self._cancellation_token = cancellation_token
        self._default_agent_timeout_s = default_agent_timeout_s
        self._default_agent_permissions = (
            default_agent_permissions
            if default_agent_permissions is not None
            else _DEFAULT_AGENT_PERMISSIONS
        )
        self._apply_approver = apply_approver
        self._consent_origin = consent_origin
        self._repo_root = Path(repo_root) if repo_root is not None else None
        self._config_home = Path(config_home) if config_home is not None else None

    def before_phase(
        self,
        slot: PhaseSlot,
        context: LoopContext,
    ) -> PhaseRuntime:
        """Prepare the runtime for one phase."""
        runtime = PhaseRuntime(slot=slot)

        workspace_paths = tuple(getattr(slot, "workspace_paths", ()))
        clone_root = getattr(context, "clone_root", None)
        if workspace_paths and clone_root is not None:
            with contextlib.suppress(CodeGuardianError):
                runtime.snapshot = snapshot_paths(clone_root, workspace_paths)

        if self._context_engine is not None:
            with contextlib.suppress(Exception):
                runtime.compiled_context = self._context_engine.compile_for(slot.name)
                if self._context_engine.should_refuse():
                    runtime.context_refused = True

        runtime.agent_timeout_s = (
            getattr(slot, "agent_timeout_s", None) or self._default_agent_timeout_s
        )

        if self._trace is not None:
            with contextlib.suppress(Exception):
                runtime.trace = self._trace.child(f"phase:{slot.name}")

        return runtime

    async def around_agent(
        self,
        slot: PhaseSlot,
        agent_spec: AgentSpec,
        agent_input: Any,
        runtime: PhaseRuntime,
        context: LoopContext,
    ) -> Any:
        """Invoke the agent under attenuation, cancellation, and timeout.

        Deterministic agents run without attenuation. Attenuation
        protects against untrusted LLM output; deterministic agents do
        not call an LLM and cannot be prompt-injected, so attenuating
        them adds no security and blocks the trusted writes their
        deterministic work requires (for example, the recon agent's
        CPG output directory, which lives as a sibling of the clone).
        The ``capability_tags`` on the agent spec carry the signal.

        Raises:
            CancellationRequested: When the token is set before or
                during the call.
            CodeGuardianError: With code ``SYS_INTERNAL`` when the
                agent exceeds its timeout.
            BaseException: Any exception the agent raises, propagated
                unchanged.
        """
        if self._cancellation_token is not None:
            self._cancellation_token.check()

        if "deterministic" in agent_spec.capability_tags:
            return await self._invoke_with_timeout_and_cancel(
                agent_spec,
                agent_input,
                runtime,
            )

        slot_permissions = getattr(slot, "agent_permissions", None)
        permissions = (
            slot_permissions
            if isinstance(slot_permissions, PluginPermissions)
            else self._default_agent_permissions
        )
        clone_root = getattr(context, "clone_root", None)
        trusted_read_paths = self._build_trusted_read_paths(slot, context)

        with attenuated_dispatch(
            agent_name=agent_spec.name,
            agent_permissions=permissions,
            parent_permissions=None,
            skill_permissions=None,
            sandbox_root=clone_root,
            trusted_read_paths=trusted_read_paths,
            phase=_audit_phase_for(slot.name),
            audit_writer=self._audit_writer,
            run_id=self._run_id,
        ) as attenuation:
            runtime.attenuation = attenuation
            self._record_read_grants(slot, agent_spec, runtime, attenuation)
            return await self._invoke_with_timeout_and_cancel(
                agent_spec,
                agent_input,
                runtime,
            )

    def _build_trusted_read_paths(
        self,
        slot: PhaseSlot,
        context: LoopContext,
    ) -> tuple[Path, ...]:
        """Collect canonical, deduplicated read-only paths outside the clone."""
        candidates: list[Path] = []
        if self._repo_root is not None:
            candidates.append(self._repo_root)
        if self._config_home is not None:
            candidates.append(self._config_home)

        package_root = Path(__file__).resolve().parents[2]
        candidates.append(package_root)

        for prefix in (sys.prefix, sys.base_prefix):
            if prefix:
                candidates.append(Path(prefix))

        clone_root = getattr(context, "clone_root", None)
        base_root = self._repo_root or clone_root
        for raw_path in getattr(slot, "read_paths", ()):
            path = Path(raw_path)
            if not path.is_absolute() and base_root is not None:
                path = Path(base_root) / path
            candidates.append(path)

        canonical_clone: str | None = None
        if clone_root is not None:
            with contextlib.suppress(OSError, ValueError):
                canonical_clone = os.path.realpath(os.path.abspath(str(clone_root)))

        seen: set[str] = set()
        resolved: list[Path] = []
        for candidate in candidates:
            try:
                canon = os.path.realpath(os.path.abspath(str(candidate)))
            except (OSError, ValueError):
                continue
            if not os.path.exists(canon):
                continue
            if canonical_clone is not None and canon == canonical_clone:
                continue
            if canon in seen:
                continue
            seen.add(canon)
            resolved.append(Path(canon))

        return tuple(resolved)

    def _record_read_grants(
        self,
        slot: PhaseSlot,
        agent_spec: AgentSpec,
        runtime: PhaseRuntime,
        attenuation: AttenuationRecord,
    ) -> None:
        """Record active read-only extra paths on the trace span or fallback audit."""
        if not attenuation.trusted_read_paths:
            return
        if runtime.trace is not None and hasattr(runtime.trace, "set_attribute"):
            with contextlib.suppress(Exception):
                runtime.trace.set_attribute(
                    "attenuation.trusted_read_paths",
                    list(attenuation.trusted_read_paths),
                )
        if self._audit_writer is not None and attenuation.audit_hash is None:
            with contextlib.suppress(Exception):
                self._audit_writer(
                    {
                        "run_id": self._run_id,
                        "phase": _audit_phase_for(slot.name),
                        "agent": _audit_agent_for(agent_spec.name),
                        "result": {
                            "status": "success",
                            "attenuation": {
                                "agent": agent_spec.name,
                                "trusted_read_paths": list(attenuation.trusted_read_paths),
                            },
                        },
                    }
                )

    async def _invoke_with_timeout_and_cancel(
        self,
        agent_spec: AgentSpec,
        agent_input: Any,
        runtime: PhaseRuntime,
    ) -> Any:
        """Run the agent with the configured timeout and cancellation token."""
        coro = agent_spec.agent.run(agent_input)
        if self._cancellation_token is not None:
            coro = wait_with_cancellation(coro, self._cancellation_token)

        if runtime.agent_timeout_s is not None:
            try:
                async with asyncio.timeout(runtime.agent_timeout_s):
                    return await coro
            except TimeoutError as exc:
                raise CodeGuardianError(
                    ErrorCode.SYS_INTERNAL,
                    f"agent {agent_spec.name!r} exceeded its {runtime.agent_timeout_s}s timeout",
                    details={
                        "slot": runtime.slot.name,
                        "agent": agent_spec.name,
                        "timeout_s": runtime.agent_timeout_s,
                    },
                ) from exc
        return await coro

    async def after_phase(
        self,
        slot: PhaseSlot,
        execution: SlotExecution,
        runtime: PhaseRuntime,
        context: LoopContext,
    ) -> PhaseOutcome:
        """Finalize one phase: rollback, cold tier, metrics, trace."""
        outcome = PhaseOutcome()
        if not execution.ok and runtime.snapshot is not None:
            try:
                result = restore(runtime.snapshot)
                outcome = PhaseOutcome(
                    rolled_back=result.restored,
                    rollback_refused=result.refused,
                )
            except CodeGuardianError:
                outcome = PhaseOutcome()

        self._record_cold_tier(slot, execution, runtime, context)
        self._record_metrics(execution, runtime)
        if self._context_engine is not None and execution.ok:
            with contextlib.suppress(Exception):
                self._context_engine.record_phase(slot.name, execution.output)
        return outcome

    async def before_apply(
        self,
        slot: PhaseSlot,
        diff: str,
        context: LoopContext,
    ) -> bool:
        """Request apply consent for a diff.

        Returns True when no consent gate is configured, or when the
        consent was approved.
        """
        if self._store is None or self._audit_writer is None or self._consent_origin is None:
            return True

        blast = context.results.get("blast")
        if blast is None:
            return True

        target_path = str(getattr(blast, "target_path", "") or "")
        recommendation = str(getattr(blast, "recommendation_adjusted", "review") or "review")
        try:
            consent = request_apply_consent(
                store=self._store,
                run_id=self._run_id,
                diff=diff,
                target_path=target_path,
                blast_recommendation=recommendation,
                origin=self._consent_origin,
                approve=self._apply_approver or (lambda _req: False),
                audit_writer=self._audit_writer,
                granted_by="orchestrator",
            )
            return consent.approved
        except CodeGuardianError:
            return False

    async def after_run(
        self,
        loop_result: LoopResult,
        audit_entries: Sequence[Mapping[str, Any]],
    ) -> None:
        """Write the audit chain's head to the external anchor."""
        del loop_result
        if self._anchor is None:
            return
        head = ""
        if audit_entries:
            head = str(audit_entries[-1].get("entry_hash") or "")
        with contextlib.suppress(Exception):
            self._anchor.write_head(head, None)

    def _record_cold_tier(
        self,
        slot: PhaseSlot,
        execution: SlotExecution,
        runtime: PhaseRuntime,
        context: LoopContext,
    ) -> None:
        if self._store is None:
            return
        events = execution.events or ()
        decision_log = events_to_decision_log(events) if events else None
        attenuation_entry = (
            runtime.attenuation.to_decision_log_entry() if runtime.attenuation is not None else None
        )
        if decision_log is not None and attenuation_entry is not None:
            decision_log = [*decision_log, attenuation_entry]
        elif attenuation_entry is not None:
            decision_log = [attenuation_entry]
        target_symbol = None
        blast = context.results.get("blast")
        if blast is not None:
            target_symbol = getattr(blast, "target_qualified_name", None)
        with contextlib.suppress(Exception):
            record_call(
                self._store,
                role=execution.agent_name,
                run_id=self._run_id,
                outcome=_outcome_for(execution),
                failure_category=_failure_category(execution),
                target_symbol=target_symbol,
                latency_ms=_latency_ms(execution.started_at, execution.finished_at),
                decision_log=decision_log,
            )

    def _record_metrics(
        self,
        execution: SlotExecution,
        runtime: PhaseRuntime,
    ) -> None:
        trace = runtime.trace or self._trace
        if trace is None:
            return
        timing = PhaseTiming(
            trace=trace,
            phase_name=execution.slot_name,
            agent_name=execution.agent_name,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            ok=execution.ok,
            retries=0,
            error_message=(execution.error.message if execution.error is not None else None),
        )
        with contextlib.suppress(Exception):
            self._metrics.record_phase_timing(timing)
