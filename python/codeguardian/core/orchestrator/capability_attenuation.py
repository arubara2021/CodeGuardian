"""Capability attenuation at subagent dispatch.

Every dispatch in the orchestrator runs a subagent with a permission set
that is the intersection of three inputs: the parent's permission set,
the set the agent declares it needs, and the set the skill manifest
declares if the dispatch is skill-scoped. The intersection is a lattice
meet in each dimension — the filesystem level takes the lower rank, the
boolean dimensions are AND-ed, and the environment-key dimension is set
intersection. Authority can be preserved or lost as it moves down the
delegation chain; it can never be gained. This is the monotonic
narrowing invariant, and it is what makes a compromised subagent a
bounded problem rather than an unbounded one.

The computed set is bound for the duration of the dispatch through the
same contextvars the Phase 9 audit hook already reads. That hook is the
enforcement mechanism: while the dispatch is active, any audited syscall
the process performs is checked against the bound set, and a call
outside it raises ``PermissionError``. The attenuation module does not
reimplement enforcement. It computes the set, binds it, and records the
result.

Recording is the second half of the job. Every dispatch writes one audit
entry naming the parent, the agent, the skill, the effective set, and
every dimension that was narrowed. The entry hash is returned by the
caller-supplied audit writer and stored on the attenuation record, so a
caller can log it to ``agent_calls.decision_log`` alongside the phase's
other decisions. An investigator reading the log can reconstruct exactly
which authority was active at every hop, and where it was lost.

The module is pure computation plus context binding. It does not call a
model, touch the network, or write to SQLite. The audit writer is the
caller's, so the transport is the caller's choice.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins import (
    AgentMode,
    FilesystemLevel,
    PluginPermissions,
    agent_mode_active,
    plugin_active,
    sandbox_scope,
)

__all__ = [
    "AttenuationRecord",
    "attenuated_dispatch",
    "intersect_permissions",
    "meet_filesystem",
    "meet_permissions",
]


AuditWriter = Callable[[dict[str, Any]], str]
"""Write one audit entry and return its entry_hash."""

PhaseName = Literal[
    "preflight",
    "reconnaissance",
    "blast_radius",
    "context",
    "safety_net",
    "refactor",
    "sandbox_verify",
    "independent_verify",
    "output",
    "consent",
    "apply",
]

_VALID_PHASES: frozenset[str] = frozenset(
    {
        "preflight",
        "reconnaissance",
        "blast_radius",
        "context",
        "safety_net",
        "refactor",
        "sandbox_verify",
        "independent_verify",
        "output",
        "consent",
        "apply",
    }
)

_AUDIT_AGENT_NAMES: frozenset[str] = frozenset(
    {
        "recon",
        "blast_radius",
        "analyst",
        "tester",
        "writer",
        "correctness_verifier",
        "security_verifier",
        "contract_verifier",
        "skill_curator",
        "orchestrator",
        "cli",
    }
)

_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_FILESYSTEM_RANK: dict[FilesystemLevel, int] = {
    "none": 0,
    "sandbox_only": 1,
    "read_only": 2,
    "full": 3,
}


def meet_filesystem(a: FilesystemLevel, b: FilesystemLevel) -> FilesystemLevel:
    """Return the more restrictive of two filesystem levels.

    The levels form a total order: ``none`` is the most restrictive and
    ``full`` is the least. The meet is the lower-ranked level. Ties
    return ``a`` so the function is deterministic and does not depend on
    argument order beyond the documented tie-break.
    """
    return a if _FILESYSTEM_RANK[a] <= _FILESYSTEM_RANK[b] else b


def meet_permissions(
    a: PluginPermissions,
    b: PluginPermissions,
) -> PluginPermissions:
    """Return the lattice meet of two permission sets.

    The meet is dimension-wise: the filesystem level takes the lower
    rank, the boolean dimensions are AND-ed, and the environment-key
    dimension is the set intersection. The result is the authority a
    caller holding both sets can exercise.
    """
    return PluginPermissions(
        filesystem=meet_filesystem(a.filesystem, b.filesystem),
        network=a.network and b.network,
        process_spawn=a.process_spawn and b.process_spawn,
        env_read=tuple(sorted(set(a.env_read) & set(b.env_read))),
    )


def intersect_permissions(
    *operands: PluginPermissions | None,
) -> PluginPermissions:
    """Fold the meet across every non-None operand.

    A ``None`` operand means "no declaration at this level", so it is
    skipped rather than treated as deny-all. The caller must supply at
    least one non-None operand — normally the agent's declared set,
    which every agent carries.

    Args:
        *operands: Permission sets to intersect. Order does not affect
            the result because the meet is associative and commutative.

    Returns:
        The intersection of every non-None operand.

    Raises:
        CodeGuardianError: With code ``SYS_INVARIANT_VIOLATED`` when
            every operand is ``None``. An empty intersection would be
            meaningless; the agent's declared set is the floor.
    """
    present = [operand for operand in operands if operand is not None]
    if not present:
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            "capability attenuation needs at least one declared "
            "permission set; every operand was None",
        )

    result = present[0]
    for operand in present[1:]:
        result = meet_permissions(result, operand)
    return result


def _constraining_operands(
    effective_value: object,
    baseline_value: object,
    agent: PluginPermissions | None,
    skill: PluginPermissions | None,
    read: Callable[[PluginPermissions], object],
) -> list[str]:
    """Return the operands whose declaration reduced one dimension.

    The baseline is the parent's authority. An operand is named when
    its declared value equals the effective value and differs from the
    baseline, which means that operand is the reason the effective set
    fell below the parent's authority in this dimension. Naming both
    agent and skill when both are equally restrictive is intentional:
    the log should show every source of the reduction.
    """
    sources: list[str] = []
    if agent is not None:
        agent_value = read(agent)
        if agent_value == effective_value and agent_value != baseline_value:
            sources.append("agent")
    if skill is not None:
        skill_value = read(skill)
        if skill_value == effective_value and skill_value != baseline_value:
            sources.append("skill")
    return sources


def _narrowed_dimensions(
    agent: PluginPermissions,
    effective: PluginPermissions,
    parent: PluginPermissions | None,
    skill: PluginPermissions | None,
) -> tuple[str, ...]:
    """Return one note per dimension the meet reduced below the parent.

    The notes are computed against the parent's declared set, because
    that is the baseline the delegation chain started from and what a
    security auditor wants to know was taken away. A dispatch with no
    parent has nothing to attenuate from, so its notes are empty. An
    empty tuple means the child inherited the parent's full authority.
    """
    if parent is None:
        return ()

    notes: list[str] = []

    if effective.filesystem != parent.filesystem:
        sources = _constraining_operands(
            effective.filesystem,
            parent.filesystem,
            agent,
            skill,
            lambda permissions: permissions.filesystem,
        )
        notes.append(
            f"filesystem {parent.filesystem} -> {effective.filesystem} "
            f"({', '.join(sources) or 'intersection'})"
        )

    if effective.network != parent.network:
        sources = _constraining_operands(
            effective.network,
            parent.network,
            agent,
            skill,
            lambda permissions: permissions.network,
        )
        notes.append(
            f"network {parent.network} -> {effective.network} "
            f"({', '.join(sources) or 'intersection'})"
        )

    if effective.process_spawn != parent.process_spawn:
        sources = _constraining_operands(
            effective.process_spawn,
            parent.process_spawn,
            agent,
            skill,
            lambda permissions: permissions.process_spawn,
        )
        notes.append(
            f"process_spawn {parent.process_spawn} -> "
            f"{effective.process_spawn} "
            f"({', '.join(sources) or 'intersection'})"
        )

    dropped_keys = sorted(set(parent.env_read) - set(effective.env_read))
    if dropped_keys:
        notes.append(f"env_read dropped {', '.join(dropped_keys)}")

    return tuple(notes)


@dataclass(frozen=True, slots=True)
class AttenuationRecord:
    """The outcome of one attenuation computation.

    Attributes:
        agent_name: The agent the permission set was computed for.
        parent: The parent's permission set, or ``None`` when the
            dispatch happened with no enclosing permission context.
        agent: The permission set the agent declared.
        skill: The skill manifest's permission set, or ``None`` when
            the dispatch was not skill-scoped.
        effective: The set actually bound for the dispatch.
        narrowed: One note per dimension the meet reduced, relative to
            the parent's authority. Empty when no parent was present or
            when the child inherited the parent's full authority.
        audit_hash: The hash returned by the audit writer, or ``None``
            when no writer was supplied.
        phase: The audit phase the record was written under.
    """

    agent_name: str
    parent: PluginPermissions | None
    agent: PluginPermissions
    skill: PluginPermissions | None
    effective: PluginPermissions
    narrowed: tuple[str, ...]
    audit_hash: str | None
    phase: str

    def describe(self) -> str:
        """Return a one-line summary suitable for a log line."""
        keys = len(self.effective.env_read)
        return (
            f"agent {self.agent_name}: "
            f"filesystem={self.effective.filesystem}, "
            f"network={self.effective.network}, "
            f"process_spawn={self.effective.process_spawn}, "
            f"env_keys={keys}"
        )

    def to_decision_log_entry(self) -> dict[str, object]:
        """Return the record in the shape ``decision_log`` expects.

        The first four keys are the canonical ``DecisionLogEntry``
        fields. The ``attenuation`` key carries the structured detail.
        The column is untyped JSON, so an extra key is stored without a
        schema change.
        """
        reason = "; ".join(self.narrowed) if self.narrowed else "no dimension narrowed"
        return {
            "step": "capability_attenuation",
            "candidate": None,
            "verdict": "attenuated" if self.narrowed else "unchanged",
            "reason": reason,
            "attenuation": {
                "agent": self.agent_name,
                "phase": self.phase,
                "effective": {
                    "filesystem": self.effective.filesystem,
                    "network": self.effective.network,
                    "process_spawn": self.effective.process_spawn,
                    "env_read": list(self.effective.env_read),
                },
                "narrowed": list(self.narrowed),
                "audit_hash": self.audit_hash,
            },
        }


@contextmanager
def attenuated_dispatch(
    *,
    agent_name: str,
    agent_permissions: PluginPermissions,
    parent_permissions: PluginPermissions | None = None,
    skill_permissions: PluginPermissions | None = None,
    sandbox_root: Path | None = None,
    agent_mode: AgentMode | None = None,
    phase: str = "preflight",
    audit_writer: AuditWriter | None = None,
    run_id: str | None = None,
) -> Iterator[AttenuationRecord]:
    """Bind an attenuated permission set around a subagent dispatch.

    The effective set is computed before the block is entered and
    recorded in the audit chain. Inside the block, the Phase 9 context
    vars carry the effective set, so the audit hook enforces it on every
    audited syscall the subagent makes. On exit the previous context is
    restored, which is what makes nested dispatches compose: an inner
    dispatch attenuates from the outer dispatch's effective set rather
    than from the process default.

    Args:
        agent_name: The agent being dispatched. Must match the registry
            name pattern so the audit entry and the telemetry row agree
            on the identifier.
        agent_permissions: The set the agent declares it needs. This is
            the floor; every other operand can only reduce it.
        parent_permissions: The enclosing dispatch's effective set, or
            ``None`` at the top of the delegation chain.
        skill_permissions: The skill manifest's set when the dispatch is
            skill-scoped, or ``None`` when it is not.
        sandbox_root: The clone root. When supplied, it is bound as the
            sandbox scope so the audit hook can enforce the
            ``sandbox_only`` filesystem level.
        agent_mode: The agent mode to bind for the duration of the
            dispatch. When ``None``, the enclosing mode is inherited.
        phase: The audit phase name. Must be one of the eleven values
            the audit-entry schema enumerates.
        audit_writer: Called with a dict-shaped audit entry. Returns
            the entry's hash. When ``None``, no audit entry is written
            and ``AttenuationRecord.audit_hash`` is ``None``.
        run_id: The run identifier recorded on the audit entry.

    Yields:
        The ``AttenuationRecord`` describing the computation.

    Raises:
        CodeGuardianError: ``SYS_INVARIANT_VIOLATED`` when
            ``agent_name`` does not match the registry pattern, when
            ``phase`` is not a valid audit phase, or when every
            permission operand is ``None``.
    """
    if not _AGENT_NAME_RE.match(agent_name):
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            f"agent name {agent_name!r} does not match the registry "
            "pattern ^[a-z][a-z0-9_]{0,63}$",
            details={"agent_name": agent_name},
        )
    if phase not in _VALID_PHASES:
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            f"unknown audit phase {phase!r}; allowed: {sorted(_VALID_PHASES)}",
            details={"phase": phase},
        )

    effective = intersect_permissions(
        parent_permissions,
        agent_permissions,
        skill_permissions,
    )
    narrowed = _narrowed_dimensions(
        agent_permissions,
        effective,
        parent_permissions,
        skill_permissions,
    )

    audit_hash: str | None = None
    if audit_writer is not None:
        audit_agent = agent_name if agent_name in _AUDIT_AGENT_NAMES else "orchestrator"
        audit_hash = audit_writer(
            {
                "run_id": run_id or "",
                "phase": phase,
                "agent": audit_agent,
                "result": {
                    "status": "success",
                    "attenuation": {
                        "agent": agent_name,
                        "effective": {
                            "filesystem": effective.filesystem,
                            "network": effective.network,
                            "process_spawn": effective.process_spawn,
                            "env_read": list(effective.env_read),
                        },
                        "narrowed": list(narrowed),
                    },
                },
            }
        )

    record = AttenuationRecord(
        agent_name=agent_name,
        parent=parent_permissions,
        agent=agent_permissions,
        skill=skill_permissions,
        effective=effective,
        narrowed=narrowed,
        audit_hash=audit_hash,
        phase=phase,
    )

    with ExitStack() as stack:
        stack.enter_context(plugin_active(agent_name, effective))
        if sandbox_root is not None:
            stack.enter_context(sandbox_scope(sandbox_root))
        if agent_mode is not None:
            stack.enter_context(agent_mode_active(agent_mode))
        yield record
