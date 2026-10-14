"""MCP server contract.

The MCP server exposes CodeGuardian's capabilities as MCP tools to
external clients. It is not a plugin point, so it is not marked as
runtime-checkable.
"""

from __future__ import annotations

from typing import Literal, Protocol

from .tool import ToolDescriptor

__all__ = ["MCPServer", "MCPTransport"]

MCPTransport = Literal["stdio", "http"]


class MCPServer(Protocol):
    """An MCP server exposing CodeGuardian's tools."""

    async def start(self, transport: MCPTransport) -> None:
        """Start the server and begin accepting clients."""
        ...

    async def stop(self) -> None:
        """Stop the server and release its transport."""
        ...

    def list_tools(self) -> list[ToolDescriptor]:
        """Return the tools this server exposes to clients."""
        ...
