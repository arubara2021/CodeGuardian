"""Every error code CodeGuardian can raise, as plain string constants.

Codes are grouped by the component that raises them. The grouping is a
reading aid only: the authoritative classification of each code into a
category lives in ``taxonomy.py``. Nothing in this module imports another
CodeGuardian module, so anything that needs to reference a code as a
literal string can import from here without pulling in the taxonomy.

Codes are never renamed once shipped. Downstream systems, audit entries,
and user-facing documentation reference these strings by value.
"""

__all__ = [
    "APPLY_AUTH_FAILED",
    "APPLY_CONFLICT",
    "APPLY_PARTIAL",
    "DIFF_COMPUTE_FAILED",
    "FILE_BINARY",
    "FILE_NOT_FOUND",
    "FILE_PERMISSION_DENIED",
    "FILE_TOO_LARGE",
    "MCP_PROTOCOL_ERROR",
    "MCP_SERVER_UNAVAILABLE",
    "MCP_TOOL_ERROR",
    "MCP_TOOL_NOT_FOUND",
    "MCP_TOOL_TIMEOUT",
    "METADATA_UNAVAILABLE",
    "MODEL_AUTH_FAILED",
    "MODEL_CONTEXT_EXCEEDED",
    "MODEL_RATE_LIMITED",
    "MODEL_UNAVAILABLE",
    "MODEL_UNKNOWN",
    "NET_DNS_FAILED",
    "NET_TIMEOUT",
    "NET_UNREACHABLE",
    "PATTERN_INVALID",
    "PLUGIN_DISABLED",
    "PLUGIN_LOAD_FAILED",
    "PLUGIN_MANIFEST_INVALID",
    "PLUGIN_PERMISSION_DENIED",
    "PROVIDER_UNAVAILABLE",
    "REPO_AUTH_FAILED",
    "REPO_CLONE_FAILED",
    "REPO_NOT_FOUND",
    "ROLLBACK_FAILED",
    "ROLLBACK_REF_NOT_FOUND",
    "SANDBOX_CREATE_FAILED",
    "SANDBOX_EXEC_FAILED",
    "SANDBOX_IMAGE_UNAVAILABLE",
    "SANDBOX_NOT_FOUND",
    "SANDBOX_OOM",
    "SANDBOX_QUOTA_EXCEEDED",
    "SANDBOX_TIMEOUT",
    "SYS_INTERNAL",
    "SYS_INVARIANT_VIOLATED",
    "SYS_NOT_IMPLEMENTED",
]

# Repository access codes are raised while reading from a local path, a
# GitHub repository, or a GitLab repository, and by the file-level reads
# those providers expose. REPO_CLONE_FAILED is classified as a network
# error because the dominant failure mode is a transient remote issue;
# the retry policy naturally gives up on a permanently bad URL.
REPO_CLONE_FAILED = "REPO_CLONE_FAILED"
REPO_AUTH_FAILED = "REPO_AUTH_FAILED"
REPO_NOT_FOUND = "REPO_NOT_FOUND"
PATTERN_INVALID = "PATTERN_INVALID"
DIFF_COMPUTE_FAILED = "DIFF_COMPUTE_FAILED"
METADATA_UNAVAILABLE = "METADATA_UNAVAILABLE"
FILE_NOT_FOUND = "FILE_NOT_FOUND"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
FILE_BINARY = "FILE_BINARY"

# FILE_PERMISSION_DENIED is distinct from FILE_NOT_FOUND because the
# correct user-facing message is different. "File not found" tells the
# user their path is wrong. "Permission denied" tells them the path is
# correct but the process cannot read it, which points at file mode,
# ownership, or an ACL. Collapsing the two would mislead the user.
FILE_PERMISSION_DENIED = "FILE_PERMISSION_DENIED"

# Apply codes are raised by the ApplyProvider, which is instantiated only
# after the user grants explicit consent. A partial apply is a system
# error rather than a user error because the rollback reference exists
# and a retry can complete the change.
APPLY_CONFLICT = "APPLY_CONFLICT"
APPLY_AUTH_FAILED = "APPLY_AUTH_FAILED"
APPLY_PARTIAL = "APPLY_PARTIAL"
ROLLBACK_FAILED = "ROLLBACK_FAILED"
ROLLBACK_REF_NOT_FOUND = "ROLLBACK_REF_NOT_FOUND"

# Model provider codes. Authentication failures are classified as auth
# even though they carry a MODEL_ prefix, because the correct response is
# the same as for any other credential problem: stop and report.
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
MODEL_CONTEXT_EXCEEDED = "MODEL_CONTEXT_EXCEEDED"
MODEL_AUTH_FAILED = "MODEL_AUTH_FAILED"
MODEL_UNKNOWN = "MODEL_UNKNOWN"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

# Sandbox codes. These are raised by the Sandbox Engine, never by the
# Python orchestrator directly, because the orchestrator does not execute
# untrusted code.
SANDBOX_CREATE_FAILED = "SANDBOX_CREATE_FAILED"
SANDBOX_IMAGE_UNAVAILABLE = "SANDBOX_IMAGE_UNAVAILABLE"
SANDBOX_QUOTA_EXCEEDED = "SANDBOX_QUOTA_EXCEEDED"
SANDBOX_TIMEOUT = "SANDBOX_TIMEOUT"
SANDBOX_OOM = "SANDBOX_OOM"
SANDBOX_EXEC_FAILED = "SANDBOX_EXEC_FAILED"
SANDBOX_NOT_FOUND = "SANDBOX_NOT_FOUND"

# Model Context Protocol codes. They cover both directions: CodeGuardian
# as a client consuming external MCP tools, and CodeGuardian as a server
# exposing its own tools to IDEs and other agents.
MCP_SERVER_UNAVAILABLE = "MCP_SERVER_UNAVAILABLE"
MCP_PROTOCOL_ERROR = "MCP_PROTOCOL_ERROR"
MCP_TOOL_NOT_FOUND = "MCP_TOOL_NOT_FOUND"
MCP_TOOL_TIMEOUT = "MCP_TOOL_TIMEOUT"
MCP_TOOL_ERROR = "MCP_TOOL_ERROR"

# Plugin codes. A plugin failure is isolated by the fault barrier and
# never propagates as an unhandled exception; it is converted into one of
# these codes so the rest of the run continues.
PLUGIN_LOAD_FAILED = "PLUGIN_LOAD_FAILED"
PLUGIN_MANIFEST_INVALID = "PLUGIN_MANIFEST_INVALID"
PLUGIN_PERMISSION_DENIED = "PLUGIN_PERMISSION_DENIED"
PLUGIN_DISABLED = "PLUGIN_DISABLED"

# Network codes are raised by the HTTP client and the git subprocess
# wrapper. They are the only category with a retry budget of five
# attempts, because transient network failures are the most common cause
# of a single-shot failure that resolves on retry.
NET_TIMEOUT = "NET_TIMEOUT"
NET_UNREACHABLE = "NET_UNREACHABLE"
NET_DNS_FAILED = "NET_DNS_FAILED"

# Internal codes indicate a defect in CodeGuardian itself, not a failure
# of an external system. SYS_INVARIANT_VIOLATED in particular is only
# raised when an assertion that the codebase holds as true has been
# falsified, which is always a bug.
SYS_INTERNAL = "SYS_INTERNAL"
SYS_NOT_IMPLEMENTED = "SYS_NOT_IMPLEMENTED"
SYS_INVARIANT_VIOLATED = "SYS_INVARIANT_VIOLATED"
