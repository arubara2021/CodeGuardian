"""End-to-end blast radius test against psf/requests.

The test builds a real Code Property Graph from the requests source
and runs the blast agent against two targets:

- A class (``Session``) that has many intra-file callers. Blast must
  find them.
- A function (``get_environ_proxies``) called cross-file via an
  imported bare call. Blast currently misses this due to G-16.

The function-target test is marked xfail with a named reason. If the
kernel's call-graph resolution is later extended to handle imported
bare calls, the test will xpass and should be unmarked.

A fourth test exercises the calibration shift: passing
``clone_root=None`` forces ``caller_search_complete=False`` and the
recommendation shifts one band toward caution. This is the honest
signal for incomplete caller search on real data.

The suite skips cleanly when the requests source is not available.
Place the repo at ``tests/e2e/fixtures/requests/`` or run
``sandtest/deep_test.py`` first (which clones into
``sandtest/work/clone/``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("codeguardian._native")

from codeguardian import _native
from codeguardian.core.agents.blast import BlastAgent, BlastInput

__test__ = True


def _find_requests_root() -> Path | None:
    """Return a directory that contains ``requests/sessions.py``.

    Two layouts are accepted: a directory that contains a
    ``requests/`` package, or a directory named ``requests`` whose
    parent contains it. Both appear in practice depending on how the
    source was fetched.
    """
    candidates = [
        Path("tests/e2e/fixtures/requests"),
        Path("sandtest/work/requests"),
        Path("sandtest/work/clone"),
    ]
    for candidate in candidates:
        if (candidate / "requests" / "sessions.py").is_file():
            return candidate
        if candidate.name == "requests" and (candidate / "sessions.py").is_file():
            return candidate.parent
    return None


@pytest.fixture(scope="module")
def requests_repo() -> Path:
    """Return the requests source root, or skip the whole module."""
    root = _find_requests_root()
    if root is None:
        pytest.skip(
            "psf/requests source not available locally. Either run "
            "sandtest/deep_test.py first (it clones into "
            "sandtest/work/clone/) or place the repo at "
            "tests/e2e/fixtures/requests/."
        )
    return root


@pytest.fixture(scope="module")
def cpg_bytes(requests_repo: Path) -> bytes:
    """Build the CPG once for every test in the module."""
    files: list[tuple[str, str, str]] = []
    for path in requests_repo.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(requests_repo).as_posix()
        files.append((rel, "python", source))
    assert files, f"no Python files under {requests_repo}"
    return _native.build_graph(files)


async def test_builds_real_cpg_from_requests(cpg_bytes: bytes) -> None:
    """The CPG built from requests has the expected bulk."""
    summary = _native.graph_summary(cpg_bytes)
    assert summary["node_count"] > 1000
    assert summary["file_count"] > 50
    assert len(cpg_bytes) > 100_000


async def test_blast_on_class_target(cpg_bytes: bytes, requests_repo: Path) -> None:
    """Blast on a class finds intra-file callers."""
    result = await BlastAgent().run(
        BlastInput(
            cpg_bytes=cpg_bytes,
            target_path="requests/sessions.py",
            target_qualified_name="Session",
            clone_root=requests_repo,
        )
    )
    assert len(result.direct_callers) > 0
    assert 0 <= result.blast_score <= 100
    assert result.recommendation_raw in {"proceed", "review", "block"}
    assert result.recommendation_adjusted in {"proceed", "review", "block"}
    artifact = result.to_artifact()
    assert artifact["target"]["qualified_name"] == "Session"
    assert artifact["target"]["path"] == "requests/sessions.py"


@pytest.mark.xfail(
    strict=False,
    reason=(
        "G-16: cross-file imported-function callers under-reported. "
        "get_environ_proxies is imported into requests/sessions.py "
        "and called with a bare name; the kernel's call graph does "
        "not resolve the edge. When this test starts passing, the "
        "kernel has closed the gap and the xfail marker should be "
        "removed."
    ),
)
async def test_blast_on_function_target_imported(cpg_bytes: bytes, requests_repo: Path) -> None:
    """Function imported and called cross-file: under G-16, no callers.

    The test asserts the desired behavior (at least one direct
    caller). Under the current kernel, the call edge does not
    resolve and ``direct_callers`` is empty, so the assertion fails
    and pytest marks the test xfail.
    """
    result = await BlastAgent().run(
        BlastInput(
            cpg_bytes=cpg_bytes,
            target_path="requests/utils.py",
            target_qualified_name="get_environ_proxies",
            clone_root=requests_repo,
        )
    )
    assert len(result.direct_callers) >= 1


async def test_calibration_shift_on_real_data(
    cpg_bytes: bytes,
) -> None:
    """Passing ``clone_root=None`` forces the calibration shift.

    The blast agent cannot verify that all call sites were found
    without a source tree to search. The recommendation must shift
    one band toward caution and the calibration reason must be
    present.
    """
    result = await BlastAgent().run(
        BlastInput(
            cpg_bytes=cpg_bytes,
            target_path="requests/sessions.py",
            target_qualified_name="Session",
            clone_root=None,
        )
    )
    assert result.caller_search_complete is False
    assert result.calibration_reason is not None
    assert "incomplete" in result.calibration_reason.lower()
    assert result.recommendation_adjusted in {"review", "block"}
