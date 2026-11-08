"""Synthetic multi-language corpus with three-deep call chains.

Writes eight files, one per language the kernel supports, each
containing a three-function chain. Blasts the leaf function of each
chain and verifies that the blast engine found both the direct caller
and the transitive caller. The eight languages are Python,
TypeScript, JavaScript, Java, Go, Rust, C, and C++.

C and C++ are expected to fail with a specific reason. The kernel's
function-name extractor does not walk declarator chains, so a C
function whose name is nested inside a ``function_definition`` node
cannot be resolved as a blast target. That gap is tracked as G-15 in
``BUILD_STATE.md``. The test reports those two languages as expected
failures rather than masking the gap; when the declarator chain is
walked, the two expected failures become passes without any change to
this module.

The chain shape is the same in every language: three functions where
``_c`` calls ``_b`` calls ``_a``. Blasting on ``_a`` produces one
direct caller (``_b``) and at least one transitive caller (``_c``).
The pipeline's ability to reconstruct that two-hop path is the actual
test; a single-function-per-language corpus would not exercise it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._preflight import CheckResult

__all__ = [
    "LANGUAGES",
    "MultiLangResult",
    "parse_all",
    "verify_three_deep",
    "write_multi_lang_repo",
]


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    """One language's contribution to the multi-language corpus.

    Attributes:
        name: The language name as the kernel's dispatch expects it.
        filename: The file to write in the corpus directory.
        source: The file contents, containing a three-deep call chain.
        target: The simple name of the leaf function (called by the
            middle function). This is what the test blasts on.
        direct_caller: The name of the function that calls the target
            directly. Blast's direct-caller list must contain this
            name.
        expected_to_work: False for C and C++, where the kernel's
            declarator chain gap (G-15) prevents resolution.
        xfail_reason: The reason recorded when the language is
            expected to fail.
    """

    name: str
    filename: str
    source: str
    target: str
    direct_caller: str
    expected_to_work: bool
    xfail_reason: str = ""


@dataclass(frozen=True, slots=True)
class MultiLangResult:
    """The outcome of a full multi-language verification pass.

    ``passed`` and ``failed`` are the checks that ran; ``xfailed`` is
    the set the caller expected to fail. The ``ok`` property is True
    when ``failed`` is empty.
    """

    passed: tuple[CheckResult, ...]
    failed: tuple[CheckResult, ...]
    xfailed: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        """True when no unexpected failures were recorded."""
        return len(self.failed) == 0


_PYTHON = """\
def py_a(x):
    return x


def py_b(x):
    return py_a(x)


def py_c(x):
    return py_b(x)
"""

_TYPESCRIPT = """\
function ts_a(x: number): number {
    return x;
}

function ts_b(x: number): number {
    return ts_a(x);
}

function ts_c(x: number): number {
    return ts_b(x);
}
"""

_JAVASCRIPT = """\
function js_a(x) {
    return x;
}

function js_b(x) {
    return js_a(x);
}

function js_c(x) {
    return js_b(x);
}
"""

_JAVA = """\
public class JavaChain {
    static int java_a(int x) {
        return x;
    }

    static int java_b(int x) {
        return java_a(x);
    }

    static int java_c(int x) {
        return java_b(x);
    }
}
"""

_GO = """\
package main

func go_a(x int) int {
    return x
}

func go_b(x int) int {
    return go_a(x)
}

func go_c(x int) int {
    return go_b(x)
}
"""

_RUST = """\
fn rust_a(x: i32) -> i32 {
    x
}

fn rust_b(x: i32) -> i32 {
    rust_a(x)
}

fn rust_c(x: i32) -> i32 {
    rust_b(x)
}
"""

_C = """\
int c_a(int x) {
    return x;
}

int c_b(int x) {
    return c_a(x);
}

int c_c(int x) {
    return c_b(x);
}
"""

_CPP = """\
int cpp_a(int x) {
    return x;
}

int cpp_b(int x) {
    return cpp_a(x);
}

int cpp_c(int x) {
    return cpp_b(x);
}
"""


LANGUAGES: tuple[LanguageSpec, ...] = (
    LanguageSpec(
        name="python",
        filename="chain.py",
        source=_PYTHON,
        target="py_a",
        direct_caller="py_b",
        expected_to_work=True,
    ),
    LanguageSpec(
        name="typescript",
        filename="chain.ts",
        source=_TYPESCRIPT,
        target="ts_a",
        direct_caller="ts_b",
        expected_to_work=True,
    ),
    LanguageSpec(
        name="javascript",
        filename="chain.js",
        source=_JAVASCRIPT,
        target="js_a",
        direct_caller="js_b",
        expected_to_work=True,
    ),
    LanguageSpec(
        name="java",
        filename="JavaChain.java",
        source=_JAVA,
        target="JavaChain.java_a",
        direct_caller="JavaChain.java_b",
        expected_to_work=True,
    ),
    LanguageSpec(
        name="go",
        filename="chain.go",
        source=_GO,
        target="go_a",
        direct_caller="go_b",
        expected_to_work=True,
    ),
    LanguageSpec(
        name="rust",
        filename="chain.rs",
        source=_RUST,
        target="rust_a",
        direct_caller="rust_b",
        expected_to_work=True,
    ),
    LanguageSpec(
        name="c",
        filename="chain.c",
        source=_C,
        target="c_a",
        direct_caller="c_b",
        expected_to_work=False,
        xfail_reason="G-15: C declarator chain not walked by extract_name_id",
    ),
    LanguageSpec(
        name="cpp",
        filename="chain.cpp",
        source=_CPP,
        target="cpp_a",
        direct_caller="cpp_b",
        expected_to_work=False,
        xfail_reason="G-15: C++ declarator chain not walked by extract_name_id",
    ),
)


def write_multi_lang_repo(dest: Path) -> Path:
    """Write the eight language files into ``dest``.

    Args:
        dest: The directory to write into. Created when missing.
            A fresh directory is created for each call so a repeated
            run does not mix state from the previous one.

    Returns:
        The ``dest`` path, for chaining into ``parse_all``.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for spec in LANGUAGES:
        (dest / spec.filename).write_text(spec.source, encoding="utf-8")
    return dest


def parse_all(native: Any, repo_dir: Path) -> bytes:
    """Parse every language file in ``repo_dir`` into one Code Property Graph.

    Args:
        native: The ``codeguardian._native`` module.
        repo_dir: The directory produced by ``write_multi_lang_repo``.

    Returns:
        The serialized CPG bytes. Every file is included in a single
        graph so the blast engine sees one consistent picture of the
        corpus.

    Raises:
        RuntimeError: When the kernel raises for any reason. A parse
            failure on a synthetic two-line function is a kernel bug,
            not a test-conditional; it is not caught.
    """
    sources: list[tuple[str, str, str]] = []
    for spec in LANGUAGES:
        path = Path(repo_dir) / spec.filename
        content = path.read_text(encoding="utf-8")
        sources.append((spec.filename, spec.name, content))
    try:
        return bytes(native.build_graph(sources))
    except Exception as exc:
        raise RuntimeError(f"multi-language parse failed: {type(exc).__name__}: {exc}") from exc


def _check_language(
    native: Any,
    cpg_bytes: bytes,
    spec: LanguageSpec,
) -> CheckResult:
    """Blast one language's leaf function and return the check result."""
    label = f"blast {spec.name}:{spec.target}"
    try:
        report = native.compute_blast(
            cpg_bytes,
            spec.filename,
            spec.target,
        )
    except Exception as exc:
        return CheckResult(
            label,
            False,
            f"{type(exc).__name__}: {exc}",
        )
    if not isinstance(report, dict):
        return CheckResult(label, False, f"non-dict report: {type(report).__name__}")
    direct = list(report.get("direct_callers") or [])
    transitive = list(report.get("transitive_callers") or [])
    direct_names = {caller.get("qualified_name") for caller in direct if isinstance(caller, dict)}
    if spec.direct_caller not in direct_names:
        return CheckResult(
            label,
            False,
            f"expected direct caller {spec.direct_caller!r}, got {sorted(direct_names)}",
        )
    if len(transitive) < 1:
        return CheckResult(
            label,
            False,
            "three-deep chain: expected at least one transitive caller, got none",
        )
    return CheckResult(
        label,
        True,
        f"direct={spec.direct_caller}, transitive={len(transitive)}",
    )


def verify_three_deep(
    native: Any,
    cpg_bytes: bytes,
) -> MultiLangResult:
    """Blast each language's leaf function and verify the chain.

    Each language produces a single check. A check passes when the
    blast report contains both the expected direct caller and at least
    one transitive caller. A language whose ``expected_to_work`` is
    False is reported in the ``xfailed`` list with the reason from its
    spec; if it unexpectedly passes, it is reported in ``failed`` so
    the caller learns that the gap was fixed.

    Args:
        native: The ``codeguardian._native`` module.
        cpg_bytes: The combined CPG from ``parse_all``.

    Returns:
        A ``MultiLangResult`` splitting the checks into passed, failed,
        and xfailed. The caller decides how to record each list.
    """
    passed: list[CheckResult] = []
    failed: list[CheckResult] = []
    xfailed: list[CheckResult] = []

    for spec in LANGUAGES:
        result = _check_language(native, cpg_bytes, spec)
        if spec.expected_to_work:
            if result.ok:
                passed.append(result)
            else:
                failed.append(result)
        else:
            if result.ok:
                # The gap that justified the xfail has been fixed. The
                # caller must see this so the xfail can be removed.
                failed.append(
                    CheckResult(
                        result.name,
                        False,
                        f"unexpectedly passed; remove xfail for {spec.name}",
                    )
                )
            else:
                xfailed.append(
                    CheckResult(
                        result.name,
                        False,
                        spec.xfail_reason or "expected failure",
                    )
                )

    return MultiLangResult(
        passed=tuple(passed),
        failed=tuple(failed),
        xfailed=tuple(xfailed),
    )
