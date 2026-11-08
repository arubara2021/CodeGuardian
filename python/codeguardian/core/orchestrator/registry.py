"""Agent registry and slot resolution.

The registry is the piece that makes "adding an agent never touches
the loop" true. It maps a slot name from a PhaseGraph to a registered
agent by three structural checks in a fixed order: capability tags,
input schema compatibility, output schema compatibility. It never
looks at an agent class name. The loop asks the registry to resolve a
slot; the registry answers with an AgentSpec or raises a typed error
naming the specific failure.

An AgentSpec carries eight fields. ``name`` is the role name written
to agent_calls.role; migration 007 makes that column a foreign key
into a ``registered_agents`` table, so the name must satisfy the
pattern the registry enforces at registration time. ``agent`` is the
instantiated object with a ``run`` method; the resolver introspects
the run method's annotations for the schema-compatibility check.
``capability_tags`` is the set matched by subset against the slot's
requirements. ``input_type`` and ``output_type`` are recorded so a
caller can inspect them without re-introspecting the agent.
``backend`` describes the execution model (in-process for v1; the
field allows a future CLI, ACP, or OpenAI-API backend without
changing the resolver). ``priority`` breaks ties when more than one
agent satisfies a slot; higher wins.

The resolver does three things. First, for each slot in a PhaseGraph
it collects every agent whose capability_tags is a superset of the
slot's. Second, it filters by schema compatibility: a slot that
declares ``Any`` for input or output accepts anything; a slot that
declares a specific type accepts only agents whose run annotation is
structurally compatible. Third, for the surviving candidates it picks
the highest-priority agent, breaking remaining ties by registration
order. A missing required slot is a graph-start error, not a
mid-pipeline crash. A missing optional slot is recorded in the
resolution's ``unresolved`` map and the loop skips it.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, get_type_hints

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .phase_graph import PhaseGraph, PhaseSlot

__all__ = [
    "AgentRegistry",
    "AgentSpec",
    "SlotResolution",
    "SlotResolver",
]


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_VALID_BACKENDS: frozenset[str] = frozenset({"in-process", "cli", "acp", "openai-api"})


def _read_run_types(agent: Any) -> tuple[Any, Any]:
    """Return (input_type, output_type) from an agent's run method.

    Returns ``(Any, Any)`` when the method is missing, its annotations
    cannot be resolved, or the method is not callable. The resolver
    treats ``Any`` as "no constraint" so a partially-annotated agent
    still resolves.
    """
    run = getattr(agent, "run", None)
    if run is None or not callable(run):
        return Any, Any
    try:
        hints = get_type_hints(run)
    except (NameError, TypeError):
        # get_type_hints fails when a forward reference cannot be
        # resolved. Fall back to the raw annotations mapping, which
        # may contain strings; the compatibility check treats strings
        # as opaque and returns True.
        raw = getattr(run, "__annotations__", {})
        return raw.get("input", Any), raw.get("return", Any)
    try:
        params = list(inspect.signature(run).parameters.values())
    except (TypeError, ValueError):
        return Any, Any
    # The first non-self parameter is "input" by contract.
    input_annotation: Any = Any
    for param in params:
        if param.name == "self":
            continue
        input_annotation = hints.get(param.name, Any)
        break
    output_annotation = hints.get("return", Any)
    return input_annotation, output_annotation


def _is_compatible(agent_type: Any, slot_type: Any) -> bool:
    """Return True when ``agent_type`` satisfies ``slot_type``.

    ``Any`` on either side always passes. When both are classes the
    check is ``issubclass``: the agent's type must be a subclass of
    what the slot expects, because the slot will call the agent with a
    value of ``slot_type``. Any other combination (typing constructs,
    forward-reference strings, mixed) passes; the resolver is
    permissive by design and only rejects the clearly incompatible
    case of two unrelated concrete classes.
    """
    if agent_type is Any or slot_type is Any:
        return True
    if agent_type is slot_type:
        return True
    try:
        if isinstance(agent_type, type) and isinstance(slot_type, type):
            return issubclass(agent_type, slot_type)
    except TypeError:
        return True
    return True


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """One registered agent.

    Attributes:
        name: The role name written to agent_calls.role. Must match
            ``^[a-z][a-z0-9_]{0,63}$`` and must equal the agent's own
            ``name`` class attribute so a caller cannot register an
            instance under a name it does not claim.
        agent: The instantiated agent object.
        capability_tags: The tags the agent provides.
        input_type: The type the run method accepts. Read from the
            run method's input annotation, or ``Any``.
        output_type: The type the run method returns, or ``Any``.
        backend: One of ``"in-process"``, ``"cli"``, ``"acp"``,
            ``"openai-api"``.
        priority: Tiebreaker. Higher wins.
        description: A short human-readable description.
    """

    name: str
    agent: Any
    capability_tags: frozenset[str]
    input_type: Any = Any
    output_type: Any = Any
    backend: str = "in-process"
    priority: int = 0
    description: str = ""

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                f"agent name {self.name!r} does not match the "
                "registry pattern ^[a-z][a-z0-9_]{0,63}$; names go to "
                "agent_calls.role which is a foreign key in migration 007",
                details={"name": self.name},
            )
        if self.backend not in _VALID_BACKENDS:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                f"agent {self.name!r} declares unknown backend "
                f"{self.backend!r}; valid: {sorted(_VALID_BACKENDS)}",
                details={"name": self.name, "backend": self.backend},
            )
        agent_name = getattr(self.agent, "name", None)
        if agent_name is not None and agent_name != self.name:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                f"AgentSpec name {self.name!r} does not match the agent's own name {agent_name!r}",
                details={
                    "spec_name": self.name,
                    "agent_name": agent_name,
                },
            )


@dataclass(frozen=True, slots=True)
class SlotResolution:
    """The resolver's outcome for one PhaseGraph.

    Attributes:
        resolved: Every slot that has at least one compatible agent,
            keyed by slot name.
        unresolved: Optional slots that no registered agent satisfies,
            keyed by slot name with a short reason as the value.
    """

    resolved: Mapping[str, AgentSpec]
    unresolved: Mapping[str, str] = field(default_factory=dict)

    def require(self, slot_name: str) -> AgentSpec:
        """Return the resolved agent for ``slot_name``.

        Raises:
            KeyError: If the slot was not resolved.
        """
        try:
            return self.resolved[slot_name]
        except KeyError as exc:
            raise KeyError(
                f"slot {slot_name!r} has no resolved agent; resolved slots: {sorted(self.resolved)}"
            ) from exc


class AgentRegistry:
    """In-memory registry of AgentSpec entries."""

    def __init__(self) -> None:
        self._specs: dict[str, AgentSpec] = {}
        self._order: list[str] = []

    def register(self, spec: AgentSpec) -> None:
        """Register an agent spec.

        Raises:
            CodeGuardianError: With code ``SYS_INVARIANT_VIOLATED``
                when the name is already taken.
        """
        if spec.name in self._specs:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                f"agent {spec.name!r} is already registered",
                details={"name": spec.name},
            )
        self._specs[spec.name] = spec
        self._order.append(spec.name)

    def unregister(self, name: str) -> None:
        """Remove an agent spec. No-op if not registered."""
        self._specs.pop(name, None)
        if name in self._order:
            self._order.remove(name)

    def get(self, name: str) -> AgentSpec:
        """Return the spec for ``name``.

        Raises:
            CodeGuardianError: With code ``SYS_INVARIANT_VIOLATED``
                when the name is not registered.
        """
        try:
            return self._specs[name]
        except KeyError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                f"agent {name!r} is not registered; registered: {sorted(self._specs)}",
                details={"name": name},
            ) from exc

    def list_all(self) -> tuple[AgentSpec, ...]:
        """Return every registered spec in registration order."""
        return tuple(self._specs[name] for name in self._order)

    def names(self) -> tuple[str, ...]:
        """Return every registered name in registration order."""
        return tuple(self._order)

    def clear(self) -> None:
        """Remove every registration. Used by tests."""
        self._specs.clear()
        self._order.clear()


class SlotResolver:
    """Resolves every slot in a PhaseGraph against the registry."""

    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    def resolve(self, graph: PhaseGraph) -> SlotResolution:
        """Resolve every slot in ``graph``.

        Raises:
            CodeGuardianError: With code ``SYS_INVARIANT_VIOLATED``
                when a required slot has no compatible agent, or when
                more than one agent ties at the highest priority.
        """
        resolved: dict[str, AgentSpec] = {}
        unresolved: dict[str, str] = {}
        specs = self._registry.list_all()

        for slot in graph.slots:
            candidates = [spec for spec in specs if self._matches(slot, spec)]
            if not candidates:
                if slot.required:
                    raise CodeGuardianError(
                        ErrorCode.SYS_INVARIANT_VIOLATED,
                        f"required slot {slot.name!r} has no "
                        "registered agent satisfying its capability "
                        "tags and schema contract",
                        details={
                            "slot": slot.name,
                            "required_tags": sorted(slot.capability_tags),
                            "registered_agents": [
                                {
                                    "name": spec.name,
                                    "tags": sorted(spec.capability_tags),
                                }
                                for spec in specs
                            ],
                        },
                    )
                unresolved[slot.name] = (
                    "no registered agent satisfies the capability tags and schema contract"
                )
                continue

            # Ambiguity check: more than one agent at the highest
            # priority means the graph author forgot to give one a
            # higher priority. Raise rather than pick arbitrarily.
            highest = max(spec.priority for spec in candidates)
            top = [spec for spec in candidates if spec.priority == highest]
            if len(top) > 1:
                raise CodeGuardianError(
                    ErrorCode.SYS_INVARIANT_VIOLATED,
                    f"slot {slot.name!r} is ambiguous: {len(top)} agents share priority {highest}",
                    details={
                        "slot": slot.name,
                        "tied_agents": [spec.name for spec in top],
                        "priority": highest,
                    },
                )
            resolved[slot.name] = top[0]

        return SlotResolution(
            resolved=dict(resolved),
            unresolved=dict(unresolved),
        )

    def _matches(self, slot: PhaseSlot, spec: AgentSpec) -> bool:
        """Return True when ``spec`` can fill ``slot``.

        Three checks in fixed order: capability tag subset, input
        schema compatibility, output schema compatibility.
        """
        if not slot.capability_tags.issubset(spec.capability_tags):
            return False
        if not _is_compatible(spec.input_type, slot.input_schema):
            return False
        return _is_compatible(spec.output_type, slot.output_schema)
