"""Tool plugin contract.

A tool is an MCP-style callable that CodeGuardian can invoke.
Plugins are validated against this protocol at load time using
``isinstance``.

# Action classification

Every tool declares what it *does*, not just what it *takes*.
``action_kind`` tells the approval layer how to gate the call:

- ``read`` — reads files or the CPG. No side effects. Safe for any mode.
- ``write`` — writes files. Requires an agent in ``auto-edit`` mode.
- ``execute`` — spawns a subprocess. Requires an agent in ``auto-edit``
  mode and a sandbox that permits the spawn.
- ``network`` — reaches the network. Requires an agent in ``workspace``
  or ``yolo`` mode and a sandbox with network enabled.

Without ``action_kind`` the approval layer cannot distinguish a tool
that reads from a tool that writes. A ``plan``-mode agent could call a
write tool because the tool's manifest declared the permission. That is
the exact loophole this field closes.

# Sensitivity

``sensitivity`` is a hint for telemetry and budget accounting. It does
not gate the call. A ``high`` sensitivity tool is one whose output is
expensive to inspect, expensive to run, or likely to influence the run
in a large way. The pipeline uses this to prioritize which calls to log
verbosely in the audit trail.

# Read-only flag

``is_read_only`` is a shortcut for tools whose behavior is provably free
of side effects. It must agree with ``action_kind == "read"``. A tool
that declares ``action_kind = "write"`` and ``is_read_only = True`` is
a contract violation caught by the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

__all__ = ["ActionKind", "Sensitivity", "ToolDescriptor", "ToolPlugin"]

ActionKind = Literal["read", "write", "execute", "network"]
Sensitivity = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """Metadata describing a callable tool.

    Used by the MCP client to describe tools discovered from external
    servers without instantiating a plugin.
    """

    name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]


@runtime_checkable
class ToolPlugin(Protocol):
    """An MCP-style tool contributed by a plugin."""

    name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]

    action_kind: ActionKind
    sensitivity: Sensitivity
    is_read_only: bool

    async def call(self, input: dict[str, object]) -> dict[str, object]:
        """Invoke the tool with a JSON-shaped input and return its output."""
        ...
