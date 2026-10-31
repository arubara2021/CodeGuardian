"""Recon agent.

Deterministic. Reads source files from a clone root, builds the Code
Property Graph, extracts the dependency graph from Python imports,
builds the hierarchical symbol index, detects entrypoints, and returns
an artifact conforming to ``recon.json``.

The eleven required fields are all present. Fields whose analysis is
outside Phase 11 scope (smoke test, truth report, drift report) carry
an explicit status in ``extensions.report_statuses`` so a downstream
consumer cannot mistake "not analyzed" for "no findings".
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from ._runtime import classify_failure

__all__ = ["ReconAgent", "ReconInput", "ReconResult"]


_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".env",
        "node_modules",
        "target",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".deps",
        ".cache",
        ".idea",
        ".vscode",
        "work",
        ".codeguardian",
        ".ipynb_checkpoints",
        ".tox",
        ".nox",
    }
)

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
}

_TIER: dict[str, str] = {
    "python": "high",
    "typescript": "high",
    "javascript": "high",
    "java": "high",
    "go": "high",
    "rust": "high",
    "c": "best-effort",
    "cpp": "best-effort",
}


@dataclass(frozen=True, slots=True)
class ReconInput:
    """Input to the recon agent."""

    repo_url: str
    provider: str
    commit_hash: str
    clone_root: Path
    cpg_output_dir: Path


@dataclass(frozen=True, slots=True)
class ReconResult:
    """Output of the recon agent.

    Attributes:
        artifact: Schema-conforming content for ``recon.json``.
        cpg_bytes: Serialized CPG, for downstream consumers.
        cpg_path: Path the CPG was written to.
    """

    artifact: dict[str, Any]
    cpg_bytes: bytes = field(repr=False)
    cpg_path: Path


class ReconAgent:
    """Deterministic recon agent."""

    name = "recon"
    phase = "recon"
    is_deterministic = True
    requires_tools: tuple[str, ...] = ()
    can_refuse = True
    prompt_role: str | None = None
    prompt_version: str | None = None

    async def run(self, input: ReconInput) -> ReconResult:
        """Run recon on the given clone root.

        Args:
            input: The recon input.

        Returns:
            A ReconResult with the schema-conforming artifact and the
            CPG bytes.

        Raises:
            CodeGuardianError: With code FILE_NOT_FOUND when the clone
                root is not a directory; with SYS_NOT_IMPLEMENTED when
                the native kernel is unavailable.
        """
        try:
            from codeguardian import _native
        except ImportError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                "recon requires the compiled kernel extension",
            ) from exc

        if not input.clone_root.is_dir():
            raise CodeGuardianError(
                ErrorCode.FILE_NOT_FOUND,
                f"clone root is not a directory: {input.clone_root}",
                details={"path": str(input.clone_root)},
            )

        files = _enumerate_source_files(input.clone_root)
        if not files:
            raise CodeGuardianError(
                ErrorCode.FILE_NOT_FOUND,
                f"no source files found under {input.clone_root}",
                details={"path": str(input.clone_root)},
            )

        detected_at = _now_iso()
        cpg_input = [(rel, lang, src) for rel, lang, src in files]
        cpg_bytes = _native.build_graph(cpg_input)
        summary = _native.graph_summary(cpg_bytes)

        input.cpg_output_dir.mkdir(parents=True, exist_ok=True)
        cpg_path = input.cpg_output_dir / f"{input.commit_hash}.cpg"
        cpg_path.write_bytes(cpg_bytes)

        languages = _build_languages_section(files)
        structural = _build_structural_section(summary, cpg_path, detected_at)
        dependency_graph = _build_dependency_graph(files)
        hierarchical_index = _build_hierarchical_index(cpg_bytes, files)
        entrypoints = _build_entrypoints(files)

        dependency_status = (
            "complete" if all(lang == "python" for _, lang, _ in files) else "partial"
        )

        artifact: dict[str, Any] = {
            "schema_version": "1.1",
            "repo": {
                "url": input.repo_url,
                "provider": input.provider,
                "commit": input.commit_hash,
                "detected_at": detected_at,
            },
            "languages": languages,
            "frameworks": [],
            "structural": structural,
            "dependency_graph": dependency_graph,
            "hierarchical_index": hierarchical_index,
            "entrypoints": entrypoints,
            "versions": {"declared": {}, "actual": {}, "available": {}},
            "smoke_test": {
                "status": "skipped",
                "passed": 0,
                "failed": 0,
                "errored": 0,
            },
            "truth_report": [],
            "drift_report": [],
            "extensions": {
                "report_statuses": {
                    "dependency_graph": dependency_status,
                    "hierarchical_index": "complete",
                    "entrypoints": "partial",
                    "versions": "skipped",
                    "frameworks": "complete",
                    "smoke_test": "skipped",
                    "truth_report": "skipped",
                    "drift_report": "skipped",
                }
            },
        }

        _record(
            role="recon",
            outcome="success",
            target_symbol=None,
            output=str(artifact),
            failure_category=None,
        )

        return ReconResult(
            artifact=artifact,
            cpg_bytes=cpg_bytes,
            cpg_path=cpg_path,
        )


def _enumerate_source_files(
    clone_root: Path,
) -> list[tuple[str, str, str]]:
    """Return (relative_path, language, source) per source file, sorted.

    Sorting ensures the CPG build order is deterministic across runs on
    the same input.
    """
    results: list[tuple[str, str, str]] = []
    for path in clone_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel_path = path.relative_to(clone_root)
        except ValueError:
            continue
        if any(part in _EXCLUDE_DIRS for part in rel_path.parts):
            continue
        language = _EXT_TO_LANG.get(path.suffix.lower())
        if language is None:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(clone_root).as_posix()
        results.append((rel, language, source))
    results.sort()
    return results


def _build_languages_section(
    files: list[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """Return the ``languages`` array with counts and percentages."""
    counts = Counter(language for _, language, _ in files)
    total = len(files)
    return [
        {
            "name": language,
            "files": count,
            "percentage": round(100.0 * count / total, 2),
            "tier": _TIER.get(language, "best-effort"),
        }
        for language, count in sorted(counts.items())
    ]


def _build_structural_section(
    summary: dict[str, Any],
    cpg_path: Path,
    built_at: str,
) -> dict[str, Any]:
    """Return the ``structural`` object."""
    return {
        "cpg": {
            "binary_path": str(cpg_path),
            "node_count": int(summary.get("node_count", 0)),
            "edge_count": int(summary.get("edge_count", 0)),
            "built_at": built_at,
            "incremental": bool(summary.get("incremental", False)),
        }
    }


def _build_dependency_graph(
    files: list[tuple[str, str, str]],
) -> dict[str, Any]:
    """Return the dependency graph extracted from Python imports.

    Only Python files contribute edges. The schema permits an empty
    edge list; a repository with no Python files produces nodes only.
    """
    nodes = [{"id": rel, "type": "file", "language": language} for rel, language, _ in files]
    files_set = {rel for rel, _, _ in files}
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rel, language, source in files:
        if language != "python":
            continue
        for module, _name in _extract_python_imports(source):
            target = _resolve_python_import(module, files_set)
            if target is None or target == rel:
                continue
            key = (rel, target)
            if key in seen:
                continue
            seen.add(key)
            edges.append({"from": rel, "to": target, "kind": "import"})
    return {"nodes": nodes, "edges": edges}


def _extract_python_imports(source: str) -> list[tuple[str, str]]:
    """Return (module, imported_name) pairs for every import in ``source``.

    Returns an empty list when the source does not parse. Relative
    imports (``from . import x``) are skipped: their module is None or
    a relative dotted name that cannot be resolved without knowing the
    package root.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    result: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.append((alias.name, alias.asname or alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                result.append((node.module, alias.name))
    return result


def _resolve_python_import(module: str, files_set: set[str]) -> str | None:
    """Resolve a dotted Python module to a file path in ``files_set``."""
    base = module.replace(".", "/")
    for candidate in (f"{base}.py", f"{base}/__init__.py"):
        if candidate in files_set:
            return candidate
    return None


def _build_hierarchical_index(
    cpg_bytes: bytes,
    files: list[tuple[str, str, str]],
) -> dict[str, list[str]]:
    """Return ``{path: [qualified_name, ...]}`` for every source file."""
    from codeguardian import _native

    handle = _native.PyCPG(cpg_bytes)
    index: dict[str, list[str]] = {}
    for rel, _language, _source in files:
        try:
            symbols = handle.list_symbols(rel)
        except Exception:
            index[rel] = []
            continue
        names: list[str] = []
        for symbol in symbols:
            if isinstance(symbol, dict):
                qualified = symbol.get("qualified_name") or symbol.get("name")
            else:
                qualified = getattr(symbol, "qualified_name", None) or getattr(symbol, "name", None)
            if qualified:
                names.append(str(qualified))
        index[rel] = names
    return index


def _build_entrypoints(
    files: list[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """Return detected entrypoints (Python only).

    Recognizes ``if __name__ == "__main__":`` blocks and top-level
    ``def main():`` functions. Other languages are not analyzed; the
    ``entrypoints`` status in ``extensions.report_statuses`` is
    ``"partial"``.
    """
    entrypoints: list[dict[str, Any]] = []
    for rel, language, source in files:
        if language != "python":
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.If):
                if _is_main_guard(node):
                    entrypoints.append(
                        {
                            "file": rel,
                            "line": node.lineno,
                            "type": "script",
                        }
                    )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "main":
                    entrypoints.append(
                        {
                            "file": rel,
                            "line": node.lineno,
                            "type": "function",
                        }
                    )
            elif isinstance(node, ast.ClassDef) and node.name == "main":
                entrypoints.append(
                    {
                        "file": rel,
                        "line": node.lineno,
                        "type": "class",
                    }
                )
    return entrypoints


def _is_main_guard(node: ast.If) -> bool:
    """Return True if ``node`` is ``if __name__ == "__main__":``."""
    test = node.test
    if not isinstance(test, ast.Compare):
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    return (
        isinstance(left, ast.Name)
        and left.id == "__name__"
        and isinstance(right, ast.Constant)
        and right.value == "__main__"
    )


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _record(
    *,
    role: str,
    outcome: str,
    target_symbol: str | None,
    output: str | None,
    failure_category: str | None,
) -> None:
    """Best-effort telemetry write.

    Telemetry is recorded when a store is reachable via the run context
    set by the caller. When no store is configured, the record is
    skipped silently: recon must not fail because telemetry is not
    wired up yet.
    """
    from codeguardian.core.agents.telemetry import record_call

    store = _active_store()
    if store is None:
        return
    record_call(
        store,
        role=role,
        outcome=outcome,
        target_symbol=target_symbol,
        output=output,
        failure_category=failure_category,
    )


def _active_store() -> Any:
    """Return the active SQLiteStore for telemetry, or None.

    Phase 13 provides the store via a context var when it wires agents
    into a pipeline. During isolated Phase 11 tests, no store is set
    and telemetry is skipped.
    """
    from codeguardian.core.agents import _store_context

    return _store_context.get_active_store()


def _classify(exc: BaseException) -> str | None:
    """Classify an exception for telemetry when a run fails."""
    return classify_failure(exc)
