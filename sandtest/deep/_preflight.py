"""Environment checks before the deep test begins.

This module verifies that every external dependency the deep test needs
is present and functional. It runs before any sandbox is created and
before any repository is cloned, so a failure here means no partial
state has been written to disk.

The checks are:

1. The rpcd binary is present. ``CODEGUARDIAN_RPCD`` overrides the
   default cargo build path.
2. Bubblewrap is installed and can create a sandbox. The self-test runs
   a trivial ``bwrap --ro-bind / / --dev /dev true`` command because
   bwrap can be installed while the kernel still refuses to hand out
   user namespaces.
3. ``git`` is installed. Every repository clone uses it.
4. The ``codeguardian._native`` extension imports and exposes the four
   functions the deep test requires: ``build_graph``, ``graph_summary``,
   ``compute_blast``, and ``PyCPG``.
5. Python 3.12 or newer is running.
6. At least one Groq API key is available, from the environment or the
   operating system keychain. Keys are never printed.
7. A candidate set of language toolchains is probed for availability.
   Repositories whose toolchain is missing are skipped by the caller,
   not by this module. Missing tools are recorded but do not fail the
   preflight, because the deep test still runs against the
   repositories that do not need them.

The result is a ``PreflightResult`` that records every check's outcome,
plus the paths and key set later modules need. When any critical check
fails, the caller should not proceed.

This module has no filesystem side effects until every check has
passed. The work directory is created last, after all verification.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CANDIDATE_TOOLS",
    "CheckResult",
    "PreflightResult",
    "run_preflight",
]


CANDIDATE_TOOLS: tuple[str, ...] = (
    "python3",
    "pip",
    "git",
    "bwrap",
    "go",
    "node",
    "npm",
    "javac",
    "cargo",
)

_REQUIRED_NATIVE_FUNCTIONS: tuple[str, ...] = (
    "build_graph",
    "graph_summary",
    "compute_blast",
    "PyCPG",
)

_GROQ_KEY_SOURCES: tuple[tuple[str, str], ...] = (
    ("GROQ_API_KEY", "groq"),
    ("GROQ_API_KEY_2", "groq_2"),
    ("GROQ_API_KEY_3", "groq_3"),
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One environment check and its outcome.

    Attributes:
        name: The human-readable name of the check.
        ok: True when the check passed.
        detail: A short, non-sensitive explanation of the outcome.
    """

    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """The outcome of a full preflight run.

    Every path field is populated when the check that produced it
    passed, and is ``None`` otherwise. ``checks`` records every check,
    whether passed or failed. ``available_tools`` is the set of
    candidate language toolchains present on PATH. The
    ``groq_keys`` field is a tuple of API keys and must never be
    printed or logged.
    """

    rpcd_path: Path | None
    bwrap_path: Path | None
    git_path: Path | None
    native_module: Any
    groq_keys: tuple[str, ...]
    work_dir: Path | None
    available_tools: frozenset[str]
    checks: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        """Return True when every recorded check passed."""
        return all(check.ok for check in self.checks)


def _find_rpcd() -> tuple[Path | None, str]:
    """Locate the rpcd binary.

    Returns the path and an empty reason on success, or ``None`` and a
    human-readable reason on failure. The environment variable takes
    precedence over the default cargo build path so a custom build
    can be tested without editing the source.
    """
    env = os.environ.get("CODEGUARDIAN_RPCD")
    if env:
        path = Path(env).expanduser()
        if path.is_file():
            return path, ""
        return None, f"CODEGUARDIAN_RPCD={env!r} is not a regular file"
    default = Path.home() / ".cache" / "codeguardian-target" / "release" / "codeguardian-rpcd"
    if default.is_file():
        return default, ""
    return None, (f"not found at {default}; build with cargo or set CODEGUARDIAN_RPCD")


def _find_bwrap() -> tuple[Path | None, str]:
    """Locate the bubblewrap binary on PATH."""
    found = shutil.which("bwrap")
    if found is None:
        return None, "bwrap not on PATH; install bubblewrap"
    return Path(found), ""


def _bwrap_self_test(bwrap_path: Path) -> tuple[bool, str]:
    """Run a minimal bwrap command to confirm it can create a sandbox.

    Bubblewrap can be installed while the kernel still refuses to hand
    out user namespaces, while unprivileged user namespaces are
    disabled, or while the caller lacks the capability bwrap needs.
    Every one of those failure modes presents as a nonzero exit from
    any bwrap command. Catching them here means the deep test fails
    fast instead of halfway through the first sandbox creation.
    """
    try:
        result = subprocess.run(
            [str(bwrap_path), "--ro-bind", "/", "/", "--dev", "/dev", "true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "bwrap self-test timed out after 10s"
    except OSError as exc:
        return False, f"bwrap self-test could not start: {exc}"
    if result.returncode == 0:
        return True, ""
    stderr = result.stderr.decode("utf-8", errors="replace")[:300]
    return False, f"bwrap exit={result.returncode}: {stderr}"


def _find_git() -> tuple[Path | None, str]:
    """Locate the git binary on PATH."""
    found = shutil.which("git")
    if found is None:
        return None, "git not on PATH; install git"
    return Path(found), ""


def _load_native() -> tuple[Any, str]:
    """Import the native extension and verify its required functions.

    Returns the module and an empty reason on success, or ``None`` and
    a reason on failure. A missing function means the compiled
    extension is out of date relative to the Python code; rebuild with
    ``cargo build --workspace --release`` and copy the resulting shared
    library, or run against a matching checkout.
    """
    try:
        import codeguardian._native as native
    except ImportError as exc:
        return None, f"import codeguardian._native failed: {exc}"
    missing = [name for name in _REQUIRED_NATIVE_FUNCTIONS if not hasattr(native, name)]
    if missing:
        return None, f"_native is missing: {', '.join(missing)}"
    return native, ""


def _check_python_version() -> tuple[bool, str]:
    """Confirm the running Python is 3.12 or newer."""
    major, minor, micro = sys.version_info[:3]
    version = f"{major}.{minor}.{micro}"
    if major < 3 or (major == 3 and minor < 12):
        return False, f"Python {version}; requires 3.12 or newer"
    return True, version


def _collect_groq_keys() -> tuple[tuple[str, ...], str]:
    """Collect Groq API keys from the environment and the keychain.

    The environment variable takes precedence because CI pipelines
    typically supply keys that way. Keychain entries ``groq``,
    ``groq_2``, and ``groq_3`` are the fallback for a local
    development machine. A key present in both places is used once;
    duplicates are removed so the rotation slot is not wasted.

    Returns the collected keys and a short label describing the
    source. The keys themselves are never printed by this module or
    its callers.
    """
    keys: list[str] = []
    seen: set[str] = set()
    from_env = 0
    from_keyring = 0

    for env_var, provider in _GROQ_KEY_SOURCES:
        value = os.environ.get(env_var)
        if value:
            from_env += 1
        else:
            try:
                from codeguardian.core.config.secrets import get_api_key

                value = get_api_key(provider)
            except Exception:
                value = None
            if value:
                from_keyring += 1
        if value and value not in seen:
            keys.append(value)
            seen.add(value)

    if from_env and from_keyring:
        source = f"{from_env} env, {from_keyring} keyring"
    elif from_env:
        source = f"{from_env} env"
    elif from_keyring:
        source = f"{from_keyring} keyring"
    else:
        source = "none"

    return tuple(keys), source


def _collect_available_tools() -> frozenset[str]:
    """Return the set of candidate language toolchains on PATH.

    Only the names in ``CANDIDATE_TOOLS`` are probed. A repository
    whose ``required_tools`` is not a subset of the returned set is
    skipped by the caller. Missing tools are not a preflight failure,
    because the deep test still runs against repositories that do not
    need them.
    """
    present: set[str] = set()
    for tool in CANDIDATE_TOOLS:
        if shutil.which(tool) is not None:
            present.add(tool)
    return frozenset(present)


def _prepare_work_dir(clean: bool) -> Path:
    """Create and return the work directory, wiping it first when clean.

    The work directory is the parent of every repository clone and
    sandbox artifact. Wiping it before a run guarantees the state is
    fresh and no stale clone or previous SQLite database interferes
    with the current run. On WSL2 with the project on a Windows
    filesystem the wipe can take a few seconds; the cost is paid once
    per invocation.
    """
    work = Path(__file__).resolve().parent.parent / "work"
    if work.exists() and not work.is_dir():
        raise RuntimeError(f"{work} exists but is not a directory; remove it manually")
    if clean and work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    return work


def run_preflight(*, clean: bool = True) -> PreflightResult:
    """Run every environment check and return the structured result.

    The function does not raise. Every check's outcome is recorded in
    the returned ``PreflightResult.checks``, and the ``ok`` property
    reports whether all checks passed. The caller should print each
    check and stop when ``ok`` is False.

    Args:
        clean: When True, wipe the work directory before creating it
            fresh. Pass False to preserve the previous run's artifacts
            for inspection.

    Returns:
        A ``PreflightResult`` whose ``checks`` records every outcome.
        ``work_dir`` is ``None`` when any check failed.
    """
    checks: list[CheckResult] = []

    rpcd_path, rpcd_reason = _find_rpcd()
    rpcd_detail = rpcd_reason if rpcd_path is None else str(rpcd_path)
    checks.append(CheckResult("rpcd binary present", rpcd_path is not None, rpcd_detail))

    bwrap_path, bwrap_reason = _find_bwrap()
    bwrap_detail = bwrap_reason if bwrap_path is None else str(bwrap_path)
    checks.append(CheckResult("bwrap on PATH", bwrap_path is not None, bwrap_detail))

    bwrap_ok = False
    bwrap_selftest_detail = "skipped: bwrap not on PATH"
    if bwrap_path is not None:
        bwrap_ok, bwrap_selftest_detail = _bwrap_self_test(bwrap_path)
    checks.append(CheckResult("bwrap self-test", bwrap_ok, bwrap_selftest_detail))

    git_path, git_reason = _find_git()
    git_detail = git_reason if git_path is None else str(git_path)
    checks.append(CheckResult("git on PATH", git_path is not None, git_detail))

    native_module, native_reason = _load_native()
    checks.append(
        CheckResult(
            "_native extension",
            native_module is not None,
            native_reason or "ok",
        )
    )

    py_ok, py_detail = _check_python_version()
    checks.append(CheckResult("Python 3.12 or newer", py_ok, py_detail))

    groq_keys, groq_source = _collect_groq_keys()
    checks.append(
        CheckResult(
            "Groq API key(s) available",
            len(groq_keys) > 0,
            groq_source if groq_keys else "no key in env or keychain",
        )
    )

    available_tools = _collect_available_tools()

    work_dir: Path | None = None
    if all(check.ok for check in checks):
        try:
            work_dir = _prepare_work_dir(clean=clean)
            checks.append(
                CheckResult(
                    "work directory ready",
                    True,
                    f"{work_dir} (clean={clean})",
                )
            )
        except (OSError, RuntimeError) as exc:
            checks.append(CheckResult("work directory ready", False, str(exc)))

    return PreflightResult(
        rpcd_path=rpcd_path,
        bwrap_path=bwrap_path,
        git_path=git_path,
        native_module=native_module,
        groq_keys=groq_keys,
        work_dir=work_dir,
        available_tools=available_tools,
        checks=tuple(checks),
    )
