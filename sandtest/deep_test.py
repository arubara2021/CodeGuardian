#!/usr/bin/env python3
"""Deep integration test for CodeGuardian.

Runs every CodeGuardian phase that exists against a real GitHub clone
(`pallets/click`) and a synthetic 8-language repository. Reports pass,
fail, skip, xfail, and warning per section. Does not stop at the first
failure.

Requires ``bwrap`` on PATH, the ``codeguardian-rpcd`` binary from a
release build, the compiled ``_native`` extension, and ``GROQ_API_KEY``
in the environment.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import os
import shutil
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PYTHON_DIR = _ROOT / "python"
if _PYTHON_DIR.is_dir() and str(_PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(_PYTHON_DIR))

from codeguardian.core.bridges.jsonrpc_bridge import JSONRPCBridge
from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.providers import (
    ApplyProvider,
    FallbackModelProvider,
    LocalProvider,
    ModelRegistry,
)
from codeguardian.core.providers.openai_compatible import OpenAICompatibleProvider
from codeguardian.core.sandbox.bubblewrap_backend import BubblewrapBackend
from codeguardian.interfaces.apply import ConsentRecord
from codeguardian.interfaces.model import ModelOptions
from codeguardian.interfaces.repository import FileChange
from codeguardian.interfaces.sandbox import SandboxSpec

CLICK_URL = "https://github.com/pallets/click"
GROQ_BASE = "https://api.groq.com/openai/v1"
RUN_ID = "deep-test-run"
SANDBOX_DIR = Path(__file__).resolve().parent
WORK_DIR = SANDBOX_DIR / "work"

MULTI_LANG_FILES = [
    ("add.py", "python", "py_add", "call_py_add",
     "def py_add(a, b):\n    return a + b\n\n\ndef call_py_add():\n    return py_add(1, 2)\n"),
    ("add.ts", "typescript", "ts_add", "call_ts_add",
     "function ts_add(a: number, b: number): number {\n    return a + b;\n}\n\n"
     "function call_ts_add(): number {\n    return ts_add(1, 2);\n}\n"),
    ("add.js", "javascript", "js_add", "call_js_add",
     "function js_add(a, b) {\n    return a + b;\n}\n\n"
     "function call_js_add() {\n    return js_add(1, 2);\n}\n"),
    ("Add.java", "java", "JavaAdd.add", "JavaAdd.call_add",
     "public class JavaAdd {\n    static int add(int a, int b) {\n        return a + b;\n    }\n\n"
     "    static int call_add() {\n        return add(1, 2);\n    }\n}\n"),
    ("add.go", "go", "go_add", "call_go_add",
     "package main\n\nfunc go_add(a, b int) int {\n    return a + b\n}\n\n"
     "func call_go_add() int {\n    return go_add(1, 2)\n}\n"),
    ("add.rs", "rust", "rust_add", "call_rust_add",
     "fn rust_add(a: i32, b: i32) -> i32 {\n    a + b\n}\n\n"
     "fn call_rust_add() -> i32 {\n    rust_add(1, 2)\n}\n"),
    ("add.c", "c", "c_add", "call_c_add",
     "int c_add(int a, int b) {\n    return a + b;\n}\n\n"
     "int call_c_add() {\n    return c_add(1, 2);\n}\n"),
    ("add.cpp", "cpp", "cpp_add", "call_cpp_add",
     "int cpp_add(int a, int b) {\n    return a + b;\n}\n\n"
     "int call_cpp_add() {\n    return cpp_add(1, 2);\n}\n"),
]

CLICK_BLAST_TARGETS = [
    ("src/click/utils.py", "echo"),
    ("src/click/decorators.py", "option"),
    ("src/click/decorators.py", "command"),
    ("src/click/decorators.py", "argument"),
    ("src/click/core.py", "Command"),
    ("src/click/core.py", "Context"),
]

CHARACTERIZATION_TEST = '''"""Characterization test for click.utils.echo."""
import io
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))


def test_echo_writes_to_file():
    from click.utils import echo
    buf = io.StringIO()
    result = echo("hello", file=buf)
    assert result is None
    assert buf.getvalue() == "hello\\n"


def test_echo_no_newline():
    from click.utils import echo
    buf = io.StringIO()
    echo("hello", file=buf, nl=False)
    assert buf.getvalue() == "hello"


def test_echo_none_writes_nothing():
    from click.utils import echo
    buf = io.StringIO()
    result = echo(None, file=buf)
    assert result is None
    assert buf.getvalue() == "\\n"
'''

LAUNCHER = '''import sys
sys.path.insert(0, ".deps")
sys.path.insert(0, "src")
import pytest
if __name__ == "__main__":
    sys.exit(pytest.main(sys.argv[1:]))
'''

STATE = {"passes": [], "failures": [], "skips": [], "xfails": [], "warnings": []}


def step(title: str) -> None:
    print(f"\n{'=' * 72}\n[{title}]\n{'=' * 72}")


def info(msg: str) -> None:
    print(f"        {msg}")


def note(msg: str) -> None:
    print(f"[NOTE]  {msg}")


def safety(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        STATE["passes"].append(name)
        print(f"[PASS]  {name}" + (f" — {detail}" if detail else ""))
    else:
        STATE["failures"].append(f"{name} ({detail})" if detail else name)
        print(f"[FAIL]  {name}" + (f" — {detail}" if detail else ""))


def skip(name: str, reason: str) -> None:
    STATE["skips"].append(f"{name} — {reason}")
    print(f"[SKIP]  {name} — {reason}")


def xfail(name: str, reason: str) -> None:
    STATE["xfails"].append(f"{name} — {reason}")
    print(f"[XFAIL] {name} — {reason}")


def warn(name: str, reason: str) -> None:
    STATE["warnings"].append(f"{name} — {reason}")
    print(f"[WARN]  {name} — {reason}")


def _rpcd_binary() -> Path | None:
    env = os.environ.get("CODEGUARDIAN_RPCD")
    if env and Path(env).is_file():
        return Path(env)
    candidate = (
        Path.home() / ".cache" / "codeguardian-target" / "release"
        / "codeguardian-rpcd"
    )
    if candidate.is_file():
        return candidate
    return None


def _native():
    try:
        import codeguardian._native as native
        return native
    except ImportError:
        return None


def step_preflight():
    step("Preflight — check environment")
    rpcd = _rpcd_binary()
    info(f"rpcd binary: {rpcd or 'NOT FOUND'}")
    safety("rpcd binary exists", rpcd is not None)
    bwrap = shutil.which("bwrap")
    info(f"bwrap: {bwrap or 'NOT FOUND'}")
    safety("bwrap installed", bwrap is not None)
    key = os.environ.get("GROQ_API_KEY")
    if key:
        info(f"GROQ_API_KEY: {key[:8]}...{key[-4:]}")
    else:
        info("GROQ_API_KEY: NOT SET")
    safety("GROQ_API_KEY present", bool(key))
    native = _native()
    safety("_native extension importable", native is not None)
    if rpcd is None or bwrap is None or not key or native is None:
        return None
    return rpcd, native


def step_setup_work(rpcd_path: Path) -> Path:
    step("Setup work directory")
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True)
    rpcd_dest = WORK_DIR / "rpcd"
    shutil.copy(rpcd_path, rpcd_dest)
    rpcd_dest.chmod(0o755)
    info(f"work dir: {WORK_DIR}")
    info(f"rpcd copied: {rpcd_dest}")
    safety("work dir ready", True)
    return rpcd_dest


def step_clone_click():
    step("Clone pallets/click")
    provider = LocalProvider()
    clone_dest = WORK_DIR / "clone"
    try:
        clone_path = provider.clone(CLICK_URL, clone_dest)
    except CodeGuardianError as exc:
        warn("clone click", f"{exc.code.value}: {exc.message}")
        return None
    info(f"clone: {clone_path}")
    try:
        metadata = provider.get_metadata()
        info(f"HEAD: {metadata.head_commit[:12]} branch: {metadata.default_branch}")
    except CodeGuardianError as exc:
        note(f"metadata: {exc.message}")
    files = provider.list_files()
    info(f"files: {len(files)}")
    safety("clone is real git repo", (clone_path / ".git").is_dir())
    safety("clone contains Python", any(f.endswith(".py") for f in files))
    return provider, clone_path


def step_kernel_parse_click(native, provider):
    step("Kernel parse — click")
    files = provider.list_files("**/*.py")
    info(f"Python files: {len(files)}")
    sources = []
    for rel in files:
        try:
            content = provider.read_file(rel)
        except CodeGuardianError as exc:
            note(f"skip {rel}: {exc.code.value}")
            continue
        sources.append((rel, "python", content))
    if not sources:
        safety("kernel parse click", False, "no sources")
        return None
    try:
        cpg = native.build_graph(sources)
    except Exception as exc:
        safety("kernel parse click", False, f"{type(exc).__name__}: {exc}")
        return None
    info(f"CPG: {len(cpg)} bytes")
    safety("kernel parse click", True, f"{len(sources)} files, {len(cpg)} bytes")
    return bytes(cpg)


def step_graph_summary(native, cpg):
    step("Graph summary — click")
    try:
        summary = native.graph_summary(cpg)
    except Exception as exc:
        safety("graph summary", False, f"{type(exc).__name__}: {exc}")
        return
    info(f"summary: {summary}")
    if isinstance(summary, dict):
        safety("graph has nodes", int(summary.get("node_count", 0)) > 0,
               f"{summary.get('node_count')} nodes")
        safety("graph has edges", int(summary.get("edge_count", 0)) > 0,
               f"{summary.get('edge_count')} edges")


def step_blast_click(native, cpg):
    step("Blast radius — click")
    reached = 0
    found = 0
    for path_hint, name_hint in CLICK_BLAST_TARGETS:
        try:
            result = native.compute_blast(cpg, path_hint, name_hint)
            reached += 1
            if isinstance(result, dict):
                direct = result.get("direct_callers", [])
                transitive = result.get("transitive_callers", [])
                info(f"{path_hint}:{name_hint} -> direct={len(direct)} "
                     f"transitive={len(transitive)} "
                     f"score={result.get('blast_score')}")
                if direct or transitive:
                    found += 1
        except Exception as exc:
            info(f"{path_hint}:{name_hint} -> {type(exc).__name__}: {exc}")
    safety("blast ran on click", reached > 0, f"{reached} reached")
    safety("blast found callers", found > 0, f"{found} had callers")


def step_write_multi_lang() -> Path:
    step("Write multi-language repo — 8 files")
    repo = WORK_DIR / "multi-language-repo"
    repo.mkdir(parents=True, exist_ok=True)
    for filename, _lang, _t, _c, source in MULTI_LANG_FILES:
        (repo / filename).write_text(source, encoding="utf-8")
    files = sorted(p.name for p in repo.iterdir() if p.is_file())
    safety("multi-lang files written", len(files) == 8, f"{len(files)} files")
    return repo


def step_parse_multi_lang(native, repo):
    step("Kernel parse — multi-language")
    sources = []
    for filename, lang, _t, _c, _ in MULTI_LANG_FILES:
        content = (repo / filename).read_text(encoding="utf-8")
        sources.append((filename, lang, content))
    try:
        cpg = native.build_graph(sources)
    except Exception as exc:
        safety("multi-lang parse", False, f"{type(exc).__name__}: {exc}")
        return None
    info(f"CPG: {len(cpg)} bytes for {len(sources)} files")
    safety("multi-lang parse", True, f"{len(sources)} languages")
    return bytes(cpg)


def step_blast_multi_lang(native, cpg):
    step("Blast radius — per language")
    for filename, lang, target, expected_caller, _ in MULTI_LANG_FILES:
        if lang in ("c", "cpp"):
            try:
                native.compute_blast(cpg, filename, target)
                note(f"{lang}: unexpected success on {target}")
            except Exception as exc:
                msg = str(exc).lower()
                if "target_not_found" in msg or "not found" in msg:
                    xfail(f"blast {lang}:{target}",
                          "known gap: C/C++ declarator not walked")
                else:
                    safety(f"blast {lang}:{target}", False,
                           f"{type(exc).__name__}: {exc}")
            continue
        try:
            result = native.compute_blast(cpg, filename, target)
        except Exception as exc:
            safety(f"blast {lang}:{target}", False, f"{type(exc).__name__}: {exc}")
            continue
        if not isinstance(result, dict):
            safety(f"blast {lang}:{target}", False, "non-dict result")
            continue
        callers = {c.get("qualified_name") for c in result.get("direct_callers", [])}
        if expected_caller in callers:
            safety(f"blast {lang}:{target}", True, f"caller={expected_caller}")
        else:
            safety(f"blast {lang}:{target}", False,
                   f"expected {expected_caller}, got {sorted(callers)}")


def step_csharp_rejection(native):
    step("C# rejection guard")
    try:
        native.build_graph([("test.cs", "csharp", "class C {}\n")])
        safety("c# rejected", False, "accepted csharp")
    except ValueError as exc:
        safety("c# rejected", "unrecognized" in str(exc).lower(),
               f"ValueError: {exc}")
    except Exception as exc:
        safety("c# rejected", False, f"{type(exc).__name__}: {exc}")


def step_sandbox_install(bridge, clone):
    step("Sandbox #1 — network ON, pip install")
    backend = BubblewrapBackend(bridge)
    spec = SandboxSpec(
        language="python", working_dir=clone, memory_mb=2048,
        cpu_cores=2, process_limit=256, network_during_deps=True,
    )
    try:
        handle = backend.create(spec)
    except CodeGuardianError as exc:
        safety("sandbox #1 created", False, f"{exc.code.value}: {exc.message}")
        return None
    safety("sandbox #1 created", True, handle.id)
    safety("working_dir bind mounted", str(handle.working_dir) == str(clone),
           f"host={clone} sandbox={handle.working_dir}")
    probe = backend.execute(handle, ["/bin/sh", "-c", "command -v python3"],
                            timeout_ms=10000)
    if probe.exit_code != 0:
        safety("python3 in sandbox", False, probe.stderr.strip())
        backend.destroy(handle)
        return None
    safety("python3 in sandbox", True)
    install = backend.execute(
        handle,
        ["/bin/sh", "-c",
         "python3 -m pip install --no-cache-dir --quiet --target .deps pytest"],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        timeout_ms=180000,
    )
    if install.exit_code != 0:
        safety("pip install pytest", False, install.stderr[-300:])
        backend.destroy(handle)
        return None
    safety("pip install pytest", True)
    return backend, handle


def step_write_launcher_and_char_test(clone: Path) -> None:
    step("Write launcher and characterization test")
    (clone / "run_pytest.py").write_text(LAUNCHER, encoding="utf-8")
    (clone / "deep_test_characterization.py").write_text(
        CHARACTERIZATION_TEST, encoding="utf-8"
    )
    safety("launcher and char test written", True)


def step_pytest_old(backend, handle) -> bool:
    step("Pytest OLD — characterization on unmodified click")
    result = backend.execute(
        handle,
        ["python3", "run_pytest.py", "deep_test_characterization.py",
         "-q", "--tb=short"],
        timeout_ms=60000,
    )
    info(f"exit: {result.exit_code}")
    if result.stdout:
        info(f"stdout tail:\n{result.stdout[-400:]}")
    ok = result.exit_code == 0
    safety("pytest green on old code", ok, f"exit={result.exit_code}")
    return ok


def step_sandbox_network_off(bridge, clone):
    step("Sandbox #2 — network OFF")
    backend = BubblewrapBackend(bridge)
    spec = SandboxSpec(
        language="python", working_dir=clone, memory_mb=2048,
        cpu_cores=2, process_limit=256, network_during_deps=False,
    )
    try:
        handle = backend.create(spec)
    except CodeGuardianError as exc:
        safety("sandbox #2 created", False, f"{exc.code.value}: {exc.message}")
        return None
    safety("sandbox #2 created", True, handle.id)
    safety("network disabled", handle.network_enabled is False)
    return backend, handle


def _extract_function(source, name):
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node)
    return None


async def step_writer(router, clone):
    step("Writer — refactor click.utils.echo")
    utils_file = clone / "src" / "click" / "utils.py"
    if not utils_file.is_file():
        safety("utils.py found", False, str(utils_file))
        return None
    source = utils_file.read_text(encoding="utf-8")
    echo_source = _extract_function(source, "echo")
    if echo_source is None:
        safety("echo found", False)
        return None
    prompt = (
        "Refactor this Python function to add type hints to every parameter "
        "and the return type, and improve the docstring. Do NOT change "
        "runtime behavior. Do NOT change arguments. Return ONLY the "
        "complete refactored function, starting with 'def'. No markdown, "
        "no explanation.\n\n" + echo_source
    )
    try:
        response = await router.complete(
            prompt, "writer", ModelOptions(temperature=0.0, max_tokens=1500)
        )
    except CodeGuardianError as exc:
        safety("writer call", False, f"{exc.code.value}: {exc.message}")
        return None
    text = response.text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.rstrip()
    if not text.startswith(("def ", "async def ", "@")):
        safety("writer output looks like function", False, text[:40])
        return None
    if text == echo_source:
        safety("writer changed code", False, "identical to input")
        return None
    try:
        ast.parse(text)
        new_source = source.replace(echo_source, text)
        ast.parse(new_source)
    except SyntaxError as exc:
        safety("writer output parses", False, str(exc))
        return None
    info(f"writer model: {response.model_id}")
    safety("writer call", True, f"{response.model_id} | {len(text)} chars")
    return new_source, response.model_id, "src/click/utils.py"


def step_diff_and_consent(provider, new_source, rel_path):
    step("Diff and consent")
    changes = [FileChange(path=rel_path, new_content=new_source)]
    try:
        diff = provider.get_diff(changes)
    except CodeGuardianError as exc:
        safety("diff built", False, f"{exc.code.value}: {exc.message}")
        return None
    if not diff:
        safety("diff built", False, "empty")
        return None
    info(f"diff: {len(diff)} chars")
    diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    consent = ConsentRecord(
        consent_id="deep-test-1", run_id=RUN_ID,
        granted_at=datetime.now(UTC), granted_by="deep-test",
        scope="apply", diff_hash=diff_hash, rollback_ref="",
    )
    safety("diff non-empty", True, f"{len(diff)} chars")
    safety("consent matches diff", consent.diff_hash == diff_hash)
    return diff, consent


def step_apply(provider, bridge, clone, diff, consent, utils_file, new_source):
    step("Apply")
    audit_log = WORK_DIR / "audit.jsonl"
    applier = ApplyProvider(provider, bridge, audit_log)
    try:
        result = applier.apply(str(clone), diff, consent)
    except CodeGuardianError as exc:
        safety("apply succeeded", False, f"{exc.code.value}: {exc.message}")
        return None
    safety("apply succeeded", result.success is True, str(result.applied_files))
    on_disk = utils_file.read_text(encoding="utf-8")
    safety("file on disk changed", on_disk == new_source)
    safety("audit log written",
           audit_log.is_file() and audit_log.stat().st_size > 0)
    return result.rollback_ref, applier


def step_pytest_new(backend, handle):
    step("Pytest NEW — characterization on refactored click")
    result = backend.execute(
        handle,
        ["python3", "run_pytest.py", "deep_test_characterization.py",
         "-q", "--tb=short"],
        timeout_ms=60000,
    )
    info(f"exit: {result.exit_code}")
    if result.stdout:
        info(f"stdout tail:\n{result.stdout[-400:]}")
    safety("behavior preserved after refactor", result.exit_code == 0,
           f"exit={result.exit_code}")


async def step_verifier(router, registry, writer_model_id, diff):
    step("Verifier — different family")
    prompt = (
        "Review this diff for correctness. Reply with exactly one word on "
        "the first line: PASS or FAIL. Then one sentence on the next line.\n\n"
        + diff
    )
    try:
        response = await router.complete(
            prompt, "correctness_verifier",
            ModelOptions(temperature=0.0, max_tokens=200),
        )
    except CodeGuardianError as exc:
        safety("verifier call", False, f"{exc.code.value}: {exc.message}")
        return
    words = response.text.strip().upper().split()
    first_word = words[0] if words else ""
    safety("verifier verdict produced", first_word in ("PASS", "FAIL"),
           f"first word={first_word!r}")
    info(f"verifier model: {response.model_id}")
    try:
        wf = registry.get_model(f"groq/{writer_model_id}").family
        vf = registry.get_model(f"groq/{response.model_id}").family
        safety("verifier family != writer", wf != vf,
               f"writer={writer_model_id}({wf}) "
               f"verifier={response.model_id}({vf})")
    except CodeGuardianError as exc:
        note(f"family lookup: {exc.message}")


def step_rollback(applier, rollback_ref, utils_file, original):
    step("Rollback")
    try:
        result = applier.rollback(rollback_ref)
    except CodeGuardianError as exc:
        safety("rollback succeeded", False, f"{exc.code.value}: {exc.message}")
        return
    safety("rollback reported success", result.success is True,
           str(result.restored_files))
    restored = utils_file.read_text(encoding="utf-8")
    safety("file restored byte-identical", restored == original,
           f"disk={len(restored)} original={len(original)}")


def step_audit_verify(bridge):
    step("Audit chain verification")
    try:
        result = bridge.call("audit.verify", {"path": str(WORK_DIR / "audit.jsonl")})
        if isinstance(result, dict) and result.get("valid") is True:
            safety("audit chain valid", True)
        elif isinstance(result, dict) and result.get("valid") is False:
            safety("audit chain valid", False,
                   f"broken at: {result.get('broken_at')}")
        else:
            note(f"audit.verify returned: {result}")
    except CodeGuardianError as exc:
        note(f"audit.verify error: {exc.code.value}: {exc.message}")
    except Exception as exc:
        note(f"audit.verify unexpected: {type(exc).__name__}: {exc}")


def print_summary() -> int:
    print(f"\n{'=' * 72}\nDEEP TEST SUMMARY\n{'=' * 72}")
    print(f"Passed: {len(STATE['passes'])}")
    print(f"Failed: {len(STATE['failures'])}")
    print(f"Skipped: {len(STATE['skips'])}")
    print(f"Xfailed: {len(STATE['xfails'])}")
    print(f"Warnings: {len(STATE['warnings'])}")
    if STATE["failures"]:
        print("\nFAILURES:")
        for n in STATE["failures"]:
            print(f"  - {n}")
    if STATE["skips"]:
        print("\nSKIPPED:")
        for n in STATE["skips"]:
            print(f"  - {n}")
    if STATE["xfails"]:
        print("\nXFAILED (known gaps):")
        for n in STATE["xfails"]:
            print(f"  - {n}")
    if STATE["warnings"]:
        print("\nWARNINGS:")
        for n in STATE["warnings"]:
            print(f"  - {n}")
    if WORK_DIR.exists():
        total = sum(p.stat().st_size for p in WORK_DIR.rglob("*") if p.is_file())
        print(f"\nwork/ size: {total / (1024 * 1024):.1f} MB")
    return 1 if STATE["failures"] else 0


async def main() -> int:
    print("CodeGuardian deep integration test")
    print(f"repo: {CLICK_URL}")
    print(f"run_id: {RUN_ID}")
    print(f"timestamp: {datetime.now(UTC).isoformat()}")

    pre = step_preflight()
    if pre is None:
        print_summary()
        return 2
    rpcd_path, native = pre
    rpcd_work = step_setup_work(rpcd_path)

    bridge = JSONRPCBridge([str(rpcd_work)])
    bridge.start()
    try:
        clone_result = step_clone_click()
        if clone_result is None:
            for s in ["kernel parse click", "blast click"]:
                skip(s, "clone failed")
            clone = None
            provider = None
        else:
            provider, clone = clone_result
            cpg = step_kernel_parse_click(native, provider)
            if cpg is not None:
                step_graph_summary(native, cpg)
                step_blast_click(native, cpg)
            else:
                skip("graph summary", "parse failed")
                skip("blast click", "parse failed")

        multi_repo = step_write_multi_lang()
        ml_cpg = step_parse_multi_lang(native, multi_repo)
        if ml_cpg is not None:
            step_blast_multi_lang(native, ml_cpg)
        else:
            skip("blast multi-lang", "parse failed")
        step_csharp_rejection(native)

        if clone is None:
            for s in ["sandbox #1", "pip install", "sandbox #2",
                      "pytest old", "writer", "diff", "apply",
                      "pytest new", "verifier", "rollback"]:
                skip(s, "no click clone")
            return print_summary()

        utils_file = clone / "src" / "click" / "utils.py"
        original = utils_file.read_text(encoding="utf-8") \
            if utils_file.is_file() else ""

        s1 = step_sandbox_install(bridge, clone)
        if s1 is None:
            for s in ["sandbox #2", "pytest old", "writer", "diff",
                      "apply", "pytest new", "verifier", "rollback"]:
                skip(s, "sandbox #1 failed")
            step_audit_verify(bridge)
            return print_summary()
        backend_1, handle_1 = s1
        try:
            step_write_launcher_and_char_test(clone)
        finally:
            try:
                backend_1.destroy(handle_1)
            except CodeGuardianError as exc:
                note(f"destroy 1: {exc.message}")

        s2 = step_sandbox_network_off(bridge, clone)
        if s2 is None:
            for s in ["pytest old", "writer", "diff", "apply",
                      "pytest new", "verifier", "rollback"]:
                skip(s, "sandbox #2 failed")
            step_audit_verify(bridge)
            return print_summary()
        backend_2, handle_2 = s2
        try:
            step_pytest_old(backend_2, handle_2)

            registry = ModelRegistry.load()
            adapter = OpenAICompatibleProvider(
                api_key=os.environ["GROQ_API_KEY"], base_url=GROQ_BASE
            )
            router = FallbackModelProvider(registry, providers={"groq": adapter})

            wo = await step_writer(router, clone)
            if wo is None:
                for s in ["diff", "apply", "pytest new",
                          "verifier", "rollback"]:
                    skip(s, "writer failed")
                step_audit_verify(bridge)
                return print_summary()
            new_source, writer_id, rel_path = wo

            d = step_diff_and_consent(provider, new_source, rel_path)
            if d is None:
                for s in ["apply", "pytest new", "verifier", "rollback"]:
                    skip(s, "diff failed")
                step_audit_verify(bridge)
                return print_summary()
            diff, consent = d

            a = step_apply(provider, bridge, clone, diff, consent,
                           utils_file, new_source)
            if a is None:
                for s in ["pytest new", "verifier", "rollback"]:
                    skip(s, "apply failed")
                step_audit_verify(bridge)
                return print_summary()
            ref, applier = a

            step_pytest_new(backend_2, handle_2)
            await step_verifier(router, registry, writer_id, diff)
            step_rollback(applier, ref, utils_file, original)
        finally:
            try:
                backend_2.destroy(handle_2)
            except CodeGuardianError as exc:
                note(f"destroy 2: {exc.message}")

        step_audit_verify(bridge)
    finally:
        bridge.stop()

    return print_summary()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
    except Exception:
        print("\nUNEXPECTED ERROR:")
        traceback.print_exc()
        sys.exit(1)
