"""Blast radius tests parameterized across every wired language.

The blast engine walks reverse `Call` edges from a target function to
find its callers. A same-file call produces a correctly-shaped edge.
A cross-file call does not, for two reasons documented in Phase 8
§11.1: (a) some call-site shapes (qualified identifiers, member
access with an object prefix) are not handled by the shared callee
extractor, and (b) the edge destination's file offset is not applied
when the callee lives in a file other than the first.

# What this suite proves

Four sections, ordered from the tests that must pass to the tests
that document known gaps.

**Section A — same-file calls for the six languages whose function
declarations carry a `name` field.** Python, TypeScript, JavaScript,
Java, Go, and Rust must all resolve a same-file call from `caller`
to `target`. These tests MUST pass.

**Section B — same-file calls for C and C++.** tree-sitter-c's and
tree-sitter-cpp's `function_definition` node does not have a `name`
field; the name is inside a nested declarator. Until
`parser.rs::extract_name_id` walks declarators, C and C++ functions
produce no symbols and cannot be the target of a call edge. Marked
`xfail(strict=False)` with a reason naming the fix.

**Section C — cross-file calls for all eight languages.** Marked
`xfail(strict=False)` for the reasons above.

**Section D — lone functions.** A target with no callers must produce
an empty `direct_callers` list and a blast score of zero. This must
pass for the six name-carrying languages. C and C++ are expected to
raise target-not-found and are marked `xfail`.

C# was removed from the supported set because `tree-sitter-c-sharp`
0.23.5 produces a grammar ABI that tree-sitter 0.24 refuses to load.

The suite is skipped entirely when the native extension is not built.
"""

from __future__ import annotations

import pytest

from codeguardian.core.bridges import pyo3_bridge

pytestmark = pytest.mark.skipif(
    not pyo3_bridge.is_native_available(),
    reason="native extension not built",
)


def _native():
    import codeguardian._native as native

    return native


def _build(sources):
    return _native().build_graph(sources)


# -- Section A: same-file, must pass -----------------------------------

SAME_FILE_WORKING = [
    pytest.param(
        "python",
        ".py",
        "def target():\n    return 1\n\n\ndef caller():\n    return target()\n",
        "target",
        "caller",
        id="python",
    ),
    pytest.param(
        "typescript",
        ".ts",
        (
            "function target(): number { return 1; }\n"
            "function caller(): number { return target(); }\n"
        ),
        "target",
        "caller",
        id="typescript",
    ),
    pytest.param(
        "javascript",
        ".js",
        "function target() { return 1; }\nfunction caller() { return target(); }\n",
        "target",
        "caller",
        id="javascript",
    ),
    pytest.param(
        "java",
        ".java",
        (
            "class Main {\n"
            "    static int target() { return 1; }\n"
            "    static int caller() { return target(); }\n"
            "}\n"
        ),
        "Main.target",
        "Main.caller",
        id="java",
    ),
    pytest.param(
        "go",
        ".go",
        (
            "package main\n\n"
            "func target() int { return 1 }\n\n"
            "func caller() int { return target() }\n"
        ),
        "target",
        "caller",
        id="go",
    ),
    pytest.param(
        "rust",
        ".rs",
        "fn target() -> i32 { 1 }\n\nfn caller() -> i32 { target() }\n",
        "target",
        "caller",
        id="rust",
    ),
]


@pytest.mark.parametrize(
    "language,suffix,source,target_qualified_name,expected_caller",
    SAME_FILE_WORKING,
)
def test_same_file_call_resolves(language, suffix, source, target_qualified_name, expected_caller):
    data = _build([(f"main{suffix}", language, source)])
    result = _native().compute_blast(data, f"main{suffix}", target_qualified_name)
    caller_names = {c["qualified_name"] for c in result["direct_callers"]}
    assert expected_caller in caller_names, (
        f"{language}: expected caller {expected_caller!r} in "
        f"direct_callers {sorted(caller_names)}, target was "
        f"{target_qualified_name!r}"
    )


@pytest.mark.parametrize(
    "language,suffix,source,target_qualified_name,expected_caller",
    SAME_FILE_WORKING,
)
def test_same_file_blast_recommendation_is_proceed_or_review(
    language, suffix, source, target_qualified_name, expected_caller
):
    del expected_caller
    data = _build([(f"main{suffix}", language, source)])
    result = _native().compute_blast(data, f"main{suffix}", target_qualified_name)
    assert result["recommendation"] in {"proceed", "review"}
    assert result["blast_score"] >= 0


# -- Section B: same-file, C and C++ known gap -------------------------

SAME_FILE_KNOWN_GAP = [
    pytest.param(
        "c",
        ".c",
        "int target() { return 1; }\nint caller() { return target(); }\n",
        "target",
        "caller",
        id="c",
    ),
    pytest.param(
        "cpp",
        ".cpp",
        "int target() { return 1; }\nint caller() { return target(); }\n",
        "target",
        "caller",
        id="cpp",
    ),
]


@pytest.mark.xfail(
    strict=False,
    reason=(
        "tree-sitter-c/cpp function_definition has no `name` field; "
        "parser.rs::extract_name_id does not walk declarator chains, so "
        "C/C++ functions produce no symbols and cannot be call targets"
    ),
)
@pytest.mark.parametrize(
    "language,suffix,source,target_qualified_name,expected_caller",
    SAME_FILE_KNOWN_GAP,
)
def test_same_file_call_resolves_c_family(
    language, suffix, source, target_qualified_name, expected_caller
):
    data = _build([(f"main{suffix}", language, source)])
    result = _native().compute_blast(data, f"main{suffix}", target_qualified_name)
    caller_names = {c["qualified_name"] for c in result["direct_callers"]}
    assert expected_caller in caller_names


# -- Section C: cross-file, known gap ----------------------------------

CROSS_FILE_KNOWN_GAP = [
    pytest.param(
        "python",
        ".py",
        "def target():\n    return 1\n",
        "from defs import target\n\n\ndef caller():\n    return target()\n",
        "target",
        id="python",
    ),
    pytest.param(
        "typescript",
        ".ts",
        "export function target(): number { return 1; }\n",
        'import { target } from "./defs";\n\nfunction caller(): number { return target(); }\n',
        "target",
        id="typescript",
    ),
    pytest.param(
        "javascript",
        ".js",
        "export function target() { return 1; }\n",
        'import { target } from "./defs.js";\n\nfunction caller() { return target(); }\n',
        "target",
        id="javascript",
    ),
    pytest.param(
        "java",
        ".java",
        "class Defs {\n    static int target() { return 1; }\n}\n",
        "class Use {\n    static int caller() { return Defs.target(); }\n}\n",
        "Defs.target",
        id="java",
    ),
    pytest.param(
        "go",
        ".go",
        "package main\n\nfunc target() int { return 1 }\n",
        "package main\n\nfunc caller() int { return target() }\n",
        "target",
        id="go",
    ),
    pytest.param(
        "rust",
        ".rs",
        "pub fn target() -> i32 { 1 }\n",
        "fn caller() -> i32 { target() }\n",
        "target",
        id="rust",
    ),
    pytest.param(
        "c",
        ".c",
        "int target() { return 1; }\n",
        "extern int target(void);\n\nint caller() { return target(); }\n",
        "target",
        id="c",
    ),
    pytest.param(
        "cpp",
        ".cpp",
        "int target() { return 1; }\n",
        "int target();\n\nint caller() { return target(); }\n",
        "target",
        id="cpp",
    ),
]


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Phase 8 §11.1: cross-file call resolution has known limitations. "
        "Object-prefixed callees (`Defs.target()`) are not handled by the "
        "shared callee extractor, and the edge destination's file offset "
        "is order-dependent. Treat cross-file blast as best-effort until "
        "these are fixed."
    ),
)
@pytest.mark.parametrize(
    "language,suffix,defs_source,use_source,target_qualified_name",
    CROSS_FILE_KNOWN_GAP,
)
def test_cross_file_call_resolves(language, suffix, defs_source, use_source, target_qualified_name):
    sources = [
        (f"defs{suffix}", language, defs_source),
        (f"use{suffix}", language, use_source),
    ]
    data = _build(sources)
    result = _native().compute_blast(data, f"defs{suffix}", target_qualified_name)
    caller_names = {c["qualified_name"] for c in result["direct_callers"]}
    assert caller_names, (
        f"{language}: cross-file call to {target_qualified_name!r} " f"resolved to no callers"
    )


# -- Section D: lone function, all eight languages ---------------------

LONE_FUNCTION_WORKING = [
    pytest.param(
        "python",
        ".py",
        "def target():\n    return 1\n",
        "target",
        id="python",
    ),
    pytest.param(
        "typescript",
        ".ts",
        "function target(): number { return 1; }\n",
        "target",
        id="typescript",
    ),
    pytest.param(
        "javascript",
        ".js",
        "function target() { return 1; }\n",
        "target",
        id="javascript",
    ),
    pytest.param(
        "java",
        ".java",
        "class Main {\n    static int target() { return 1; }\n}\n",
        "Main.target",
        id="java",
    ),
    pytest.param(
        "go",
        ".go",
        "package main\n\nfunc target() int { return 1 }\n",
        "target",
        id="go",
    ),
    pytest.param(
        "rust",
        ".rs",
        "fn target() -> i32 { 1 }\n",
        "target",
        id="rust",
    ),
]


@pytest.mark.parametrize(
    "language,suffix,source,target_qualified_name",
    LONE_FUNCTION_WORKING,
)
def test_lone_function_has_zero_callers(language, suffix, source, target_qualified_name):
    data = _build([(f"main{suffix}", language, source)])
    result = _native().compute_blast(data, f"main{suffix}", target_qualified_name)
    assert result["direct_callers"] == []
    assert result["transitive_callers"] == []
    assert result["blast_score"] == 0
    assert result["recommendation"] == "proceed"


LONE_FUNCTION_KNOWN_GAP = [
    pytest.param(
        "c",
        ".c",
        "int target(void) { return 1; }\n",
        "target",
        id="c",
    ),
    pytest.param(
        "cpp",
        ".cpp",
        "int target() { return 1; }\n",
        "target",
        id="cpp",
    ),
]


@pytest.mark.xfail(
    strict=False,
    reason=(
        "C/C++ function_definition has no `name` field; the blast engine "
        "cannot locate the target node by qualified name"
    ),
)
@pytest.mark.parametrize(
    "language,suffix,source,target_qualified_name",
    LONE_FUNCTION_KNOWN_GAP,
)
def test_lone_function_has_zero_callers_c_family(language, suffix, source, target_qualified_name):
    data = _build([(f"main{suffix}", language, source)])
    result = _native().compute_blast(data, f"main{suffix}", target_qualified_name)
    assert result["direct_callers"] == []
    assert result["blast_score"] == 0


# -- Section E: coexistence -------------------------------------------


def test_eight_languages_build_and_blast_independently():
    sources = [
        ("main.py", "python", "def target():\n    return 1\n"),
        ("main.ts", "typescript", "function target(): number { return 1; }\n"),
        ("main.js", "javascript", "function target() { return 1; }\n"),
        ("main.java", "java", "class Main { static int target() { return 1; } }\n"),
        ("main.go", "go", "package main\n\nfunc target() int { return 1 }\n"),
        ("main.rs", "rust", "fn target() -> i32 { 1 }\n"),
        ("main.c", "c", "int target(void) { return 1; }\n"),
        ("main.cpp", "cpp", "int target() { return 1; }\n"),
    ]
    data = _build(sources)
    header = _native().graph_summary(data)
    assert header["file_count"] == 8
    for _language, suffix, target in [
        ("python", ".py", "target"),
        ("typescript", ".ts", "target"),
        ("javascript", ".js", "target"),
        ("java", ".java", "Main.target"),
        ("go", ".go", "target"),
        ("rust", ".rs", "target"),
    ]:
        result = _native().compute_blast(data, f"main{suffix}", target)
        assert result["direct_callers"] == []
        assert result["blast_score"] == 0
