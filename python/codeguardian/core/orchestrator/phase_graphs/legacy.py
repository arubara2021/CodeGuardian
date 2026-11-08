"""The legacy phase graph.

The pipeline that ships in v1: recon → blast → analyst → tester →
writer → sandbox_verify → three verifiers → skill_curator, with one
optional terminal slot reserved for a future full-test-suite agent.

The graph does not branch on mode. The greenfield pipeline will be a
sibling module; adding it is a new file, not an edit here. The
VerifierLoop's retry logic lives inside the ``verifier_stack`` module,
not in graph transitions, so no cycle appears in this graph.

Slot capability tags are used by the registry's slot resolver: an
agent must provide every tag a slot requires. Tags are strings, not
enums, so an agent author can declare a new tag and a slot can
require it without a schema change. The tags chosen here match what
the Phase 11 agents already provide; the registry (block 2) reads
them from the agent class or its manifest.

Schemas in this graph are ``Any`` for every slot. Phase 11 agents do
not yet declare explicit input and output types as class attributes;
the registry introspects the agent's ``run`` method annotations
instead. When a future agent needs stricter resolution, the graph can
declare a specific type and the registry will match it structurally.
"""

from __future__ import annotations

from typing import Any

from ..phase_graph import PhaseGraph, PhaseSlot

__all__ = ["LEGACY_GRAPH"]


_SLOTS: tuple[PhaseSlot, ...] = (
    PhaseSlot(
        name="recon",
        required=True,
        capability_tags=frozenset({"deterministic", "cpg-builder"}),
        input_schema=Any,
        output_schema=Any,
        gate="recon",
        blast_band=None,
        phase_name="reconnaissance",
    ),
    PhaseSlot(
        name="blast",
        required=True,
        capability_tags=frozenset({"deterministic", "graph-query"}),
        input_schema=Any,
        output_schema=Any,
        gate="blast",
        blast_band=None,
        phase_name="blast_radius",
    ),
    PhaseSlot(
        name="analyst",
        required=True,
        capability_tags=frozenset({"llm", "reasoning", "spec-producer"}),
        input_schema=Any,
        output_schema=Any,
        gate="analyst",
        blast_band=None,
        phase_name="analyst",
    ),
    PhaseSlot(
        name="tester",
        required=True,
        capability_tags=frozenset({"llm", "coding", "test-producer"}),
        input_schema=Any,
        output_schema=Any,
        gate="tester",
        blast_band=None,
        phase_name="tester",
    ),
    PhaseSlot(
        name="writer",
        required=True,
        capability_tags=frozenset({"llm", "coding", "code-producer"}),
        input_schema=Any,
        output_schema=Any,
        gate="writer",
        blast_band=None,
        phase_name="writer",
    ),
    PhaseSlot(
        name="sandbox_verify",
        required=True,
        capability_tags=frozenset({"deterministic", "sandbox-runner"}),
        input_schema=Any,
        output_schema=Any,
        gate="sandbox_verify",
        blast_band=None,
        phase_name="sandbox_verify",
    ),
    PhaseSlot(
        name="verifier_correctness",
        required=True,
        capability_tags=frozenset({"llm", "reasoning", "verdict-producer", "correctness"}),
        input_schema=Any,
        output_schema=Any,
        gate="verifier",
        blast_band=None,
        phase_name="verifier_correctness",
    ),
    PhaseSlot(
        name="verifier_security",
        required=True,
        capability_tags=frozenset({"llm", "reasoning", "verdict-producer", "security"}),
        input_schema=Any,
        output_schema=Any,
        gate="verifier",
        blast_band=None,
        phase_name="verifier_security",
    ),
    PhaseSlot(
        name="verifier_contract",
        required=True,
        capability_tags=frozenset({"llm", "reasoning", "verdict-producer", "contract"}),
        input_schema=Any,
        output_schema=Any,
        gate="verifier",
        blast_band=None,
        phase_name="verifier_contract",
    ),
    PhaseSlot(
        name="skill_curator",
        required=True,
        capability_tags=frozenset({"llm", "reasoning", "skill-producer"}),
        input_schema=Any,
        output_schema=Any,
        gate="skill_curator",
        blast_band=None,
        phase_name="skill_curator",
    ),
    PhaseSlot(
        name="project_test_suite",
        required=False,
        capability_tags=frozenset({"llm", "coding", "test-suite-producer"}),
        input_schema=Any,
        output_schema=Any,
        gate=None,
        blast_band=None,
        phase_name="project_test_suite",
    ),
)


_TRANSITIONS: dict[str, tuple[str | None, ...]] = {
    "recon": ("blast",),
    "blast": ("analyst",),
    "analyst": ("tester",),
    "tester": ("writer",),
    "writer": ("sandbox_verify",),
    "sandbox_verify": ("verifier_correctness",),
    "verifier_correctness": ("verifier_security",),
    "verifier_security": ("verifier_contract",),
    "verifier_contract": ("skill_curator",),
    "skill_curator": ("project_test_suite", None),
    "project_test_suite": (None,),
}


LEGACY_GRAPH = PhaseGraph(
    name="legacy",
    entry="recon",
    slots=_SLOTS,
    transitions=_TRANSITIONS,
    description=(
        "The ten-slot legacy pipeline plus one optional future slot. "
        "Recon to blast to analyst to tester to writer to sandbox_verify "
        "to three verifiers to skill_curator, with an optional terminal "
        "project_test_suite slot that runs only when a full-test-suite "
        "agent is registered."
    ),
)

LEGACY_GRAPH.validate()
