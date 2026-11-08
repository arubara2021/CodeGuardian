"""Deep test modules for CodeGuardian.

The deep test is split into one module per concern so that each can
be understood, tested, and replaced in isolation:

- ``_preflight`` checks the environment before any work begins.
- ``_repos`` is the repository manifest; adding a repository is one
  dictionary entry.
- ``_sandbox`` wraps the two-phase bubblewrap lifecycle.
- ``_discovery`` walks a cloned repository and picks band targets.
- ``_multi_lang`` writes and parses the eight-language corpus.
- ``_fault_injection`` injects AST drift and verifies the value test
  catches it.
- ``_pipeline`` runs the Phase 11 agents on one target.
- ``_audit`` verifies the audit chain and inspects telemetry.
- ``_crucible`` parses the 50+ language corpus.

The orchestrator, ``sandtest/deep_test.py``, imports from this package
and composes the sections per repository.
"""

from ._audit import (
    EXPECTED_ROLES,
    AgentCallsReport,
    query_agent_calls,
    verify_agent_calls,
    verify_audit_chain,
)
from ._crucible import (
    CrucibleResult,
    LanguageReport,
    run_crucible,
)
from ._discovery import (
    BandSelection,
    CandidateTarget,
    RankedTarget,
    discover_candidates,
    pick_bands,
    rank_by_fan_in,
)
from ._fault_injection import (
    FaultInjectionResult,
    can_inject_drift,
    inject_behavioral_drift,
    run_fault_injection,
)
from ._generator import (
    execution_map,
    read_generator_results,
    run_generator,
    write_generator_artifacts,
)
from ._multi_lang import (
    LANGUAGES,
    LanguageSpec,
    MultiLangResult,
    parse_all,
    verify_three_deep,
    write_multi_lang_repo,
)
from ._pipeline import (
    Band,
    PipelineResult,
    PipelineStageResult,
    run_pipeline,
)
from ._preflight import (
    CANDIDATE_TOOLS,
    CheckResult,
    PreflightResult,
    run_preflight,
)
from ._repos import (
    ALL_REPOS,
    DEFAULT_REPO,
    RepoSpec,
    filter_by_tools,
    get_repo,
    list_repo_names,
    resolve_repo_arg,
)
from ._sandbox import SandboxSession

__all__ = [
    "ALL_REPOS",
    "CANDIDATE_TOOLS",
    "DEFAULT_REPO",
    "EXPECTED_ROLES",
    "LANGUAGES",
    "AgentCallsReport",
    "Band",
    "BandSelection",
    "CandidateTarget",
    "CheckResult",
    "CrucibleResult",
    "FaultInjectionResult",
    "LanguageReport",
    "LanguageSpec",
    "MultiLangResult",
    "PipelineResult",
    "PipelineStageResult",
    "PreflightResult",
    "RankedTarget",
    "RepoSpec",
    "SandboxSession",
    "can_inject_drift",
    "discover_candidates",
    "execution_map",
    "filter_by_tools",
    "get_repo",
    "inject_behavioral_drift",
    "list_repo_names",
    "parse_all",
    "pick_bands",
    "query_agent_calls",
    "rank_by_fan_in",
    "read_generator_results",
    "resolve_repo_arg",
    "run_crucible",
    "run_fault_injection",
    "run_generator",
    "run_pipeline",
    "run_preflight",
    "verify_agent_calls",
    "verify_audit_chain",
    "verify_three_deep",
    "write_generator_artifacts",
    "write_multi_lang_repo",
]
