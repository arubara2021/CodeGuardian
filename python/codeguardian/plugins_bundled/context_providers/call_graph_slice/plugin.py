"""Call graph slice context provider.

Returns the callers or callees of one named declaration, one hop by
default, depth configurable. Output is a line per neighbour, with
``path:line name``.
"""

from __future__ import annotations

from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins.context_runtime import current_cpg

_DEFAULT_DIRECTION = "callers"
_DEFAULT_DEPTH = 1
_MAX_DEPTH = 3


class CallGraphSlicePlugin:
    """One-hop callers or callees of a named declaration."""

    name = "call-graph-slice"
    description = "Callers or callees of a named declaration."
    is_read_only = True
    input_schema = {
        "type": "object",
        "required": ["path", "qualified_name"],
        "properties": {
            "path": {"type": "string"},
            "qualified_name": {"type": "string"},
            "direction": {
                "type": "string",
                "enum": ["callers", "callees"],
                "default": _DEFAULT_DIRECTION,
            },
            "depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_DEPTH,
                "default": _DEFAULT_DEPTH,
            },
        },
    }
    output_schema = {"type": "string"}

    __manifest__ = {
        "name": "call-graph-slice",
        "version": "1.0.0",
        "type": "context_provider",
        "interface_version": "1.0",
        "description": "Callers or callees of a named declaration.",
        "capabilities": ["call-graph", "structural-navigation"],
        "permissions": {
            "filesystem": "read_only",
            "network": False,
            "process_spawn": False,
        },
    }

    async def provide(self, input: dict[str, object]) -> str:
        path = input.get("path")
        qualified_name = input.get("qualified_name")
        direction = input.get("direction", _DEFAULT_DIRECTION)
        depth_raw = input.get("depth", _DEFAULT_DEPTH)

        if not isinstance(path, str) or not path:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                "call-graph-slice requires a non-empty 'path' string",
                details={"field": "path"},
            )
        if not isinstance(qualified_name, str) or not qualified_name:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                "call-graph-slice requires a non-empty 'qualified_name' string",
                details={"field": "qualified_name"},
            )
        if direction not in ("callers", "callees"):
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"'direction' must be 'callers' or 'callees'; got {direction!r}",
                details={"field": "direction", "value": direction},
            )
        depth = int(depth_raw) if isinstance(depth_raw, int) else _DEFAULT_DEPTH
        depth = max(1, min(_MAX_DEPTH, depth))

        cpg: Any = current_cpg()
        header = f"# {direction} of {path}:{qualified_name} (depth {depth})"

        seen: set[str] = set()
        lines: list[str] = [header]
        frontier: list[tuple[str, str]] = [(path, qualified_name)]
        for level in range(depth):
            next_frontier: list[tuple[str, str]] = []
            for level_path, level_qualified in frontier:
                try:
                    neighbours = (
                        cpg.callers_of(level_path, level_qualified)
                        if direction == "callers"
                        else cpg.callees_of(level_path, level_qualified)
                    )
                except Exception:
                    continue
                for neighbour in neighbours:
                    key = f"{neighbour.get('file_path', '')}:{neighbour.get('qualified_name', '')}"
                    if key in seen:
                        continue
                    seen.add(key)
                    lines.append(
                        f"{neighbour.get('file_path', '?')}:"
                        f"{neighbour.get('line', 0)} "
                        f"{neighbour.get('qualified_name', '?')} "
                        f"({neighbour.get('kind', '?')}, depth {level + 1})"
                    )
                    next_frontier.append(
                        (
                            neighbour.get("file_path", level_path),
                            neighbour.get("qualified_name", level_qualified),
                        )
                    )
            frontier = next_frontier
            if not frontier:
                break

        if len(lines) == 1:
            lines.append("(no " + direction + " found)")
        return "\n".join(lines)
