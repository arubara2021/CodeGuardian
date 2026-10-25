"""List symbols context provider.

Returns the names of every declaration in a file, one per line, with
line number and kind. Cheaper than ``code-skeleton`` because it does
not read or slice the source; it only queries the CPG.

Use this when the AI needs names but not signatures. Use
``code-skeleton`` when it needs signatures.
"""

from __future__ import annotations

from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins.context_runtime import current_cpg


class ListSymbolsPlugin:
    """Names of every declaration in a file."""

    name = "list-symbols"
    description = "Names of every declaration in a file, with line and kind."
    is_read_only = True
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {"path": {"type": "string"}},
    }
    output_schema = {"type": "string"}

    __manifest__ = {
        "name": "list-symbols",
        "version": "1.0.0",
        "type": "context_provider",
        "interface_version": "1.0",
        "description": "Names of every declaration in a file, with line and kind.",
        "capabilities": ["symbol-list", "names-only"],
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
                "list-symbols requires a non-empty 'path' string",
                details={"field": "path"},
            )
        cpg: Any = current_cpg()
        try:
            symbols = cpg.list_symbols(path)
        except Exception as exc:
            return f"(cannot list symbols in {path}: {exc})"
        if not symbols:
            return f"{path}: (no declarations)"
        lines: list[str] = [f"{path}:"]
        for sym in symbols:
            line_no = sym.get("line", 0)
            name = sym.get("name", "?")
            kind = sym.get("kind", "?")
            qualified = sym.get("qualified_name", name)
            lines.append(f"  {line_no}: {qualified} ({kind})")
        return "\n".join(lines)
