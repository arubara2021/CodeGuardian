"""Orchestrator public surface.

The names exported here are the ones a caller outside this package
needs: the protocol the orchestrator satisfies, the concrete
implementation, the graph data model, the registry and resolver that
match slots to agents, and the shipped legacy graph.

Internal collaborators — the loop walker, the checkpoint supervisor,
the evidence bus, the phase gates, the verifier stack, the context
engine, the sandbox adapter, and the attenuation helper — are not
re-exported. A caller that needs one of them imports its module
directly. Keeping the public surface small is what lets the internal
surface change without a caller-visible break.
"""

from __future__ import annotations

from codeguardian.interfaces.orchestrator import Orchestrator

from .langgraph_orchestrator import LangGraphOrchestrator
from .phase_graph import PhaseGraph, PhaseSlot
from .phase_graphs import LEGACY_GRAPH
from .registry import AgentRegistry, SlotResolver

LegacyGraph = LEGACY_GRAPH

__all__ = [
    "AgentRegistry",
    "LangGraphOrchestrator",
    "LegacyGraph",
    "Orchestrator",
    "PhaseGraph",
    "PhaseSlot",
    "SlotResolver",
]
