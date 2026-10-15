"""Tests for the blast radius and policy bindings exposed through PyO3.

The tests exercise the compiled native extension end to end. They are
skipped entirely when the extension is not built.

The suite is split into two sections. The blast section builds a CPG
from source files, calls `compute_blast`, and asserts structural facts
about the returned dictionary. The policy section calls the three
policy bindings with primitive arguments and asserts that valid inputs
pass and invalid inputs raise `KernelError` with the expected code.
"""

from __future__ import annotations

import pytest

from codeguardian.core.bridges import pyo3_bridge

pytestmark = pytest.mark.skipif(
    not pyo3_bridge.is_native_available(),
    reason="native extension not built",
)


def _native():
    """Return the imported native module."""
    import codeguardian._native as native

    return native


# -- Blast section ------------------------------------------------------


_ONE_FUNCTION = "def greet():\n    return 1\n"

_TWO_FUNCTIONS_CALLER = "def inner():\n    return 1\n\n\ndef outer():\n    return inner()\n"

_THREE_LEVELS = (
    "def leaf():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def middle():\n"
    "    return leaf()\n"
    "\n"
    "\n"
    "def top():\n"
    "    return middle()\n"
)

_MULTIPLE_CALLERS = (
    "def target():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def first():\n"
    "    return target()\n"
    "\n"
    "\n"
    "def second():\n"
    "    return target()\n"
)


def _build(sources):
    return _native().build_graph(sources)


def test_compute_blast_empty_graph_file_not_found():
    data = _build([])
    with pytest.raises(_native().KernelError) as exc_info:
        _native().compute_blast(data, "main.py", "greet")
    assert exc_info.value.code == "FILE_NOT_FOUND"


def test_compute_blast_target_not_found():
    data = _build([("main.py", "python", "def other():\n    return 1\n")])
    with pytest.raises(_native().KernelError) as exc_info:
        _native().compute_blast(data, "main.py", "greet")
    assert exc_info.value.code == "BLAST_TARGET_NOT_FOUND"


def test_compute_blast_lone_function_has_zero_callers():
    data = _build([("main.py", "python", _ONE_FUNCTION)])
    result = _native().compute_blast(data, "main.py", "greet")
    assert result["direct_callers"] == []
    assert result["transitive_callers"] == []
    assert result["blast_score"] == 0
    assert result["recommendation"] == "proceed"


def test_compute_blast_single_direct_caller():
    data = _build([("main.py", "python", _TWO_FUNCTIONS_CALLER)])
    result = _native().compute_blast(data, "main.py", "inner")
    assert len(result["direct_callers"]) == 1
    assert result["direct_callers"][0]["qualified_name"] == "outer"
    assert result["direct_callers"][0]["call_kind"] == "direct"
    assert result["direct_callers"][0]["depth"] == 1


def test_compute_blast_transitive_caller():
    data = _build([("main.py", "python", _THREE_LEVELS)])
    result = _native().compute_blast(data, "main.py", "leaf")
    direct_names = {c["qualified_name"] for c in result["direct_callers"]}
    transitive_names = {c["qualified_name"] for c in result["transitive_callers"]}
    assert direct_names == {"middle"}
    assert transitive_names == {"middle", "top"}


def test_compute_blast_multiple_direct_callers():
    data = _build([("main.py", "python", _MULTIPLE_CALLERS)])
    result = _native().compute_blast(data, "main.py", "target")
    names = {c["qualified_name"] for c in result["direct_callers"]}
    assert names == {"first", "second"}


def test_compute_blast_score_rises_with_callers():
    lone = _build([("main.py", "python", _ONE_FUNCTION)])
    with_caller = _build([("main.py", "python", _TWO_FUNCTIONS_CALLER)])
    lone_score = _native().compute_blast(lone, "main.py", "greet")["blast_score"]
    with_caller_score = _native().compute_blast(with_caller, "main.py", "inner")["blast_score"]
    assert with_caller_score > lone_score


def test_compute_blast_transitive_truncated_false_for_small_graph():
    data = _build([("main.py", "python", _THREE_LEVELS)])
    result = _native().compute_blast(data, "main.py", "leaf")
    assert result["transitive_truncated"] is False


def test_compute_blast_rejects_garbage_bytes():
    with pytest.raises(_native().KernelError) as exc_info:
        _native().compute_blast(b"XXXX" + b"\x00" * 60, "main.py", "greet")
    assert exc_info.value.code == "INVALID_MAGIC"


def test_compute_blast_rejects_truncated_cpg():
    data = _build([("main.py", "python", _ONE_FUNCTION)])
    with pytest.raises(_native().KernelError) as exc_info:
        _native().compute_blast(data[:50], "main.py", "greet")
    assert exc_info.value.code in ("GRAPH_IO", "GRAPH_TRUNCATED")


# -- Policy section -----------------------------------------------------


def test_check_sandbox_spec_accepts_spec_within_policy():
    _native().check_sandbox_spec(
        "python",
        512,
        1,
        64,
        1024,
        2,
        256,
        ["python", "typescript"],
    )


def test_check_sandbox_spec_rejects_excess_memory():
    with pytest.raises(_native().KernelError) as exc_info:
        _native().check_sandbox_spec(
            "python",
            4096,
            1,
            64,
            1024,
            2,
            256,
            ["python"],
        )
    assert exc_info.value.code == "SANDBOX_QUOTA_EXCEEDED"


def test_check_sandbox_spec_rejects_excess_cpu():
    with pytest.raises(_native().KernelError) as exc_info:
        _native().check_sandbox_spec(
            "python",
            512,
            8,
            64,
            1024,
            2,
            256,
            ["python"],
        )
    assert exc_info.value.code == "SANDBOX_QUOTA_EXCEEDED"


def test_check_sandbox_spec_rejects_unknown_language():
    with pytest.raises(_native().KernelError) as exc_info:
        _native().check_sandbox_spec(
            "ruby",
            512,
            1,
            64,
            1024,
            2,
            256,
            ["python"],
        )
    assert exc_info.value.code == "SANDBOX_QUOTA_EXCEEDED"


def test_check_plugin_permissions_accepts_within_policy():
    _native().check_plugin_permissions(
        "my-plugin",
        "sandbox_only",
        False,
        False,
        [],
        "sandbox_only",
        False,
        False,
        [],
    )


def test_check_plugin_permissions_rejects_excess_filesystem():
    with pytest.raises(_native().KernelError) as exc_info:
        _native().check_plugin_permissions(
            "my-plugin",
            "full",
            False,
            False,
            [],
            "sandbox_only",
            False,
            False,
            [],
        )
    assert exc_info.value.code == "PLUGIN_PERMISSION_DENIED"


def test_check_plugin_permissions_rejects_network():
    with pytest.raises(_native().KernelError) as exc_info:
        _native().check_plugin_permissions(
            "my-plugin",
            "sandbox_only",
            True,
            False,
            [],
            "sandbox_only",
            False,
            False,
            [],
        )
    assert exc_info.value.code == "PLUGIN_PERMISSION_DENIED"


def test_check_plugin_permissions_rejects_env_key_not_in_policy():
    with pytest.raises(_native().KernelError) as exc_info:
        _native().check_plugin_permissions(
            "my-plugin",
            "sandbox_only",
            False,
            False,
            ["SECRET_TOKEN"],
            "sandbox_only",
            False,
            False,
            ["PATH"],
        )
    assert exc_info.value.code == "PLUGIN_PERMISSION_DENIED"


def test_check_plugin_permissions_rejects_unknown_filesystem_name():
    with pytest.raises(ValueError):
        _native().check_plugin_permissions(
            "my-plugin",
            "semi-restricted",
            False,
            False,
            [],
            "sandbox_only",
            False,
            False,
            [],
        )


def test_detect_host_limits_returns_dict():
    result = _native().detect_host_limits()
    assert isinstance(result, dict)
    assert result["total_memory_mb"] > 0
    assert result["total_cpu_cores"] > 0
    assert result["max_processes"] > 0
