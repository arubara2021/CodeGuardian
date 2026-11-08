"""AST-based discovery of pipeline candidate targets.

Walks a cloned repository for free Python functions, filters them for
safe execution, ranks them by fan-in via the native blast engine, and
selects one candidate from each of the three blast bands.

The band picker is the piece that fixes the Writer-refusal coverage
gap in the legacy deep test. The legacy code deliberately avoided
block-band targets because the Writer refuses on them, which meant
that the Writer's refusal path was never exercised against real code.
The beast selects one target from each band: proceed, review, block.
The block target exists precisely so that the pipeline is proven to
refuse on it.

Discovery uses ``ast.parse`` directly, not the kernel's
``build_graph``. The kernel is the right tool for the CPG the pipeline
consumes; the Python AST is the right tool for the source-level
filters this module applies (annotations, defaults, decorators, and
call-site safety). Using the AST here keeps the candidate shape and
the CPG in sync without requiring a second deserialisation.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "BandSelection",
    "CandidateTarget",
    "RankedTarget",
    "discover_candidates",
    "pick_bands",
    "rank_by_fan_in",
]


# Directories that are never scanned for candidates. A cloned
# repository rarely contains these, but a user who reruns the test
# against a checkout that has run the deep test before will. Skipping
# them keeps the candidate list identical run over run.
_EXCLUDED_PATH_SEGMENTS: frozenset[str] = frozenset(
    {
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "node_modules",
        "target",
        "dist",
        "build",
        ".deps",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "tests",
        "test",
    }
)

# Callees that disqualify a candidate: any function that calls one of
# these could execute arbitrary code, access the network, or touch
# files outside the sandbox during characterization. The list is not
# exhaustive; it covers the common escape hatches that appear in
# ordinary library code.
_FORBIDDEN_CALLEES: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "system",
        "popen",
        "spawn",
        "spawnl",
        "spawnv",
        "urlopen",
        "urlretrieve",
        "connect",
        "bind",
    }
)

_FORBIDDEN_MODULE_PREFIXES: tuple[str, ...] = (
    "socket",
    "subprocess",
    "urllib",
    "http.client",
    "os.system",
)

# Annotations whose sample value is unambiguous and JSON-serialisable.
# A candidate whose required arguments use other annotations is
# skipped rather than guessed at; a wrong sample value would produce a
# characterization test that fails on the old code and hides the real
# behaviour the pipeline is meant to preserve.
_JSON_SAFE_ANNOTATIONS: dict[str, Any] = {
    "str": "sample",
    "int": 0,
    "float": 0.0,
    "bool": False,
    "list": [],
    "dict": {},
    "list[str]": [],
    "list[int]": [],
    "dict[str, str]": {},
    "dict[str, int]": {},
    "Optional[str]": None,
    "Optional[int]": None,
    "str | None": None,
    "int | None": None,
}

# Blast-score bands. The thresholds match the recommendation logic in
# the blast crate and the deep test's existing picker. The bands are
# used here, not there, because the picker is the single place that
# decides which candidates the pipeline runs against.
_PROCEED_MAX_SCORE = 31
_REVIEW_MAX_SCORE = 71

_MIN_FUNCTION_LINES = 5
_MAX_FUNCTION_LINES = 50


@dataclass(frozen=True, slots=True)
class CandidateTarget:
    """A free Python function that could serve as a pipeline target.

    Attributes:
        path: The relative path of the file the function lives in.
        module: The dotted module name derived from the path.
        function: The function's simple name.
        qualified_name: The name the blast engine will use to resolve
            the target. For a free function this is the same as
            ``function``.
        line: The 1-based line the function starts on.
        length: The number of lines in the function's body.
        args: The sample argument values for a characterization call.
        source_text: The exact source of the function, suitable for
            splicing back into the file after a writer edit.
    """

    path: str
    module: str
    function: str
    qualified_name: str
    line: int
    length: int
    args: tuple[Any, ...]
    source_text: str


@dataclass(frozen=True, slots=True)
class RankedTarget:
    """A ``CandidateTarget`` with fan-in and band information.

    ``execution`` is filled in after the generator has run. A ranked
    target that has not been executed yet carries ``execution=None``.
    """

    candidate: CandidateTarget
    direct_count: int
    transitive_count: int
    blast_score: int
    recommendation: str
    cross_file_caller: bool
    execution: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class BandSelection:
    """One ranked target per blast band, or a reason it is missing.

    ``skipped`` records a short reason for each band whose target is
    ``None``. The pipeline runs whatever is present; a missing band
    does not fail the run, it just means the pipeline covered fewer
    bands than the full set.
    """

    proceed: RankedTarget | None = None
    review: RankedTarget | None = None
    block: RankedTarget | None = None
    skipped: dict[str, str] = field(default_factory=dict)

    def selected(self) -> list[RankedTarget]:
        """Return the non-None targets in proceed, review, block order."""
        return [target for target in (self.proceed, self.review, self.block) if target is not None]


def _is_excluded_path(rel: Path) -> bool:
    """Return True when any path segment is in the exclusion set."""
    return any(part in _EXCLUDED_PATH_SEGMENTS for part in rel.parts)


def _module_name_for(rel: Path) -> str | None:
    """Derive the dotted module name from a relative path."""
    parts = list(rel.parts)
    if not parts:
        return None
    # A ``src/`` layout is common enough that skipping the prefix
    # matches what ``pip install -e .`` produces. The same rule is
    # applied by the pipeline when it imports the target module.
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    last = parts[-1]
    if last.endswith(".py"):
        last = last[:-3]
    parts[-1] = last
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def _is_candidate_name(name: str) -> bool:
    """Return True when the function name is eligible."""
    if name.startswith("__") and name.endswith("__"):
        return False
    if name.startswith("_"):
        return False
    if name.startswith("test_"):
        return False
    return not name.endswith("_test")


def _is_call_safe(node: ast.AST) -> bool:
    """Return True when the function body contains no forbidden calls."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            target = sub.func
            name = ""
            if isinstance(target, ast.Name):
                name = target.id
            elif isinstance(target, ast.Attribute):
                name = target.attr
            if name in _FORBIDDEN_CALLEES:
                return False
        if isinstance(sub, ast.Import):
            for alias in sub.names:
                if any(alias.name.startswith(prefix) for prefix in _FORBIDDEN_MODULE_PREFIXES):
                    return False
        if (
            isinstance(sub, ast.ImportFrom)
            and sub.module
            and any(sub.module.startswith(prefix) for prefix in _FORBIDDEN_MODULE_PREFIXES)
        ):
            return False
    return True


def _synthesize_args(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[tuple[Any, ...], str | None]:
    """Return sample argument values for a candidate function call.

    Only required positional arguments are synthesized. A required
    argument whose annotation is not in the safe set disqualifies the
    function. Variadic arguments (``*args``, ``**kwargs``) and
    keyword-only arguments without defaults also disqualify it,
    because the pipeline cannot construct a valid call without more
    information than the AST provides.
    """
    args = func.args
    if args.vararg or args.kwarg:
        return (), "has *args or **kwargs"
    # Keyword-only without a default is missing data we cannot
    # invent. A keyword-only with a default is harmless.
    if args.kwonlyargs and any(default is None for default in args.kw_defaults):
        return (), "has required keyword-only arguments"
    positional = list(args.args)
    n_defaults = len(args.defaults)
    n_required = len(positional) - n_defaults
    if n_required < 0:
        return (), "malformed signature"
    synthesized: list[Any] = []
    for arg in positional[:n_required]:
        ann = arg.annotation
        if ann is None:
            return (), f"unannotated required arg {arg.arg!r}"
        text = ast.unparse(ann)
        if text not in _JSON_SAFE_ANNOTATIONS:
            return (), f"unsupported annotation {text!r} on {arg.arg!r}"
        synthesized.append(_JSON_SAFE_ANNOTATIONS[text])
    return tuple(synthesized), None


def discover_candidates(clone_root: Path) -> list[CandidateTarget]:
    """Return every eligible free Python function in the clone.

    Args:
        clone_root: The directory the repository was cloned into.

    Returns:
        A deterministic list of candidates, sorted by path then by
        line. The sort is stable across runs even when the operating
        system reorders directory entries.

    The list is not ranked. It is the raw material for
    ``rank_by_fan_in``, which is the stage that decides which
    candidates are useful and which are not.
    """
    candidates: list[CandidateTarget] = []
    for path in sorted(clone_root.rglob("*.py")):
        rel = path.relative_to(clone_root)
        if _is_excluded_path(rel):
            continue
        module = _module_name_for(rel)
        if module is None:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(node, ast.AsyncFunctionDef):
                # Async functions are skipped: the characterization
                # generator calls the function synchronously and
                # would need an event loop wrapper that the existing
                # generator does not implement. Extending the
                # generator is future work.
                continue
            if not _is_candidate_name(node.name):
                continue
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            length = end - start + 1
            if length < _MIN_FUNCTION_LINES or length > _MAX_FUNCTION_LINES:
                continue
            if not _is_call_safe(node):
                continue
            args, reason = _synthesize_args(node)
            if reason is not None:
                continue
            source_text = ast.get_source_segment(source, node)
            if source_text is None:
                continue
            candidates.append(
                CandidateTarget(
                    path=str(rel),
                    module=module,
                    function=node.name,
                    qualified_name=node.name,
                    line=start,
                    length=length,
                    args=args,
                    source_text=source_text,
                )
            )
    return candidates


def rank_by_fan_in(
    native: Any,
    cpg_bytes: bytes,
    candidates: list[CandidateTarget],
) -> list[RankedTarget]:
    """Rank candidates by their fan-in using the blast engine.

    Args:
        native: The ``codeguardian._native`` module.
        cpg_bytes: The Code Property Graph the candidates were parsed
            from. It must be the same graph the blast engine will
            query, not a rebuild; the two must agree on node ids.
        candidates: The list produced by ``discover_candidates``.

    Returns:
        The candidates that resolved successfully, ordered by
        descending direct-caller count and then descending transitive
        count. A candidate whose blast report raises is omitted from
        the result and the omission is not an error; the pipeline
        simply has fewer targets to choose from.
    """
    ranked: list[RankedTarget] = []
    for candidate in candidates:
        try:
            report = native.compute_blast(
                cpg_bytes,
                candidate.path,
                candidate.qualified_name,
            )
        except Exception:
            continue
        if not isinstance(report, dict):
            continue
        direct = list(report.get("direct_callers") or [])
        transitive = list(report.get("transitive_callers") or [])
        if not direct and not transitive:
            continue
        cross_file = False
        for caller in direct + transitive:
            caller_path = caller.get("path") if isinstance(caller, dict) else None
            if caller_path and caller_path != candidate.path:
                cross_file = True
                break
        ranked.append(
            RankedTarget(
                candidate=candidate,
                direct_count=len(direct),
                transitive_count=len(transitive),
                blast_score=int(report.get("blast_score", 0)),
                recommendation=str(report.get("recommendation", "review")),
                cross_file_caller=cross_file,
            )
        )
    ranked.sort(
        key=lambda target: (
            -target.direct_count,
            -target.transitive_count,
            target.candidate.path,
            target.candidate.line,
        )
    )
    return ranked


def pick_bands(
    ranked: list[RankedTarget],
    executions: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> BandSelection:
    """Select one target per blast band from the ranked list.

    Args:
        ranked: The list returned by ``rank_by_fan_in``.
        executions: A map from ``(module, function)`` to the
            generator's result record. When supplied, the chosen
            ``RankedTarget`` carries the record in its ``execution``
            field, and the pipeline can write a value-based
            characterization test. When omitted, the pipeline gets a
            target with no execution record and can only write a
            smoke test.

    Returns:
        A ``BandSelection`` with up to three targets. Each selected
        target has its ``execution`` field populated from the map.
        A band that could not be filled records its reason in
        ``skipped``. The picker does not raise; it returns what it
        found.
    """
    from dataclasses import replace

    executions = executions or {}
    selection = BandSelection()

    bands: dict[str, list[RankedTarget]] = {
        "proceed": [],
        "review": [],
        "block": [],
    }
    for target in ranked:
        score = target.blast_score
        if score < _PROCEED_MAX_SCORE:
            bands["proceed"].append(target)
        elif score < _REVIEW_MAX_SCORE:
            bands["review"].append(target)
        else:
            bands["block"].append(target)

    def _execution_key(target: RankedTarget) -> tuple[int, int, str, int]:
        record = executions.get((target.candidate.module, target.candidate.function))
        if record is None:
            deterministic_rank = 2
            error_rank = 2
        else:
            has_error = bool(record.get("error"))
            deterministic = bool(record.get("deterministic"))
            deterministic_rank = 0 if (not has_error and deterministic) else 1
            error_rank = 0 if not has_error else 1
        return (
            deterministic_rank,
            error_rank,
            target.candidate.path,
            target.candidate.line,
        )

    def _with_execution(target: RankedTarget) -> RankedTarget:
        record = executions.get((target.candidate.module, target.candidate.function))
        if record is None:
            return target
        return replace(target, execution=record)

    for band_name in ("proceed", "review", "block"):
        members = bands[band_name]
        if not members:
            selection.skipped[band_name] = "no candidates in this band"
            continue
        members_sorted = sorted(members, key=_execution_key)
        chosen = _with_execution(members_sorted[0])
        if band_name == "proceed":
            selection = BandSelection(
                proceed=chosen,
                review=selection.review,
                block=selection.block,
                skipped=selection.skipped,
            )
        elif band_name == "review":
            selection = BandSelection(
                proceed=selection.proceed,
                review=chosen,
                block=selection.block,
                skipped=selection.skipped,
            )
        else:
            selection = BandSelection(
                proceed=selection.proceed,
                review=selection.review,
                block=chosen,
                skipped=selection.skipped,
            )

    return selection
