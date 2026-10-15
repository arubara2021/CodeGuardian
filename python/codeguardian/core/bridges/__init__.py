"""Python-to-Rust bridge layer.

Two bridges carry calls across the language boundary. The PyO3 bridge
is in-process and used for trusted, high-volume calls. The JSON-RPC
bridge is out-of-process and used for isolation-critical calls. The
wire format is defined in ``protocol``.
"""

from .protocol import (
    AUDIT_EXPORT,
    AUDIT_VERIFY,
    AUDIT_WRITE,
    SANDBOX_CREATE,
    SANDBOX_DESTROY,
    SANDBOX_EXECUTE,
    SANDBOX_HEALTH,
    RpcError,
    RpcNotification,
    RpcRequest,
    RpcResponse,
)

__all__ = [
    "AUDIT_EXPORT",
    "AUDIT_VERIFY",
    "AUDIT_WRITE",
    "SANDBOX_CREATE",
    "SANDBOX_DESTROY",
    "SANDBOX_EXECUTE",
    "SANDBOX_HEALTH",
    "RpcError",
    "RpcNotification",
    "RpcRequest",
    "RpcResponse",
]
