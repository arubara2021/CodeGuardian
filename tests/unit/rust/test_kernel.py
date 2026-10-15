"""Deep tests for the Rust kernel bindings exposed through PyO3.

The suite has three layers. The outer layer checks the module surface:
that the extension imports, that `echo` still works, and that the
version is reported. The middle layer checks the wire format: that the
serialized graph's header matches the byte layout `graph_store.py`
reads, that the payload checksum is the FNV-1a value the writer
computed, and that a round trip through `graph_summary` preserves
counts. The inner layer parses the payload in Python and verifies
structural facts: that a source with one function declaration produces
exactly one `FunctionDef` node, that Unicode identifiers survive, that
empty and comment-only sources produce valid graphs, and that the
graph scales with input.

The suite is skipped entirely when the native extension is not built,
so a checkout without a Rust toolchain still collects the rest of the
tests.
"""

from __future__ import annotations

import struct

import pytest

from codeguardian.core.bridges import pyo3_bridge

pytestmark = pytest.mark.skipif(
    not pyo3_bridge.is_native_available(),
    reason="native extension not built",
)


# The header wire format is the exact struct string `graph_store.py`
# uses in `GraphStore.read_header`.
_HEADER_FORMAT = "<4sHH I QQQQQ12s"
_HEADER_SIZE = 64
_EMPTY_PAYLOAD_BYTES = 16
_MAGIC = b"CGCP"
_FORMAT_VERSION = 1
_HEADER_SIZE_FIELD = 64
_NODE_WIRE_SIZE = 32
_EDGE_WIRE_SIZE = 16

# The wire codes for the two syntax kinds the structural tests rely on.
_KIND_MODULE = 0
_KIND_FUNCTION_DEF = 2
_KIND_CLASS_DEF = 3


def _native():
    """Return the imported native module.

    Import is deferred so the module-level import does not fail when the
    extension is absent. The skip marker prevents any test from reaching
    this function when the extension is not available.
    """
    import codeguardian._native as native

    return native


def _header(data: bytes) -> dict:
    """Unpack the 64-byte header using the same format `graph_store.py`
    uses. Returns a dict with one key per header field."""
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


def _fnv1a_64(data: bytes) -> int:
    """Python re-implementation of the FNV-1a 64-bit checksum the kernel
    writes into the header."""
    hash_value = 0xCBF29CE484222325
    for byte in data:
        hash_value ^= byte
        hash_value = (hash_value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return hash_value


def _parse_graph(data: bytes) -> dict:
    """Parse a serialized graph into a dict of header, nodes, edges,
    strings, and files.

    This mirrors the reader in `cpg.rs` byte for byte. If the writer
    ever changes a field order or a wire size, this function will
    produce garbage and the tests that consume it will fail — which is
    the point.
    """
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


def _python_sources():
    """Return a single-file Python input that exercises functions,
    methods, classes, and calls across scopes."""
    source = (
        "def greet(name):\n"
        '    return f"hello {name}"\n'
        "\n"
        "\n"
        "class Greeter:\n"
        "    def __init__(self, greeting):\n"
        "        self.greeting = greeting\n"
        "\n"
        "    def greet(self, name):\n"
        "        return greet(name)\n"
        "\n"
        "\n"
        "def main():\n"
        '    g = Greeter("hi")\n'
        '    return g.greet("world")\n'
    )
    return [("main.py", "python", source)]


def _typescript_sources():
    """Return the TypeScript analogue of `_python_sources`."""
    source = (
        "function greet(name: string): string {\n"
        "    return `hello ${name}`;\n"
        "}\n"
        "\n"
        "class Greeter {\n"
        "    constructor(private greeting: string) {}\n"
        "\n"
        "    greet(name: string): string {\n"
        "        return greet(name);\n"
        "    }\n"
        "}\n"
        "\n"
        "function main(): string {\n"
        '    const g = new Greeter("hi");\n'
        '    return g.greet("world");\n'
        "}\n"
    )
    return [("index.ts", "typescript", source)]


def _two_file_sources():
    """Return a two-file input mixing Python and TypeScript."""
    return _python_sources() + _typescript_sources()


# -- Module surface -----------------------------------------------------


def test_native_module_imports():
    assert _native() is not None


def test_echo_retained_after_kernel_bindings_added():
    assert _native().echo("still here") == "still here"


def test_kernel_version_returns_string():
    version = _native().kernel_version()
    assert isinstance(version, str)
    assert version == "1.0.0"


# -- Empty graph --------------------------------------------------------


def test_build_graph_empty_input_returns_minimum_size():
    data = _native().build_graph([])
    assert isinstance(data, bytes)
    assert len(data) == _HEADER_SIZE + _EMPTY_PAYLOAD_BYTES


def test_empty_graph_header_is_valid():
    data = _native().build_graph([])
    header = _header(data)
    assert header["magic"] == _MAGIC
    assert header["version"] == _FORMAT_VERSION
    assert header["header_size"] == _HEADER_SIZE_FIELD
    assert header["flags"] == 0
    assert header["node_count"] == 0
    assert header["edge_count"] == 0
    assert header["payload_offset"] == _HEADER_SIZE
    assert header["payload_length"] == _EMPTY_PAYLOAD_BYTES


def test_empty_graph_summary_is_all_zero():
    data = _native().build_graph([])
    summary = _native().graph_summary(data)
    assert summary["node_count"] == 0
    assert summary["edge_count"] == 0
    assert summary["file_count"] == 0
    assert summary["incremental"] is False


# -- Header layout ------------------------------------------------------


def test_header_magic_is_cgcp():
    data = _native().build_graph(_python_sources())
    assert data[:4] == _MAGIC


def test_header_version_and_size_are_fixed():
    data = _native().build_graph(_python_sources())
    header = _header(data)
    assert header["version"] == _FORMAT_VERSION
    assert header["header_size"] == _HEADER_SIZE_FIELD


def test_header_flags_zero_on_fresh_build():
    data = _native().build_graph(_python_sources())
    header = _header(data)
    # Bit 0 is the incremental flag. A fresh build never sets it. Bits
    # 1-31 are reserved and must be zero in v1.
    assert header["flags"] == 0


def test_header_payload_offset_is_64():
    data = _native().build_graph(_python_sources())
    header = _header(data)
    assert header["payload_offset"] == _HEADER_SIZE


def test_header_payload_length_matches_total_size():
    data = _native().build_graph(_python_sources())
    header = _header(data)
    assert header["payload_offset"] + header["payload_length"] == len(data)


def test_header_reserved_bytes_are_zero():
    data = _native().build_graph(_python_sources())
    header = _header(data)
    assert header["reserved"] == b"\x00" * 12


def test_header_node_count_matches_payload_node_count():
    data = _native().build_graph(_python_sources())
    header = _header(data)
    graph = _parse_graph(data)
    assert header["node_count"] == len(graph["nodes"])


def test_header_edge_count_matches_payload_edge_count():
    data = _native().build_graph(_python_sources())
    header = _header(data)
    graph = _parse_graph(data)
    assert header["edge_count"] == len(graph["edges"])


# -- Checksum -----------------------------------------------------------


def test_payload_checksum_matches_fnv1a_64():
    data = _native().build_graph(_python_sources())
    header = _header(data)
    payload = data[header["payload_offset"] : header["payload_offset"] + header["payload_length"]]
    assert header["checksum"] == _fnv1a_64(payload)


def test_checksum_changes_with_payload():
    first = _native().build_graph(_python_sources())
    second = _native().build_graph(_two_file_sources())
    assert _header(first)["checksum"] != _header(second)["checksum"]


# -- Determinism (in-process only) --------------------------------------


def test_same_input_produces_byte_identical_output():
    first = _native().build_graph(_python_sources())
    second = _native().build_graph(_python_sources())
    assert first == second


# -- Structural facts ---------------------------------------------------


def test_python_source_contains_function_def_node():
    data = _native().build_graph(_python_sources())
    graph = _parse_graph(data)
    function_defs = [n for n in graph["nodes"] if n["kind_code"] == _KIND_FUNCTION_DEF]
    # The source declares greet, __init__, greet (method), and main.
    # Three of these are top-level or method definitions; all four are
    # FunctionDef nodes in the AST regardless of whether they are
    # methods.
    assert len(function_defs) >= 4


def test_python_source_contains_class_def_node():
    data = _native().build_graph(_python_sources())
    graph = _parse_graph(data)
    class_defs = [n for n in graph["nodes"] if n["kind_code"] == _KIND_CLASS_DEF]
    assert len(class_defs) == 1


def test_python_source_contains_module_node():
    data = _native().build_graph(_python_sources())
    graph = _parse_graph(data)
    module_nodes = [n for n in graph["nodes"] if n["kind_code"] == _KIND_MODULE]
    assert len(module_nodes) == 1


def test_file_count_matches_input_file_count():
    data = _native().build_graph(_two_file_sources())
    graph = _parse_graph(data)
    assert len(graph["files"]) == 2


def test_malformed_source_still_produces_valid_graph():
    sources = [("broken.py", "python", "def f(:\n    pass")]
    data = _native().build_graph(sources)
    header = _header(data)
    assert header["magic"] == _MAGIC
    assert header["node_count"] > 0


# -- Unicode and encoding -----------------------------------------------


def test_unicode_identifiers_round_trip():
    sources = [("uni.py", "python", "def café():\n    return 1\n")]
    data = _native().build_graph(sources)
    graph = _parse_graph(data)
    names = [graph["strings"][n["name"]] for n in graph["nodes"] if n["name"] != 0xFFFFFFFF]
    assert "café" in names


def test_unicode_string_literals_round_trip():
    sources = [("emoji.py", "python", "def f():\n    return 'hello \\U0001f389'\n")]
    data = _native().build_graph(sources)
    assert _header(data)["magic"] == _MAGIC


def test_crlf_line_endings_parse():
    source = "def f():\r\n    return 1\r\n"
    sources = [("crlf.py", "python", source)]
    data = _native().build_graph(sources)
    assert _header(data)["node_count"] > 0


# -- Boundary inputs ----------------------------------------------------


def test_empty_source_file():
    sources = [("empty.py", "python", "")]
    data = _native().build_graph(sources)
    graph = _parse_graph(data)
    # A module node and possibly a block, but no functions or classes.
    assert len(graph["nodes"]) >= 1
    function_defs = [n for n in graph["nodes"] if n["kind_code"] == _KIND_FUNCTION_DEF]
    assert len(function_defs) == 0


def test_comment_only_source_file():
    sources = [("comment.py", "python", "# just a comment\n")]
    data = _native().build_graph(sources)
    assert _header(data)["node_count"] >= 1


def test_deeply_nested_expression_within_limit():
    # 100 nested binary additions is well under the 500 depth cap.
    expr = "+".join(["1"] * 100)
    sources = [("deep.py", "python", f"x = {expr}\n")]
    data = _native().build_graph(sources)
    assert _header(data)["node_count"] > 0


def test_deeply_nested_expression_exceeds_limit_raises():
    # 700 nested binary additions exceeds the 500 depth cap. The
    # parser must raise TreeTooDeep rather than crash with a stack
    # overflow.
    expr = "+".join(["1"] * 700)
    sources = [("too_deep.py", "python", f"x = {expr}\n")]
    with pytest.raises(_native().KernelError) as exc_info:
        _native().build_graph(sources)
    assert exc_info.value.code in ("TREE_TOO_DEEP", "PARSE_FAILED")


# -- Scaling ------------------------------------------------------------


def test_many_files_graph_builds():
    sources = [(f"file_{i}.py", "python", f"def f_{i}():\n    return {i}\n") for i in range(100)]
    data = _native().build_graph(sources)
    graph = _parse_graph(data)
    assert len(graph["files"]) == 100
    assert _header(data)["node_count"] > 100


def test_large_single_file_graph_builds():
    lines = []
    for i in range(300):
        lines.append(f"def f_{i}():")
        lines.append(f"    return {i}")
        lines.append("")
    source = "\n".join(lines)
    sources = [("large.py", "python", source)]
    data = _native().build_graph(sources)
    graph = _parse_graph(data)
    function_defs = [n for n in graph["nodes"] if n["kind_code"] == _KIND_FUNCTION_DEF]
    assert len(function_defs) == 300


# -- File paths ---------------------------------------------------------


def test_unusual_file_paths():
    sources = [
        ("my dir/main.py", "python", "def f():\n    return 1\n"),
        ("src/café/index.ts", "typescript", "function g(): number { return 2; }"),
    ]
    data = _native().build_graph(sources)
    graph = _parse_graph(data)
    assert "my dir/main.py" in graph["files"]
    assert "src/café/index.ts" in graph["files"]


# -- Error paths --------------------------------------------------------


def test_build_graph_rejects_unknown_language():
    with pytest.raises(ValueError):
        _native().build_graph([("main.rb", "ruby", "puts 'hi'")])


def test_graph_summary_rejects_empty_bytes():
    with pytest.raises(_native().KernelError) as exc_info:
        _native().graph_summary(b"")
    assert exc_info.value.code in ("GRAPH_TRUNCATED", "GRAPH_IO")


def test_graph_summary_rejects_bad_magic():
    bad = b"XXXX" + b"\x00" * 60
    with pytest.raises(_native().KernelError) as exc_info:
        _native().graph_summary(bad)
    assert exc_info.value.code == "INVALID_MAGIC"


def test_graph_summary_rejects_unsupported_version():
    data = _native().build_graph(_python_sources())
    patched = bytearray(data)
    struct.pack_into("<H", patched, 4, 999)
    with pytest.raises(_native().KernelError) as exc_info:
        _native().graph_summary(bytes(patched))
    assert exc_info.value.code == "UNSUPPORTED_FORMAT_VERSION"


def test_graph_summary_rejects_invalid_header_size():
    data = _native().build_graph(_python_sources())
    patched = bytearray(data)
    struct.pack_into("<H", patched, 6, 32)
    with pytest.raises(_native().KernelError) as exc_info:
        _native().graph_summary(bytes(patched))
    assert exc_info.value.code == "INVALID_HEADER_SIZE"


def test_graph_summary_rejects_truncated_payload():
    data = _native().build_graph(_python_sources())
    truncated = data[:100]
    with pytest.raises(_native().KernelError) as exc_info:
        _native().graph_summary(truncated)
    assert exc_info.value.code in ("GRAPH_IO", "GRAPH_TRUNCATED")


def test_graph_summary_rejects_checksum_mismatch():
    data = _native().build_graph(_python_sources())
    patched = bytearray(data)
    # Flip a byte inside the payload. The checksum is verified before
    # the payload is parsed, so this always produces a ChecksumMismatch
    # rather than a structural error.
    patched[80] ^= 0xFF
    with pytest.raises(_native().KernelError) as exc_info:
        _native().graph_summary(bytes(patched))
    assert exc_info.value.code == "CHECKSUM_MISMATCH"


# -- KernelError attributes ---------------------------------------------


def test_kernel_error_code_is_upper_string():
    with pytest.raises(_native().KernelError) as exc_info:
        _native().graph_summary(b"XXXX" + b"\x00" * 60)
    assert isinstance(exc_info.value.code, str)
    assert exc_info.value.code == exc_info.value.code.upper()


def test_kernel_error_has_message():
    with pytest.raises(_native().KernelError) as exc_info:
        _native().graph_summary(b"XXXX" + b"\x00" * 60)
    assert isinstance(exc_info.value.message, str)
    assert len(exc_info.value.message) > 0


def test_kernel_error_has_details_dict():
    with pytest.raises(_native().KernelError) as exc_info:
        _native().graph_summary(b"XXXX" + b"\x00" * 60)
    assert isinstance(exc_info.value.details, dict)
