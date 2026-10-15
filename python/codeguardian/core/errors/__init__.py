"""Typed errors, retry classification, translation, and redaction.

Callers import the public surface from this package rather than from the
individual modules, so the internal file layout is free to change without
touching call sites. The package has five responsibilities, each in its
own module: raw string codes, category classification and the typed
exception, translation from standard library exceptions, the retry
engine, and the redaction walker.
"""

from .redaction import DEFAULT_MARKER, redact
from .retry import compute_delay, retry_async, retry_sync
from .taxonomy import (
    Backoff,
    CodeGuardianError,
    ErrorCategory,
    ErrorCode,
    RetryPolicy,
    UnknownCodeError,
    category_of,
    retry_policy_of,
)
from .translation import translate, typed

__all__ = [
    "DEFAULT_MARKER",
    "Backoff",
    "CodeGuardianError",
    "ErrorCategory",
    "ErrorCode",
    "RetryPolicy",
    "UnknownCodeError",
    "category_of",
    "compute_delay",
    "redact",
    "retry_async",
    "retry_policy_of",
    "retry_sync",
    "translate",
    "typed",
]
