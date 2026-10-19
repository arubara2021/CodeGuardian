"""In-process structural analysis tool plugin.

Two analyses, both driven by the kernel:

- ``dead-code``: parses the file, enumerates Python functions, asks the
  blast engine for each function's callers, and reports the ones with
  zero direct and zero transitive callers. Entry points (``main``,
  dunder methods, test functions) are excluded so a normal library file
  does not report every public function as dead.
- ``complexity``: reports an empty dict with a note. Per-function
  cyclomatic complexity requires a kernel binding that does not exist
  yet (``compute_blast`` returns caller counts, ``graph_summary``
  returns aggregate graph counts; neither yields a per-function CFG
  edge count). The note records this honestly.

Dead-code analysis is Python-only. Other languages return an empty list
with a note explaining the limitation.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode, translate

_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
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

_SKIP_FUNCTION_NAMES = frozenset(
    {
        "main",
        "setUp",
        "tearDown",
        "setUpClass",
        "tearDownClass",
        "setUpModule",
        "tearDownModule",
    }
)


class CodeAnalysisPlugin:
    """Structural analysis: dead code (Python) and complexity placeholder."""

    name = "code-analysis"
    description = "Structural analysis: dead code detection and complexity."
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string"},
            "analysis": {
                "type": "string",
                "enum": ["complexity", "dead-code", "all"],
                "default": "all",
            },
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "complexity": {"type": "object"},
            "dead_code": {"type": "array"},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
    }

    __manifest__ = {
        "name": "code-analysis",
        "version": "1.0.0",
        "type": "tool",
        "interface_version": "1.0",
        "description": "In-process structural analysis.",
        "capabilities": ["complexity", "dead-code", "structure"],
        "permissions": {
            "filesystem": "read_only",
            "network": False,
            "process_spawn": False,
        },
    }

    async def call(self, input: dict[str, Any]) -> dict[str, Any]:
        path_value = input.get("path")
        analysis = input.get("analysis", "all")

        if not isinstance(path_value, str) or not path_value:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                "code-analysis requires a non-empty 'path' string",
                details={"field": "path"},
            )
        if analysis not in ("complexity", "dead-code", "all"):
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"code-analysis 'analysis' must be one of complexity, "
                f"dead-code, all; got {analysis!r}",
                details={"field": "analysis", "value": analysis},
            )

        path = Path(path_value)
        if not path.is_file():
            raise CodeGuardianError(
                ErrorCode.FILE_NOT_FOUND,
                f"code-analysis cannot read {path}",
                details={"path": str(path)},
            )

        result: dict[str, Any] = {}
        notes: list[str] = []

        if analysis in ("dead-code", "all"):
            dead = self._dead_code(path, notes)
            result["dead_code"] = dead

        if analysis in ("complexity", "all"):
            result["complexity"] = {}
            notes.append(
                "cyclomatic complexity requires a kernel binding that "
                "reports per-function CFG edge counts; not available yet"
            )

        if notes:
            result["notes"] = notes
        return result

    def _dead_code(self, path: Path, notes: list[str]) -> list[dict[str, Any]]:
        language = _LANGUAGE_BY_SUFFIX.get(path.suffix)
        if language is None:
            notes.append(f"dead-code analysis does not support extension {path.suffix or '<none>'}")
            return []
        if language != "python":
            notes.append(f"dead-code analysis is Python-only; {language} not supported")
            return []

        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise translate(exc) from exc
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"code-analysis could not parse {path}: {exc.msg}",
                details={"path": str(path), "line": exc.lineno},
            ) from exc

        from codeguardian import _native

        try:
            cpg = _native.build_graph([(str(path), language, source)])
        except Exception as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"code-analysis could not build graph for {path}: {exc}",
                details={"path": str(path)},
            ) from exc

        candidates = _collect_python_functions(tree)
        dead: list[dict[str, Any]] = []
        for simple, qualified in candidates:
            if _is_exempt(simple):
                continue
            try:
                report = _native.compute_blast(cpg, str(path), qualified)
            except Exception:
                continue
            if not isinstance(report, dict):
                continue
            direct = report.get("direct_callers") or []
            transitive = report.get("transitive_callers") or []
            if len(direct) == 0 and len(transitive) == 0:
                dead.append(
                    {
                        "function": simple,
                        "qualified_name": qualified,
                        "reason": "no direct or transitive callers",
                    }
                )
        return dead


def _collect_python_functions(tree: ast.AST) -> list[tuple[str, str]]:
    """Yield ``(simple_name, qualified_name)`` for every function."""
    results: list[tuple[str, str]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{prefix}{child.name}"
                results.append((child.name, qualified))
                visit(child, f"{qualified}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return results


def _is_exempt(name: str) -> bool:
    if name in _SKIP_FUNCTION_NAMES:
        return True
    if name.startswith("__") and name.endswith("__"):
        return True
    return name.startswith("test_") or name.endswith("_test")
