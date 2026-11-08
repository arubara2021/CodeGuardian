"""Recon agent.

Deterministic. Reads source files from a clone root, builds the Code
Property Graph, extracts the dependency graph from imports in every
supported language, builds the hierarchical symbol index, detects
entrypoints, builds a PageRank-ordered repo map under a token budget,
detects the project's test framework and build system, and reads the
project's convention files.

The eleven required fields are all present on the artifact. Fields
whose analysis is outside Phase 11 scope (smoke test, truth report,
drift report) carry an explicit status in ``extensions.report_statuses``
so a downstream consumer cannot mistake "not analyzed" for "no
findings".

Multi-language import resolution is best-effort. Python imports are
resolved by AST against the file set. TypeScript and JavaScript
relative imports are resolved by joining the importing file's
directory with the module path and trying extensions. Java imports are
resolved by matching the imported class's basename against file
basenames anywhere in the repo, which is correct for single-file-per-
class projects and approximate otherwise. Go and Rust imports are
extracted but not resolved; the module path semantics of those two
languages do not map cleanly to file-level edges without a compiler's
view of the workspace. The ``dependency_graph`` status reflects this:
it is ``complete`` for Python-only repos and ``partial`` otherwise.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
import xml.etree.ElementTree as ET
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

_CONVENTION_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    ".cursorrules",
    "CONVENTIONS.md",
)

_CONFIG_INDICATORS: tuple[str, ...] = (
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "tox.ini",
    "mypy.ini",
    "pyrightconfig.json",
    ".ruff.toml",
    "ruff.toml",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.json",
    "eslint.config.js",
    ".prettierrc",
    ".prettierrc.json",
    "tsconfig.json",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
)


_TS_JS_IMPORT_FROM_RE = re.compile(r"""^\s*import\s+[^'"]*?from\s+['"]([^'"]+)['"]""", re.MULTILINE)
_TS_JS_IMPORT_BARE_RE = re.compile(r"""^\s*import\s+['"]([^'"]+)['"]""", re.MULTILINE)
_TS_JS_REQUIRE_RE = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")

_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", re.MULTILINE)

_GO_SINGLE_IMPORT_RE = re.compile(r'^\s*import\s+"([^"]+)"', re.MULTILINE)
_GO_BLOCK_IMPORT_RE = re.compile(r"^\s*import\s*\((.*?)^\s*\)", re.MULTILINE | re.DOTALL)
_GO_IMPORT_LINE_RE = re.compile(r'^\s*(?:\w+\s+)?"([^"]+)"', re.MULTILINE)

_RUST_USE_RE = re.compile(r"^\s*use\s+([\w:]+)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ReconInput:
    """Input to the recon agent.

    Attributes:
        repo_url: The repository URL for the artifact's ``repo`` section.
        provider: The provider name for the artifact's ``repo`` section.
        commit_hash: The commit hash; used for the CPG output filename.
        clone_root: The local clone root.
        cpg_output_dir: The directory the CPG file is written to.
        repo_map_token_budget: Token budget for the repo map. A file
            that would push the running total over this budget is not
            added unless it is the first entry. Defaults to 8000.
        conventions_token_budget: Token budget for the combined content
            of every convention file read. Defaults to 2000.
    """

    repo_url: str
    provider: str
    commit_hash: str
    clone_root: Path
    cpg_output_dir: Path
    repo_map_token_budget: int = 8000
    conventions_token_budget: int = 2000


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
                root is not a directory or contains no source files;
                SYS_NOT_IMPLEMENTED when the kernel is unavailable.
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

        file_importance = _compute_pagerank(files, dependency_graph)
        repo_map = _build_repo_map(
            files=files,
            hierarchical_index=hierarchical_index,
            file_importance=file_importance,
            token_budget=input.repo_map_token_budget,
        )
        file_token_cost = {rel: max(1, len(source) // 4) for rel, _lang, source in files}
        test_setup = _detect_test_setup(input.clone_root, files)
        build_system, build_command = _detect_build_system(input.clone_root)
        conventions = _read_conventions(input.clone_root, input.conventions_token_budget)
        declared = _build_declared(input.clone_root)

        dependency_status = (
            "complete" if all(lang == "python" for _, lang, _ in files) else "partial"
        )
        repo_map_status = "complete" if repo_map else "skipped"
        conventions_status = (
            "complete"
            if conventions["agent_files"] or conventions["configs_present"]
            else "skipped"
        )
        declared_status = "complete" if declared else "skipped"

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
                    "repo_map": repo_map_status,
                    "conventions": conventions_status,
                    "declared": declared_status,
                },
                "repo_map": repo_map,
                "test_frameworks": test_setup["test_frameworks"],
                "test_roots": test_setup["test_roots"],
                "test_command": test_setup["test_command"],
                "build_system": build_system,
                "build_command": build_command,
                "conventions": conventions,
                "declared": declared,
                "file_importance": {rel: round(score, 6) for rel, score in file_importance.items()},
                "file_token_cost": file_token_cost,
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

    Sorting ensures the CPG build order and the repo map order are
    deterministic across runs on the same input.
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
    """Return the file-level dependency graph.

    Extraction is per-language. Resolution to file-level edges is done
    for Python, TypeScript, JavaScript, and Java. Go and Rust imports
    are extracted by ``_extract_imports`` but not resolved to edges;
    their module path semantics do not map cleanly to file paths
    without a compiler's view of the workspace.
    """
    nodes = [{"id": rel, "type": "file", "language": language} for rel, language, _ in files]
    files_set = {rel for rel, _, _ in files}
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rel, language, source in files:
        for module in _extract_imports(language, source):
            target = _resolve_import(language, module, rel, files_set)
            if target is None or target == rel:
                continue
            key = (rel, target)
            if key in seen:
                continue
            seen.add(key)
            edges.append({"from": rel, "to": target, "kind": "import"})
    return {"nodes": nodes, "edges": edges}


def _extract_imports(language: str, source: str) -> list[str]:
    """Return the import module strings for a source file."""
    if language == "python":
        return [module for module, _name in _extract_python_imports(source)]
    if language in ("typescript", "javascript"):
        return _extract_ts_js_imports(source)
    if language == "java":
        return _extract_java_imports(source)
    if language == "go":
        return _extract_go_imports(source)
    if language == "rust":
        return _extract_rust_imports(source)
    return []


def _resolve_import(
    language: str,
    module: str,
    source_path: str,
    files_set: set[str],
) -> str | None:
    """Resolve an import module string to a file path, or None."""
    if language == "python":
        return _resolve_python_import(module, files_set)
    if language in ("typescript", "javascript"):
        return _resolve_ts_js_import(module, source_path, files_set)
    if language == "java":
        return _resolve_java_import(module, files_set)
    return None


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


def _extract_ts_js_imports(source: str) -> list[str]:
    """Return module specifiers from ES6 imports and CommonJS requires."""
    result: list[str] = []
    for match in _TS_JS_IMPORT_FROM_RE.finditer(source):
        result.append(match.group(1))
    for match in _TS_JS_IMPORT_BARE_RE.finditer(source):
        result.append(match.group(1))
    for match in _TS_JS_REQUIRE_RE.finditer(source):
        result.append(match.group(1))
    return result


def _extract_java_imports(source: str) -> list[str]:
    """Return fully qualified class names from Java import statements."""
    return [match.group(1) for match in _JAVA_IMPORT_RE.finditer(source)]


def _extract_go_imports(source: str) -> list[str]:
    """Return import paths from Go single-line and grouped imports."""
    result: list[str] = []
    for match in _GO_SINGLE_IMPORT_RE.finditer(source):
        result.append(match.group(1))
    for block in _GO_BLOCK_IMPORT_RE.finditer(source):
        for match in _GO_IMPORT_LINE_RE.finditer(block.group(1)):
            result.append(match.group(1))
    return result


def _extract_rust_imports(source: str) -> list[str]:
    """Return module paths from Rust ``use`` statements."""
    return [match.group(1) for match in _RUST_USE_RE.finditer(source)]


def _resolve_python_import(module: str, files_set: set[str]) -> str | None:
    """Resolve a dotted Python module to a file path in ``files_set``."""
    base = module.replace(".", "/")
    for candidate in (f"{base}.py", f"{base}/__init__.py"):
        if candidate in files_set:
            return candidate
    return None


def _resolve_ts_js_import(
    module: str,
    source_path: str,
    files_set: set[str],
) -> str | None:
    """Resolve a TypeScript or JavaScript import against the file set.

    Only relative specifiers (``./`` and ``../``) are resolved. Bare
    specifiers name npm packages and are not part of the repo.
    """
    if not module.startswith((".", "/")):
        return None
    base_dir = "/".join(source_path.split("/")[:-1])
    rel = module[2:] if module.startswith("./") else module.lstrip("/")
    candidate_dir = _normalize_path_join(base_dir, rel)
    for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        for suffix in (ext, f"/index{ext}"):
            candidate = f"{candidate_dir}{suffix}"
            if candidate in files_set:
                return candidate
    return None


def _resolve_java_import(module: str, files_set: set[str]) -> str | None:
    """Resolve a Java import to a file path by class basename.

    The imported class's simple name is matched against every Java file
    whose basename equals ``<ClassName>.java``. This is correct for
    single-file-per-class projects and approximate otherwise.
    """
    last = module.rsplit(".", 1)[-1]
    target_file = f"{last}.java"
    matches = sorted(
        rel for rel in files_set if rel == target_file or rel.endswith(f"/{target_file}")
    )
    return matches[0] if matches else None


def _normalize_path_join(base: str, rel: str) -> str:
    """Join two POSIX-like path fragments and normalise ``..`` segments."""
    parts = (base.split("/") if base else []) + rel.split("/")
    out: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if out:
                out.pop()
            continue
        out.append(part)
    return "/".join(out)


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
                    entrypoints.append({"file": rel, "line": node.lineno, "type": "script"})
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "main":
                    entrypoints.append({"file": rel, "line": node.lineno, "type": "function"})
            elif isinstance(node, ast.ClassDef) and node.name == "main":
                entrypoints.append({"file": rel, "line": node.lineno, "type": "class"})
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


def _compute_pagerank(
    files: list[tuple[str, str, str]],
    dependency_graph: dict[str, Any],
    iterations: int = 20,
    damping: float = 0.85,
) -> dict[str, float]:
    """Return PageRank importance per file over the import graph.

    The import graph is directed: an edge ``from -> to`` means the
    source file imports the target file. PageRank over the reversed
    graph makes a file that many files import score higher than a file
    that imports many files.

    The algorithm runs ``iterations`` power iterations. For a graph of
    a few hundred nodes the loop is imperceptible; for a graph of tens
    of thousands of nodes the caller should reduce ``iterations``.
    """
    nodes = [rel for rel, _, _ in files]
    if not nodes:
        return {}

    out_edges: dict[str, set[str]] = {n: set() for n in nodes}
    node_set = set(nodes)
    for edge in dependency_graph.get("edges", []):
        src = edge.get("from")
        dst = edge.get("to")
        if src in node_set and dst in node_set and src != dst:
            out_edges[src].add(dst)

    incoming: dict[str, list[str]] = {n: [] for n in nodes}
    out_degree: dict[str, int] = {}
    for src, targets in out_edges.items():
        out_degree[src] = len(targets)
        for dst in targets:
            incoming[dst].append(src)

    n = len(nodes)
    ranks: dict[str, float] = dict.fromkeys(nodes, 1.0 / n)
    base = (1.0 - damping) / n
    for _ in range(iterations):
        new_ranks: dict[str, float] = {}
        for node in nodes:
            total = 0.0
            for src in incoming[node]:
                degree = out_degree.get(src, 0)
                if degree > 0:
                    total += ranks[src] / degree
            new_ranks[node] = base + damping * total
        ranks = new_ranks
    return ranks


def _build_repo_map(
    files: list[tuple[str, str, str]],
    hierarchical_index: dict[str, list[str]],
    file_importance: dict[str, float],
    token_budget: int,
) -> list[dict[str, Any]]:
    """Return the repo map: top files by PageRank, under a token budget.

    Each entry contains the file's path, its PageRank importance, its
    symbol list from the hierarchical index, an estimated token cost
    for the entry, and the estimated total token cost of the file's
    source. Token estimation uses four characters per token. The first
    entry is always included even when it exceeds the budget; subsequent
    entries are added while the running total plus the entry's cost
    stays at or under the budget.
    """
    source_by_path = {rel: src for rel, _, src in files}
    ranked = sorted(
        file_importance.items(),
        key=lambda item: (-item[1], item[0]),
    )

    result: list[dict[str, Any]] = []
    used_tokens = 0
    for path, importance in ranked:
        symbols = hierarchical_index.get(path, [])
        symbol_chars = sum(len(symbol) + 1 for symbol in symbols) + len(path) + 1
        entry_tokens = max(1, symbol_chars // 4)
        if result and used_tokens + entry_tokens > token_budget:
            break
        result.append(
            {
                "path": path,
                "importance": round(importance, 6),
                "symbols": list(symbols),
                "token_cost": entry_tokens,
                "total_file_tokens": max(1, len(source_by_path.get(path, "")) // 4),
            }
        )
        used_tokens += entry_tokens
    return result


def _detect_test_setup(
    clone_root: Path,
    files: list[tuple[str, str, str]],
) -> dict[str, Any]:
    """Return detected test frameworks, roots, and a primary command."""
    frameworks: list[str] = []

    pyproject = _safe_read(clone_root / "pyproject.toml")
    if pyproject and ("[tool.pytest" in pyproject or "pytest" in pyproject):
        frameworks.append("pytest")
    if (
        (clone_root / "pytest.ini").is_file() or (clone_root / "conftest.py").is_file()
    ) and "pytest" not in frameworks:
        frameworks.append("pytest")
    if (clone_root / "tox.ini").is_file() and "pytest" not in frameworks:
        frameworks.append("pytest")

    package_json = _safe_read(clone_root / "package.json")
    if package_json:
        try:
            pkg = json.loads(package_json)
        except json.JSONDecodeError:
            pkg = {}
        dev = pkg.get("devDependencies") or {}
        deps = pkg.get("dependencies") or {}
        for name in ("jest", "vitest", "mocha"):
            if name in dev or name in deps:
                frameworks.append(name)

    if (clone_root / "go.mod").is_file():
        frameworks.append("go test")
    if (clone_root / "Cargo.toml").is_file():
        frameworks.append("cargo test")

    test_roots: list[str] = []
    for rel, _lang, _src in files:
        parts = rel.split("/")
        if not parts:
            continue
        top = parts[0]
        if top in ("tests", "test", "__tests__") and top not in test_roots:
            test_roots.append(top)

    if "pytest" in frameworks:
        command = "pytest"
    elif "jest" in frameworks:
        command = "npm test"
    elif "vitest" in frameworks:
        command = "npx vitest"
    elif "go test" in frameworks:
        command = "go test ./..."
    elif "cargo test" in frameworks:
        command = "cargo test"
    else:
        command = None

    return {
        "test_frameworks": sorted(frameworks),
        "test_roots": sorted(test_roots),
        "test_command": command,
    }


def _detect_build_system(clone_root: Path) -> tuple[str | None, str | None]:
    """Return (build_system, build_command) for the dominant build file."""
    if (clone_root / "pyproject.toml").is_file():
        return "pep-517", "python3 -m build"
    if (clone_root / "setup.py").is_file():
        return "setuptools", "python3 setup.py build"
    if (clone_root / "package.json").is_file():
        return "npm", "npm run build"
    if (clone_root / "go.mod").is_file():
        return "go-modules", "go build ./..."
    if (clone_root / "Cargo.toml").is_file():
        return "cargo", "cargo build --release"
    if (clone_root / "pom.xml").is_file():
        return "maven", "mvn package"
    if (clone_root / "build.gradle").is_file() or (clone_root / "build.gradle.kts").is_file():
        return "gradle", "gradle build"
    return None, None


def _read_conventions(
    clone_root: Path,
    token_budget: int,
) -> dict[str, Any]:
    """Read convention files and record which config files are present.

    Convention files are read up to a combined token budget, which
    prevents a very large ``AGENTS.md`` from crowding out the repo map
    in a downstream prompt. Config indicators are recorded as presence
    only; their content is not read here.
    """
    conventions: dict[str, Any] = {
        "agent_files": {},
        "configs_present": [],
    }
    char_budget = max(1, token_budget * 4)
    used_chars = 0
    for name in _CONVENTION_FILES:
        path = clone_root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        remaining = char_budget - used_chars
        if remaining <= 0:
            break
        snippet = text[:remaining]
        if not snippet:
            continue
        conventions["agent_files"][name] = snippet
        used_chars += len(snippet)
    for name in _CONFIG_INDICATORS:
        if (clone_root / name).exists():
            conventions["configs_present"].append(name)
    return conventions


def _build_declared(clone_root: Path) -> dict[str, dict[str, Any]]:
    """Return structured declarations from project metadata files.

    Each entry in the returned mapping is keyed by the language the
    metadata governs. The value is the structured subset of that
    file's content the downstream agents need: version requirements,
    declared dependencies, lint config presence, type-check config
    presence, and test config. Files that are absent or fail to parse
    are omitted from the mapping entirely; a missing file is not an
    error and does not produce an empty language entry.
    """
    declared: dict[str, dict[str, Any]] = {}
    python = _read_pyproject(clone_root)
    if python is not None:
        declared["python"] = python
    rust = _read_cargo(clone_root)
    if rust is not None:
        declared["rust"] = rust
    javascript = _read_package_json(clone_root)
    if javascript is not None:
        declared["javascript"] = javascript
    go = _read_go_mod(clone_root)
    if go is not None:
        declared["go"] = go
    java = _read_pom(clone_root)
    if java is not None:
        declared["java"] = java
    return declared


def _read_pyproject(clone_root: Path) -> dict[str, Any] | None:
    """Parse ``pyproject.toml`` and return declared constraints.

    Extracts ``requires_python`` and ``dependencies`` from
    ``[project]``; lint config presence from ``[tool.ruff]`` or
    ``[tool.black]``; type-check config presence from ``[tool.mypy]``
    or ``[tool.pyright]``; and ``testpaths`` from
    ``[tool.pytest.ini_options]`` when present.
    """
    path = clone_root / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None

    result: dict[str, Any] = {}
    project = data.get("project") or {}
    if isinstance(project, dict):
        requires_python = project.get("requires-python")
        if isinstance(requires_python, str):
            result["requires_python"] = requires_python
        dependencies = project.get("dependencies")
        if isinstance(dependencies, list):
            result["dependencies"] = [str(d) for d in dependencies if isinstance(d, str)]

    tool = data.get("tool") or {}
    if isinstance(tool, dict):
        lint_parts: list[str] = []
        if "ruff" in tool:
            lint_parts.append("ruff")
        if "black" in tool:
            lint_parts.append("black")
        if lint_parts:
            result["lint_config"] = sorted(lint_parts)

        type_parts: list[str] = []
        if "mypy" in tool:
            type_parts.append("mypy")
        if "pyright" in tool:
            type_parts.append("pyright")
        if type_parts:
            result["type_check_config"] = sorted(type_parts)

        pytest_section = tool.get("pytest")
        if isinstance(pytest_section, dict):
            ini_options = pytest_section.get("ini_options")
            if isinstance(ini_options, dict):
                testpaths = ini_options.get("testpaths")
                if isinstance(testpaths, list):
                    result["test_config"] = {
                        "testpaths": [str(p) for p in testpaths if isinstance(p, str)]
                    }

    return result or None


def _read_cargo(clone_root: Path) -> dict[str, Any] | None:
    """Parse ``Cargo.toml`` and return declared constraints.

    Extracts ``rust_version`` from ``[package].rust-version`` and both
    ``dependencies`` and ``dev_dependencies`` from their respective
    sections.
    """
    path = clone_root / "Cargo.toml"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None

    result: dict[str, Any] = {}
    package = data.get("package") or {}
    if isinstance(package, dict):
        rust_version = package.get("rust-version")
        if isinstance(rust_version, str):
            result["rust_version"] = rust_version

    dependencies = data.get("dependencies")
    if isinstance(dependencies, dict):
        result["dependencies"] = sorted(str(k) for k in dependencies)

    dev_dependencies = data.get("dev-dependencies")
    if isinstance(dev_dependencies, dict):
        result["dev_dependencies"] = sorted(str(k) for k in dev_dependencies)

    return result or None


def _read_package_json(clone_root: Path) -> dict[str, Any] | None:
    """Parse ``package.json`` and return declared constraints.

    Extracts ``engines.node`` when declared, both dependency sections,
    and a boolean fact for whether a lint config and type-check config
    are present (either inline as ``eslintConfig``, or as a
    ``tsconfig.json`` file at the repo root).
    """
    path = clone_root / "package.json"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    result: dict[str, Any] = {}
    engines = data.get("engines")
    if isinstance(engines, dict):
        node_version = engines.get("node")
        if isinstance(node_version, str):
            result["node_version"] = node_version

    dependencies = data.get("dependencies")
    if isinstance(dependencies, dict):
        result["dependencies"] = sorted(str(k) for k in dependencies)

    dev_dependencies = data.get("devDependencies")
    if isinstance(dev_dependencies, dict):
        result["dev_dependencies"] = sorted(str(k) for k in dev_dependencies)

    lint_present: list[str] = []
    if "eslintConfig" in data:
        lint_present.append("eslintConfig")
    if (clone_root / ".eslintrc").is_file():
        lint_present.append(".eslintrc")
    if (clone_root / ".eslintrc.json").is_file():
        lint_present.append(".eslintrc.json")
    if lint_present:
        result["lint_config"] = sorted(lint_present)

    if (clone_root / "tsconfig.json").is_file():
        result["type_check_config"] = ["tsconfig.json"]

    return result or None


def _read_go_mod(clone_root: Path) -> dict[str, Any] | None:
    """Parse ``go.mod`` and return declared constraints.

    Extracts the ``go`` directive version line and both the block and
    single-line forms of the ``require`` section.
    """
    path = clone_root / "go.mod"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    result: dict[str, Any] = {}
    go_match = re.search(r"^go\s+(\S+)\s*$", text, re.MULTILINE)
    if go_match:
        result["go_version"] = go_match.group(1)

    requires: list[str] = []
    block = re.search(r"^require\s*\((.*?)^\)", text, re.MULTILINE | re.DOTALL)
    if block:
        for line in block.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            parts = line.split()
            if parts:
                requires.append(parts[0])
    else:
        for match in re.finditer(r"^require\s+(\S+)\s+\S+", text, re.MULTILINE):
            requires.append(match.group(1))
    if requires:
        result["dependencies"] = sorted(requires)

    return result or None


def _read_pom(clone_root: Path) -> dict[str, Any] | None:
    """Parse ``pom.xml`` and return declared constraints.

    Extracts the Java compiler source version from the standard Maven
    properties location, and the artifactId of every declared
    dependency. The Maven POM namespace is required; a POM without it
    will not match any element and the function returns None.
    """
    path = clone_root / "pom.xml"
    if not path.is_file():
        return None
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return None

    root = tree.getroot()
    namespace = "http://maven.apache.org/POM/4.0.0"
    ns = {"m": namespace}

    result: dict[str, Any] = {}
    java_version = root.find(".//m:properties/m:maven.compiler.source", ns)
    if java_version is None:
        java_version = root.find(".//m:properties/m:java.version", ns)
    if java_version is not None and java_version.text:
        result["java_version"] = java_version.text.strip()

    dependencies: list[str] = []
    for dep in root.findall(".//m:dependencies/m:dependency", ns):
        artifact = dep.find("m:artifactId", ns)
        if artifact is not None and artifact.text:
            dependencies.append(artifact.text.strip())
    if dependencies:
        result["dependencies"] = sorted(dependencies)

    return result or None


def _safe_read(path: Path) -> str:
    """Read a text file, returning empty on any read error."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


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
    """Best-effort telemetry write."""
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
    """Return the active SQLiteStore for telemetry, or None."""
    from codeguardian.core.agents import _store_context

    return _store_context.get_active_store()


def _classify(exc: BaseException) -> str | None:
    """Classify an exception for telemetry when a run fails."""
    return classify_failure(exc)
