"""Classification and retry policy for every CodeGuardian error code.

The taxonomy is the vocabulary every component uses to describe failure.
Instead of each subsystem inventing its own strings, raise sites pick a
member of ``ErrorCode``, and the orchestrator, the CLI, and the MCP server
consult the same classification to decide whether to retry, escalate, or
map to an exit code.

Two invariants are enforced by the test suite:

1. Every member of ``ErrorCode`` appears exactly once in ``CATEGORY_MAP``,
   and every value in that map is a member of ``ErrorCategory``.
2. Every member of ``ErrorCategory`` appears exactly once in
   ``RETRY_POLICY``, and every value is a ``RetryPolicy``.

``codes.py`` and the ``ErrorCode`` enum below are also kept in exact
bijection: every module-level uppercase constant in ``codes.py`` is an
enum member, and every enum member has a matching constant. Adding a new
code means adding it to both files; the test catches any drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

from . import codes

__all__ = [
    "Backoff",
    "CodeGuardianError",
    "ErrorCategory",
    "ErrorCode",
    "RetryPolicy",
    "UnknownCodeError",
    "category_of",
    "retry_policy_of",
]


class Backoff(str, Enum):
    """Retry delay strategy attached to a category's retry policy.

    The concrete delay values (base seconds, jitter, cap) are the
    responsibility of the retry engine in ``errors/retry.py``. This enum
    only names the strategy so the taxonomy stays decoupled from any
    specific timing implementation.
    """

    NONE = "none"
    FIXED = "fixed"
    EXPONENTIAL = "exponential"


class ErrorCategory(str, Enum):
    """The seven failure categories CodeGuardian recognizes.

    Categories are the unit of retry policy. Codes are classified into a
    category by the failure's cause, not by its prefix. ``MODEL_AUTH_FAILED``
    and ``REPO_AUTH_FAILED`` are both auth errors despite the different
    prefixes, because the correct response to each is the same.
    """

    USER = "user"
    SYSTEM = "system"
    MODEL = "model"
    SANDBOX = "sandbox"
    PLUGIN = "plugin"
    NETWORK = "network"
    AUTH = "auth"


class ErrorCode(str, Enum):
    """Every error code CodeGuardian can raise.

    The member name and value are identical strings, both taken from
    ``codes.py``. Inheriting from ``str`` means the enum is directly
    serializable to JSON and comparable to its string value, which is
    what the audit log and the MCP error envelope require.

    Using an enum instead of raw strings gives raise sites IDE
    autocomplete, turns a typo into an immediate import-time failure
    rather than a silent miss, and lets mypy verify coverage.
    """

    REPO_CLONE_FAILED = codes.REPO_CLONE_FAILED
    REPO_AUTH_FAILED = codes.REPO_AUTH_FAILED
    REPO_NOT_FOUND = codes.REPO_NOT_FOUND
    PATTERN_INVALID = codes.PATTERN_INVALID
    DIFF_COMPUTE_FAILED = codes.DIFF_COMPUTE_FAILED
    METADATA_UNAVAILABLE = codes.METADATA_UNAVAILABLE
    FILE_NOT_FOUND = codes.FILE_NOT_FOUND
    FILE_TOO_LARGE = codes.FILE_TOO_LARGE
    FILE_BINARY = codes.FILE_BINARY
    FILE_PERMISSION_DENIED = codes.FILE_PERMISSION_DENIED

    APPLY_CONFLICT = codes.APPLY_CONFLICT
    APPLY_AUTH_FAILED = codes.APPLY_AUTH_FAILED
    APPLY_PARTIAL = codes.APPLY_PARTIAL
    ROLLBACK_FAILED = codes.ROLLBACK_FAILED
    ROLLBACK_REF_NOT_FOUND = codes.ROLLBACK_REF_NOT_FOUND

    MODEL_UNAVAILABLE = codes.MODEL_UNAVAILABLE
    MODEL_RATE_LIMITED = codes.MODEL_RATE_LIMITED
    MODEL_CONTEXT_EXCEEDED = codes.MODEL_CONTEXT_EXCEEDED
    MODEL_AUTH_FAILED = codes.MODEL_AUTH_FAILED
    MODEL_UNKNOWN = codes.MODEL_UNKNOWN
    PROVIDER_UNAVAILABLE = codes.PROVIDER_UNAVAILABLE

    SANDBOX_CREATE_FAILED = codes.SANDBOX_CREATE_FAILED
    SANDBOX_IMAGE_UNAVAILABLE = codes.SANDBOX_IMAGE_UNAVAILABLE
    SANDBOX_QUOTA_EXCEEDED = codes.SANDBOX_QUOTA_EXCEEDED
    SANDBOX_TIMEOUT = codes.SANDBOX_TIMEOUT
    SANDBOX_OOM = codes.SANDBOX_OOM
    SANDBOX_EXEC_FAILED = codes.SANDBOX_EXEC_FAILED
    SANDBOX_NOT_FOUND = codes.SANDBOX_NOT_FOUND

    MCP_SERVER_UNAVAILABLE = codes.MCP_SERVER_UNAVAILABLE
    MCP_PROTOCOL_ERROR = codes.MCP_PROTOCOL_ERROR
    MCP_TOOL_NOT_FOUND = codes.MCP_TOOL_NOT_FOUND
    MCP_TOOL_TIMEOUT = codes.MCP_TOOL_TIMEOUT
    MCP_TOOL_ERROR = codes.MCP_TOOL_ERROR

    PLUGIN_LOAD_FAILED = codes.PLUGIN_LOAD_FAILED
    PLUGIN_MANIFEST_INVALID = codes.PLUGIN_MANIFEST_INVALID
    PLUGIN_PERMISSION_DENIED = codes.PLUGIN_PERMISSION_DENIED
    PLUGIN_DISABLED = codes.PLUGIN_DISABLED

    NET_TIMEOUT = codes.NET_TIMEOUT
    NET_UNREACHABLE = codes.NET_UNREACHABLE
    NET_DNS_FAILED = codes.NET_DNS_FAILED

    SYS_INTERNAL = codes.SYS_INTERNAL
    SYS_NOT_IMPLEMENTED = codes.SYS_NOT_IMPLEMENTED
    SYS_INVARIANT_VIOLATED = codes.SYS_INVARIANT_VIOLATED


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """The retry behavior attached to an error category.

    ``retryable`` is redundant with ``max_retries > 0`` by design. It is
    explicit so that callers reading the code do not have to infer the
    boolean from the count. The two fields must agree; the test suite
    enforces the invariant.

    ``max_retries`` counts retries after the first attempt, not total
    attempts. A policy of ``max_retries=3`` means one initial attempt
    followed by up to three retries, for four attempts total.
    """

    retryable: bool
    max_retries: int
    backoff: Backoff


class UnknownCodeError(KeyError):
    """Raised when a code is not present in the taxonomy.

    This indicates a defect in CodeGuardian itself: a raise site used a
    code that exists in neither ``codes.py`` nor the ``ErrorCode`` enum,
    or the taxonomy was edited without updating both. It is deliberately
    a ``KeyError`` rather than a ``CodeGuardianError`` so it cannot be
    caught by the same handlers that swallow real run errors. A broken
    taxonomy should stop the process, not be retried.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _resolve_code(code: ErrorCode | str) -> ErrorCode:
    """Convert a code or its string value into an ``ErrorCode`` member.

    Accepting both forms at the public lookup boundary lets raise sites
    pass either the enum member or the raw string they already have in
    hand without a wrapping call. Anything that is neither raises
    ``UnknownCodeError``.
    """
    if isinstance(code, ErrorCode):
        return code
    try:
        return ErrorCode(code)
    except ValueError as exc:
        raise UnknownCodeError(str(code)) from exc


# The category of every code. This is a complete mapping: the test suite
# asserts that the key set equals the ErrorCode member set. Prefix does
# not determine category; the semantic cause of the failure does. For
# example, MODEL_AUTH_FAILED is an auth error, not a model error, because
# the correct response is the same as for REPO_AUTH_FAILED.
CATEGORY_MAP: Mapping[ErrorCode, ErrorCategory] = MappingProxyType(
    {
        ErrorCode.REPO_CLONE_FAILED: ErrorCategory.NETWORK,
        ErrorCode.REPO_AUTH_FAILED: ErrorCategory.AUTH,
        ErrorCode.REPO_NOT_FOUND: ErrorCategory.USER,
        ErrorCode.PATTERN_INVALID: ErrorCategory.USER,
        ErrorCode.DIFF_COMPUTE_FAILED: ErrorCategory.SYSTEM,
        ErrorCode.METADATA_UNAVAILABLE: ErrorCategory.SYSTEM,
        ErrorCode.FILE_NOT_FOUND: ErrorCategory.USER,
        ErrorCode.FILE_TOO_LARGE: ErrorCategory.USER,
        ErrorCode.FILE_BINARY: ErrorCategory.USER,
        ErrorCode.FILE_PERMISSION_DENIED: ErrorCategory.USER,
        ErrorCode.APPLY_CONFLICT: ErrorCategory.SYSTEM,
        ErrorCode.APPLY_AUTH_FAILED: ErrorCategory.AUTH,
        ErrorCode.APPLY_PARTIAL: ErrorCategory.SYSTEM,
        ErrorCode.ROLLBACK_FAILED: ErrorCategory.SYSTEM,
        ErrorCode.ROLLBACK_REF_NOT_FOUND: ErrorCategory.USER,
        ErrorCode.MODEL_UNAVAILABLE: ErrorCategory.NETWORK,
        ErrorCode.MODEL_RATE_LIMITED: ErrorCategory.MODEL,
        ErrorCode.MODEL_CONTEXT_EXCEEDED: ErrorCategory.MODEL,
        ErrorCode.MODEL_AUTH_FAILED: ErrorCategory.AUTH,
        ErrorCode.MODEL_UNKNOWN: ErrorCategory.USER,
        ErrorCode.PROVIDER_UNAVAILABLE: ErrorCategory.NETWORK,
        ErrorCode.SANDBOX_CREATE_FAILED: ErrorCategory.SANDBOX,
        ErrorCode.SANDBOX_IMAGE_UNAVAILABLE: ErrorCategory.SANDBOX,
        ErrorCode.SANDBOX_QUOTA_EXCEEDED: ErrorCategory.SANDBOX,
        ErrorCode.SANDBOX_TIMEOUT: ErrorCategory.SANDBOX,
        ErrorCode.SANDBOX_OOM: ErrorCategory.SANDBOX,
        ErrorCode.SANDBOX_EXEC_FAILED: ErrorCategory.SANDBOX,
        ErrorCode.SANDBOX_NOT_FOUND: ErrorCategory.SYSTEM,
        ErrorCode.MCP_SERVER_UNAVAILABLE: ErrorCategory.NETWORK,
        ErrorCode.MCP_PROTOCOL_ERROR: ErrorCategory.SYSTEM,
        ErrorCode.MCP_TOOL_NOT_FOUND: ErrorCategory.USER,
        ErrorCode.MCP_TOOL_TIMEOUT: ErrorCategory.NETWORK,
        ErrorCode.MCP_TOOL_ERROR: ErrorCategory.SYSTEM,
        ErrorCode.PLUGIN_LOAD_FAILED: ErrorCategory.PLUGIN,
        ErrorCode.PLUGIN_MANIFEST_INVALID: ErrorCategory.PLUGIN,
        ErrorCode.PLUGIN_PERMISSION_DENIED: ErrorCategory.PLUGIN,
        ErrorCode.PLUGIN_DISABLED: ErrorCategory.PLUGIN,
        ErrorCode.NET_TIMEOUT: ErrorCategory.NETWORK,
        ErrorCode.NET_UNREACHABLE: ErrorCategory.NETWORK,
        ErrorCode.NET_DNS_FAILED: ErrorCategory.NETWORK,
        ErrorCode.SYS_INTERNAL: ErrorCategory.SYSTEM,
        ErrorCode.SYS_NOT_IMPLEMENTED: ErrorCategory.SYSTEM,
        ErrorCode.SYS_INVARIANT_VIOLATED: ErrorCategory.SYSTEM,
    }
)


# The default retry policy for each category. The values are taken from
# 06-api-contracts.md section 15.3, which describes retry behavior per
# category rather than per code. Where a specific code needs a policy
# different from its category's default, the code appears in
# RETRY_POLICY_OVERRIDES below instead.
RETRY_POLICY: Mapping[ErrorCategory, RetryPolicy] = MappingProxyType(
    {
        ErrorCategory.USER: RetryPolicy(retryable=False, max_retries=0, backoff=Backoff.NONE),
        ErrorCategory.AUTH: RetryPolicy(retryable=False, max_retries=0, backoff=Backoff.NONE),
        ErrorCategory.PLUGIN: RetryPolicy(retryable=False, max_retries=0, backoff=Backoff.NONE),
        ErrorCategory.SYSTEM: RetryPolicy(
            retryable=True, max_retries=3, backoff=Backoff.EXPONENTIAL
        ),
        ErrorCategory.MODEL: RetryPolicy(
            retryable=True, max_retries=3, backoff=Backoff.EXPONENTIAL
        ),
        ErrorCategory.SANDBOX: RetryPolicy(retryable=True, max_retries=1, backoff=Backoff.FIXED),
        ErrorCategory.NETWORK: RetryPolicy(
            retryable=True, max_retries=5, backoff=Backoff.EXPONENTIAL
        ),
    }
)


# Per-code overrides for codes whose behavior differs from their
# category's default. The documentation for the model category says:
# "Retry up to 3 times if rate-limited. Never retry if context exceeded."
# The rate-limit case is the category default; the context-exceeded case
# is the override below, because retrying a request whose context is too
# large will fail identically every time.
RETRY_POLICY_OVERRIDES: Mapping[ErrorCode, RetryPolicy] = MappingProxyType(
    {
        ErrorCode.MODEL_CONTEXT_EXCEEDED: RetryPolicy(
            retryable=False, max_retries=0, backoff=Backoff.NONE
        ),
    }
)


def category_of(code: ErrorCode | str) -> ErrorCategory:
    """Return the category that the given code belongs to.

    Args:
        code: An ``ErrorCode`` member or its string value.

    Returns:
        The ``ErrorCategory`` for the code.

    Raises:
        UnknownCodeError: If the code is not present in the taxonomy. The
            caller passed a string that does not match any known code.
    """
    resolved = _resolve_code(code)
    category = CATEGORY_MAP.get(resolved)
    if category is None:
        raise UnknownCodeError(resolved.value)
    return category


def retry_policy_of(code: ErrorCode | str) -> RetryPolicy:
    """Return the retry policy that applies to the given code.

    The lookup first checks per-code overrides, then falls back to the
    category default. Callers use this function rather than reading
    ``RETRY_POLICY`` directly so that overrides are always honored.

    Args:
        code: An ``ErrorCode`` member or its string value.

    Returns:
        The effective ``RetryPolicy`` for the code.

    Raises:
        UnknownCodeError: If the code is not present in the taxonomy.
    """
    resolved = _resolve_code(code)
    override = RETRY_POLICY_OVERRIDES.get(resolved)
    if override is not None:
        return override
    return RETRY_POLICY[category_of(resolved)]


class CodeGuardianError(Exception):
    """Base typed exception raised by every CodeGuardian component.

    Every raise site in the codebase uses this class or a subclass. The
    orchestrator, the CLI, and the MCP server inspect the ``code``,
    ``category``, and ``retry_policy`` attributes to decide whether to
    retry, to escalate to the user, or to map to an exit code. That
    decision is made once, here, and never duplicated at the raise site.

    The ``details`` argument is copied at construction time so that a
    caller mutating its own dictionary after raising does not change the
    error that was recorded. The ``evidence`` argument is a short,
    human-readable string that points at the artifact that proves the
    failure, such as a run identifier, a log line, or a file location.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
        evidence: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details) if details is not None else {}
        self.evidence = evidence
        self.category = category_of(code)
        self.retry_policy = retry_policy_of(code)

    def __str__(self) -> str:
        base = f"[{self.code.value}] {self.message}"
        if self.evidence:
            return f"{base} (evidence: {self.evidence})"
        return base
