"""Tests for the secret redaction walker.

Redaction is the last line of defense before a secret reaches a log, an
audit entry, or an exception payload. A missed case here leaks a
credential permanently into wherever the log is stored. These tests cover
the two rules independently, their interaction, the walker's handling of
every container type in the domain, and the guarantee that the input is
never mutated.
"""

from codeguardian.core.errors import DEFAULT_MARKER, redact


def test_redact_returns_empty_dict_for_empty_dict():
    assert redact({}) == {}


def test_redact_returns_input_for_non_secret_plain_values():
    source = {"name": "alice", "age": 30, "active": True, "score": 4.5}
    assert redact(source) == source


def test_redact_preserves_none():
    assert redact({"note": None}) == {"note": None}


def test_redact_exact_key_password():
    assert redact({"password": "hunter2"}) == {"password": DEFAULT_MARKER}


def test_redact_exact_key_token():
    assert redact({"token": "abc"}) == {"token": DEFAULT_MARKER}


def test_redact_exact_key_api_key():
    assert redact({"api_key": "sk-abc"}) == {"api_key": DEFAULT_MARKER}


def test_redact_exact_key_authorization():
    assert redact({"authorization": "Basic abc"}) == {"authorization": DEFAULT_MARKER}


def test_redact_key_case_insensitive():
    assert redact({"PASSWORD": "x"}) == {"PASSWORD": DEFAULT_MARKER}
    assert redact({"Password": "x"}) == {"Password": DEFAULT_MARKER}


def test_redact_key_suffix_key():
    assert redact({"openai_key": "x"}) == {"openai_key": DEFAULT_MARKER}


def test_redact_key_suffix_secret():
    assert redact({"client_secret": "x"}) == {"client_secret": DEFAULT_MARKER}


def test_redact_key_suffix_token():
    assert redact({"session_token": "x"}) == {"session_token": DEFAULT_MARKER}


def test_redact_key_suffix_password():
    assert redact({"db_password": "x"}) == {"db_password": DEFAULT_MARKER}


def test_redact_key_suffix_does_not_match_inside_words():
    # "monkey" ends with "key" but not with "_key".
    assert redact({"monkey": "value"}) == {"monkey": "value"}


def test_redact_value_pattern_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123"
    assert redact({"note": jwt}) == {"note": DEFAULT_MARKER}


def test_redact_value_pattern_aws_access_key():
    assert redact({"note": "AKIAIOSFODNN7EXAMPLE"}) == {"note": DEFAULT_MARKER}


def test_redact_value_pattern_github_pat_new_format():
    token = "github_pat_11ABCDEFG0abcdefghijkl_abcdefghijklmnop"
    assert redact({"note": token}) == {"note": DEFAULT_MARKER}


def test_redact_value_pattern_github_pat_old_format():
    token = "ghp_" + "a" * 40
    assert redact({"note": token}) == {"note": DEFAULT_MARKER}


def test_redact_value_pattern_anthropic_key():
    # A real Anthropic key has the form sk-ant-api03-<identifier>. The
    # identifier after the final hyphen is what the pattern validates,
    # and it is at least twenty characters.
    key = "sk-ant-api03-abcdefghij1234567890"
    assert redact({"note": key}) == {"note": DEFAULT_MARKER}


def test_redact_value_pattern_openai_key():
    # Real OpenAI keys include a hyphen in the project segment
    # (sk-proj-...). The pattern must accept that hyphen.
    assert redact({"note": "sk-proj-abc123def456ghi789"}) == {"note": DEFAULT_MARKER}


def test_redact_value_pattern_bearer_token():
    assert redact({"note": "Bearer abcdefghijklmnopqrstuvwxyz"}) == {"note": DEFAULT_MARKER}


def test_redact_value_pattern_connection_string():
    dsn = "postgres://user:pass@localhost:5432/db"
    assert redact({"dsn": dsn}) == {"dsn": DEFAULT_MARKER}


def test_redact_leaves_plain_url_unchanged():
    assert redact({"homepage": "https://example.com/"}) == {"homepage": "https://example.com/"}


def test_redact_leaves_short_string_unchanged():
    assert redact({"greeting": "hello"}) == {"greeting": "hello"}


def test_redact_leaves_short_sk_prefix_unchanged():
    # "sk-short" is too short to match the OpenAI pattern (min 20 chars
    # after the prefix).
    assert redact({"note": "sk-short"}) == {"note": "sk-short"}


def test_redact_nested_dict():
    result = redact({"user": {"password": "x", "name": "alice"}})
    assert result == {"user": {"password": DEFAULT_MARKER, "name": "alice"}}


def test_redact_list_of_dicts():
    result = redact([{"password": "a"}, {"token": "b"}])
    assert result == [{"password": DEFAULT_MARKER}, {"token": DEFAULT_MARKER}]


def test_redact_tuple_preserves_type():
    result = redact(({"password": "x"}, "plain"))
    assert isinstance(result, tuple)
    assert result == ({"password": DEFAULT_MARKER}, "plain")


def test_redact_dict_inside_list_inside_dict():
    result = redact({"outer": [{"inner": {"password": "x"}}]})
    assert result == {"outer": [{"inner": {"password": DEFAULT_MARKER}}]}


def test_redact_sensitive_key_replaces_whole_subtree():
    # Everything under "password" is part of the secret, including the
    # hash and the salt. Walking inside would expose both.
    result = redact({"password": {"hash": "abc", "salt": "xyz"}})
    assert result == {"password": DEFAULT_MARKER}


def test_redact_value_rule_applies_inside_non_sensitive_key():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123"
    assert redact({"metadata": {"jwt": jwt}}) == {"metadata": {"jwt": DEFAULT_MARKER}}


def test_redact_custom_marker():
    result = redact({"password": "x"}, marker="***")
    assert result == {"password": "***"}


def test_redact_does_not_mutate_input():
    source = {"password": "x", "nested": {"token": "y"}, "list": [{"secret": "z"}]}
    snapshot = {
        "password": "x",
        "nested": {"token": "y"},
        "list": [{"secret": "z"}],
    }
    redact(source)
    assert source == snapshot


def test_redact_non_string_key_is_never_sensitive():
    # A non-string key cannot be a secret name.
    result = redact({42: "value", None: "other"})
    assert result == {42: "value", None: "other"}


def test_redact_preserves_integers_and_floats():
    source = {"count": 42, "ratio": 3.14}
    assert redact(source) == source


def test_redact_sensitive_key_with_none_value():
    # The key rule fires regardless of value type.
    assert redact({"password": None}) == {"password": DEFAULT_MARKER}


def test_redact_sensitive_key_with_int_value():
    assert redact({"token": 12345}) == {"token": DEFAULT_MARKER}
