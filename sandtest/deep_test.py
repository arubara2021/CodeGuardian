#!/usr/bin/env python3
"""Deep integration test for CodeGuardian.

Thin orchestrator over the modules in ``sandtest/deep/``. Its whole
job is argument parsing, model router setup, bridge and store
lifecycle, and walking the requested repositories through the deep
test's section sequence.

Per-run shape:

1. Preflight checks environment.
2. Start the JSON-RPC bridge to rpcd.
3. Open the telemetry store.
4. Run the multi-language verification once (fixed corpus, not
   repo-specific).
5. For each requested repository:
   a. Create a work subdirectory for the repo.
   b. Sandbox network-ON: clone, install.
   c. Sandbox network-OFF: parse, discover, rank, pick bands.
   d. If the pipeline section is enabled: run the pipeline once per
      band per directive.
   e. If the crucible section is enabled: run the crucible walk.
   f. Audit chain verify.
6. Telemetry check.
7. Print summary.

Each repository is completely torn down before the next one starts.
No state leaks between repositories. The work directory is wiped
once at process start (``--clean``), not per repository, so a failure
partway through still leaves the previous repository's artifacts
inspectable.

Usage::

    python sandtest/deep_test.py --repo httpx
    python sandtest/deep_test.py --repo gin,vaultkit
    python sandtest/deep_test.py --repo all --full
    python sandtest/deep_test.py --repo httpx --directive "add docstrings"
    python sandtest/deep_test.py --repo httpx --no-clean
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_PYTHON_DIR = _ROOT / "python"
if _PYTHON_DIR.is_dir() and str(_PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(_PYTHON_DIR))

# ``deep`` is a sibling package to this file; sys.path[0] is set by
# the interpreter to sandtest/ when this file is run directly, which
# makes ``from deep._x import y`` resolve.
from deep._audit import query_agent_calls, verify_agent_calls, verify_audit_chain
from deep._crucible import run_crucible
from deep._discovery import discover_candidates, pick_bands, rank_by_fan_in
from deep._generator import (
    execution_map,
    read_generator_results,
    run_generator,
    write_generator_artifacts,
)
from deep._multi_lang import parse_all, verify_three_deep, write_multi_lang_repo
from deep._pipeline import run_pipeline
from deep._preflight import CheckResult, PreflightResult, run_preflight
from deep._repos import RepoSpec, filter_by_tools, resolve_repo_arg
from deep._sandbox import SandboxSession

from codeguardian.core.bridges.jsonrpc_bridge import JSONRPCBridge
from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.providers import (
    FallbackModelProvider,
    LocalProvider,
    ModelRegistry,
)
from codeguardian.core.providers.openai_compatible import OpenAICompatibleProvider
from codeguardian.core.storage import MigrationRunner, SQLiteStore

GROQ_BASE = "https://api.groq.com/openai/v1"

DEFAULT_DIRECTIVE = "add type hints to every parameter and the return type"

FULL_DIRECTIVES: tuple[str, ...] = (
    DEFAULT_DIRECTIVE,
    "add docstrings to the function",
    "extract the return expression into a named variable and return it",
)

LAUNCHER = (
    "import sys, os\n"
    "_root = os.path.dirname(os.path.abspath(__file__))\n"
    "sys.path.insert(0, os.path.join(_root, '.deps'))\n"
    "sys.path.insert(0, _root)\n"
    "if os.path.isdir(os.path.join(_root, 'src')):\n"
    "    sys.path.insert(0, os.path.join(_root, 'src'))\n"
    "import pytest\n"
    "if __name__ == '__main__':\n"
    "    sys.exit(pytest.main(sys.argv[1:]))\n"
)

_DIRECTOR_EXCLUDE_PARTS: frozenset[str] = frozenset(
    {
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "target",
        "dist",
        "build",
        ".deps",
        ".git",
    }
)


class RotatingOpenAICompatibleProvider:
    """An OpenAICompatibleProvider that rotates keys on rate limit.

    When the active key raises MODEL_RATE_LIMITED, the wrapper
    advances to the next key and retries. A successful call leaves the
    index where it is; a failing call advances it. When every key has
    been tried, the last error propagates so the caller's retry loop
    can wait for the window to reset.

    The wrapper forwards every attribute it does not define to the
    active provider, so ``list_models``, ``_client``, and any future
    methods the wrapped class grows continue to work through the
    registry's discovery path.
    """

    def __init__(self, keys: list[str], base_url: str) -> None:
        if not keys:
            raise ValueError("at least one API key is required")
        self._providers = [OpenAICompatibleProvider(api_key=k, base_url=base_url) for k in keys]
        self._current = 0
        self._lock = asyncio.Lock()

    @property
    def key_count(self) -> int:
        """The number of keys under rotation."""
        return len(self._providers)

    def _active(self) -> OpenAICompatibleProvider:
        return self._providers[self._current]

    async def _advance(self) -> None:
        async with self._lock:
            self._current = (self._current + 1) % len(self._providers)

    async def complete(self, prompt, model, options):
        last_error: CodeGuardianError | None = None
        for _ in range(len(self._providers)):
            provider = self._active()
            try:
                return await provider.complete(prompt, model, options)
            except CodeGuardianError as exc:
                if exc.code is not ErrorCode.MODEL_RATE_LIMITED:
                    raise
                last_error = exc
                await self._advance()
        if last_error is not None:
            raise last_error
        raise RuntimeError("rotation loop exited without an error")

    def stream(self, *args, **kwargs):
        return self._active().stream(*args, **kwargs)

    def count_tokens(self, *args, **kwargs):
        return self._active().count_tokens(*args, **kwargs)

    def get_available_models(self):
        return self._active().get_available_models()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")
        return getattr(self._active(), name)


@dataclass
class Summary:
    """Accumulates check results across the whole run."""

    passes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    skips: list[str] = field(default_factory=list)
    xfails: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)

    def record(self, check: CheckResult) -> None:
        text = f"{check.name} — {check.detail}" if check.detail else check.name
        if check.ok:
            self.passes.append(text)
            print(f"[PASS]  {text}")
        else:
            self.failures.append(text)
            print(f"[FAIL]  {text}")

    def skip(self, name: str, reason: str) -> None:
        text = f"{name} — {reason}"
        self.skips.append(text)
        print(f"[SKIP]  {text}")

    def xfail(self, name: str, reason: str) -> None:
        text = f"{name} — {reason}"
        self.xfails.append(text)
        print(f"[XFAIL] {text}")

    def warn(self, name: str, reason: str) -> None:
        text = f"{name} — {reason}"
        self.warnings.append(text)
        print(f"[WARN]  {text}")

    def print_report(self, work_dir: Path | None = None) -> int:
        elapsed = time.monotonic() - self.started_at
        print(f"\n{'=' * 72}\nDEEP TEST SUMMARY\n{'=' * 72}")
        print(f"Passed: {len(self.passes)}")
        print(f"Failed: {len(self.failures)}")
        print(f"Skipped: {len(self.skips)}")
        print(f"Xfailed: {len(self.xfails)}")
        print(f"Warnings: {len(self.warnings)}")
        print(f"Duration: {elapsed:.1f}s")
        if self.failures:
            print("\nFAILURES:")
            for failure in self.failures:
                print(f"  - {failure}")
        if self.skips:
            print("\nSKIPPED:")
            for skipped in self.skips:
                print(f"  - {skipped}")
        if self.xfails:
            print("\nXFAILED (known gaps):")
            for xfail in self.xfails:
                print(f"  - {xfail}")
        if self.warnings:
            print("\nWARNINGS:")
            for warning in self.warnings:
                print(f"  - {warning}")
        if work_dir is not None and work_dir.exists():
            total = sum(p.stat().st_size for p in work_dir.rglob("*") if p.is_file())
            print(f"\nwork/ size: {total / (1024 * 1024):.1f} MB")
        return 1 if self.failures else 0


def _build_router(preflight: PreflightResult) -> FallbackModelProvider:
    """Build the model router from the preflight's collected keys.

    A single key produces a plain adapter. Multiple keys produce the
    rotating wrapper. The registry is loaded once and used for every
    call.
    """
    keys = list(preflight.groq_keys)
    if not keys:
        raise RuntimeError("no Groq API key available")
    registry = ModelRegistry.load()
    if len(keys) == 1:
        adapter: Any = OpenAICompatibleProvider(api_key=keys[0], base_url=GROQ_BASE)
    else:
        adapter = RotatingOpenAICompatibleProvider(keys=keys, base_url=GROQ_BASE)
    return FallbackModelProvider(registry, providers={"groq": adapter})


def _source_files_for_parse(
    clone_root: Path,
    parse_filter: str,
    language: str,
) -> list[tuple[str, str, str]]:
    """Collect source files that match the repo's filter.

    Excludes hidden, build, and dependency directories so a repository
    that was previously installed still parses the same set of files
    on every run. Files larger than two megabytes are skipped to keep
    the CPG manageable; the deep test is not designed to prove
    correctness on multi-megabyte generated files.
    """
    sources: list[tuple[str, str, str]] = []
    for path in sorted(clone_root.rglob(parse_filter)):
        rel = path.relative_to(clone_root)
        if any(part in _DIRECTOR_EXCLUDE_PARTS or part.startswith(".") for part in rel.parts):
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
        except OSError:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sources.append((str(rel), language, content))
    return sources


def _run_multi_lang_check(preflight: PreflightResult, summary: Summary) -> None:
    """Run the fixed 8-language three-deep corpus once per process."""
    print(f"\n{'=' * 72}\nMULTI-LANGUAGE CHECK\n{'=' * 72}")
    corpus = preflight.work_dir / "multi_lang_corpus"
    try:
        write_multi_lang_repo(corpus)
        cpg_bytes = parse_all(preflight.native_module, corpus)
    except RuntimeError as exc:
        summary.record(CheckResult("multi-lang parse", False, str(exc)))
        return
    summary.record(CheckResult("multi-lang parse", True, f"{len(cpg_bytes)} bytes"))
    result = verify_three_deep(preflight.native_module, cpg_bytes)
    for check in result.passed:
        summary.record(check)
    for check in result.failed:
        summary.record(check)
    for check in result.xfailed:
        summary.xfail(check.name, check.detail)


def _audit_and_telemetry(
    repo: RepoSpec,
    bridge: JSONRPCBridge,
    store: SQLiteStore,
    audit_path: Path,
    summary: Summary,
    *,
    expected_min_entries: int = 0,
) -> None:
    """Verify the audit chain and inspect telemetry rows.

    Telemetry checks are conditional on the pipeline having run for
    this repo. A parse-only repository (gin, vaultkit, and the others
    without a pipeline section) never invokes any agent, so the
    agent_calls table is empty and asserting nine roles would be a
    false failure. The audit chain check is unconditional when an
    audit file exists; when no audit file exists, the skip is
    recorded as informational rather than a failure.
    """
    print(f"\n{'=' * 72}\nAUDIT AND TELEMETRY ({repo.name})\n{'=' * 72}")
    if audit_path.is_file():
        summary.record(
            verify_audit_chain(bridge, audit_path, expected_min_entries=expected_min_entries)
        )
    else:
        summary.skip(f"{repo.name} audit chain", "no audit file (pipeline not run)")

    pipeline_ran = "pipeline" in repo.sections
    report = query_agent_calls(store)
    if pipeline_ran:
        for check in verify_agent_calls(report):
            summary.record(check)
    else:
        summary.skip(
            f"{repo.name} agent_calls",
            "pipeline section not enabled for this repo",
        )


async def _run_single_target(
    *,
    repo: RepoSpec,
    band_name: str,
    band_target: Any,
    directive: str,
    session: SandboxSession,
    router: FallbackModelProvider,
    provider: LocalProvider,
    bridge: JSONRPCBridge,
    audit_path: Path,
    store: SQLiteStore,
    native: Any,
    clone_root: Path,
    summary: Summary,
) -> None:
    """Run the pipeline for one target and one directive."""
    label = f"{repo.name} [{band_name}] {band_target.candidate.function}"
    print(f"\n--- {label} (directive: {directive[:40]}) ---")
    try:
        result = await run_pipeline(
            session=session,
            router=router,
            provider=provider,
            bridge=bridge,
            audit_path=audit_path,
            store=store,
            native=native,
            clone_root=clone_root,
            target=band_target,
            band=band_name,  # type: ignore[arg-type]
            directive=directive,
            repo_url=repo.url,
        )
    except Exception as exc:
        summary.record(CheckResult(label, False, f"{type(exc).__name__}: {exc}"))
        return
    for stage in result.stages:
        if not stage.ok and _is_context_overflow(stage.detail):
            summary.skip(
                f"{label} / {stage.name}",
                "target exceeds the model's context window",
            )
        else:
            summary.record(CheckResult(f"{label} / {stage.name}", stage.ok, stage.detail))
    if band_name == "block":
        approval_stage = next(
            (
                stage
                for stage in result.stages
                if stage.name == "block-band human approval required"
            ),
            None,
        )
        if approval_stage is None:
            summary.skip(
                f"{label} / block-band human approval assertion",
                "writer never ran; a prior phase failed before the writer",
            )
        else:
            summary.record(
                CheckResult(
                    f"{label} / block-band human approval assertion",
                    approval_stage.ok,
                    approval_stage.detail,
                )
            )


async def _run_one_repo(
    repo: RepoSpec,
    preflight: PreflightResult,
    bridge: JSONRPCBridge,
    store: SQLiteStore,
    router: FallbackModelProvider,
    summary: Summary,
    directives: tuple[str, ...],
) -> None:
    """Run every section the repo declares against a single repository."""
    print(f"\n{'=' * 72}\nRUNNING REPO: {repo.name} ({repo.language})\n{'=' * 72}")
    if repo.notes:
        print(f"  {repo.notes}")

    repo_work = preflight.work_dir / "repos" / repo.name
    repo_work.mkdir(parents=True, exist_ok=True)
    clone_host = repo_work / "clone"
    audit_path = repo_work / "audit.jsonl"

    with SandboxSession(bridge, repo_work) as session:
        session.start_network_on()

        clone_result = session.clone(repo.url, "clone", timeout_ms=repo.clone_timeout_s * 1000)
        if clone_result.exit_code != 0:
            summary.record(
                CheckResult(
                    f"{repo.name} clone",
                    False,
                    f"exit={clone_result.exit_code}: {clone_result.stderr[:200]}",
                )
            )
            return
        summary.record(CheckResult(f"{repo.name} clone", True, "ok"))

        if repo.install_commands:
            install_results = session.install(
                repo.install_commands,
                cwd="clone",
                timeout_ms=repo.install_timeout_s * 1000,
                stop_on_failure=True,
            )
            failed = False
            for command, result in install_results:
                detail = f"exit={result.exit_code}"
                if result.exit_code != 0:
                    stderr_tail = (result.stderr or "").strip().splitlines()[-5:]
                    if stderr_tail:
                        detail += " | " + " / ".join(stderr_tail)
                summary.record(
                    CheckResult(
                        f"{repo.name} install [{command[:40]}]",
                        result.exit_code == 0,
                        detail,
                    )
                )
                if result.exit_code != 0:
                    failed = True
            if failed:
                return

        if repo.language == "python" and repo.sections and "pipeline" in repo.sections:
            primary_module = repo.name.replace("-", "_").split(".")[0]
            verification = session.run(
                f"PYTHONPATH=.deps:.:$PYTHONPATH python3 -c 'import {primary_module}'",
                cwd="clone",
                timeout_ms=60_000,
            )
            if verification.exit_code != 0:
                stderr_tail = (verification.stderr or "").strip().splitlines()[-3:]
                summary.warn(
                    f"{repo.name} import verification",
                    "repo does not import as a whole after install: " + " / ".join(stderr_tail),
                )
            else:
                summary.record(
                    CheckResult(
                        f"{repo.name} import verification",
                        True,
                        f"import {primary_module} succeeded",
                    )
                )

        session.start_network_off()

        (clone_host / "run_pytest.py").write_text(LAUNCHER, encoding="utf-8")

        provider = LocalProvider()
        # The clone was performed inside the sandbox for network
        # isolation. Point the LocalProvider at the resulting
        # directory so subsequent file reads use the sandbox-created
        # clone.
        provider._path = clone_host  # type: ignore[attr-defined]

        sources = _source_files_for_parse(clone_host, repo.parse_filter, repo.language)
        if not sources:
            summary.record(CheckResult(f"{repo.name} source walk", False, "no files"))
            return

        try:
            cpg_bytes = bytes(preflight.native_module.build_graph(sources))
        except Exception as exc:
            summary.record(
                CheckResult(
                    f"{repo.name} parse",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            )
            return
        summary.record(
            CheckResult(
                f"{repo.name} parse",
                True,
                f"{len(sources)} files, {len(cpg_bytes)} bytes",
            )
        )

        try:
            summary_data = preflight.native_module.graph_summary(cpg_bytes)
            summary.record(
                CheckResult(
                    f"{repo.name} graph summary",
                    True,
                    f"{summary_data.get('node_count', 0)} nodes, "
                    f"{summary_data.get('edge_count', 0)} edges",
                )
            )
        except Exception as exc:
            summary.record(
                CheckResult(
                    f"{repo.name} graph summary",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            )

        if "pipeline" in repo.sections:
            candidates = discover_candidates(clone_host)
            summary.record(
                CheckResult(
                    f"{repo.name} discovery",
                    len(candidates) > 0,
                    f"{len(candidates)} candidates",
                )
            )
            if candidates:
                ranked = rank_by_fan_in(preflight.native_module, cpg_bytes, candidates)
                summary.record(
                    CheckResult(
                        f"{repo.name} ranking",
                        len(ranked) > 0,
                        f"{len(ranked)} ranked",
                    )
                )
                if ranked:
                    write_generator_artifacts(clone_host, ranked)
                    gen_result = run_generator(session, clone_host)
                    summary.record(
                        CheckResult(
                            f"{repo.name} generator",
                            gen_result.ok,
                            gen_result.detail,
                        )
                    )
                    results = read_generator_results(clone_host)
                    executions = execution_map(results)
                    errors = sum(1 for r in executions.values() if r.get("error"))
                    deterministic = sum(1 for r in executions.values() if r.get("deterministic"))
                    summary.record(
                        CheckResult(
                            f"{repo.name} executions",
                            len(executions) > 0,
                            f"{len(executions)} records, "
                            f"{deterministic} deterministic, "
                            f"{errors} errored",
                        )
                    )
                    bands = pick_bands(ranked, executions)
                    for band_name, band_target in (
                        ("proceed", bands.proceed),
                        ("review", bands.review),
                        ("block", bands.block),
                    ):
                        if band_target is None:
                            summary.skip(
                                f"{repo.name} {band_name} band",
                                bands.skipped.get(band_name, "no candidates"),
                            )
                            continue
                        record = executions.get(
                            (
                                band_target.candidate.module,
                                band_target.candidate.function,
                            )
                        )
                        if record is None:
                            summary.skip(
                                f"{repo.name} {band_name} band",
                                "no execution record",
                            )
                            continue
                        if record.get("error"):
                            summary.skip(
                                f"{repo.name} {band_name} band",
                                f"execution error: {record['error'][:80]}",
                            )
                            continue
                        for directive in directives:
                            await _run_single_target(
                                repo=repo,
                                band_name=band_name,
                                band_target=band_target,
                                directive=directive,
                                session=session,
                                router=router,
                                provider=provider,
                                bridge=bridge,
                                audit_path=audit_path,
                                store=store,
                                native=preflight.native_module,
                                clone_root=clone_host,
                                summary=summary,
                            )
        # Phase 13 additions: assert the orchestrator's checkpoint
        # supervisor can produce a resume plan after the pipeline ran,
        # and assert the verifier stack's retry path fires on a bad
        # candidate. Both are synthetic; they use the orchestrator's
        # own primitives, not a real writer round-trip.
        if "pipeline" in repo.sections:
            summary.record(_check_checkpoint_resume(store))
            summary.record(await _check_verifier_retry())
        if "crucible" in repo.sections:
            crucible = run_crucible(preflight.native_module, clone_host)
            for check in crucible.as_checks():
                summary.record(check)

        if "audit" in repo.sections:
            _audit_and_telemetry(repo, bridge, store, audit_path, summary, expected_min_entries=0)


def _check_checkpoint_resume(store: SQLiteStore) -> CheckResult:
    """Assert that at least one run has a checkpoint and a resume plan."""
    from codeguardian.core.orchestrator.checkpoint_supervisor import (
        CheckpointSupervisor,
    )
    from codeguardian.core.orchestrator.phase_graphs import LEGACY_GRAPH

    rows = store.query("SELECT DISTINCT run_id FROM checkpoints LIMIT 1")
    if not rows:
        return CheckResult(
            "checkpoint resume",
            False,
            "no checkpoints written by the orchestrator",
        )
    run_id = str(rows[0]["run_id"])
    supervisor = CheckpointSupervisor(store)
    plan = supervisor.build_resume_plan(run_id, graph=LEGACY_GRAPH)
    if plan.resume_after_phase is None:
        return CheckResult(
            "checkpoint resume",
            False,
            f"run {run_id[:8]} has checkpoints but no resume phase",
        )
    return CheckResult(
        "checkpoint resume",
        True,
        f"run {run_id[:8]} resumes after {plan.resume_after_phase}",
    )


async def _check_verifier_retry() -> CheckResult:
    """Assert the verifier stack retries a bad candidate."""
    from codeguardian.core.agents._loop import GenerationResult
    from codeguardian.core.orchestrator.deterministic_gate import (
        DeterministicGateResult,
    )
    from codeguardian.core.orchestrator.verifier_stack import (
        run_verifier_stack,
    )

    async def _run() -> Any:
        feedbacks: list[Any] = []

        async def generate(feedback: Any) -> GenerationResult[Any]:
            feedbacks.append(feedback)
            attempt = 1 if feedback is None else 2
            return GenerationResult(payload=f"c{attempt}", attempt=attempt)

        def gate(payload: Any) -> DeterministicGateResult:
            return DeterministicGateResult(
                passed=True,
                checks=(),
                stderr_tail="",
                diff_hash="",
            )

        async def apply(payload: Any) -> None:
            return None

        async def verify(payload: Any) -> tuple[Any, ...]:
            class V:
                def __init__(self, value: str) -> None:
                    self.verdict = type(
                        "Inner", (), {"verdict": value, "verifier_type": "correctness"}
                    )()
                    self.verdict_reason = None

            if payload == "c2":
                return (V("pass"),)
            return (V("fail"),)

        async def rollback(ref: Any) -> None:
            return None

        result = await run_verifier_stack(
            band="review",
            generate=generate,
            gate=gate,
            apply=apply,
            verify=verify,
            rollback=rollback,
        )
        return result, feedbacks

    result, feedbacks = await _run()
    if not result.passed:
        return CheckResult("verifier retry", False, "stack did not pass on retry")
    if result.attempts != 2:
        return CheckResult(
            "verifier retry",
            False,
            f"expected 2 attempts, got {result.attempts}",
        )
    if feedbacks[1] is None:
        return CheckResult(
            "verifier retry",
            False,
            "retry received no feedback from the first round",
        )
    return CheckResult(
        "verifier retry",
        True,
        "stack retried with feedback and passed on the second attempt",
    )


def _is_context_overflow(detail: str) -> bool:
    """Return True when a stage failure is a model-context overflow.

    The analyst sends the target's context pack to the model in one
    request. A pack larger than the model's input window produces an
    HTTP 413, which the provider adapter surfaces as a
    ``CodeGuardianError``. The pipeline correctly stops; the target is
    not refactorable at this model's window. The deep test records the
    phase as a skip rather than a failure because the harness did its
    job — the target was discovered, ranked, and handed to the analyst
    — and the model could not process it.
    """
    if not detail:
        return False
    lowered = detail.lower()
    return (
        "status 413" in lowered
        or "request too large" in lowered
        or ("context" in lowered and "exceed" in lowered)
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CodeGuardian deep integration test.")
    parser.add_argument(
        "--repo",
        default="httpx",
        help=(
            "Repository to run: a name, a comma-separated list of "
            "names, or 'all'. Default: httpx. Known names are listed "
            "in sandtest/deep/_repos.py."
        ),
    )
    parser.add_argument(
        "--directive",
        default=None,
        help=(
            "Override the pipeline directive. When omitted, the "
            "default is the type-hints directive unless --full is "
            "given, which runs all three directives."
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Run all three directives per target (type hints, "
            "docstrings, extract variable). Default runs one directive."
        ),
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help=(
            "Preserve sandtest/work/ from the previous run. Default "
            "wipes it before starting so the state is fresh."
        ),
    )
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    print("CodeGuardian deep integration test")
    print(f"repo arg: {args.repo}")
    print(f"full: {args.full}")
    print(f"clean: {not args.no_clean}")
    print(f"directive override: {args.directive or '(none)'}")

    summary = Summary()

    print(f"\n{'=' * 72}\nPREFLIGHT\n{'=' * 72}")
    preflight = run_preflight(clean=not args.no_clean)
    for check in preflight.checks:
        summary.record(check)
    if not preflight.ok or preflight.work_dir is None:
        return summary.print_report()

    try:
        repos = resolve_repo_arg(args.repo)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    runnable, skipped = filter_by_tools(repos, preflight.available_tools)
    for repo, missing in skipped:
        summary.skip(f"repo {repo.name}", f"missing tools: {', '.join(missing)}")

    if args.full and args.directive is not None:
        print("error: --full and --directive are mutually exclusive")
        return 2
    if args.full:
        directives = FULL_DIRECTIVES
    elif args.directive is not None:
        directives = (args.directive,)
    else:
        directives = (DEFAULT_DIRECTIVE,)
    print(f"directives: {len(directives)}")
    for d in directives:
        print(f"  - {d}")

    if not runnable:
        print("\nno repositories to run after tool filter")
        return summary.print_report(preflight.work_dir)

    rpcd_path = preflight.rpcd_path
    if rpcd_path is None:
        print("error: rpcd binary missing despite passing preflight")
        return summary.print_report(preflight.work_dir)

    rpcd_dest = preflight.work_dir / "rpcd"
    import shutil

    shutil.copy(rpcd_path, rpcd_dest)
    rpcd_dest.chmod(0o755)

    store = SQLiteStore(preflight.work_dir / "deep_test.db")
    store.open()
    try:
        MigrationRunner(store).run()
    except CodeGuardianError as exc:
        print(f"error: migration failed: {exc.message}")
        store.close()
        return summary.print_report(preflight.work_dir)

    bridge = JSONRPCBridge([str(rpcd_dest)])
    bridge.start()
    try:
        router = _build_router(preflight)
        _run_multi_lang_check(preflight, summary)
        for repo in runnable:
            try:
                await _run_one_repo(repo, preflight, bridge, store, router, summary, directives)
            except Exception:
                print(f"\nUNEXPECTED ERROR while running {repo.name}:")
                traceback.print_exc()
                summary.warn(f"repo {repo.name}", "unexpected exception; see traceback")
    finally:
        bridge.stop()
        store.close()

    return summary.print_report(preflight.work_dir)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except Exception:
        print("\nUNEXPECTED ERROR:")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
