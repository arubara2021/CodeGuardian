"""Redact secrets from any nested structure before it is logged or stored.

Secrets reach log lines and audit entries through many paths: API keys in
configuration dicts, bearer tokens in HTTP headers, connection strings in
provider metadata, JWT values in model responses. Writing them anywhere
persistent is unacceptable. This module is the single chokepoint every
component calls before it serializes anything.

Two rules apply in order. First, key-based: a value under a key whose
name matches a known secret-name pattern is redacted regardless of what
the value looks like. Second, value-based: a string value that matches a
known secret-shape pattern is redacted regardless of what the key is
called. Both rules are data-driven, so extending the redactor means
adding a name to a set or a pattern to a tuple, not touching the walker.

The redactor never mutates its input. It builds a new structure on every
call. Callers can safely pass the same dict to ``redact`` and to a
logger without worrying about ordering.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["redact"]

# The string that replaces a redacted value. It is short and obvious so
# that a human reading a log sees immediately that content was removed
# and does not mistake the marker for real data.
DEFAULT_MARKER = "[REDACTED]"

# Exact key names that always indicate a secret. Comparison is
# case-insensitive. The set is deliberately small; broad name matching
# produces false positives on innocuous fields such as ``sort_key`` or
# ``doc_key``. Where a broader match is needed, a suffix is used
# instead of an exact name.
_SENSITIVE_KEYS_EXACT: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "pwd",
        "token",
        "bearer",
        "secret",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "private_key",
        "access_key",
        "signing_key",
        "client_secret",
    }
)

# Suffixes that mark a key as sensitive. A key matching any of these is
# redacted. The leading underscore is deliberate: it prevents the suffix
# from matching inside a longer word such as ``monkey`` while still
# matching compound names such as ``openai_key`` and ``session_token``.
_SENSITIVE_KEY_SUFFIXES: tuple[str, ...] = (
    "_key",
    "_secret",
    "_token",
    "_password",
    "_passwd",
    "_pass",
)

# Value patterns. Each is anchored at both ends so that it only matches a
# string that consists entirely of a secret. A sentence like
# "the key is AKIA..." is deliberately not redacted because the
# surrounding text changes the meaning; a caller that embeds a secret in
# a message has made a choice that this module cannot correct.
#
# Order matters within the tuple. Anthropic keys share the ``sk-``
# prefix with OpenAI keys, so the Anthropic pattern must come first.
#
# The Anthropic and OpenAI patterns both accept hyphens in the
# identifier. Real keys in both families contain hyphens: Anthropic uses
# the form ``sk-ant-api03-<id>`` and OpenAI uses ``sk-proj-<id>`` for
# project-scoped keys. Rejecting the hyphen would make the pattern miss
# the two most common shapes it is meant to catch.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # JSON Web Tokens: three base64url segments, the first of which is a
    # JSON header that always begins with the bytes ``{"`` and therefore
    # always base64-encodes to a prefix of ``eyJ``.
    re.compile(r"^eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$"),
    # AWS access key IDs. The prefix AKIA is reserved for long-lived
    # access keys and is not used for anything else.
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
    # GitHub personal access tokens. The new format uses a
    # ``github_pat_`` prefix; the old format uses ``ghp_``, ``gho_``,
    # ``ghu_``, ``ghs_``, or ``ghr_`` followed by a long identifier.
    re.compile(r"^github_pat_[A-Za-z0-9_]{22,}$"),
    re.compile(r"^gh[pousr]_[A-Za-z0-9]{36,}$"),
    # Anthropic API keys. Must precede the OpenAI pattern.
    re.compile(r"^sk-ant-[A-Za-z0-9_-]{20,}$"),
    # OpenAI API keys, including the newer project-scoped ``sk-proj-``
    # form. The identifier is long enough that a false positive on a
    # human-typed string is unlikely.
    re.compile(r"^sk-[A-Za-z0-9-]{20,}$"),
    # Bearer tokens in HTTP header values.
    re.compile(r"^Bearer\s+[A-Za-z0-9._\-]{20,}$"),
    # Connection strings with embedded credentials. The pattern matches
    # a URI scheme followed by user:password@ before the host. It does
    # not match a URL without credentials.
    re.compile(r"^[a-z][a-z0-9+.\-]*://[^:/@\s]+:[^@\s]+@"),
)


def _is_sensitive_key(key: Any) -> bool:
    """Return whether a dict key names a value that must be redacted.

    Non-string keys are never treated as sensitive, because a secret is
    identified by its name and a non-string key carries no name.
    Comparison is case-insensitive so that ``API_KEY``, ``Api_Key``, and
    ``api_key`` are all recognized.
    """
    if not isinstance(key, str):
        return False
    lower = key.lower()
    if lower in _SENSITIVE_KEYS_EXACT:
        return True
    return any(lower.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES)


def _is_sensitive_value(value: str) -> bool:
    """Return whether a string matches any known secret-shape pattern."""
    return any(pattern.match(value) is not None for pattern in _VALUE_PATTERNS)


def _walk(value: Any, marker: str) -> Any:
    """Return a copy of ``value`` with secrets replaced by ``marker``.

    Dicts, lists, and tuples are walked recursively. Tuples preserve
    their type. Strings are checked against the value patterns. Every
    other type is returned unchanged, because a secret is a string and
    a container of secrets is a dict, list, or tuple.
    """
    if isinstance(value, dict):
        return _redact_dict(value, marker)
    if isinstance(value, list):
        return [_walk(item, marker) for item in value]
    if isinstance(value, tuple):
        return tuple(_walk(item, marker) for item in value)
    if isinstance(value, str):
        if _is_sensitive_value(value):
            return marker
        return value
    return value


def _redact_dict(source: dict[Any, Any], marker: str) -> dict[Any, Any]:
    """Return a new dict with sensitive values replaced by ``marker``.

    A key that names a secret has its entire value replaced, including
    when the value is itself a nested structure. This is deliberate:
    ``{"password": {"hash": ..., "salt": ...}}`` is a secret object and
    every field under it is part of the secret. Walking inside would
    expose the hash and the salt, which is exactly what redaction is
    meant to prevent.
    """
    result: dict[Any, Any] = {}
    for key, value in source.items():
        if _is_sensitive_key(key):
            result[key] = marker
        else:
            result[key] = _walk(value, marker)
    return result


def redact(value: Any, *, marker: str = DEFAULT_MARKER) -> Any:
    """Return a copy of ``value`` with every recognized secret redacted.

    The input is never modified. Dicts, lists, and tuples are walked
    recursively. A value under a key that names a secret is replaced by
    ``marker``. A string value that matches a known secret shape is
    replaced by ``marker`` regardless of its key name. Every other value
    is returned unchanged.

    Args:
        value: Any value, typically a dict of log or audit fields.
        marker: The replacement string. Defaults to ``[REDACTED]``.

    Returns:
        A new structure of the same shape with secrets replaced.
    """
    return _walk(value, marker)
