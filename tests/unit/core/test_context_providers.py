"""Tests for the four context provider plugins.

Each test builds a real Code Property Graph, writes the source to a
temp directory (needed for ``function_source`` to read byte offsets),
and wraps the calls in ``cpg_available`` so the provider contextvars
are set.
"""

from __future__ import annotations

import pytest

pytest.importorskip("codeguardian._native")

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins import (
    PluginRegistry,
    cpg_available,
)

PYTHON_SOURCE = """\
def py_inner(x: int) -> int:
    # Adds one to x
    result = x + 1
    return result


def py_outer() -> int:
    # Calls py_inner
    return py_inner(1)
"""

TYPESCRIPT_SOURCE = """\
function ts_inner(x: number): number {
    return x + 1;
}

function ts_outer(): number {
    return ts_inner(1);
}
"""


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    return PluginRegistry.discover_bundled()


@pytest.fixture
def python_workspace(tmp_path):
    from codeguardian import _native

    (tmp_path / "main.py").write_text(PYTHON_SOURCE, encoding="utf-8")
    cpg_bytes = _native.build_graph([("main.py", "python", PYTHON_SOURCE)])
    return tmp_path, _native.PyCPG(cpg_bytes)


@pytest.fixture
def typescript_workspace(tmp_path):
    from codeguardian import _native

    (tmp_path / "main.ts").write_text(TYPESCRIPT_SOURCE, encoding="utf-8")
    cpg_bytes = _native.build_graph([("main.ts", "typescript", TYPESCRIPT_SOURCE)])
    return tmp_path, _native.PyCPG(cpg_bytes)


def test_context_runtime_is_exported():
    from codeguardian.core.plugins import (
        cpg_available as cpg_available_export,
    )
    from codeguardian.core.plugins import (
        current_clone_root as current_clone_root_export,
    )
    from codeguardian.core.plugins import (
        current_cpg as current_cpg_export,
    )

    assert callable(cpg_available_export)
    assert callable(current_cpg_export)
    assert callable(current_clone_root_export)


async def test_code_skeleton_python(registry, python_workspace):
    root, cpg = python_workspace
    with cpg_available(cpg, root):
        barrier = registry.get_context_provider("code-skeleton")
        result = await barrier.call_async("provide", {"path": "main.py"})
    assert isinstance(result, str)
    assert "py_inner" in result
    assert "py_outer" in result
    assert "return result" not in result
    assert "return py_inner" not in result
    assert len(result) < len(PYTHON_SOURCE)


async def test_code_skeleton_typescript(registry, typescript_workspace):
    root, cpg = typescript_workspace
    with cpg_available(cpg, root):
        barrier = registry.get_context_provider("code-skeleton")
        result = await barrier.call_async("provide", {"path": "main.ts"})
    assert isinstance(result, str)
    assert "ts_inner" in result
    assert "ts_outer" in result
    assert "return x + 1" not in result


async def test_function_body_python(registry, python_workspace):
    root, cpg = python_workspace
    with cpg_available(cpg, root):
        barrier = registry.get_context_provider("function-body")
        result = await barrier.call_async(
            "provide", {"path": "main.py", "qualified_name": "py_inner"}
        )
    assert isinstance(result, str)
    assert result.startswith("def py_inner")
    assert "return result" in result
    assert "def py_outer" not in result


async def test_function_body_typescript(registry, typescript_workspace):
    root, cpg = typescript_workspace
    with cpg_available(cpg, root):
        barrier = registry.get_context_provider("function-body")
        result = await barrier.call_async(
            "provide", {"path": "main.ts", "qualified_name": "ts_inner"}
        )
    assert isinstance(result, str)
    assert "ts_inner" in result
    assert "return x + 1" in result
    assert "ts_outer" not in result


async def test_function_body_missing_target(registry, python_workspace):
    root, cpg = python_workspace
    with cpg_available(cpg, root):
        barrier = registry.get_context_provider("function-body")
        result = await barrier.call_async(
            "provide", {"path": "main.py", "qualified_name": "no_such_function"}
        )
    assert isinstance(result, str)
    assert "not found" in result


async def test_call_graph_slice_callers(registry, python_workspace):
    root, cpg = python_workspace
    with cpg_available(cpg, root):
        barrier = registry.get_context_provider("call-graph-slice")
        result = await barrier.call_async(
            "provide",
            {"path": "main.py", "qualified_name": "py_inner", "direction": "callers"},
        )
    assert "py_outer" in result
    assert "depth 1" in result


async def test_call_graph_slice_callees(registry, python_workspace):
    root, cpg = python_workspace
    with cpg_available(cpg, root):
        barrier = registry.get_context_provider("call-graph-slice")
        result = await barrier.call_async(
            "provide",
            {"path": "main.py", "qualified_name": "py_outer", "direction": "callees"},
        )
    assert "py_inner" in result


async def test_list_symbols_python(registry, python_workspace):
    root, cpg = python_workspace
    with cpg_available(cpg, root):
        barrier = registry.get_context_provider("list-symbols")
        result = await barrier.call_async("provide", {"path": "main.py"})
    assert "py_inner" in result
    assert "py_outer" in result
    assert "(function)" in result


async def test_list_symbols_typescript(registry, typescript_workspace):
    root, cpg = typescript_workspace
    with cpg_available(cpg, root):
        barrier = registry.get_context_provider("list-symbols")
        result = await barrier.call_async("provide", {"path": "main.ts"})
    assert "ts_inner" in result
    assert "ts_outer" in result


async def test_providers_raise_without_cpg_context(registry):
    for name, input_data in [
        ("code-skeleton", {"path": "main.py"}),
        ("function-body", {"path": "main.py", "qualified_name": "py_inner"}),
        ("call-graph-slice", {"path": "main.py", "qualified_name": "py_inner"}),
        ("list-symbols", {"path": "main.py"}),
    ]:
        barrier = registry.get_context_provider(name)
        with pytest.raises(CodeGuardianError) as exc_info:
            await barrier.call_async("provide", input_data)
        assert exc_info.value.code is ErrorCode.SYS_INVARIANT_VIOLATED, name
