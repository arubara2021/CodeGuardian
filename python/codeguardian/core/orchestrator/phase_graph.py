"""Phase graph data model.

A PhaseGraph describes the ordered set of slots a pipeline walks, the
transitions between them, and the entry point. The graph is data, not
code: the orchestrator reads a PhaseGraph and walks it, but never
branches on a mode. Adding a new pipeline (greenfield, upgrade) is a
new PhaseGraph constant, not a new code path.

A PhaseSlot describes one step. It carries a unique name, the
capability tags any agent filling it must provide, an input schema and
output schema that the agent's registry entry must satisfy
structurally, a ``required`` flag that lets optional slots be skipped
when no agent is registered, an optional gate name, an optional blast
band hint, and a phase_name override for the phase_executions table.

A PhaseTransition describes one edge. The ``transitions`` mapping is
from a slot name to a tuple of possible next slot names. A tuple
containing ``None`` means the pipeline may terminate after that slot.
A tuple of length greater than one means a branch point the
orchestrator resolves based on evidence, not on the model.

``validate_graph`` rejects seven failure modes:

1. Duplicate slot names.
2. An entry point that does not name a slot.
3. A transition that names a slot not in the graph.
4. A slot that declares an input schema but no output schema, or
   vice versa. A slot must declare both or neither.
5. A required slot that is not reachable from the entry point.
6. A graph where no terminal is reachable from the entry (it would
   loop forever).
7. A cycle formed entirely by single-edge transitions. Branch points
   can be broken by evidence; a mandatory loop cannot.

The module has no imports outside the standard library and performs no
I/O. Every validation failure produces a ``GraphValidationError``
carrying every distinct failure as a separate entry in ``failures`` so
a caller can fix more than one issue per attempt.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

__all__ = [
    "GraphValidationError",
    "PhaseGraph",
    "PhaseSlot",
    "PhaseTransition",
    "validate_graph",
]


class GraphValidationError(ValueError):
    """Raised when a PhaseGraph fails structural validation.

    Attributes:
        failures: Every distinct failure the validator found, in the
            order it found them. Empty only if the error is constructed
            directly rather than by ``validate_graph``.
    """

    def __init__(self, message: str, *, failures: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.failures = failures


@dataclass(frozen=True, slots=True)
class PhaseSlot:
    """One step in a pipeline graph.

    Attributes:
        name: Unique slot name. Never an agent class name; slots are
            matched to agents by the registry using capability tags
            and schema compatibility.
        required: When False, the slot may be skipped if the registry
            cannot resolve it. When True, a missing agent is a
            graph-start error.
        capability_tags: Tags any agent filling this slot must provide.
            Matched by subset against the agent's declared tag set.
        input_schema: The typed input the slot expects to receive.
            ``Any`` means the slot declares no input contract; the
            registry resolver passes whatever the previous slot
            produced.
        output_schema: The typed output the slot produces. ``Any``
            means the slot declares no output contract.
        gate: The name of the phase gate pair (entry plus exit) applied
            around the slot. ``None`` means no gate.
        blast_band: A static default blast band the routing layer may
            use when the runtime does not supply one. ``None`` means
            the router uses the runtime band from the blast report.
        phase_name: The value written to ``phase_executions.phase_name``
            when this slot runs. Defaults to the slot name.
    """

    name: str
    required: bool = True
    capability_tags: frozenset[str] = frozenset()
    input_schema: Any = Any
    output_schema: Any = Any
    gate: str | None = None
    blast_band: Literal["proceed", "review", "block"] | None = None
    phase_name: str | None = None

    def resolved_phase_name(self) -> str:
        """Return the name written to phase_executions.phase_name."""
        return self.phase_name or self.name


@dataclass(frozen=True, slots=True)
class PhaseTransition:
    """One edge between two slots.

    Attributes:
        src: The slot that just completed.
        dst: The next slot. ``None`` means the pipeline terminates
            after ``src``.
    """

    src: str
    dst: str | None


@dataclass(frozen=True, slots=True)
class PhaseGraph:
    """An ordered, validated pipeline graph.

    Attributes:
        name: The graph's name for logging and error messages.
        entry: The slot the pipeline starts at.
        slots: Ordered tuple of slots. Order is a display hint; the
            ``transitions`` mapping decides execution.
        transitions: Mapping from slot name to a tuple of possible
            next slot names. See module docstring for semantics.
        description: A human-readable description.
    """

    name: str
    entry: str
    slots: tuple[PhaseSlot, ...]
    transitions: Mapping[str, tuple[str | None, ...]]
    description: str = ""

    def __post_init__(self) -> None:
        # Freeze transitions so callers cannot mutate at runtime.
        object.__setattr__(
            self,
            "transitions",
            MappingProxyType({k: tuple(v) for k, v in dict(self.transitions).items()}),
        )

    def slot(self, name: str) -> PhaseSlot:
        """Return the slot with the given name.

        Raises:
            KeyError: If no slot has that name.
        """
        for slot in self.slots:
            if slot.name == name:
                return slot
        raise KeyError(name)

    def slot_names(self) -> tuple[str, ...]:
        """Return slot names in declaration order."""
        return tuple(s.name for s in self.slots)

    def validate(self) -> None:
        """Run structural validation.

        Raises:
            GraphValidationError: If any of the seven checks fails.
        """
        validate_graph(self)


def validate_graph(graph: PhaseGraph) -> None:
    """Validate a PhaseGraph and raise on any failure.

    Collects every distinct failure before raising so the caller can
    fix them all in one pass.

    Raises:
        GraphValidationError: When at least one failure is found.
    """
    failures: list[str] = []

    # 1. Unique slot names.
    seen: set[str] = set()
    duplicates: set[str] = set()
    for slot in graph.slots:
        if slot.name in seen:
            duplicates.add(slot.name)
        seen.add(slot.name)
    if duplicates:
        failures.append(f"duplicate slot names: {sorted(duplicates)}")

    slot_names = set(seen)

    # 2. Entry point exists.
    if graph.entry not in slot_names:
        failures.append(
            f"entry point {graph.entry!r} is not in slot list; known slots: {sorted(slot_names)}"
        )

    # 3. Transitions reference known slots.
    for src, dests in graph.transitions.items():
        if src not in slot_names:
            failures.append(f"transition from unknown slot {src!r}")
        for dst in dests:
            if dst is not None and dst not in slot_names:
                failures.append(f"transition {src!r} -> {dst!r} targets unknown slot")

    # 4. Slots declare both schemas or neither.
    for slot in graph.slots:
        has_in = slot.input_schema is not Any
        has_out = slot.output_schema is not Any
        if has_in != has_out:
            failures.append(
                f"slot {slot.name!r} declares "
                f"{'an input' if has_in else 'no input'} schema but "
                f"{'an output' if has_out else 'no output'} schema; "
                "a slot must declare both or neither"
            )

    # 5. Every required slot is reachable from entry.
    if graph.entry in slot_names:
        reachable = _reachable_from(graph, graph.entry)
        unreachable_required = [
            s.name for s in graph.slots if s.required and s.name not in reachable
        ]
        if unreachable_required:
            failures.append(f"required slots not reachable from entry: {unreachable_required}")

    # 6. A terminal is reachable from entry.
    if graph.entry in slot_names and not _has_terminal_reachable(graph, graph.entry):
        failures.append("no terminal is reachable from entry; the graph would loop forever")

    # 7. No cycles through mandatory (single-edge) transitions.
    cycles = _find_mandatory_cycles(graph)
    if cycles:
        failures.append(f"mandatory cycles: {cycles}")

    if failures:
        raise GraphValidationError(
            f"PhaseGraph {graph.name!r} failed validation: " + "; ".join(failures),
            failures=tuple(failures),
        )


def _reachable_from(graph: PhaseGraph, start: str) -> set[str]:
    """Return every slot reachable from ``start``."""
    seen: set[str] = set()
    stack: list[str] = [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for nxt in graph.transitions.get(node, ()):
            if nxt is not None and nxt not in seen:
                stack.append(nxt)
    return seen


def _has_terminal_reachable(graph: PhaseGraph, start: str) -> bool:
    """Return True when a slot that permits termination is reachable."""
    seen: set[str] = set()
    stack: list[str] = [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        dests = graph.transitions.get(node, ())
        if not dests or None in dests:
            return True
        for nxt in dests:
            if nxt is not None and nxt not in seen:
                stack.append(nxt)
    return False


def _find_mandatory_cycles(graph: PhaseGraph) -> list[list[str]]:
    """Return any cycles formed entirely by single-edge transitions.

    A transition of length greater than one is a branch point the
    orchestrator resolves based on evidence; a single-edge transition
    is a forced next step. Forced loops cannot be broken by evidence,
    so they indicate a graph bug.
    """
    forced: dict[str, str] = {}
    for src, dests in graph.transitions.items():
        if len(dests) == 1 and dests[0] is not None:
            forced[src] = dests[0]

    cycles: list[list[str]] = []
    visited: set[str] = set()
    on_stack: set[str] = set()
    path: list[str] = []

    # Iterative DFS. Forced chains on a validated graph are short (10-
    # 20 nodes at most); the iterative form keeps the module usable
    # from any thread without relying on the default recursion limit.
    def dfs(start: str) -> None:
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                on_stack.discard(node)
                if path and path[-1] == node:
                    path.pop()
                continue
            if node in on_stack:
                if node in path:
                    idx = path.index(node)
                    cycles.append([*path[idx:], node])
                continue
            if node in visited:
                continue
            visited.add(node)
            on_stack.add(node)
            path.append(node)
            nxt = forced.get(node)
            if nxt is not None:
                stack.append((node, True))
                stack.append((nxt, False))
            else:
                # Terminal for the forced subgraph.
                on_stack.discard(node)
                path.pop()

    for src in forced:
        if src not in visited:
            dfs(src)
    return cycles
