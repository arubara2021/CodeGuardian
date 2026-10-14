"""MCP client contract.

The MCP client discovers and calls tools exposed by external MCP
servers. It is a singleton within a run, not a plugin point, so it is
not marked as runtime-checkable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .tool import ToolDescriptor

__all__ = ["MCPClient", "ToolResult"]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The result of an MCP tool call."""

    content: object
    is_error: bool = False


class MCPClient(Protocol):
    """Client for external Model Context Protocol servers."""

    async def discover_tools(self, server_name: str) -> list[ToolDescriptor]:
        """Return the tools exposed by ``server_name``."""
        ...

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        input: dict[str, object],
    ) -> ToolResult:
        """Call a tool on ``server_name`` and return its result."""
        ...

    def list_servers(self) -> list[str]:
        """Return the names of registered MCP servers."""
        ...
