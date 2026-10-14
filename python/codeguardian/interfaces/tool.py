"""Tool plugin contract.

A tool is an MCP-style callable that CodeGuardian can invoke.
Plugins are validated against this protocol at load time using
``isinstance``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["ToolDescriptor", "ToolPlugin"]


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

    async def call(self, input: dict[str, object]) -> dict[str, object]:
        """Invoke the tool with a JSON-shaped input and return its output."""
        ...
