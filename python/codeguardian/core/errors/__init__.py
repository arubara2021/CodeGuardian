"""Typed errors and retry classification for every CodeGuardian component.

Callers import the public surface from this package rather than from the
individual modules, so the internal file layout is free to change without
touching call sites. ``codes.py`` holds the raw string constants;
``taxonomy.py`` holds the classification and the exception class;
``translation.py`` converts standard library exceptions into typed errors.
"""

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
    "Backoff",
    "CodeGuardianError",
    "ErrorCategory",
    "ErrorCode",
    "RetryPolicy",
    "UnknownCodeError",
    "category_of",
    "retry_policy_of",
    "translate",
    "typed",
]
