"""Code skeleton context provider.

Returns the signature-level skeleton of a source file: declarations
with their parameters and return type, but no function bodies.

# Why this exists

Reading a full file costs tokens. Reading its signatures costs a
fraction of that and answers the question "what is in this file?"
Reading the body of one named function costs about the same as the
skeleton and answers "what does this function do?" The skeleton is
the cheapest way to answer the first question before deciding which
second question to ask.

# Python exact, other languages best-effort

For Python files the skeleton is extracted from the AST with
``ast.parse`` and ``ast.unparse``, so signatures are exact including
decorators, default arguments, and return annotations.

For other languages the provider calls ``PyCPG.function_source`` for
each declaration and cuts at the first ``{`` character. That gives a
signature in the common case (TypeScript, Java, Go, Rust, C, C++)
and a first-line-only signature when the function has no opening
brace on the signature line. The heuristic is documented because it
is a heuristic.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins.context_runtime import current_clone_root, current_cpg

_DEFAULT_MAX_LINES = 200


class CodeSkeletonPlugin:
    """Signature-level summary of a source file."""

    name = "code-skeleton"
    description = "Signature-level summary of a source file (no function bodies)."
    is_read_only = True
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the clone root.",
            },
            "max_lines": {
                "type": "integer",
                "minimum": 1,
                "default": _DEFAULT_MAX_LINES,
            },
        },
    }
    output_schema = {"type": "string"}

    __manifest__ = {
        "name": "code-skeleton",
        "version": "1.0.0",
        "type": "context_provider",
        "interface_version": "1.0",
        "description": "Signature-level summary of a source file (no function bodies).",
        "capabilities": ["structural-summary", "signatures-only"],
        "permissions": {
            "filesystem": "read_only",
            "network": False,
            "process_spawn": False,
        },
    }

    async def provide(self, input: dict[str, object]) -> str:
        path = input.get("path")
        if not isinstance(path, str) or not path:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                "code-skeleton requires a non-empty 'path' string",
                details={"field": "path"},
            )
        raw_max = input.get("max_lines", _DEFAULT_MAX_LINES)
        max_lines = int(raw_max) if isinstance(raw_max, int) else _DEFAULT_MAX_LINES

        cpg = current_cpg()
        clone_root = current_clone_root()

        if path.endswith(".py") or path.endswith(".pyi"):
            return self._skeleton_python(cpg, path, clone_root, max_lines)
        return self._skeleton_generic(cpg, path, clone_root, max_lines)

    def _skeleton_python(self, cpg: Any, path: str, clone_root: Path, max_lines: int) -> str:
        try:
            source_path = (clone_root / path).resolve()
            source_path.relative_to(clone_root.resolve())
        except (ValueError, OSError):
            return f"(path escapes clone root: {path})"
        try:
            source = source_path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"(cannot read {path}: {exc})"
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return f"(cannot parse {path}: {exc.msg} at line {exc.lineno})"

        entries = _walk_python_declarations(tree)
        if not entries:
            return f"{path}: (no declarations)"

        lines: list[str] = [f"{path}:"]
        truncated = False
        for indent, line, signature in entries:
            if len(lines) - 1 >= max_lines:
                truncated = True
                break
            prefix = "  " * indent
            lines.append(f"{line}: {prefix}{signature}")
        if truncated:
            remaining = len(entries) - (len(lines) - 1)
            lines.append(f"... ({remaining} more declarations not shown)")
        return "\n".join(lines)

    def _skeleton_generic(self, cpg: Any, path: str, clone_root: Path, max_lines: int) -> str:
        try:
            symbols = cpg.list_symbols(path)
        except Exception as exc:
            return f"(cannot list symbols in {path}: {exc})"
        if not symbols:
            return f"{path}: (no declarations)"

        lines: list[str] = [f"{path}:"]
        truncated = False
        for sym in symbols:
            if len(lines) - 1 >= max_lines:
                truncated = True
                break
            name = sym.get("name", "")
            qualified = sym.get("qualified_name", name)
            line_no = sym.get("line", 0)
            kind = sym.get("kind", "?")
            try:
                src = cpg.function_source(path, qualified, str(clone_root))
            except Exception:
                lines.append(f"{line_no}: {kind} {qualified}")
                continue
            sig = _extract_generic_signature(src)
            lines.append(f"{line_no}: {sig}")
        if truncated:
            remaining = len(symbols) - (len(lines) - 1)
            lines.append(f"... ({remaining} more declarations not shown)")
        return "\n".join(lines)


def _walk_python_declarations(
    tree: ast.AST, prefix: str = "", depth: int = 0
) -> list[tuple[int, int, str]]:
    """Return ``(depth, line, signature)`` for every declaration."""
    entries: list[tuple[int, int, str]] = []
    for child in ast.iter_child_nodes(tree):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        sig = _format_python_signature(child)
        if sig is not None:
            entries.append((depth, child.lineno, sig))
        if isinstance(child, ast.ClassDef):
            entries.extend(_walk_python_declarations(child, prefix, depth + 1))
    return entries


def _format_python_signature(node: ast.AST) -> str | None:
    decorators: list[str] = []
    if hasattr(node, "decorator_list"):
        for dec in getattr(node, "decorator_list", []):
            decorators.append(f"@{ast.unparse(dec)}")
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        args = ast.unparse(node.args)
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        sig = f"{kw} {node.name}({args}){ret}"
    elif isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        sig = f"class {node.name}({bases})" if bases else f"class {node.name}"
    else:
        return None
    if decorators:
        return "\n".join([*decorators, sig])
    return sig


def _extract_generic_signature(source: str) -> str:
    brace = source.find("{")
    if brace >= 0:
        return source[:brace].rstrip()
    for line in source.splitlines():
        if line.strip():
            return line.rstrip()
    return ""
