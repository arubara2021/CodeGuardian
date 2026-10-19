"""ast-grep tool plugin.

Structural code search via the ``ast-grep`` CLI. The plugin spawns the
binary with ``asyncio.create_subprocess_exec`` so the fault barrier's
timeout remains effective; a blocking ``subprocess.run`` would prevent
the loop from firing it.

If ``ast-grep`` is not on PATH, ``call`` raises ``SYS_NOT_IMPLEMENTED``
with a clear message. The contract test accepts either outcome.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode, translate

_SUBPROCESS_TIMEOUT = 25.0


class AstGrepPlugin:
    """Structural code search and refactoring via ast-grep."""

    name = "ast-grep"
    description = "Structural code search via ast-grep CLI."
    input_schema = {
        "type": "object",
        "required": ["pattern", "path"],
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "language": {"type": "string"},
        },
    }
    output_schema = {
        "type": "object",
        "required": ["matches"],
        "properties": {"matches": {"type": "array"}},
    }

    __manifest__ = {
        "name": "ast-grep",
        "version": "1.0.0",
        "type": "tool",
        "interface_version": "1.0",
        "description": "Structural code search via ast-grep CLI.",
        "capabilities": ["code-search", "structural-refactoring"],
        "permissions": {
            "filesystem": "read_only",
            "network": False,
            "process_spawn": True,
        },
    }

    async def call(self, input: dict[str, Any]) -> dict[str, Any]:
        pattern = input.get("pattern")
        path = input.get("path")
        language = input.get("language")

        if not isinstance(pattern, str) or not pattern:
            raise CodeGuardianError(
                ErrorCode.PATTERN_INVALID,
                "ast-grep requires a non-empty 'pattern' string",
                details={"field": "pattern"},
            )
        if not isinstance(path, str) or not path:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                "ast-grep requires a non-empty 'path' string",
                details={"field": "path"},
            )

        binary = shutil.which("ast-grep")
        if binary is None:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                "ast-grep binary not found on PATH; install ast-grep to use this tool",
                details={"binary": "ast-grep"},
            )

        cmd = [binary, "run", "--pattern", pattern]
        if isinstance(language, str) and language:
            cmd.extend(["--lang", language])
        cmd.extend(["--json=stream", path])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise translate(exc) from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=_SUBPROCESS_TIMEOUT
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise CodeGuardianError(
                ErrorCode.PLUGIN_TIMEOUT,
                f"ast-grep did not finish within {_SUBPROCESS_TIMEOUT}s",
                details={"pattern": pattern, "path": path},
            ) from exc

        if proc.returncode not in (0, 1):
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"ast-grep exited with code {proc.returncode}: "
                f"{stderr_text[:500] or '<no stderr>'}",
                details={
                    "exit_code": proc.returncode,
                    "pattern": pattern,
                    "path": path,
                },
            )

        matches: list[dict[str, Any]] = []
        for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                matches.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue

        return {"matches": matches}
