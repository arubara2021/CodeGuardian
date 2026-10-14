"""JSON-RPC wire format for the out-of-process Rust bridge.

The bridge speaks JSON-RPC 2.0 over a subprocess's stdio. This module
defines the envelope classes and the canonical method names used on
the wire. Transport is implemented by ``jsonrpc_bridge``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "AUDIT_EXPORT",
    "AUDIT_VERIFY",
    "AUDIT_WRITE",
    "RpcError",
    "RpcNotification",
    "RpcRequest",
    "RpcResponse",
    "SANDBOX_CREATE",
    "SANDBOX_DESTROY",
    "SANDBOX_EXECUTE",
    "SANDBOX_HEALTH",
]


AUDIT_WRITE: Final[str] = "audit.write"
AUDIT_VERIFY: Final[str] = "audit.verify"
AUDIT_EXPORT: Final[str] = "audit.export"

SANDBOX_CREATE: Final[str] = "sandbox.create"
SANDBOX_EXECUTE: Final[str] = "sandbox.execute"
SANDBOX_DESTROY: Final[str] = "sandbox.destroy"
SANDBOX_HEALTH: Final[str] = "sandbox.health"


@dataclass(frozen=True, slots=True)
class RpcError:
    """A JSON-RPC error object."""

    code: int
    message: str
    data: object | None = None


@dataclass(frozen=True, slots=True)
class RpcRequest:
    """A JSON-RPC request awaiting a response."""

    id: str | int
    method: str
    params: dict[str, object]


@dataclass(frozen=True, slots=True)
class RpcResponse:
    """A JSON-RPC response to a prior request.

    Exactly one of ``result`` and ``error`` is populated.
    """

    id: str | int
    result: object | None = None
    error: RpcError | None = None


@dataclass(frozen=True, slots=True)
class RpcNotification:
    """A JSON-RPC notification: a request with no id and no response."""

    method: str
    params: dict[str, object]
