"""Data definitions for every repository the deep test can run.

Each entry is a complete specification: the clone URL, the primary
language, which top-level sections of the deep test run for it, the
install commands needed inside the sandbox, the toolchain required,
and the timeouts.

The module is data, not code. Adding a repository is one dictionary
entry. The discovery, sandbox, and pipeline modules read the
``RepoSpec`` and behave according to its fields; none of them need to
know which repositories exist.

Why only the Python repository runs the agent pipeline: the pipeline
in Phase 11 is Python-specific. It splices Python source, writes a
pytest characterization test, and uses the Python AST to inject fault
drift. Generalizing the pipeline to other languages is a separate
piece of work. What the non-Python repositories prove here is that
the kernel parses them, the blast engine resolves callers on them,
and the graph query interface answers correctly. That is the whole of
G-16 and G-19 exercised against real repositories instead of
synthetic fixtures.

Install pattern for Python repositories: a Python repo whose pipeline
section runs must install its dependencies into ``clone/.deps`` via
``pip install --target .deps <pypi_name>``, not via
``pip install --break-system-packages -e .``. The ``.deps``
directory lives inside ``clone/``, which is bind-mounted and
preserved across the network-ON to network-OFF sandbox transition.
A system site-packages install does not survive that transition, and
``pip install -e .`` has been observed to return exit 0 without
installing the project's declared dependencies for complex projects
like scrapy. Installing the PyPI package into ``.deps`` pulls the
full dependency tree reliably, and the local source in ``clone/``
takes precedence over the PyPI copy at import time because the
launcher puts ``clone/`` ahead of ``.deps`` on ``sys.path``.

A future Python repository that adds a pipeline section follows the
same pattern: provide its install_commands as
``python3 -m pip install --target .deps pytest`` followed by
``python3 -m pip install --target .deps <pypi_name>``. The deep test
does not need to change; only the RepoSpec's install_commands and
the PyPI name change.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ALL_REPOS",
    "DEFAULT_REPO",
    "RepoSpec",
    "filter_by_tools",
    "get_repo",
    "list_repo_names",
    "resolve_repo_arg",
]


_VALID_SECTIONS: frozenset[str] = frozenset(
    {"clone", "parse", "blast", "pipeline", "audit", "crucible"}
)


@dataclass(frozen=True, slots=True)
class RepoSpec:
    """A complete specification for one repository the deep test runs.

    Fields:
        name: The short identifier used on the command line.
        url: The clone URL.
        language: The primary language, or ``"mixed"`` for corpora.
        sections: The subset of the deep test's top-level sections that
            run for this repository. Valid names are ``"clone"``,
            ``"parse"``, ``"blast"``, ``"pipeline"``, ``"audit"``, and
            ``"crucible"``.
        parse_filter: The glob the parser walks when collecting source
            files.
        required_tools: Language toolchains the sandbox needs to
            install the repository. Empty for parse-only repos.
        install_commands: Shell commands to run inside the sandbox
            during the network-ON phase. Empty for parse-only repos.
        test_dir: The repository's test directory, when the pipeline
            runs. Empty otherwise.
        clone_timeout_s: Wall-clock budget for the clone.
        install_timeout_s: Wall-clock budget for all install commands.
        pipeline_timeout_s: Wall-clock budget for a single pipeline run
            (one target, one directive).
        max_targets: Candidate targets to discover for the pipeline.
        notes: A short description of what the repository tests.
    """

    name: str
    url: str
    language: str
    sections: tuple[str, ...]
    parse_filter: str
    required_tools: tuple[str, ...] = ()
    install_commands: tuple[str, ...] = ()
    test_dir: str = ""
    clone_timeout_s: int = 180
    install_timeout_s: int = 360
    pipeline_timeout_s: int = 300
    max_targets: int = 20
    notes: str = ""

    def __post_init__(self) -> None:
        invalid = set(self.sections) - _VALID_SECTIONS
        if invalid:
            raise ValueError(f"RepoSpec {self.name!r} has invalid sections: {sorted(invalid)}")
        if not self.sections:
            raise ValueError(f"RepoSpec {self.name!r} has no sections")


ALL_REPOS: dict[str, RepoSpec] = {
    "httpx": RepoSpec(
        name="httpx",
        url="https://github.com/encode/httpx",
        language="python",
        sections=("clone", "parse", "blast", "pipeline", "audit"),
        parse_filter="*.py",
        required_tools=("python3", "pip"),
        install_commands=(
            "python3 -m pip install --no-cache-dir --quiet --target .deps pytest",
            "python3 -m pip install --no-cache-dir --quiet --target .deps httpx",
        ),
        test_dir="tests",
        clone_timeout_s=180,
        install_timeout_s=360,
        pipeline_timeout_s=300,
        max_targets=20,
        notes=(
            "Python. Three-hop cross-file call chain: client to transport "
            "to decoder. The only repository in the set that exercises "
            "the full Phase 11 agent pipeline."
        ),
    ),
    "scrapy": RepoSpec(
        name="scrapy",
        url="https://github.com/scrapy/scrapy",
        language="python",
        sections=("clone", "parse", "blast", "pipeline", "audit"),
        parse_filter="*.py",
        required_tools=("python3", "pip"),
        install_commands=(
            "python3 -m pip install --no-cache-dir --quiet --target .deps pytest",
            "python3 -m pip install --no-cache-dir --quiet --target .deps scrapy",
            "python3 -m pip install --no-cache-dir --quiet --target .deps --force-reinstall --no-deps 'zope.interface>=7.0'",
            "python3 -m pip install --no-cache-dir --quiet --target .deps w3lib",
        ),
        test_dir="tests",
        clone_timeout_s=300,
        install_timeout_s=900,
        pipeline_timeout_s=600,
        max_targets=20,
        notes=(
            "Python. Cross-module refactoring stress case: multi-hop "
            "call chains across scrapy/spiders, scrapy/http, "
            "scrapy/core, and scrapy/utils. Writer must preserve "
            "signatures that callers in three different packages "
            "depend on. Blast fan-in is in the hundreds."
        ),
    ),
    "gin": RepoSpec(
        name="gin",
        url="https://github.com/gin-gonic/gin",
        language="go",
        sections=("clone", "parse", "audit"),
        parse_filter="*.go",
        clone_timeout_s=180,
        max_targets=20,
        notes=(
            "Go. 99 files of production code, ~240k nodes and ~260k "
            "edges in the CPG. Proves the kernel handles large Go "
            "codebases at scale. Blast on Go is proven separately "
            "by the multi-language three-deep chain, so this repo "
            "does not declare a blast section."
        ),
    ),
    "vaultkit": RepoSpec(
        name="vaultkit",
        url="https://github.com/aleburrascano/vaultkit",
        language="typescript",
        sections=("clone", "parse", "blast", "audit"),
        parse_filter="*.ts",
        clone_timeout_s=180,
        max_targets=15,
        notes=(
            "TypeScript. Dispatch-then-command pattern: entry point "
            "imports each command module, each command imports shared "
            "library modules. Tests ESM import resolution."
        ),
    ),
    "logger-internals-java": RepoSpec(
        name="logger-internals-java",
        url="https://github.com/rajkundalia/logger-internals-java",
        language="java",
        sections=("clone", "parse", "blast", "audit"),
        parse_filter="*.java",
        clone_timeout_s=180,
        max_targets=15,
        notes=(
            "Java. Zero external dependencies, so parse and blast run "
            "without any toolchain install. Tests annotation, generics, "
            "and interface dispatch in the kernel."
        ),
    ),
    "secs": RepoSpec(
        name="secs",
        url="https://github.com/wick3dr0se/secs",
        language="rust",
        sections=("clone", "parse", "blast", "audit"),
        parse_filter="*.rs",
        clone_timeout_s=180,
        max_targets=15,
        notes=(
            "Rust. Small ECS with real method calls on World and "
            "Entity. Tests method resolution on struct receivers and "
            "qualified name construction."
        ),
    ),
    "crucible": RepoSpec(
        name="crucible",
        url="https://github.com/squid-protocol/language-crucible",
        language="mixed",
        sections=("clone", "crucible"),
        parse_filter="*",
        clone_timeout_s=180,
        notes=(
            "Multi-language parse stress corpus. Over fifty languages, "
            "uncompilable by design. Tests dispatch robustness across "
            "the kernel's language registry. No pipeline, no blast."
        ),
    ),
}


DEFAULT_REPO: str = "httpx"


def get_repo(name: str) -> RepoSpec:
    """Return the RepoSpec for the given name.

    Raises:
        ValueError: If the name is not a known repository. The error
            message lists every valid name so the caller can correct a
            typo without reading the source.
    """
    if name not in ALL_REPOS:
        valid = ", ".join(sorted(ALL_REPOS))
        raise ValueError(f"unknown repository {name!r}; valid names: {valid}")
    return ALL_REPOS[name]


def list_repo_names() -> tuple[str, ...]:
    """Return every known repository name in sorted order."""
    return tuple(sorted(ALL_REPOS))


def resolve_repo_arg(arg: str) -> tuple[RepoSpec, ...]:
    """Resolve a ``--repo`` command-line argument to one or more RepoSpecs.

    Accepts a single name (``"httpx"``), a comma-separated list
    (``"httpx,gin"``), or the literal ``"all"``. An empty string
    resolves to the default repository. The literal ``"all"`` is
    reserved and takes precedence over any repository that might be
    named ``"all"`` in the future.

    Raises:
        ValueError: If any part of the argument names an unknown
            repository, or if the resulting list is empty.
    """
    if not arg:
        return (get_repo(DEFAULT_REPO),)
    if arg == "all":
        return tuple(ALL_REPOS[name] for name in list_repo_names())
    parts = [part.strip() for part in arg.split(",") if part.strip()]
    if not parts:
        raise ValueError(f"--repo {arg!r} produced no repository names after parsing")
    return tuple(get_repo(part) for part in parts)


def filter_by_tools(
    repos: tuple[RepoSpec, ...],
    available_tools: frozenset[str],
) -> tuple[
    tuple[RepoSpec, ...],
    tuple[tuple[RepoSpec, tuple[str, ...]], ...],
]:
    """Split a repo list into runnable and skipped-by-missing-tools.

    A repository is runnable when every tool in its ``required_tools``
    is present in ``available_tools``. A repository whose toolchain is
    missing is returned in the second element alongside the specific
    tools that are missing, so the caller can print a useful skip
    message.

    Args:
        repos: The repositories requested by the caller.
        available_tools: The set returned by the preflight check.

    Returns:
        A tuple of ``(runnable, skipped)``. ``skipped`` is a tuple of
        ``(repo, missing_tools)`` pairs.
    """
    runnable: list[RepoSpec] = []
    skipped: list[tuple[RepoSpec, tuple[str, ...]]] = []
    for repo in repos:
        missing = tuple(tool for tool in repo.required_tools if tool not in available_tools)
        if missing:
            skipped.append((repo, missing))
        else:
            runnable.append(repo)
    return tuple(runnable), tuple(skipped)
