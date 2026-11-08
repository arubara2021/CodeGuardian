"""Tests for the phase graph data model and the agent registry."""

from __future__ import annotations

from typing import Any

import pytest

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.orchestrator.phase_graph import (
    GraphValidationError,
    PhaseGraph,
    PhaseSlot,
    validate_graph,
)
from codeguardian.core.orchestrator.phase_graphs import LEGACY_GRAPH
from codeguardian.core.orchestrator.registry import (
    AgentRegistry,
    AgentSpec,
    SlotResolver,
)


class StubAgent:
    """Minimal agent stub with a name attribute and async run method."""

    def __init__(self, name: str, marker: str | None = None) -> None:
        self.name = name
        self.marker = marker or f"{name}_output"

    async def run(self, input: Any) -> str:
        return self.marker


def make_graph(
    *,
    entry: str = "start",
    slots: tuple[PhaseSlot, ...],
    transitions: dict[str, tuple[str | None, ...]],
) -> PhaseGraph:
    """Build a PhaseGraph for validation tests."""
    return PhaseGraph(
        name="test",
        entry=entry,
        slots=slots,
        transitions=transitions,
        description="",
    )


def make_spec(
    name: str = "agent",
    *,
    tags: frozenset[str] = frozenset(),
    priority: int = 0,
    backend: str = "in-process",
    input_type: Any = Any,
    output_type: Any = Any,
    agent_name: str | None = None,
) -> AgentSpec:
    """Build an AgentSpec for registration and resolution tests."""
    agent = StubAgent(agent_name or name)
    return AgentSpec(
        name=name,
        agent=agent,
        capability_tags=tags,
        input_type=input_type,
        output_type=output_type,
        backend=backend,
        priority=priority,
    )


class TestGraphValidation:
    def test_valid_graph_passes(self) -> None:
        graph = make_graph(
            entry="a",
            slots=(PhaseSlot(name="a"), PhaseSlot(name="b")),
            transitions={"a": ("b",), "b": (None,)},
        )
        validate_graph(graph)

    def test_duplicate_slot_names(self) -> None:
        graph = make_graph(
            entry="a",
            slots=(PhaseSlot(name="a"), PhaseSlot(name="a")),
            transitions={"a": (None,)},
        )
        with pytest.raises(GraphValidationError) as info:
            validate_graph(graph)
        assert any("duplicate" in f for f in info.value.failures)

    def test_missing_entry_point(self) -> None:
        graph = make_graph(
            entry="b",
            slots=(PhaseSlot(name="a"),),
            transitions={"a": (None,)},
        )
        with pytest.raises(GraphValidationError) as info:
            validate_graph(graph)
        assert any("entry" in f for f in info.value.failures)

    def test_transition_to_unknown_slot(self) -> None:
        graph = make_graph(
            entry="a",
            slots=(PhaseSlot(name="a"),),
            transitions={"a": ("b",)},
        )
        with pytest.raises(GraphValidationError) as info:
            validate_graph(graph)
        assert any("unknown slot" in f for f in info.value.failures)

    def test_slot_with_input_but_no_output(self) -> None:
        graph = make_graph(
            entry="a",
            slots=(PhaseSlot(name="a", input_schema=int),),
            transitions={"a": (None,)},
        )
        with pytest.raises(GraphValidationError) as info:
            validate_graph(graph)
        assert any("both or neither" in f for f in info.value.failures)

    def test_slot_with_output_but_no_input(self) -> None:
        graph = make_graph(
            entry="a",
            slots=(PhaseSlot(name="a", output_schema=int),),
            transitions={"a": (None,)},
        )
        with pytest.raises(GraphValidationError) as info:
            validate_graph(graph)
        assert any("both or neither" in f for f in info.value.failures)

    def test_unreachable_required_slot(self) -> None:
        graph = make_graph(
            entry="a",
            slots=(PhaseSlot(name="a"), PhaseSlot(name="b")),
            transitions={"a": (None,)},
        )
        with pytest.raises(GraphValidationError) as info:
            validate_graph(graph)
        assert any("not reachable" in f for f in info.value.failures)

    def test_no_reachable_terminal(self) -> None:
        graph = make_graph(
            entry="a",
            slots=(PhaseSlot(name="a"), PhaseSlot(name="b")),
            transitions={"a": ("b",), "b": ("a",)},
        )
        with pytest.raises(GraphValidationError) as info:
            validate_graph(graph)
        assert any("terminal" in f for f in info.value.failures)

    def test_mandatory_cycle_with_reachable_terminal(self) -> None:
        graph = make_graph(
            entry="a",
            slots=(
                PhaseSlot(name="a"),
                PhaseSlot(name="b"),
                PhaseSlot(name="t"),
            ),
            transitions={
                "a": ("b", "t"),
                "b": ("b",),
                "t": (None,),
            },
        )
        with pytest.raises(GraphValidationError) as info:
            validate_graph(graph)
        assert any("mandatory cycle" in f for f in info.value.failures)

    def test_branch_point_cycle_allowed(self) -> None:
        graph = make_graph(
            entry="a",
            slots=(PhaseSlot(name="a"), PhaseSlot(name="b")),
            transitions={"a": ("b", "a"), "b": (None,)},
        )
        validate_graph(graph)

    def test_multiple_failures_reported_together(self) -> None:
        graph = make_graph(
            entry="missing",
            slots=(PhaseSlot(name="a", input_schema=int),),
            transitions={"a": ("unknown",)},
        )
        with pytest.raises(GraphValidationError) as info:
            validate_graph(graph)
        assert len(info.value.failures) >= 2


class TestLegacyGraph:
    def test_legacy_graph_validates(self) -> None:
        LEGACY_GRAPH.validate()

    def test_entry_point_is_recon(self) -> None:
        assert LEGACY_GRAPH.entry == "recon"

    def test_slot_count(self) -> None:
        assert len(LEGACY_GRAPH.slots) == 11

    def test_project_test_suite_is_optional(self) -> None:
        slot = LEGACY_GRAPH.slot("project_test_suite")
        assert slot.required is False

    def test_all_other_slots_required(self) -> None:
        for slot in LEGACY_GRAPH.slots:
            if slot.name == "project_test_suite":
                continue
            assert slot.required is True

    def test_transitions_cover_all_required_slots(self) -> None:
        required_names = {slot.name for slot in LEGACY_GRAPH.slots if slot.required}
        assert required_names.issubset(LEGACY_GRAPH.transitions.keys())

    def test_resolved_phase_name_defaults_to_slot_name(self) -> None:
        slot = PhaseSlot(name="x")
        assert slot.resolved_phase_name() == "x"

    def test_resolved_phase_name_honors_override(self) -> None:
        slot = PhaseSlot(name="x", phase_name="custom")
        assert slot.resolved_phase_name() == "custom"


class TestAgentRegistry:
    def test_register_and_get(self) -> None:
        registry = AgentRegistry()
        spec = make_spec("writer")
        registry.register(spec)
        assert registry.get("writer") is spec

    def test_invalid_name_pattern(self) -> None:
        with pytest.raises(CodeGuardianError) as info:
            make_spec("Writer")
        assert info.value.code is ErrorCode.SYS_INVARIANT_VIOLATED

    def test_name_with_hyphen_rejected(self) -> None:
        with pytest.raises(CodeGuardianError):
            make_spec("my-agent")

    def test_invalid_backend(self) -> None:
        with pytest.raises(CodeGuardianError) as info:
            make_spec("x", backend="unknown")
        assert "backend" in info.value.message.lower()

    def test_duplicate_registration(self) -> None:
        registry = AgentRegistry()
        registry.register(make_spec("writer"))
        with pytest.raises(CodeGuardianError):
            registry.register(make_spec("writer"))

    def test_get_missing_raises(self) -> None:
        registry = AgentRegistry()
        with pytest.raises(CodeGuardianError):
            registry.get("missing")

    def test_name_mismatch_with_agent(self) -> None:
        agent = StubAgent("real_name")
        with pytest.raises(CodeGuardianError) as info:
            AgentSpec(name="different_name", agent=agent, capability_tags=frozenset())
        assert "does not match" in info.value.message

    def test_unregister(self) -> None:
        registry = AgentRegistry()
        registry.register(make_spec("writer"))
        registry.unregister("writer")
        with pytest.raises(CodeGuardianError):
            registry.get("writer")

    def test_clear(self) -> None:
        registry = AgentRegistry()
        registry.register(make_spec("writer"))
        registry.register(make_spec("tester"))
        registry.clear()
        assert registry.names() == ()

    def test_list_all_preserves_registration_order(self) -> None:
        registry = AgentRegistry()
        for name in ("a", "b", "c"):
            registry.register(make_spec(name))
        names = [spec.name for spec in registry.list_all()]
        assert names == ["a", "b", "c"]


class TestSlotResolution:
    def test_resolves_matching_agent(self) -> None:
        registry = AgentRegistry()
        registry.register(make_spec("writer", tags=frozenset({"llm", "coding"})))
        graph = make_graph(
            entry="write",
            slots=(PhaseSlot(name="write", capability_tags=frozenset({"llm"})),),
            transitions={"write": (None,)},
        )
        resolution = SlotResolver(registry).resolve(graph)
        assert "write" in resolution.resolved
        assert resolution.resolved["write"].name == "writer"

    def test_requires_all_tags(self) -> None:
        registry = AgentRegistry()
        registry.register(make_spec("writer", tags=frozenset({"llm"})))
        graph = make_graph(
            entry="write",
            slots=(
                PhaseSlot(
                    name="write",
                    capability_tags=frozenset({"llm", "coding"}),
                ),
            ),
            transitions={"write": (None,)},
        )
        with pytest.raises(CodeGuardianError) as info:
            SlotResolver(registry).resolve(graph)
        assert "required slot" in info.value.message

    def test_missing_required_slot_raises(self) -> None:
        registry = AgentRegistry()
        graph = make_graph(
            entry="write",
            slots=(PhaseSlot(name="write", capability_tags=frozenset({"llm"})),),
            transitions={"write": (None,)},
        )
        with pytest.raises(CodeGuardianError):
            SlotResolver(registry).resolve(graph)

    def test_optional_slot_unresolved(self) -> None:
        registry = AgentRegistry()
        registry.register(make_spec("writer", tags=frozenset({"llm"})))
        graph = make_graph(
            entry="write",
            slots=(
                PhaseSlot(name="write", capability_tags=frozenset({"llm"})),
                PhaseSlot(
                    name="extra",
                    required=False,
                    capability_tags=frozenset({"unregistered"}),
                ),
            ),
            transitions={"write": ("extra", None), "extra": (None,)},
        )
        resolution = SlotResolver(registry).resolve(graph)
        assert "write" in resolution.resolved
        assert "extra" in resolution.unresolved

    def test_ambiguous_priority_raises(self) -> None:
        registry = AgentRegistry()
        registry.register(
            make_spec("a", tags=frozenset({"x"}), priority=5),
        )
        registry.register(
            make_spec("b", tags=frozenset({"x"}), priority=5),
        )
        graph = make_graph(
            entry="s",
            slots=(PhaseSlot(name="s", capability_tags=frozenset({"x"})),),
            transitions={"s": (None,)},
        )
        with pytest.raises(CodeGuardianError) as info:
            SlotResolver(registry).resolve(graph)
        assert "ambiguous" in info.value.message

    def test_higher_priority_wins(self) -> None:
        registry = AgentRegistry()
        registry.register(
            make_spec("low", tags=frozenset({"x"}), priority=1),
        )
        registry.register(
            make_spec("high", tags=frozenset({"x"}), priority=10),
        )
        graph = make_graph(
            entry="s",
            slots=(PhaseSlot(name="s", capability_tags=frozenset({"x"})),),
            transitions={"s": (None,)},
        )
        resolution = SlotResolver(registry).resolve(graph)
        assert resolution.resolved["s"].name == "high"

    def test_schema_compatibility_same_type(self) -> None:
        registry = AgentRegistry()
        registry.register(
            make_spec("a", tags=frozenset(), input_type=int, output_type=str),
        )
        graph = make_graph(
            entry="s",
            slots=(
                PhaseSlot(
                    name="s",
                    input_schema=int,
                    output_schema=str,
                ),
            ),
            transitions={"s": (None,)},
        )
        resolution = SlotResolver(registry).resolve(graph)
        assert "s" in resolution.resolved

    def test_schema_compatibility_subclass(self) -> None:
        class Base:
            pass

        class Sub(Base):
            pass

        registry = AgentRegistry()
        registry.register(
            make_spec("a", tags=frozenset(), input_type=Sub),
        )
        graph = make_graph(
            entry="s",
            slots=(PhaseSlot(name="s", input_schema=Base, output_schema=Any),),
            transitions={"s": (None,)},
        )
        resolution = SlotResolver(registry).resolve(graph)
        assert "s" in resolution.resolved

    def test_schema_compatibility_superclass_fails(self) -> None:
        class Base:
            pass

        class Sub(Base):
            pass

        registry = AgentRegistry()
        registry.register(
            make_spec("a", tags=frozenset(), input_type=Base),
        )
        graph = make_graph(
            entry="s",
            slots=(PhaseSlot(name="s", input_schema=Sub, output_schema=Any),),
            transitions={"s": (None,)},
        )
        with pytest.raises(CodeGuardianError):
            SlotResolver(registry).resolve(graph)

    def test_any_schema_accepts_anything(self) -> None:
        registry = AgentRegistry()
        registry.register(make_spec("a", tags=frozenset()))
        graph = make_graph(
            entry="s",
            slots=(PhaseSlot(name="s"),),
            transitions={"s": (None,)},
        )
        resolution = SlotResolver(registry).resolve(graph)
        assert "s" in resolution.resolved
