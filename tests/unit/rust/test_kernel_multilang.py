"""Parameterized tests across every language wired into the kernel.

The kernel recognises eight languages: Python, TypeScript, JavaScript,
Java, Go, Rust, C, and C++. The suite builds a small file per language
with a declaration, parses the resulting CPG, and asserts that a node
of the expected kind appears. Construct-specific tests cover the
declaration forms that don't exist in Python: Java classes, interfaces,
and enums; Go structs and interfaces; Rust traits, impls, structs, and
enums; C structs, unions, enums, and preprocessor directives; C++
classes, namespaces, and templates.

C# was removed from the supported set because `tree-sitter-c-sharp`
0.23.5 produces a grammar ABI that tree-sitter 0.24 refuses to load.
The suite includes a test that asserts C# now raises "unrecognized
language" rather than the "recognized but not yet implemented" error
it produced when the variant still existed. Re-adding C# requires a
compatible grammar release and a code path change in `langs/mod.rs`.

The suite is skipped entirely when the native extension is not built,
matching `test_kernel.py`.
"""

from __future__ import annotations

import struct

import pytest

from codeguardian.core.bridges import pyo3_bridge

pytestmark = pytest.mark.skipif(
    not pyo3_bridge.is_native_available(),
    reason="native extension not built",
)


_HEADER_FORMAT = "<4sHH I QQQQQ12s"
_HEADER_SIZE = 64
_MAGIC = b"CGCP"
_FORMAT_VERSION = 1
_HEADER_SIZE_FIELD = 64
_NODE_WIRE_SIZE = 32
_EDGE_WIRE_SIZE = 16

_KIND_MODULE = 0
_KIND_FUNCTION_DEF = 2
_KIND_CLASS_DEF = 3
_KIND_METHOD_DEF = 4
_KIND_STRUCT = 55
_KIND_INTERFACE = 56
_KIND_TRAIT = 57
_KIND_IMPL = 58
_KIND_ENUM = 59
_KIND_UNION = 60
_KIND_PREPROCESSOR = 75
_KIND_TEMPLATE_DECLARATION = 76


def _native():
    import codeguardian._native as native

    return native


def _header(data: bytes) -> dict:
    (
        magic,
        version,
        header_size,
        flags,
        node_count,
        edge_count,
        payload_offset,
        payload_length,
        checksum,
        reserved,
    ) = struct.unpack(_HEADER_FORMAT, data[:_HEADER_SIZE])
    return {
        "magic": magic,
        "version": version,
        "header_size": header_size,
        "flags": flags,
        "node_count": node_count,
        "edge_count": edge_count,
        "payload_offset": payload_offset,
        "payload_length": payload_length,
        "checksum": checksum,
        "reserved": reserved,
    }


def _parse_graph(data: bytes) -> dict:
    header = _header(data)
    offset = header["payload_offset"]

    node_count = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    nodes = []
    for _ in range(node_count):
        (
            kind_code,
            kind_payload,
            flags,
            _pad,
            file_id,
            start_byte,
            end_byte,
            start_row,
            start_col,
            name,
        ) = struct.unpack_from("<HHHHIIIIII", data, offset)
        nodes.append(
            {
                "kind_code": kind_code,
                "kind_payload": kind_payload,
                "flags": flags,
                "file_id": file_id,
                "start_byte": start_byte,
                "end_byte": end_byte,
                "start_row": start_row,
                "start_col": start_col,
                "name": name,
            }
        )
        offset += _NODE_WIRE_SIZE

    edge_count = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    edges = []
    for _ in range(edge_count):
        src, dst, kind_code, _pad, label = struct.unpack_from("<IIB3sI", data, offset)
        edges.append({"src": src, "dst": dst, "kind_code": kind_code, "label": label})
        offset += _EDGE_WIRE_SIZE

    string_count = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    strings = []
    for _ in range(string_count):
        length = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        strings.append(data[offset : offset + length].decode("utf-8"))
        offset += length

    file_count = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    files = []
    for _ in range(file_count):
        length = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        files.append(data[offset : offset + length].decode("utf-8"))
        offset += length

    return {
        "header": header,
        "nodes": nodes,
        "edges": edges,
        "strings": strings,
        "files": files,
    }


LANGUAGES = [
    pytest.param(
        "python",
        ".py",
        "def greet():\n    return 1\n",
        (_KIND_FUNCTION_DEF,),
        id="python",
    ),
    pytest.param(
        "typescript",
        ".ts",
        "function greet(): number { return 1; }\n",
        (_KIND_FUNCTION_DEF,),
        id="typescript",
    ),
    pytest.param(
        "javascript",
        ".js",
        "function greet() { return 1; }\n",
        (_KIND_FUNCTION_DEF,),
        id="javascript",
    ),
    pytest.param(
        "java",
        ".java",
        "class Main { int greet() { return 1; } }\n",
        (_KIND_CLASS_DEF, _KIND_METHOD_DEF),
        id="java",
    ),
    pytest.param(
        "go",
        ".go",
        "package main\n\nfunc greet() int { return 1 }\n",
        (_KIND_FUNCTION_DEF,),
        id="go",
    ),
    pytest.param(
        "rust",
        ".rs",
        "fn greet() -> i32 { 1 }\n",
        (_KIND_FUNCTION_DEF,),
        id="rust",
    ),
    pytest.param(
        "c",
        ".c",
        "int greet(void) { return 1; }\n",
        (_KIND_FUNCTION_DEF,),
        id="c",
    ),
    pytest.param(
        "cpp",
        ".cpp",
        "int greet() { return 1; }\n",
        (_KIND_FUNCTION_DEF,),
        id="cpp",
    ),
]


@pytest.mark.parametrize("language,suffix,source,required_kinds", LANGUAGES)
def test_language_produces_required_kinds(language, suffix, source, required_kinds):
    sources = [(f"main{suffix}", language, source)]
    data = _native().build_graph(sources)
    graph = _parse_graph(data)
    present = {n["kind_code"] for n in graph["nodes"]}
    missing = [kind for kind in required_kinds if kind not in present]
    assert not missing, (
        f"{language}: missing kind codes {missing}; "
        f"present codes are {sorted(present)}"
    )


@pytest.mark.parametrize("language,suffix,source,_required", LANGUAGES)
def test_language_header_is_valid(language, suffix, source, _required):
    sources = [(f"main{suffix}", language, source)]
    data = _native().build_graph(sources)
    header = _header(data)
    assert header["magic"] == _MAGIC
    assert header["version"] == _FORMAT_VERSION
    assert header["header_size"] == _HEADER_SIZE_FIELD
    assert header["node_count"] > 0
    assert header["payload_offset"] == _HEADER_SIZE


@pytest.mark.parametrize("language,suffix,source,_required", LANGUAGES)
def test_language_source_is_deterministic(language, suffix, source, _required):
    sources = [(f"main{suffix}", language, source)]
    first = _native().build_graph(sources)
    second = _native().build_graph(sources)
    assert first == second


@pytest.mark.parametrize("language,suffix,_source,_required", LANGUAGES)
def test_language_empty_source_is_valid(language, suffix, _source, _required):
    sources = [(f"empty{suffix}", language, "")]
    data = _native().build_graph(sources)
    header = _header(data)
    assert header["magic"] == _MAGIC
    assert header["node_count"] >= 1


# -- Java construct tests ----------------------------------------------


def test_java_class_declaration():
    data = _native().build_graph([("Main.java", "java", "class Main { }\n")])
    graph = _parse_graph(data)
    class_defs = [n for n in graph["nodes"] if n["kind_code"] == _KIND_CLASS_DEF]
    assert len(class_defs) == 1


def test_java_interface_declaration():
    data = _native().build_graph([("I.java", "java", "interface I { }\n")])
    graph = _parse_graph(data)
    interfaces = [n for n in graph["nodes"] if n["kind_code"] == _KIND_INTERFACE]
    assert len(interfaces) == 1


def test_java_enum_declaration():
    data = _native().build_graph([("E.java", "java", "enum E { A, B }\n")])
    graph = _parse_graph(data)
    enums = [n for n in graph["nodes"] if n["kind_code"] == _KIND_ENUM]
    assert len(enums) == 1


def test_java_method_declaration():
    source = "class Main { int greet() { return 1; } }\n"
    data = _native().build_graph([("Main.java", "java", source)])
    graph = _parse_graph(data)
    methods = [n for n in graph["nodes"] if n["kind_code"] == _KIND_METHOD_DEF]
    assert len(methods) >= 1


# -- Go construct tests ------------------------------------------------


def test_go_function_declaration():
    source = "package main\n\nfunc greet() int { return 1 }\n"
    data = _native().build_graph([("main.go", "go", source)])
    graph = _parse_graph(data)
    functions = [n for n in graph["nodes"] if n["kind_code"] == _KIND_FUNCTION_DEF]
    assert len(functions) >= 1


def test_go_struct_declaration():
    source = "package main\n\ntype S struct { X int }\n"
    data = _native().build_graph([("main.go", "go", source)])
    graph = _parse_graph(data)
    structs = [n for n in graph["nodes"] if n["kind_code"] == _KIND_STRUCT]
    assert len(structs) >= 1


def test_go_interface_declaration():
    source = "package main\n\ntype I interface { Do() }\n"
    data = _native().build_graph([("main.go", "go", source)])
    graph = _parse_graph(data)
    interfaces = [n for n in graph["nodes"] if n["kind_code"] == _KIND_INTERFACE]
    assert len(interfaces) >= 1


# -- Rust construct tests ----------------------------------------------


def test_rust_function_declaration():
    data = _native().build_graph([("main.rs", "rust", "fn greet() -> i32 { 1 }\n")])
    graph = _parse_graph(data)
    functions = [n for n in graph["nodes"] if n["kind_code"] == _KIND_FUNCTION_DEF]
    assert len(functions) >= 1


def test_rust_struct_declaration():
    data = _native().build_graph([("main.rs", "rust", "struct S;\n")])
    graph = _parse_graph(data)
    structs = [n for n in graph["nodes"] if n["kind_code"] == _KIND_STRUCT]
    assert len(structs) >= 1


def test_rust_trait_declaration():
    data = _native().build_graph([("main.rs", "rust", "trait T { }\n")])
    graph = _parse_graph(data)
    traits = [n for n in graph["nodes"] if n["kind_code"] == _KIND_TRAIT]
    assert len(traits) == 1


def test_rust_impl_block():
    data = _native().build_graph([("main.rs", "rust", "struct S;\nimpl S { }\n")])
    graph = _parse_graph(data)
    impls = [n for n in graph["nodes"] if n["kind_code"] == _KIND_IMPL]
    assert len(impls) == 1


def test_rust_enum_declaration():
    data = _native().build_graph([("main.rs", "rust", "enum E { A, B }\n")])
    graph = _parse_graph(data)
    enums = [n for n in graph["nodes"] if n["kind_code"] == _KIND_ENUM]
    assert len(enums) == 1


# -- C construct tests -------------------------------------------------


def test_c_function_declaration():
    data = _native().build_graph([("main.c", "c", "int greet(void) { return 1; }\n")])
    graph = _parse_graph(data)
    functions = [n for n in graph["nodes"] if n["kind_code"] == _KIND_FUNCTION_DEF]
    assert len(functions) >= 1


def test_c_struct_declaration():
    data = _native().build_graph([("main.c", "c", "struct S { int x; };\n")])
    graph = _parse_graph(data)
    structs = [n for n in graph["nodes"] if n["kind_code"] == _KIND_STRUCT]
    assert len(structs) >= 1


def test_c_union_declaration():
    data = _native().build_graph([("main.c", "c", "union U { int a; float b; };\n")])
    graph = _parse_graph(data)
    unions = [n for n in graph["nodes"] if n["kind_code"] == _KIND_UNION]
    assert len(unions) >= 1


def test_c_enum_declaration():
    data = _native().build_graph([("main.c", "c", "enum E { A, B };\n")])
    graph = _parse_graph(data)
    enums = [n for n in graph["nodes"] if n["kind_code"] == _KIND_ENUM]
    assert len(enums) >= 1


def test_c_preprocessor_include():
    data = _native().build_graph([("main.c", "c", "#include <stdio.h>\n")])
    graph = _parse_graph(data)
    preprocs = [n for n in graph["nodes"] if n["kind_code"] == _KIND_PREPROCESSOR]
    assert len(preprocs) >= 1


# -- C++ construct tests -----------------------------------------------


def test_cpp_function_declaration():
    data = _native().build_graph([("main.cpp", "cpp", "int greet() { return 1; }\n")])
    graph = _parse_graph(data)
    functions = [n for n in graph["nodes"] if n["kind_code"] == _KIND_FUNCTION_DEF]
    assert len(functions) >= 1


def test_cpp_class_declaration():
    data = _native().build_graph([("main.cpp", "cpp", "class C { };\n")])
    graph = _parse_graph(data)
    class_defs = [n for n in graph["nodes"] if n["kind_code"] == _KIND_CLASS_DEF]
    assert len(class_defs) >= 1


def test_cpp_namespace_declaration():
    data = _native().build_graph([("main.cpp", "cpp", "namespace app { }\n")])
    graph = _parse_graph(data)
    namespaces = [n for n in graph["nodes"] if n["kind_code"] == 61]
    assert len(namespaces) >= 1


def test_cpp_template_declaration():
    source = "template <typename T> T identity(T x) { return x; }\n"
    data = _native().build_graph([("main.cpp", "cpp", source)])
    graph = _parse_graph(data)
    templates = [n for n in graph["nodes"] if n["kind_code"] == _KIND_TEMPLATE_DECLARATION]
    assert len(templates) >= 1


# -- Cross-language ---------------------------------------------------


def test_eight_languages_in_one_graph():
    sources = [
        ("main.py", "python", "def py():\n    return 1\n"),
        ("main.ts", "typescript", "function ts(): number { return 1; }\n"),
        ("main.js", "javascript", "function js() { return 1; }\n"),
        ("Main.java", "java", "class Main { }\n"),
        ("main.go", "go", "package main\n"),
        ("main.rs", "rust", "fn rs() -> i32 { 1 }\n"),
        ("main.c", "c", "int c_fn(void) { return 1; }\n"),
        ("main.cpp", "cpp", "int cpp_fn() { return 1; }\n"),
    ]
    data = _native().build_graph(sources)
    graph = _parse_graph(data)
    assert len(graph["files"]) == 8
    function_defs = [n for n in graph["nodes"] if n["kind_code"] == _KIND_FUNCTION_DEF]
    # py, ts, js, rs, c, cpp each declare one function; Java declares a
    # class; Go declares only a package.
    assert len(function_defs) >= 6


def test_language_name_aliases_resolve():
    canonical = _native().build_graph([("main.js", "javascript", "function f() {}\n")])
    aliased = _native().build_graph([("main.js", "js", "function f() {}\n")])
    assert canonical == aliased

    canonical = _native().build_graph([("main.go", "go", "package main\n")])
    aliased = _native().build_graph([("main.go", "golang", "package main\n")])
    assert canonical == aliased

    canonical = _native().build_graph([("main.rs", "rust", "fn f() {}\n")])
    aliased = _native().build_graph([("main.rs", "rs", "fn f() {}\n")])
    assert canonical == aliased

    canonical = _native().build_graph([("main.cpp", "cpp", "int f() { return 0; }\n")])
    aliased = _native().build_graph([("main.cpp", "c++", "int f() { return 0; }\n")])
    assert canonical == aliased


def test_unrecognized_language_ruby():
    with pytest.raises(ValueError, match="unrecognized language"):
        _native().build_graph([("main.rb", "ruby", "puts 'hi'\n")])


def test_unrecognized_language_csharp():
    # C# was removed after tree-sitter-c-sharp 0.23.5 was found to
    # produce an ABI that tree-sitter 0.24 cannot load. The name is
    # now rejected at the same boundary as Ruby.
    with pytest.raises(ValueError, match="unrecognized language"):
        _native().build_graph([("Main.cs", "csharp", "class C { }\n")])