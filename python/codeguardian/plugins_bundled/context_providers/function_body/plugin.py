"""Function body context provider.

Returns the source of one named declaration, exactly as it appears in
the file. The output is not decorated with a header or line numbers;
the AI sees the raw source and nothing else.

# Why the raw source

The Writer agent edits source. It needs to see what it will be
editing, not a summary of it. A header or a marker comment would
appear in the diff the Writer produces, which is wrong. So the
provider returns the source slice verbatim.

# Known limitation: decorators

The CPG's ``start_byte`` for a Python ``function_definition`` points
at the ``def`` keyword, not at any preceding decorators. A decorated
function returns without its ``@`` lines. The AI can detect that a
decorator exists by reading the code skeleton (which does include
decorators for Python) and, if it needs the exact decorator, fall back
to reading the file. This is a documented limitation, not a bug.
"""

from __future__ import annotations

from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins.context_runtime import current_clone_root, current_cpg


class FunctionBodyPlugin:
    """Full source of one named declaration."""

    name = "function-body"
    description = "Full source of a named function, method, or class."
    is_read_only = True
    input_schema = {
        "type": "object",
        "required": ["path", "qualified_name"],
        "properties": {
            "path": {"type": "string"},
            "qualified_name": {"type": "string"},
        },
    }
    output_schema = {"type": "string"}

    __manifest__ = {
        "name": "function-body",
        "version": "1.0.0",
        "type": "context_provider",
        "interface_version": "1.0",
        "description": "Full source of a named function, method, or class.",
        "capabilities": ["source-slice", "function-body"],
        "permissions": {
            "filesystem": "read_only",
            "network": False,
            "process_spawn": False,
        },
    }

    async def provide(self, input: dict[str, object]) -> str:
        path = input.get("path")
        qualified_name = input.get("qualified_name")
        if not isinstance(path, str) or not path:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                "function-body requires a non-empty 'path' string",
                details={"field": "path"},
            )
        if not isinstance(qualified_name, str) or not qualified_name:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                "function-body requires a non-empty 'qualified_name' string",
                details={"field": "qualified_name"},
            )

        cpg: Any = current_cpg()
        clone_root = current_clone_root()

        try:
            return cpg.function_source(path, qualified_name, str(clone_root))
        except Exception as exc:
            if isinstance(exc, CodeGuardianError):
                raise
            code = getattr(exc, "code", None)
            if code == "TARGET_NOT_FOUND":
                return f"(declaration {qualified_name!r} not found in {path})"
            if code == "FILE_NOT_FOUND":
                return f"(file {path!r} not found in graph)"
            if code == "PATH_ESCAPES_ROOT":
                return f"(path {path!r} escapes clone root)"
            if code == "STALE_OFFSETS":
                return f"(graph offsets stale for {path}; rebuild the graph)"
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"function-body failed: {exc}",
                details={"path": path, "qualified_name": qualified_name},
            ) from exc
