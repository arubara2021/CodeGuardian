"""Parse-stress test for the kernel's language dispatch.

Clones a corpus of files in more than fifty languages, walks every
file, groups them by the language the kernel would dispatch to, and
parses each group into one Code Property Graph. The check is:

- No crash for any language the kernel recognizes.
- Every recognized language produces a non-empty graph.
- Languages the kernel does not recognize are counted and skipped.

The corpus is ``squid-protocol/language-crucible``: deliberately
uncompilable snippets in dozens of languages. The point is not to
parse them correctly, it is to prove the kernel does not crash on
them and dispatch correctly identifies every language it claims to
support.

This module never raises for a corpus problem. A missing clone, an
unreadable file, or a language that fails to parse produces a
recorded failure rather than an exception, because the whole point of
the crucible is to enumerate what the kernel does and does not handle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._preflight import CheckResult

__all__ = [
    "CrucibleResult",
    "LanguageReport",
    "run_crucible",
]


# Mapping from file extension to the kernel's language name. Only the
# languages the kernel currently recognizes are listed; a file whose
# extension is not in this map is counted as "unknown extension" and
# skipped. This is the same set the deep test's synthetic corpus
# exercises, plus the two C++ header extensions that appear in real
# repositories.
_EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
}

# A file larger than this is skipped. The crucible corpus contains
# some huge generated files that would dominate the parse time without
# adding any signal the small files do not already provide.
_MAX_FILE_SIZE_BYTES = 500_000


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LanguageReport:
    """One language's parse outcome across every file in the corpus.

    Attributes:
        language: The kernel's language name.
        files_attempted: Files whose extension mapped to this
            language.
        files_parsed: Files that contributed to the successful graph.
            A file whose read failed is excluded here.
        total_nodes: The node count across all parsed files.
        total_edges: The edge count across all parsed files.
        error: When the build_graph call failed, the exception's
            message. When set, the counts are zero.
    """

    language: str
    files_attempted: int
    files_parsed: int
    total_nodes: int
    total_edges: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CrucibleResult:
    """The complete outcome of a crucible run.

    Attributes:
        files_seen: Every file walked, including skipped ones.
        files_parsed: Files that contributed to a graph.
        files_skipped_unknown: Files whose extension was not in
            ``_EXTENSION_TO_LANGUAGE``.
        files_skipped_oversize: Files that exceeded the size cap.
        files_skipped_unreadable: Files that could not be read.
        languages: One report per recognized language.
        overall_error: When the walk itself failed, the reason. When
            set, everything else is empty.
    """

    files_seen: int = 0
    files_parsed: int = 0
    files_skipped_unknown: int = 0
    files_skipped_oversize: int = 0
    files_skipped_unreadable: int = 0
    languages: tuple[LanguageReport, ...] = ()
    overall_error: str | None = None

    def as_checks(self) -> tuple[CheckResult, ...]:
        """Convert the result into a tuple of per-fact check results."""
        if self.overall_error is not None:
            return (CheckResult("crucible walk", False, self.overall_error),)
        results = [
            CheckResult(
                "crucible files walked",
                self.files_seen > 0,
                f"{self.files_seen} seen, {self.files_parsed} parsed, "
                f"{self.files_skipped_unknown} unknown ext, "
                f"{self.files_skipped_oversize} oversize, "
                f"{self.files_skipped_unreadable} unreadable",
            )
        ]
        for report in self.languages:
            if report.error is not None:
                results.append(
                    CheckResult(
                        f"crucible {report.language}",
                        False,
                        report.error,
                    )
                )
                continue
            results.append(
                CheckResult(
                    f"crucible {report.language}",
                    report.files_parsed > 0 and report.total_nodes > 0,
                    f"{report.files_parsed}/{report.files_attempted} files, "
                    f"{report.total_nodes} nodes, {report.total_edges} edges",
                )
            )
        return tuple(results)


def _walk_files(root: Path) -> list[Path]:
    """Return every regular file under ``root``, sorted for stability."""
    return sorted(p for p in root.rglob("*") if p.is_file())


def _group_by_language(
    files: list[Path],
) -> tuple[dict[str, list[Path]], int, int, int]:
    """Group files by language, counting skipped categories separately.

    Returns a tuple of ``(grouped, unknown, oversize, unreadable)``.
    The unreadable count is filled in when a file's size cannot be
    read; a regular read failure during ``run_crucible`` is counted
    there instead.
    """
    grouped: dict[str, list[Path]] = {}
    unknown = 0
    oversize = 0
    unreadable = 0

    for path in files:
        ext = path.suffix.lower()
        language = _EXTENSION_TO_LANGUAGE.get(ext)
        if language is None:
            unknown += 1
            continue
        try:
            size = path.stat().st_size
        except OSError:
            unreadable += 1
            continue
        if size > _MAX_FILE_SIZE_BYTES:
            oversize += 1
            continue
        grouped.setdefault(language, []).append(path)

    return grouped, unknown, oversize, unreadable


def _parse_language(
    native: Any,
    language: str,
    paths: list[Path],
    corpus_root: Path,
) -> LanguageReport:
    """Parse every file of one language into a single graph."""
    sources: list[tuple[str, str, str]] = []
    unreadable = 0
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            unreadable += 1
            continue
        rel = str(path.relative_to(corpus_root))
        sources.append((rel, language, content))

    if not sources:
        return LanguageReport(
            language=language,
            files_attempted=len(paths),
            files_parsed=0,
            total_nodes=0,
            total_edges=0,
            error=f"all {len(paths)} files were unreadable",
        )

    try:
        cpg = native.build_graph(sources)
    except Exception as exc:
        return LanguageReport(
            language=language,
            files_attempted=len(paths),
            files_parsed=0,
            total_nodes=0,
            total_edges=0,
            error=f"{type(exc).__name__}: {exc}",
        )

    try:
        summary = native.graph_summary(bytes(cpg))
    except Exception:
        summary = {}

    return LanguageReport(
        language=language,
        files_attempted=len(paths),
        files_parsed=len(sources),
        total_nodes=int(summary.get("node_count", 0)) if isinstance(summary, dict) else 0,
        total_edges=int(summary.get("edge_count", 0)) if isinstance(summary, dict) else 0,
    )


def run_crucible(
    native: Any,
    corpus_root: Path,
) -> CrucibleResult:
    """Walk the crucible corpus and report per-language parse outcomes.

    Args:
        native: The ``codeguardian._native`` module.
        corpus_root: The cloned crucible corpus directory.

    Returns:
        A ``CrucibleResult`` with per-language reports. A missing
        corpus is an ``overall_error`` rather than an exception. The
        caller records the checks via ``as_checks``.
    """
    if not corpus_root.exists():
        return CrucibleResult(overall_error=f"corpus not found at {corpus_root}")
    if not corpus_root.is_dir():
        return CrucibleResult(overall_error=f"{corpus_root} is not a directory")

    try:
        files = _walk_files(corpus_root)
    except OSError as exc:
        return CrucibleResult(overall_error=f"walk failed: {exc}")

    grouped, unknown, oversize, unreadable = _group_by_language(files)

    reports: list[LanguageReport] = []
    total_parsed = 0
    for language in sorted(grouped.keys()):
        report = _parse_language(native, language, grouped[language], corpus_root)
        reports.append(report)
        total_parsed += report.files_parsed

    return CrucibleResult(
        files_seen=len(files),
        files_parsed=total_parsed,
        files_skipped_unknown=unknown,
        files_skipped_oversize=oversize,
        files_skipped_unreadable=unreadable,
        languages=tuple(reports),
        overall_error=None,
    )
