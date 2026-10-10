"""Invariants for the error code taxonomy.

The taxonomy is the vocabulary every other component depends on. When it
drifts, the failures are silent: a retry loop that never fires, a code
that maps to no category, an exception that loses its identity when it
crosses a component boundary. This test catches all of those before they
reach a runtime path.
"""

import pytest

from codeguardian.core.errors import (
    Backoff,
    CodeGuardianError,
    ErrorCategory,
    ErrorCode,
    RetryPolicy,
    UnknownCodeError,
    category_of,
    codes,
    retry_policy_of,
    taxonomy,
)


def test_error_code_values_are_unique():
    values = [member.value for member in ErrorCode]
    assert len(values) == len(set(values)), "duplicate ErrorCode values found"


def test_codes_module_matches_enum():
    """Every constant in codes.py is an enum member, and vice versa."""
    module_codes = {
        name: getattr(codes, name)
        for name in dir(codes)
        if name.isupper() and not name.startswith("_")
    }
    enum_codes = {member.name: member.value for member in ErrorCode}
    assert module_codes == enum_codes, (
        "codes.py and ErrorCode are out of sync; add or remove the missing "
        "entries so that the two sets are identical"
    )


def test_every_code_has_a_category():
    for member in ErrorCode:
        assert member in taxonomy.CATEGORY_MAP, f"{member.value} is missing from CATEGORY_MAP"


def test_category_map_values_are_categories():
    for category in taxonomy.CATEGORY_MAP.values():
        assert isinstance(category, ErrorCategory)


def test_every_category_has_a_retry_policy():
    for category in ErrorCategory:
        assert category in taxonomy.RETRY_POLICY, f"{category.value} is missing from RETRY_POLICY"


def test_retry_policy_values_are_policies():
    for policy in taxonomy.RETRY_POLICY.values():
        assert isinstance(policy, RetryPolicy)


def test_retry_policy_fields_agree():
    """retryable must be True if and only if max_retries is greater than zero."""
    for policy in taxonomy.RETRY_POLICY.values():
        assert policy.retryable is (
            policy.max_retries > 0
        ), "RetryPolicy has inconsistent retryable and max_retries fields"


def test_overrides_replace_category_default():
    """The context-exceeded override must not be retryable."""
    override = retry_policy_of(ErrorCode.MODEL_CONTEXT_EXCEEDED)
    assert override.retryable is False
    assert override.max_retries == 0
    assert override.backoff is Backoff.NONE


def test_category_of_returns_expected_category():
    assert category_of(ErrorCode.REPO_CLONE_FAILED) is ErrorCategory.NETWORK
    assert category_of(ErrorCode.REPO_AUTH_FAILED) is ErrorCategory.AUTH
    assert category_of(ErrorCode.SANDBOX_TIMEOUT) is ErrorCategory.SANDBOX
    assert category_of(ErrorCode.PLUGIN_LOAD_FAILED) is ErrorCategory.PLUGIN
    assert category_of(ErrorCode.SYS_INTERNAL) is ErrorCategory.SYSTEM
    assert category_of(ErrorCode.FILE_PERMISSION_DENIED) is ErrorCategory.USER


def test_category_of_accepts_string_values():
    assert category_of("NET_TIMEOUT") is ErrorCategory.NETWORK
    assert category_of("FILE_NOT_FOUND") is ErrorCategory.USER


def test_category_of_raises_on_unknown_code():
    with pytest.raises(UnknownCodeError) as excinfo:
        category_of("NOT_A_REAL_CODE")
    assert excinfo.value.code == "NOT_A_REAL_CODE"


def test_retry_policy_of_returns_network_policy():
    policy = retry_policy_of(ErrorCode.NET_TIMEOUT)
    assert policy.retryable is True
    assert policy.max_retries == 5
    assert policy.backoff is Backoff.EXPONENTIAL


def test_retry_policy_of_returns_user_policy():
    policy = retry_policy_of(ErrorCode.FILE_NOT_FOUND)
    assert policy.retryable is False
    assert policy.max_retries == 0
    assert policy.backoff is Backoff.NONE


def test_retry_policy_of_returns_sandbox_policy():
    policy = retry_policy_of(ErrorCode.SANDBOX_TIMEOUT)
    assert policy.retryable is True
    assert policy.max_retries == 1
    assert policy.backoff is Backoff.FIXED


def test_retry_policy_of_accepts_string_values():
    policy = retry_policy_of("NET_UNREACHABLE")
    assert policy.retryable is True
    assert policy.max_retries == 5


def test_retry_policy_of_raises_on_unknown_code():
    with pytest.raises(UnknownCodeError):
        retry_policy_of("NOT_A_REAL_CODE")


def test_codeguardian_error_carries_all_metadata():
    error = CodeGuardianError(
        code=ErrorCode.NET_TIMEOUT,
        message="connection to api.example.com timed out",
        details={"host": "api.example.com", "attempt": 3},
        evidence="run-id 42",
    )
    assert error.code is ErrorCode.NET_TIMEOUT
    assert error.message == "connection to api.example.com timed out"
    assert error.details == {"host": "api.example.com", "attempt": 3}
    assert error.evidence == "run-id 42"
    assert error.category is ErrorCategory.NETWORK
    assert error.retry_policy.max_retries == 5


def test_codeguardian_error_copies_details_dict():
    """Mutating the caller's dict after raising must not change the error."""
    source = {"key": "value"}
    error = CodeGuardianError(code=ErrorCode.SYS_INTERNAL, message="boom", details=source)
    source["key"] = "changed"
    assert error.details == {"key": "value"}


def test_codeguardian_error_defaults_details_to_empty_dict():
    error = CodeGuardianError(code=ErrorCode.SYS_INTERNAL, message="boom")
    assert error.details == {}
    assert error.evidence is None


def test_codeguardian_error_str_contains_code_and_message():
    error = CodeGuardianError(code=ErrorCode.SYS_INTERNAL, message="boom")
    text = str(error)
    assert "SYS_INTERNAL" in text
    assert "boom" in text


def test_codeguardian_error_str_includes_evidence_when_present():
    error = CodeGuardianError(code=ErrorCode.SYS_INTERNAL, message="boom", evidence="log line 17")
    assert "log line 17" in str(error)


def test_codeguardian_error_is_catchable_as_exception():
    with pytest.raises(CodeGuardianError):
        raise CodeGuardianError(code=ErrorCode.SYS_INTERNAL, message="boom")

    with pytest.raises(Exception):  # noqa: B017
        raise CodeGuardianError(code=ErrorCode.SYS_INTERNAL, message="boom")


def test_category_map_is_immutable():
    """The mapping must not be mutable at runtime."""
    with pytest.raises(TypeError):
        taxonomy.CATEGORY_MAP[ErrorCode.SYS_INTERNAL] = ErrorCategory.USER  # type: ignore[index]


def test_retry_policy_map_is_immutable():
    with pytest.raises(TypeError):
        taxonomy.RETRY_POLICY[ErrorCategory.SYSTEM] = RetryPolicy(  # type: ignore[index]
            retryable=False, max_retries=0, backoff=Backoff.NONE
        )
