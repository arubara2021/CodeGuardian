"""Skills module public surface.

This package provides the persistent memory and retrieval layer for
CodeGuardian. The public surface exposes the v1 modules unchanged and
the v2 modules added in Phase 12.

The v1 modules are ``EmbeddingProvider``, ``SkillLibrary``,
``SkillRetriever``, and ``SkillIndexer``. The v2 modules are the
governance, retrieval pipeline, negative knowledge, graph, and
evolution layers. Every v2 module is reachable through an accessor on
``SkillLibrary`` or through a direct import from this package.
"""

from .embeddings import EmbeddingProvider
from .governor import SkillGovernor
from .indexer import SkillIndexer
from .library import (
    NegativeKnowledgeStore,
    SkillEvolver,
    SkillGraph,
    SkillLibrary,
)
from .retrieval import RetrievalPipeline
from .retriever import RetrievedSkill, SkillRetriever
from .types import (
    AdmissionVerdict,
    EdgeKind,
    NegativeRecord,
    ProjectCard,
    ProvenancePin,
    RetrievalBundle,
    RetrievalItem,
    SkillEdge,
    SkillNode,
)

__all__ = [
    "AdmissionVerdict",
    "EdgeKind",
    "EmbeddingProvider",
    "NegativeKnowledgeStore",
    "NegativeRecord",
    "ProjectCard",
    "ProvenancePin",
    "RetrievalBundle",
    "RetrievalItem",
    "RetrievalPipeline",
    "RetrievedSkill",
    "SkillEdge",
    "SkillEvolver",
    "SkillGovernor",
    "SkillGraph",
    "SkillIndexer",
    "SkillLibrary",
    "SkillNode",
    "SkillRetriever",
]
