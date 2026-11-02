#!/usr/bin/env python3
"""Deep integration test for CodeGuardian, Phases 0 through 11.

Clones a real repository, parses it, discovers Python functions
structurally, runs each candidate twice inside a sandbox to classify
determinism, generates a characterization test from the recorded
output, then runs the full Phase 11 agent pipeline:

  Recon → Blast → Analyst → Tester → Writer(N candidates)
  → apply → three parallel verifiers → Skill Curator
  → rollback → fault injection → Skill Curator on failure
  → audit chain → telemetry audit.

The repository is selected by ``CODEGUARDIAN_DEEP_REPO`` (default
``psf/requests``). Number of writer candidates is selected by
``CODEGUARDIAN_DEEP_CANDIDATES`` (default ``3``).

Known limitations:

- Only free functions with primitives-only required arguments are
  candidates. Methods, async functions, and functions requiring
  synthesized objects are excluded.
- Non-deterministic targets get a smoke check only.
- ``pip install -e .`` uses ``--break-system-packages``.
- G-16: cross-file imported-function callers are under-reported; the
  blast agent shifts its recommendation toward caution when a search
  cannot be verified, and the deep test records that shift.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PYTHON_DIR = _ROOT / "python"
if _PYTHON_DIR.is_dir() and str(_PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(_PYTHON_DIR))

from codeguardian.core.agents import (
    AnalystAgent,
    BlastAgent,
    ContractVerifier,
    CorrectnessVerifier,
    ReconAgent,
    SecurityVerifier,
    SkillCuratorAgent,
    TesterAgent,
    WriterAgent,
    _store_context,
    fresh_barrier,
)
from codeguardian.core.agents.analyst import (
    AnalystInput,
    AnalystResult,
)
from codeguardian.core.agents.blast import BlastInput, BlastResult
from codeguardian.core.agents.contract_verifier import ContractVerifierInput
from codeguardian.core.agents.correctness_verifier import CorrectnessVerifierInput
from codeguardian.core.agents.recon import ReconInput, ReconResult
from codeguardian.core.agents.security_verifier import SecurityVerifierInput
from codeguardian.core.agents.skill_curator import SkillCuratorInput
from codeguardian.core.agents.tester import TesterInput, TesterResult
from codeguardian.core.agents.writer import WriterInput, WriterResult
from codeguardian.core.bridges.jsonrpc_bridge import JSONRPCBridge
from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins import (
    BudgetTracker,
    PluginRegistry,
    cpg_available,
)
from codeguardian.core.providers import (
    ApplyProvider,
    FallbackModelProvider,
    LocalProvider,
    ModelRegistry,
)
from codeguardian.core.providers.openai_compatible import OpenAICompatibleProvider
from codeguardian.core.sandbox.bubblewrap_backend import BubblewrapBackend
from codeguardian.core.storage import MigrationRunner, SQLiteStore
from codeguardian.interfaces.apply import ConsentRecord
from codeguardian.interfaces.repository import FileChange
from codeguardian.interfaces.sandbox import SandboxSpec

REPO_URL = os.environ.get("CODEGUARDIAN_DEEP_REPO", "https://github.com/psf/requests")
MAX_TARGETS = int(os.environ.get("CODEGUARDIAN_DEEP_DEPTH", "20"))
SAMPLE_SEED = int(os.environ.get("CODEGUARDIAN_DEEP_SEED", "42"))
N_CANDIDATES = int(os.environ.get("CODEGUARDIAN_DEEP_CANDIDATES", "3"))
DIRECTIVE = "add type hints to every parameter and the return type"
GROQ_BASE = "https://api.groq.com/openai/v1"
RUN_ID = "deep-test-run"
SANDBOX_DIR = Path(__file__).resolve().parent
WORK_DIR = SANDBOX_DIR / "work"
DB_PATH = WORK_DIR / "deep_test.db"

MULTI_LANG_FILES = [
    (
        "add.py",
        "python",
        "py_add",
        "call_py_add",
        "def py_add(a, b):\n    return a + b\n\n\ndef call_py_add():\n    return py_add(1, 2)\n",
    ),
    (
        "add.ts",
        "typescript",
        "ts_add",
        "call_ts_add",
        "function ts_add(a: number, b: number): number {\n    return a + b;\n}\n\n"
        "function call_ts_add(): number {\n    return ts_add(1, 2);\n}\n",
    ),
    (
        "add.js",
        "javascript",
        "js_add",
        "call_js_add",
        "function js_add(a, b) {\n    return a + b;\n}\n\n"
        "function call_js_add() {\n    return js_add(1, 2);\n}\n",
    ),
    (
        "Add.java",
        "java",
        "JavaAdd.add",
        "JavaAdd.call_add",
        "public class JavaAdd {\n    static int add(int a, int b) {\n        return a + b;\n    }\n\n"
        "    static int call_add() {\n        return add(1, 2);\n    }\n}\n",
    ),
    (
        "add.go",
        "go",
        "go_add",
        "call_go_add",
        "package main\n\nfunc go_add(a, b int) int {\n    return a + b\n}\n\n"
        "func call_go_add() int {\n    return go_add(1, 2)\n}\n",
    ),
    (
        "add.rs",
        "rust",
        "rust_add",
        "call_rust_add",
        "fn rust_add(a: i32, b: i32) -> i32 {\n    a + b\n}\n\n"
        "fn call_rust_add() -> i32 {\n    rust_add(1, 2)\n}\n",
    ),
    (
        "add.c",
        "c",
        "c_add",
        "call_c_add",
        "int c_add(int a, int b) {\n    return a + b;\n}\n\n"
        "int call_c_add() {\n    return c_add(1, 2);\n}\n",
    ),
    (
        "add.cpp",
        "cpp",
        "cpp_add",
        "call_cpp_add",
        "int cpp_add(int a, int b) {\n    return a + b;\n}\n\n"
        "int call_cpp_add() {\n    return cpp_add(1, 2);\n}\n",
    ),
]

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

GENERATOR_SCRIPT = r'''"""Auto-generated by CodeGuardian's deep test."""
import importlib
import json
import os
import sys
from pathlib import Path

_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _root)
if os.path.isdir(os.path.join(_root, "src")):
    sys.path.insert(0, os.path.join(_root, "src"))


def normalize(value):
    if value is None or isinstance(value, bool):
        return {"t": type(value).__name__, "v": value}
    if isinstance(value, int):
        return {"t": "int", "v": value}
    if isinstance(value, float):
        if value != value:
            return {"t": "float", "v": "nan"}
        if value == float("inf"):
            return {"t": "float", "v": "inf"}
        if value == float("-inf"):
            return {"t": "float", "v": "-inf"}
        return {"t": "float", "v": value}
    if isinstance(value, str):
        return {"t": "str", "v": value}
    if isinstance(value, bytes):
        return {
            "t": "bytes",
            "len": len(value),
            "sha256_16": __import__("hashlib").sha256(value).hexdigest()[:16],
        }
    if isinstance(value, dict):
        items = sorted(
            ((str(k), normalize(v)) for k, v in value.items()),
            key=lambda kv: kv[0],
        )
        return {"t": "dict", "items": items}
    if isinstance(value, (list, tuple)):
        return {"t": type(value).__name__, "items": [normalize(v) for v in value]}
    if isinstance(value, (set, frozenset)):
        items = sorted((normalize(v) for v in value), key=repr)
        return {"t": type(value).__name__, "items": items}
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict
        return {"t": type(value).__name__, "dataclass": normalize(asdict(value))}
    if hasattr(value, "__dict__"):
        public = {k: v for k, v in vars(value).items() if not k.startswith("_")}
        return {"t": type(value).__name__, "attrs": normalize(public)}
    return {"t": type(value).__name__, "repr": repr(value)[:200]}


def main():
    manifest = json.loads(Path("deep_test_manifest.json").read_text())
    results = []
    for item in manifest:
        record = {"module": item["module"], "function": item["function"], "args": item["args"]}
        try:
            module = importlib.import_module(item["module"])
            func = getattr(module, item["function"])
        except Exception as exc:
            record["error"] = f"import failed: {type(exc).__name__}: {exc}"
            results.append(record)
            continue
        try:
            first = func(*item["args"])
        except Exception as exc:
            record["error"] = f"call raised: {type(exc).__name__}: {exc}"
            results.append(record)
            continue
        try:
            second = func(*item["args"])
        except Exception as exc:
            record["error"] = f"second call raised: {type(exc).__name__}: {exc}"
            results.append(record)
            continue
        n1 = normalize(first)
        n2 = normalize(second)
        record["deterministic"] = (n1 == n2)
        record["expected_type"] = type(first).__name__
        record["expected_value_json"] = json.dumps(n1)
        results.append(record)
    Path("deep_test_targets.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
'''

STATE: dict[str, list[str]] = {
    "passes": [],
    "failures": [],
    "skips": [],
    "xfails": [],
    "warnings": [],
}


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
    candidate = Path.home() / ".cache" / "codeguardian-target" / "release" / "codeguardian-rpcd"
    return candidate if candidate.is_file() else None


def _native():
    try:
        import codeguardian._native as native

        return native
    except ImportError:
        return None

def _get_api_key(env_var: str) -> str | None:
    """Return the API key from the environment, or from keyring as fallback.

    Reads the environment variable first because an operator who set it
    expects it to win. Falls back to the keyring entry that Phase 1's
    secrets module uses, keyed by provider name. The provider name is
    derived from the environment variable by stripping the ``_API_KEY``
    suffix and lowering the remaining prefix: ``GROQ_API_KEY`` becomes
    ``"groq"``, ``ANTHROPIC_API_KEY`` becomes ``"anthropic"``. Returns
    None when neither source has a value, so the caller can decide
    whether to fail or skip.
    """
    value = os.environ.get(env_var)
    if value:
        return value
    if env_var.endswith("_API_KEY"):
        provider = env_var[: -len("_API_KEY")].lower()
    else:
        provider = env_var.lower()
    try:
        from codeguardian.core.config.secrets import get_api_key

        return get_api_key(provider)
    except Exception:
        return None

def _get_groq_keys() -> list[str]:
    """Return all available Groq API keys, in priority order.

    Reads GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3 from the
    environment, then falls back to keyring entries groq, groq_2,
    groq_3. Duplicates are removed: the same key twice wastes a
    rotation slot. Returns an empty list when no key is found in either
    source.
    """
    keys: list[str] = []
    seen: set[str] = set()
    for env_var, provider in (
        ("GROQ_API_KEY", "groq"),
        ("GROQ_API_KEY_2", "groq_2"),
        ("GROQ_API_KEY_3", "groq_3"),
    ):
        value = os.environ.get(env_var)
        if not value:
            try:
                from codeguardian.core.config.secrets import get_api_key

                value = get_api_key(provider)
            except Exception:
                value = None
        if value and value not in seen:
            keys.append(value)
            seen.add(value)
    return keys


class RotatingOpenAICompatibleProvider:
    """An OpenAICompatibleProvider that rotates API keys on rate limit.

    Tries each key in sequence when the active key hits
    MODEL_RATE_LIMITED. If every key fails, raises the last error so
    the external retry loop can wait for the window to reset. If only
    one key is provided, behaves identically to a plain
    OpenAICompatibleProvider.

    The active key index is shared across calls. When a call succeeds,
    the index stays where it is; the next call starts from the same
    key. When a key fails, the index advances. That way a healthy key
    keeps getting used and a failing key does not monopolise attempts.

    Rotation does not help when every key belongs to the same
    organisation on a provider that limits per organisation rather than
    per key. In that case the rotation exhausts all keys, raises, and
    the caller's retry loop waits. The wrapper is a no-op under those
    conditions; it does not make things worse.
    """

    def __init__(self, keys: list[str], base_url: str) -> None:
        if not keys:
            raise ValueError("at least one API key is required")
        self._providers = [
            OpenAICompatibleProvider(api_key=k, base_url=base_url) for k in keys
        ]
        self._current = 0
        self._lock = asyncio.Lock()

    @property
    def key_count(self) -> int:
        """The number of keys under rotation."""
        return len(self._providers)

    def _active(self) -> OpenAICompatibleProvider:
        """Return the currently active provider."""
        return self._providers[self._current]

    async def _advance(self) -> None:
        """Advance to the next key under the rotation lock."""
        async with self._lock:
            self._current = (self._current + 1) % len(self._providers)

    async def complete(self, prompt, model, options):
        """Try each key until one succeeds or all rate-limit."""
        from codeguardian.core.errors import CodeGuardianError, ErrorCode

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

    def stream(self, prompt, model, options):
        """Delegate streaming to the active key."""
        return self._active().stream(prompt, model, options)

    def count_tokens(self, text: str, model: str) -> int:
        """Delegate token counting to the active key."""
        return self._active().count_tokens(text, model)

    def get_available_models(self):
        """Delegate model discovery to the active key."""
        return self._active().get_available_models()
    def __getattr__(self, name: str):
        """Forward any attribute the wrapper does not define to the active provider.

        The wrapper delegates a small, known set of methods explicitly.
        Everything else — ``list_models``, ``_client``, ``_base_url``,
        future additions to the OpenAI provider's surface — falls through
        to the currently active provider. That keeps the wrapper a true
        drop-in replacement even when the wrapped class grows new methods
        that the registry's discovery path requires.

        Names that begin with an underscore are re-raised as AttributeError
        without delegation. This prevents a bootstrap loop if __init__
        fails partway: any attribute access on an incompletely
        constructed instance should fail cleanly rather than try to
        delegate through self._active() before _providers exists.
        """
        if name.startswith("_"):
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            )
        return getattr(self._active(), name)

_FORBIDDEN_CALLEE_NAMES = frozenset(
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
_FORBIDDEN_MODULE_PREFIXES = ("socket", "subprocess", "urllib", "http.client", "os.system")
_JSON_SAFE_ANNOTATIONS = {
    "str": "sample",
    "int": 0,
    "float": 0.0,
    "bool": False,
    "list": [],
    "dict": {},
    "None": None,
    "list[str]": [],
    "list[int]": [],
    "dict[str, str]": {},
    "dict[str, int]": {},
    "Optional[str]": None,
    "Optional[int]": None,
    "str | None": None,
    "int | None": None,
    "Union[str, None]": None,
    "Union[int, None]": None,
}


def _safe_to_call(func_node: ast.AST) -> bool:
    for sub in ast.walk(func_node):
        if isinstance(sub, ast.Call):
            target = sub.func
            name = ""
            if isinstance(target, ast.Name):
                name = target.id
            elif isinstance(target, ast.Attribute):
                name = target.attr
            if name in _FORBIDDEN_CALLEE_NAMES:
                return False
        if isinstance(sub, ast.Import):
            for alias in sub.names:
                if any(alias.name.startswith(p) for p in _FORBIDDEN_MODULE_PREFIXES):
                    return False
        if isinstance(sub, ast.ImportFrom) and sub.module:
            if any(sub.module.startswith(p) for p in _FORBIDDEN_MODULE_PREFIXES):
                return False
    return True


def _synthesize_args(func_node: ast.AST) -> tuple[list, str | None]:
    args = func_node.args
    if args.vararg or args.kwarg:
        return [], "has *args or **kwargs"
    positional = list(args.args)
    n_defaults = len(args.defaults)
    n_required = len(positional) - n_defaults
    if n_required < 0:
        return [], "malformed signature"
    synthesized: list = []
    for arg in positional[:n_required]:
        ann = arg.annotation
        if ann is None:
            return [], f"unannotated required arg {arg.arg!r}"
        text = ast.unparse(ann)
        if text not in _JSON_SAFE_ANNOTATIONS:
            return [], f"unsupported annotation {text!r} on {arg.arg!r}"
        synthesized.append(_JSON_SAFE_ANNOTATIONS[text])
    return synthesized, None


def _is_exempt_name(name: str) -> bool:
    if name.startswith("__") and name.endswith("__"):
        return True
    if name.startswith("_") and not name.startswith("__"):
        return True
    return bool(name.startswith("test_") or name.endswith("_test"))


def _module_name_for(path: Path, clone_root: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(clone_root.resolve())
    except ValueError:
        return None
    parts = list(rel.parts)
    if not parts:
        return None
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


def discover_python_targets(provider: LocalProvider, clone: Path) -> list[dict]:
    files = provider.list_files("**/*.py")
    targets: list[dict] = []
    for rel in files:
        if rel.startswith("tests/") or rel.startswith("test/"):
            continue
        module = _module_name_for(clone / rel, clone)
        if module is None:
            continue
        try:
            source = provider.read_file(rel)
        except CodeGuardianError:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(node, ast.AsyncFunctionDef):
                continue
            if _is_exempt_name(node.name):
                continue
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            length = end - start + 1
            if length < 5 or length > 50:
                continue
            if not _safe_to_call(node):
                continue
            synthesized, reason = _synthesize_args(node)
            if reason is not None:
                continue
            targets.append(
                {
                    "path": rel,
                    "module": module,
                    "function": node.name,
                    "qualified_name": node.name,
                    "line": start,
                    "length": length,
                    "args": synthesized,
                }
            )
    return targets


def rank_targets_by_fan_in(native, cpg: bytes, targets: list[dict]) -> list[dict]:
    ranked: list[dict] = []
    for target in targets:
        try:
            report = native.compute_blast(cpg, target["path"], target["qualified_name"])
        except Exception:
            continue
        if not isinstance(report, dict):
            continue
        direct = report.get("direct_callers") or []
        transitive = report.get("transitive_callers") or []
        if not direct and not transitive:
            continue
        cross_file = 0
        for caller in list(direct) + list(transitive):
            caller_path = caller.get("path") if isinstance(caller, dict) else None
            if caller_path and caller_path != target["path"]:
                cross_file += 1
                break
        ranked.append(
            {
                **target,
                "direct_count": len(direct),
                "transitive_count": len(transitive),
                "cross_file_caller": bool(cross_file),
                "blast_score": int(report.get("blast_score", 0)),
                "recommendation": str(report.get("recommendation", "review")),
            }
        )
    ranked.sort(key=lambda t: (t["direct_count"], t["transitive_count"]), reverse=True)
    return ranked


def blast_invariants(native, cpg: bytes, ranked: list[dict]) -> None:
    step("Blast radius — invariants on discovered targets")
    if not ranked:
        safety("blast discovered targets", False, "no eligible targets")
        return
    safety("blast ran on discovered targets", True, f"{len(ranked)} targets")
    cross = [t for t in ranked if t["cross_file_caller"]]
    if cross:
        safety(
            "blast found cross-file callers", True, f"{len(cross)} targets have cross-file callers"
        )
    else:
        xfail("blast found cross-file callers", "G-16: no cross-file callers resolved in this repo")
    for target in ranked[:MAX_TARGETS]:
        try:
            report = native.compute_blast(cpg, target["path"], target["qualified_name"])
        except Exception as exc:
            warn(f"blast {target['qualified_name']}", f"{type(exc).__name__}: {exc}")
            continue
        for caller in report.get("direct_callers") or []:
            if not isinstance(caller, dict):
                continue
            if caller.get("qualified_name") == target["qualified_name"]:
                safety(
                    f"no self-caller {target['qualified_name']}",
                    False,
                    "target reported as its own caller",
                )
                break
        score = report.get("blast_score", 0)
        if not (0 <= score <= 100):
            safety(f"blast_score in range {target['qualified_name']}", False, f"score={score}")
    safety("blast invariants checked", True, f"{min(len(ranked), MAX_TARGETS)} targets")


def pick_writer_target(ranked: list[dict], results: list[dict]) -> dict | None:
    """Return the target most suitable for a clean refactor.

    Skips any target whose blast report would cause the Writer to refuse.
    The Writer's preservation contract requires a refusal when the blast
    recommendation is "block" or when the blast score is high enough to
    signal that the function is too widely used to change safely. Picking
    such a target guarantees the pipeline stops at the Writer.

    Two passes: first preferring deterministic targets, then falling back
    to any callable target. A third pass is deliberately not attempted:
    when every safe target has an error or none is deterministic, this
    returns None and the caller records a skip reason rather than the
    Writer refusing on a doomed target.
    """
    by_key = {(r["module"], r["function"]): r for r in results}

    def _sort_key(t: dict) -> int:
        return abs(3 - t.get("direct_count", 0))

    def _is_safe(t: dict) -> bool:
        if t.get("recommendation") == "block":
            return False
        if t.get("blast_score", 0) >= 70:
            return False
        return True

    for target in sorted(ranked, key=_sort_key):
        if not _is_safe(target):
            continue
        record = by_key.get((target["module"], target["function"]))
        if record is None or record.get("error"):
            continue
        if not record.get("deterministic"):
            continue
        return {**target, "execution": record}

    for target in sorted(ranked, key=_sort_key):
        if not _is_safe(target):
            continue
        record = by_key.get((target["module"], target["function"]))
        if record is None or record.get("error"):
            continue
        return {**target, "execution": record}

    return None


def write_manifest(path: Path, targets: list[dict]) -> None:
    payload = [
        {"module": t["module"], "function": t["function"], "args": t["args"]} for t in targets
    ]
    path.write_text(json.dumps(payload, indent=2))


def write_generator(path: Path) -> None:
    path.write_text(GENERATOR_SCRIPT)


def write_launcher(path: Path) -> None:
    path.write_text(LAUNCHER)


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


_NORMALIZE_HEADER = """
import hashlib
import json
from dataclasses import asdict


def _normalize(value):
    if value is None or isinstance(value, bool):
        return {"t": type(value).__name__, "v": value}
    if isinstance(value, int):
        return {"t": "int", "v": value}
    if isinstance(value, float):
        if value != value:
            return {"t": "float", "v": "nan"}
        if value == float("inf"):
            return {"t": "float", "v": "inf"}
        if value == float("-inf"):
            return {"t": "float", "v": "-inf"}
        return {"t": "float", "v": value}
    if isinstance(value, str):
        return {"t": "str", "v": value}
    if isinstance(value, bytes):
        return {"t": "bytes", "len": len(value), "sha256_16": hashlib.sha256(value).hexdigest()[:16]}
    if isinstance(value, dict):
        items = sorted(((str(k), _normalize(v)) for k, v in value.items()), key=lambda kv: kv[0])
        return {"t": "dict", "items": items}
    if isinstance(value, (list, tuple)):
        return {"t": type(value).__name__, "items": [_normalize(v) for v in value]}
    if isinstance(value, (set, frozenset)):
        items = sorted((_normalize(v) for v in value), key=repr)
        return {"t": type(value).__name__, "items": items}
    if hasattr(value, "__dataclass_fields__"):
        return {"t": type(value).__name__, "dataclass": _normalize(asdict(value))}
    if hasattr(value, "__dict__"):
        public = {k: v for k, v in vars(value).items() if not k.startswith("_")}
        return {"t": type(value).__name__, "attrs": _normalize(public)}
    return {"t": type(value).__name__, "repr": repr(value)[:200]}
"""


def build_characterization(target: dict) -> str:
    record = target["execution"]
    deterministic = bool(record.get("deterministic"))
    module = target["module"]
    function = target["function"]
    args_repr = repr(target["args"])
    expected_type = record.get("expected_type", "")
    expected_value_json = record.get("expected_value_json", "null")
    safe_function = _slug(function)

    header = (
        f'"""Characterization test for {module}.{function}."""\n'
        "\n"
        "import os\n"
        "import sys\n"
        "_root = os.path.dirname(os.path.abspath(__file__))\n"
        "sys.path.insert(0, _root)\n"
        "if os.path.isdir(os.path.join(_root, 'src')):\n"
        "    sys.path.insert(0, os.path.join(_root, 'src'))\n" + _NORMALIZE_HEADER + "\n\n"
        f"_ARGS = {args_repr}\n"
        f"_EXPECTED_TYPE = {expected_type!r}\n"
        f"_EXPECTED_VALUE_JSON = {expected_value_json!r}\n"
        "\n\n"
        "def _call_target():\n"
        f"    from {module} import {function}\n"
        f"    return {function}(*_ARGS)\n"
        "\n\n"
        f"def test_smoke_{safe_function}():\n"
        "    result = _call_target()\n"
        '    assert result is not None, "returned None"\n'
        "    actual_type = type(result).__name__\n"
        "    assert actual_type == _EXPECTED_TYPE, (\n"
        '        f"return type drift: expected {_EXPECTED_TYPE}, got {actual_type}"\n'
        "    )\n"
    )
    if not deterministic:
        return header + "\n"
    value_test = (
        "\n\n"
        f"def test_value_{safe_function}():\n"
        "    result = _call_target()\n"
        "    actual = _normalize(result)\n"
        "    expected = json.loads(_EXPECTED_VALUE_JSON)\n"
        "    assert actual == expected, json.dumps(\n"
        '        {"actual": actual, "expected": expected}, indent=2, sort_keys=True\n'
        "    )\n"
    )
    return header + value_test


def write_characterization(clone: Path, target: dict) -> Path:
    content = build_characterization(target)
    path = clone / f"deep_test_characterization_{_slug(target['function'])}.py"
    path.write_text(content, encoding="utf-8")
    return path


def step_preflight():
    step("Preflight — check environment")
    info(f"repo: {REPO_URL}")
    rpcd = _rpcd_binary()
    info(f"rpcd binary: {rpcd or 'NOT FOUND'}")
    safety("rpcd binary exists", rpcd is not None)
    bwrap = shutil.which("bwrap")
    info(f"bwrap: {bwrap or 'NOT FOUND'}")
    safety("bwrap installed", bwrap is not None)
    keys = _get_groq_keys()
    if keys:
        for i, k in enumerate(keys, 1):
            info(f"GROQ_API_KEY[{i}]: {k[:8]}...{k[-4:]}")
        info(f"total keys available: {len(keys)}")
    else:
        info("GROQ_API_KEY: NOT SET (checked env and keyring)")
    safety("GROQ_API_KEY present", len(keys) > 0)
    native = _native()
    safety("_native extension importable", native is not None)
    if rpcd is None or bwrap is None or not keys or native is None:
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
    safety("work dir ready", True)
    return rpcd_dest


def step_open_store() -> SQLiteStore | None:
    step("Open telemetry store")
    try:
        store = SQLiteStore(DB_PATH)
        store.open()
        MigrationRunner(store).run()
    except CodeGuardianError as exc:
        safety("telemetry store open", False, f"{exc.code.value}: {exc.message}")
        return None
    info(f"db: {DB_PATH}")
    safety("telemetry store open", True)
    return store


def step_clone_repo():
    step(f"Clone {REPO_URL}")
    provider = LocalProvider()
    clone_dest = WORK_DIR / "clone"
    try:
        clone_path = provider.clone(REPO_URL, clone_dest)
    except CodeGuardianError as exc:
        warn("clone", f"{exc.code.value}: {exc.message}")
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


def step_kernel_parse_repo(native, provider):
    step("Kernel parse")
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
        safety("kernel parse", False, "no sources")
        return None
    try:
        cpg = native.build_graph(sources)
    except Exception as exc:
        safety("kernel parse", False, f"{type(exc).__name__}: {exc}")
        return None
    info(f"CPG: {len(cpg)} bytes")
    safety("kernel parse", True, f"{len(sources)} files, {len(cpg)} bytes")
    return bytes(cpg)


def step_graph_summary(native, cpg):
    step("Graph summary")
    try:
        summary = native.graph_summary(cpg)
    except Exception as exc:
        safety("graph summary", False, f"{type(exc).__name__}: {exc}")
        return
    info(f"summary: {summary}")
    if isinstance(summary, dict):
        safety(
            "graph has nodes",
            int(summary.get("node_count", 0)) > 0,
            f"{summary.get('node_count')} nodes",
        )
        safety(
            "graph has edges",
            int(summary.get("edge_count", 0)) > 0,
            f"{summary.get('edge_count')} edges",
        )


def step_discover(native, provider, clone, cpg):
    step("Discover candidate targets")
    discovered = discover_python_targets(provider, clone)
    info(f"functions considered: {len(discovered)}")
    safety("discovered functions", len(discovered) > 0, f"{len(discovered)}")
    ranked = rank_targets_by_fan_in(native, cpg, discovered)
    info(f"ranked (has callers): {len(ranked)}")
    safety("ranked targets", len(ranked) > 0, f"{len(ranked)}")
    if ranked:
        blast_invariants(native, cpg, ranked)
    return ranked


def step_write_manifest_and_generator(clone: Path, ranked: list[dict]) -> Path:
    step("Write generator and manifest")
    manifest_path = clone / "deep_test_manifest.json"
    generator_path = clone / "deep_test_generator.py"
    sample = ranked[: min(MAX_TARGETS, len(ranked))]
    rng = random.Random(SAMPLE_SEED)
    if len(ranked) > MAX_TARGETS:
        extras = rng.sample(ranked[MAX_TARGETS:], min(5, len(ranked) - MAX_TARGETS))
        sample = sample + extras
    write_manifest(manifest_path, sample)
    write_generator(generator_path)
    info(f"manifest entries: {len(sample)}")
    safety("generator and manifest written", True)
    return generator_path


def step_read_generator_results(clone: Path) -> list[dict]:
    step("Read generator results")
    path = clone / "deep_test_targets.json"
    if not path.is_file():
        safety("generator produced results", False, "deep_test_targets.json missing")
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        safety("generator produced results", False, f"bad JSON: {exc}")
        return []
    if not isinstance(data, list):
        safety("generator produced results", False, "not a list")
        return []
    ok = [r for r in data if not r.get("error")]
    deterministic = [r for r in ok if r.get("deterministic")]
    info(f"results: {len(data)}, ok: {len(ok)}, deterministic: {len(deterministic)}")
    safety("generator produced results", True, f"{len(ok)}/{len(data)} callable")
    safety("at least one deterministic target", len(deterministic) > 0, f"{len(deterministic)}")
    return data


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
                    xfail(f"blast {lang}:{target}", "known gap: C/C++ declarator not walked")
                else:
                    safety(f"blast {lang}:{target}", False, f"{type(exc).__name__}: {exc}")
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
            safety(
                f"blast {lang}:{target}",
                False,
                f"expected {expected_caller}, got {sorted(callers)}",
            )


def step_csharp_rejection(native):
    step("C# rejection guard")
    try:
        native.build_graph([("test.cs", "csharp", "class C {}\n")])
        safety("c# rejected", False, "accepted csharp")
    except ValueError as exc:
        safety("c# rejected", "unrecognized" in str(exc).lower(), f"ValueError: {exc}")
    except Exception as exc:
        safety("c# rejected", False, f"{type(exc).__name__}: {exc}")


def step_sandbox_install(bridge, clone):
    step("Sandbox #1 — network ON, pip install")
    backend = BubblewrapBackend(bridge)
    spec = SandboxSpec(
        language="python",
        working_dir=clone,
        memory_mb=2048,
        cpu_cores=2,
        process_limit=256,
        network_during_deps=True,
    )
    try:
        handle = backend.create(spec)
    except CodeGuardianError as exc:
        safety("sandbox #1 created", False, f"{exc.code.value}: {exc.message}")
        return None
    safety("sandbox #1 created", True, handle.id)
    safety(
        "working_dir bind mounted",
        str(handle.working_dir) == str(clone),
        f"host={clone} sandbox={handle.working_dir}",
    )
    probe = backend.execute(handle, ["/bin/sh", "-c", "command -v python3"], timeout_ms=10000)
    if probe.exit_code != 0:
        safety("python3 in sandbox", False, probe.stderr.strip())
        backend.destroy(handle)
        return None
    safety("python3 in sandbox", True)

    pytest_install = backend.execute(
        handle,
        ["/bin/sh", "-c", "python3 -m pip install --no-cache-dir --quiet --target .deps pytest"],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        timeout_ms=180000,
    )
    if pytest_install.exit_code != 0:
        safety("pip install pytest", False, pytest_install.stderr[-300:])
        backend.destroy(handle)
        return None
    safety("pip install pytest", True)

    repo_install = backend.execute(
        handle,
        [
            "/bin/sh",
            "-c",
            "python3 -m pip install --no-cache-dir --quiet --break-system-packages -e .",
        ],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        timeout_ms=300000,
    )
    if repo_install.exit_code != 0:
        warn("pip install -e .", f"exit={repo_install.exit_code}: {repo_install.stderr[-300:]}")
    else:
        safety("pip install -e .", True)

    backend.destroy(handle)
    return True


def step_sandbox_network_off(bridge, clone):
    step("Sandbox #2 — network OFF")
    backend = BubblewrapBackend(bridge)
    spec = SandboxSpec(
        language="python",
        working_dir=clone,
        memory_mb=2048,
        cpu_cores=2,
        process_limit=256,
        network_during_deps=False,
    )
    try:
        handle = backend.create(spec)
    except CodeGuardianError as exc:
        safety("sandbox #2 created", False, f"{exc.code.value}: {exc.message}")
        return None
    safety("sandbox #2 created", True, handle.id)
    safety("network disabled", handle.network_enabled is False)
    return backend, handle


def step_run_generator(backend, handle) -> bool:
    step("Run generator in sandbox")
    result = backend.execute(
        handle,
        ["/bin/sh", "-c", "PYTHONPATH=.deps:.:$PYTHONPATH python3 deep_test_generator.py"],
        timeout_ms=120000,
    )
    info(f"exit: {result.exit_code}")
    if result.stderr:
        info(f"stderr tail: {result.stderr[-300:]}")
    ok = result.exit_code == 0
    safety("generator ran in sandbox", ok, f"exit={result.exit_code}")
    return ok


def _run_char_test(backend, handle, filename: str, label: str) -> tuple[bool, str]:
    """Run one characterization test file in the sandbox."""
    result = backend.execute(
        handle,
        ["python3", "run_pytest.py", filename, "-v", "--tb=line"],
        timeout_ms=120000,
    )
    ok = result.exit_code == 0
    safety(label, ok, f"{filename} exit={result.exit_code}")
    if not ok:
        if result.stdout:
            info("pytest stdout (tail):")
            for line in result.stdout.splitlines()[-40:]:
                info(f"  {line}")
        if result.stderr:
            info("pytest stderr (tail):")
            for line in result.stderr.splitlines()[-20:]:
                info(f"  {line}")
    return ok, result.stdout or ""


def step_pytest_old_dual(
    backend, handle, tool_file: str, tester_file: str | None
) -> tuple[bool, bool]:
    """Run both characterization tests on the old (unmodified) code."""
    step("Pytest OLD — tool test and tester agent test")
    tool_ok, _ = _run_char_test(
        backend, handle, tool_file, "tool value-based test green on old code"
    )
    tester_ok = False
    if tester_file:
        tester_ok, _ = _run_char_test(
            backend, handle, tester_file, "tester agent test green on old code"
        )
    return tool_ok, tester_ok


def step_pytest_new_dual(
    backend, handle, tool_file: str, tester_file: str | None
) -> tuple[bool, str, bool, str]:
    """Run both characterization tests on the new (refactored) code."""
    step("Pytest NEW — tool test and tester agent test")
    tool_ok, tool_stdout = _run_char_test(
        backend, handle, tool_file, "tool value-based test green on new code"
    )
    tester_ok = False
    tester_stdout = ""
    if tester_file:
        tester_ok, tester_stdout = _run_char_test(
            backend, handle, tester_file, "tester agent test green on new code"
        )
    return tool_ok, tool_stdout, tester_ok, tester_stdout


def classify_characterization(stdout: str, deterministic: bool) -> str:
    smoke_fail = False
    value_fail = False
    for line in stdout.splitlines():
        if "FAILED" not in line:
            continue
        if "::test_smoke_" in line:
            smoke_fail = True
        elif "::test_value_" in line:
            value_fail = True
    if not smoke_fail and not value_fail:
        return "pass"
    if smoke_fail and deterministic:
        return "type-drift"
    if value_fail and not smoke_fail and deterministic:
        return "semantic-drift"
    return "hard-failure"


def _extract_function_source(source: str, name: str) -> str | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return None


def _splice_candidate(file_source: str, function_name: str, candidate_source: str) -> str | None:
    """Replace the target function in ``file_source`` with ``candidate_source``."""
    original = _extract_function_source(file_source, function_name)
    if original is None:
        return None
    new_source = file_source.replace(original, candidate_source, 1)
    try:
        ast.parse(new_source)
    except SyntaxError:
        return None
    return new_source


def step_diff_and_consent(provider, new_source, rel_path, *, consent_id="deep-test-1"):
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
        consent_id=consent_id,
        run_id=RUN_ID,
        granted_at=datetime.now(UTC),
        granted_by="deep-test",
        scope="apply",
        diff_hash=diff_hash,
        rollback_ref="",
    )
    safety("diff non-empty", True, f"{len(diff)} chars")
    safety("consent matches diff", consent.diff_hash == diff_hash)
    return diff, consent


def step_apply(provider, bridge, clone, diff, consent, target_file, new_source):
    step("Apply")
    audit_log = WORK_DIR / "audit.jsonl"
    applier = ApplyProvider(provider, bridge, audit_log)
    try:
        result = applier.apply(str(clone), diff, consent)
    except CodeGuardianError as exc:
        safety("apply succeeded", False, f"{exc.code.value}: {exc.message}")
        return None
    safety("apply succeeded", result.success is True, str(result.applied_files))
    on_disk = target_file.read_text(encoding="utf-8")
    safety("file on disk changed", on_disk == new_source)
    safety("audit log written", audit_log.is_file() and audit_log.stat().st_size > 0)
    return result.rollback_ref, applier


_CG_INJECT_HELPER = '''
def _cg_inject(value):
    """Deliberately alter the return value for fault injection."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    if isinstance(value, str):
        return value + "__cg_injected__"
    if isinstance(value, dict):
        out = dict(value)
        out["__cg_injected__"] = True
        return out
    if isinstance(value, (list, tuple)):
        out = list(value)
        out.append("__cg_injected__")
        return type(value)(out)
    return "__cg_injected__"
'''


def _insert_helper(source: str, helper: str) -> str:
    insert_line = 0
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source + "\n" + helper + "\n"
    lines = source.splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        insert_line = 1
    for node in tree.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node is tree.body[0]
        ):
            insert_line = max(insert_line, node.end_lineno or 0)
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            insert_line = max(insert_line, node.end_lineno or 0)
    prefix = "".join(lines[:insert_line])
    suffix = "".join(lines[insert_line:])
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + "\n" + helper + "\n" + suffix


def _inject_behavioral_drift(source: str, function_name: str) -> str | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    target: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            target = node
            break
    if target is None:
        return None

    wrapped = False
    for stmt in ast.walk(target):
        if isinstance(stmt, ast.Return) and stmt.value is not None:
            stmt.value = ast.Call(
                func=ast.Name(id="_cg_inject", ctx=ast.Load()),
                args=[stmt.value],
                keywords=[],
            )
            wrapped = True
    if not wrapped:
        return None

    new_func_source = ast.unparse(target)
    original_source = ast.get_source_segment(source, target)
    if original_source is None:
        return None

    modified = source.replace(original_source, new_func_source, 1)
    return _insert_helper(modified, _CG_INJECT_HELPER)


def step_rollback(applier, rollback_ref, target_file, original):
    step("Rollback")
    try:
        result = applier.rollback(rollback_ref)
    except CodeGuardianError as exc:
        safety("rollback succeeded", False, f"{exc.code.value}: {exc.message}")
        return
    safety("rollback reported success", result.success is True, str(result.restored_files))
    restored = target_file.read_text(encoding="utf-8")
    safety(
        "file restored byte-identical",
        restored == original,
        f"disk={len(restored)} original={len(original)}",
    )


def step_audit_verify(bridge, expected_min=0):
    step("Audit chain verification")
    try:
        result = bridge.call("audit.verify", {"path": str(WORK_DIR / "audit.jsonl")})
    except CodeGuardianError as exc:
        note(f"audit.verify error: {exc.code.value}: {exc.message}")
        return
    except Exception as exc:
        note(f"audit.verify unexpected: {type(exc).__name__}: {exc}")
        return
    if not isinstance(result, dict):
        note(f"audit.verify returned unexpected: {result}")
        return
    checked = int(result.get("entries_checked", 0))
    head = str(result.get("head_hash", ""))
    if checked >= expected_min:
        safety(
            "audit chain has expected entries",
            True,
            f"{checked} entries (>= {expected_min}), head={head[:16]}...",
        )
    else:
        safety(
            "audit chain has expected entries", False, f"only {checked} entries (< {expected_min})"
        )
    broken = result.get("broken_at")
    if broken is not None:
        safety("audit chain not broken", False, f"broken at: {broken}")
    else:
        safety("audit chain not broken", True)


# ============================================================
# Phase 11 agent steps
# ============================================================


import time as _time_module

_LAST_LLM_CALL_END: list[float] = [0.0]


async def _wait_for_rate_limit_window(seconds: float = 90.0) -> None:
    """Ensure at least `seconds` have passed since the last LLM step ended.

    The _LAST_LLM_CALL_END timestamp is set by _wrapped_llm_step after
    each step completes, not here. This function only waits; it does
    not advance the timestamp. That way the pause measures the gap
    between the end of one LLM call and the start of the next, which
    is what the sliding rate-limit window actually counts.
    """
    elapsed = _time_module.time() - _LAST_LLM_CALL_END[0]
    if elapsed < seconds:
        wait = seconds - elapsed
        if wait > 1:
            note(f"rate-limit pause: {wait:.0f}s")
        await asyncio.sleep(wait)


async def _wrapped_llm_step(coro_factory):
    """Run an LLM-using step and record when its LLM call ended.

    The timestamp is recorded even when the step raises, so a failure
    inside a step still counts toward the rate-limit window. The
    wrapped call is also retried on MODEL_RATE_LIMITED with the
    provider's Retry-After hint, matching the tester's pattern.
    """
    try:
        return await _retry_on_rate_limit(coro_factory)
    finally:
        _LAST_LLM_CALL_END[0] = _time_module.time()


_RETRY_AFTER_PATTERN = re.compile(r"Try again in (\d+(?:\.\d+)?)s")


def _extract_retry_after(message: str) -> float | None:
    """Parse the 'Try again in X.XXs' hint from a provider error message.

    Returns the seconds as a float, or None when the message does not
    contain the hint. Groq's free tier includes this hint on every
    MODEL_RATE_LIMITED response.
    """
    match = _RETRY_AFTER_PATTERN.search(message)
    return float(match.group(1)) if match else None


async def _retry_on_rate_limit(
    coro_factory: "Callable[[], Awaitable[Any]]",
    *,
    max_retries: int = 2,
    safety_buffer_s: float = 5.0,
) -> Any:
    """Call an async factory, retrying on MODEL_RATE_LIMITED with Retry-After.

    Retries only on MODEL_RATE_LIMITED. Any other CodeGuardianError is
    re-raised unchanged. Uses the provider's Retry-After hint from the
    error message when present, otherwise falls back to a 60-second wait
    plus the safety buffer.
    """
    from codeguardian.core.errors import CodeGuardianError, ErrorCode

    last_error: CodeGuardianError | None = None
    for _ in range(max_retries):
        try:
            return await coro_factory()
        except CodeGuardianError as exc:
            if exc.code is not ErrorCode.MODEL_RATE_LIMITED:
                raise
            last_error = exc
            wait = _extract_retry_after(exc.message) or 60.0
            wait += safety_buffer_s
            note(f"rate-limited; waiting {wait:.0f}s before retry")
            await asyncio.sleep(wait)
    if last_error is not None:
        raise last_error
    raise RuntimeError("rate-limit retry loop exited without an error")


def _compact_blast_artifact(blast: BlastResult) -> str:
    """Return a compact JSON summary of a BlastResult for prompts.

    Keeps direct callers (up to 10) and contract violations; summarizes
    transitive and coverage-gap counts. Cuts 3000-4000 tokens per
    prompt while preserving what the analyst and tester need to make
    decisions. Full data remains available via the call-graph-slice
    tool when the model asks for it.
    """
    art = blast.to_artifact()
    direct = list(art.get("direct_callers", []))[:10]
    violations = list(art.get("contract_violations", []))
    summary = {
        "target": art.get("target"),
        "change_kind": art.get("change_kind"),
        "direct_callers": direct,
        "contract_violations": violations,
        "blast_score": art.get("blast_score"),
        "recommendation": art.get("recommendation"),
        "transitive_count": len(art.get("transitive_callers", [])),
        "coverage_gap_count": len(art.get("coverage_gaps", [])),
        "note": "top-10 direct callers shown; use call-graph-slice for full data",
    }
    return json.dumps(summary, indent=2)


def _trim_blast_for_prompt(blast: BlastResult) -> BlastResult:
    """Return a BlastResult with truncated transitive/coverage lists.

    Keeps the artifact small enough that the analyst and tester prompts
    fit inside Groq free-tier ITPM limits when used back-to-back. Only
    affects what the model sees; the deep test's own safety checks run
    against the untrimmed result.
    """
    from dataclasses import replace

    return replace(
        blast,
        transitive_callers=blast.transitive_callers[:10],
        coverage_gaps=blast.coverage_gaps[:10],
        ranked_callers=blast.ranked_callers[:10],
    )


def _agent_budget() -> BudgetTracker:
    """A generous budget shared by every agent inside one deep-test run."""
    return BudgetTracker(max_calls=50, max_output_bytes=5_000_000)


async def step_recon_agent(
    clone: Path, cpg_output_dir: Path, provider: LocalProvider
) -> ReconResult | None:
    """Run the recon agent against the real clone."""
    step("Phase 11 — Recon agent")
    try:
        metadata = provider.get_metadata()
        commit_hash = metadata.head_commit
    except CodeGuardianError:
        commit_hash = "unknown"
    try:
        result = await ReconAgent().run(
            ReconInput(
                repo_url=REPO_URL,
                provider="github",
                commit_hash=commit_hash,
                clone_root=clone,
                cpg_output_dir=cpg_output_dir,
            )
        )
    except CodeGuardianError as exc:
        safety("recon agent completed", False, f"{exc.code.value}: {exc.message}")
        return None
    except Exception as exc:
        safety("recon agent completed", False, f"{type(exc).__name__}: {exc}")
        return None

    safety("recon agent completed", True, f"{len(result.cpg_bytes)} bytes CPG")
    artifact = result.artifact
    required = {
        "schema_version",
        "repo",
        "languages",
        "structural",
        "dependency_graph",
        "hierarchical_index",
        "entrypoints",
        "versions",
        "smoke_test",
        "truth_report",
        "drift_report",
    }
    missing = required - set(artifact.keys())
    safety(
        "recon artifact has all required fields",
        not missing,
        f"missing: {sorted(missing)}" if missing else "11/11",
    )
    statuses = artifact.get("extensions", {}).get("report_statuses", {})
    safety(
        "recon report_statuses complete",
        len(statuses) >= 8,
        f"{len(statuses)} statuses",
    )
    languages = artifact.get("languages", [])
    safety(
        "recon detected languages",
        len(languages) >= 1,
        f"{len(languages)} languages",
    )
    dep_nodes = artifact.get("dependency_graph", {}).get("nodes", [])
    dep_edges = artifact.get("dependency_graph", {}).get("edges", [])
    safety(
        "recon dependency graph has nodes and edges",
        len(dep_nodes) > 0 and len(dep_edges) > 0,
        f"{len(dep_nodes)} nodes, {len(dep_edges)} edges",
    )
    return result


async def step_blast_agent(recon: ReconResult, target: dict, clone: Path) -> BlastResult | None:
    """Run the blast agent on the picked target."""
    step(f"Phase 11 — Blast agent on {target['module']}.{target['function']}")
    try:
        result = await BlastAgent().run(
            BlastInput(
                cpg_bytes=recon.cpg_bytes,
                target_path=target["path"],
                target_qualified_name=target["qualified_name"],
                clone_root=clone,
                change_kind="semantic",
            )
        )
    except CodeGuardianError as exc:
        safety("blast agent completed", False, f"{exc.code.value}: {exc.message}")
        return None
    except Exception as exc:
        safety("blast agent completed", False, f"{type(exc).__name__}: {exc}")
        return None

    safety(
        "blast agent completed",
        True,
        f"score={result.blast_score}, "
        f"raw={result.recommendation_raw}, "
        f"adjusted={result.recommendation_adjusted}",
    )
    safety(
        "blast ranked_callers populated",
        len(result.ranked_callers) > 0,
        f"{len(result.ranked_callers)} ranked",
    )
    safety(
        "blast caller_search_complete flag set",
        isinstance(result.caller_search_complete, bool),
        f"complete={result.caller_search_complete}",
    )
    if not result.caller_search_complete:
        safety(
            "blast calibration fired",
            result.calibration_reason is not None,
            result.calibration_reason or "no reason",
        )
    return result


async def step_analyst_agent(
    blast: BlastResult,
    recon: ReconResult,
    clone: Path,
    router: FallbackModelProvider,
) -> AnalystResult | None:
    await _wait_for_rate_limit_window()
    """Run the analyst agent to produce a RefactorSpec and context pack."""
    step("Phase 11 — Analyst agent")
    registry = PluginRegistry.discover_bundled()
    try:
        result = await AnalystAgent().run(
            AnalystInput(
                blast=blast,
                recon=recon,
                directive=DIRECTIVE,
                registry=registry,
                provider=router,
                model="analyst",
                budget=_agent_budget(),
                clone_root=clone,
            )
        )
    except CodeGuardianError as exc:
        if exc.code is ErrorCode.MODEL_RATE_LIMITED:
            raise
        safety("analyst agent completed", False, f"{exc.code.value}: {exc.message}")
        return None
    except Exception as exc:
        safety("analyst agent completed", False, f"{type(exc).__name__}: {exc}")
        return None

    safety(
        "analyst agent completed", not result.is_refusal, "refused" if result.is_refusal else "ok"
    )
    if result.is_refusal:
        return result

    safety(
        "analyst spec has changes",
        len(result.spec.changes) > 0,
        f"{len(result.spec.changes)} changes",
    )
    kinds = [c.kind for c in result.spec.changes]
    safety(
        "analyst spec includes apply_directive",
        "apply_directive" in kinds,
        f"kinds={kinds}",
    )
    safety(
        "analyst context_pack_artifact present",
        result.context_pack_artifact is not None,
        "yes" if result.context_pack_artifact else "no",
    )
    if result.context_pack_artifact is not None:
        files = result.context_pack_artifact.get("files", [])
        safety(
            "analyst context pack has files",
            len(files) > 0,
            f"{len(files)} files",
        )
        spec_in_ext = result.context_pack_artifact.get("extensions", {}).get("refactor_spec")
        safety(
            "analyst refactor_spec carried in extensions",
            spec_in_ext is not None,
            "present" if spec_in_ext else "missing",
        )
    return result


async def step_tester_agent(
    analyst: AnalystResult,
    blast: BlastResult,
    clone: Path,
    router: FallbackModelProvider,
) -> TesterResult | None:
    await _wait_for_rate_limit_window()
    """Run the tester agent to produce a characterization test file."""
    step("Phase 11 — Tester agent")
    registry = PluginRegistry.discover_bundled()
    try:
        result = await TesterAgent().run(
            TesterInput(
                spec=analyst.spec,
                blast=blast,
                registry=registry,
                provider=router,
                model="tester",
                budget=_agent_budget(),
                clone_root=clone,
                target_path=blast.target_path,
                target_symbol=blast.target_qualified_name,
            )
        )
    except CodeGuardianError as exc:
        if exc.code is ErrorCode.MODEL_RATE_LIMITED:
            raise
        safety("tester agent completed", False, f"{exc.code.value}: {exc.message}")
        return None
    except Exception as exc:
        safety("tester agent completed", False, f"{type(exc).__name__}: {exc}")
        return None

    safety(
        "tester agent completed", not result.is_refusal, "refused" if result.is_refusal else "ok"
    )
    if result.is_refusal:
        return result
    safety(
        "tester output parses and has assertions",
        result.is_valid,
        "valid" if result.is_valid else (result.failure_reason or "invalid"),
    )
    safety(
        "tester attempted within budget",
        result.attempts <= 2,
        f"{result.attempts} attempts",
    )
    return result


async def step_writer_agent(
    analyst: AnalystResult,
    blast: BlastResult,
    skeleton: str,
    char_test: str,
    clone: Path,
    router: FallbackModelProvider,
) -> WriterResult | None:
    await _wait_for_rate_limit_window()
    """Run the writer agent to generate N ranked candidates."""
    step(f"Phase 11 — Writer agent (N={N_CANDIDATES})")
    registry = PluginRegistry.discover_bundled()
    try:
        result = await WriterAgent().run(
            WriterInput(
                spec=analyst.spec,
                blast=blast,
                skeleton=skeleton,
                characterization_test=char_test,
                registry=registry,
                provider=router,
                model="writer",
                budget=BudgetTracker(max_calls=10, max_output_bytes=5_000_000),
                clone_root=clone,
                target_path=blast.target_path,
                target_symbol=blast.target_qualified_name,
                n_candidates=N_CANDIDATES,
            )
        )
    except CodeGuardianError as exc:
        if exc.code is ErrorCode.MODEL_RATE_LIMITED:
            raise
        safety("writer agent completed", False, f"{exc.code.value}: {exc.message}")
        return None
    except Exception as exc:
        safety("writer agent completed", False, f"{type(exc).__name__}: {exc}")
        return None

    safety(
        "writer produced candidates",
        not result.is_refusal and len(result.candidates) > 0,
        (
            f"{len(result.candidates)} survivors of {result.total_attempted} attempts"
            + (
                f" | {result.refusal_reason[:300]}"
                if result.is_refusal and result.refusal_reason
                else ""
            )
        ),
    )
    if not result.candidates:
        return result
    best = result.candidates[0]
    safety(
        "writer best candidate carries compliance score",
        isinstance(best.compliance_score, float),
        f"score={best.compliance_score}, diff_size={best.diff_size}",
    )
    safety(
        "writer candidates ranked by compliance desc",
        all(
            result.candidates[i].compliance_score >= result.candidates[i + 1].compliance_score
            for i in range(len(result.candidates) - 1)
        ),
        f"{len(result.candidates)} candidates sorted",
    )
    return result


async def step_three_verifiers(
    diff: str,
    blast: BlastResult,
    tool_test_result: str,
    clone: Path,
    router: FallbackModelProvider,
) -> tuple[object | None, object | None, object | None]:
    await _wait_for_rate_limit_window()
    """Run the three verifiers in parallel."""
    step("Phase 11 — Three verifiers (parallel)")
    registry = PluginRegistry.discover_bundled()
    del registry  # verifiers do not need plugins
    budget = _agent_budget()

    correctness_input = CorrectnessVerifierInput(
        diff=diff,
        change_kind="semantic",
        blast_score=blast.blast_score,
        test_result=tool_test_result,
        provider=router,
        model="correctness_verifier",
        budget=budget,
        clone_root=clone,
        target_symbol=blast.target_qualified_name,
    )
    security_input = SecurityVerifierInput(
        diff=diff,
        change_kind="semantic",
        blast_score=blast.blast_score,
        original_security_signals=(),
        provider=router,
        model="security_verifier",
        budget=budget,
        clone_root=clone,
        target_symbol=blast.target_qualified_name,
    )
    contract_input = ContractVerifierInput(
        diff=diff,
        change_kind="semantic",
        blast_score=blast.blast_score,
        contract_assertions=blast.contract_assertions,
        provider=router,
        model="contract_verifier",
        budget=budget,
        clone_root=clone,
        target_symbol=blast.target_qualified_name,
    )

    try:
        corr, sec, con = await asyncio.gather(
            CorrectnessVerifier().run(correctness_input),
            SecurityVerifier().run(security_input),
            ContractVerifier().run(contract_input),
        )
    except CodeGuardianError as exc:
        if exc.code is ErrorCode.MODEL_RATE_LIMITED:
            raise
        safety("three verifiers completed", False, f"{exc.code.value}: {exc.message}")
        return None, None, None
    except Exception as exc:
        safety("three verifiers completed", False, f"{type(exc).__name__}: {exc}")
        return None, None, None

    safety("three verifiers completed", True, "all three returned")

    for label, result in (
        ("correctness", corr),
        ("security", sec),
        ("contract", con),
    ):
        safety(
            f"verifier {label} produced verdict",
            result.verdict.verdict in ("pass", "fail", "uncertain", "refused"),
            f"verdict={result.verdict.verdict}",
        )
        if result.verdict.verdict == "fail":
            safety(
                f"verifier {label} verdict_reason populated on fail",
                result.verdict_reason is not None,
                str(result.verdict_reason),
            )
        if result.pre_scan_conflict:
            safety(
                f"verifier {label} pre_scan_conflict surfaced",
                True,
                "pass verdict contradicted by pre-scan",
            )
    return corr, sec, con


async def step_skill_curator(
    *,
    outcome: str,
    diff: str,
    verifier_summary: str,
    test_result_summary: str,
    failure_reason: str,
    blast: BlastResult,
    clone: Path,
    router: FallbackModelProvider,
) -> object | None:
    await _wait_for_rate_limit_window()
    """Run the skill curator on a completed refactor (success or failure)."""
    step(f"Phase 11 — Skill Curator ({outcome})")
    try:
        result = await SkillCuratorAgent().run(
            SkillCuratorInput(
                outcome=outcome,  # type: ignore[arg-type]
                directive=DIRECTIVE,
                target_symbol=blast.target_qualified_name,
                target_path=blast.target_path,
                diff=diff,
                verifier_verdicts=verifier_summary,
                test_results=test_result_summary,
                failure_reason=failure_reason,
                provider=router,
                model="skill_curator",
                budget=_agent_budget(),
                clone_root=clone,
            )
        )
    except CodeGuardianError as exc:
        if exc.code is ErrorCode.MODEL_RATE_LIMITED:
            raise
        safety(f"skill curator ({outcome}) completed", False, f"{exc.code.value}: {exc.message}")
        return None
    except Exception as exc:
        safety(f"skill curator ({outcome}) completed", False, f"{type(exc).__name__}: {exc}")
        return None

    safety(
        f"skill curator ({outcome}) completed",
        not result.is_refusal,
        "refused" if result.is_refusal else "ok",
    )
    if not result.has_proposal:
        note(f"skill curator ({outcome}) proposed no skill: {result.warnings}")
        return result
    safety(
        f"skill curator ({outcome}) polarity",
        result.polarity == ("positive" if outcome == "success" else "negative"),
        f"polarity={result.polarity}",
    )
    safety(
        f"skill curator ({outcome}) proposal in draft state",
        result.state == "draft",
        f"state={result.state}",
    )
    return result


def step_telemetry_check(store: SQLiteStore) -> None:
    """Audit the agent_calls table for Phase 11 evidence."""
    step("Phase 11 — Telemetry audit")
    try:
        rows = [
            dict(r)
            for r in store.query(
                "SELECT role, outcome, verdict_reason, failure_category, "
                "prompt_role, prompt_version, prompt_hash, output_hash "
                "FROM agent_calls ORDER BY created_at"
            )
        ]
    except CodeGuardianError as exc:
        safety("telemetry queryable", False, f"{exc.code.value}: {exc.message}")
        return
    safety(
        "telemetry rows written",
        len(rows) >= 9,
        f"{len(rows)} rows",
    )
    roles = {r["role"] for r in rows}
    expected_roles = {
        "recon",
        "blast",
        "analyst",
        "tester",
        "writer",
        "correctness_verifier",
        "security_verifier",
        "contract_verifier",
        "skill_curator",
    }
    missing_roles = expected_roles - roles
    safety(
        "telemetry covers all nine roles",
        not missing_roles,
        f"missing: {sorted(missing_roles)}" if missing_roles else "9/9",
    )
    provenance = [r for r in rows if r["prompt_hash"] and r["output_hash"]]
    safety(
        "telemetry has provenance hashes on LLM rows",
        len(provenance) >= 7,
        f"{len(provenance)} rows with prompt+output hashes",
    )
    fail_rows = [r for r in rows if r["verdict_reason"] is not None]
    if fail_rows:
        safety(
            "telemetry verdict_reason populated on verifier fails",
            True,
            f"{len(fail_rows)} rows",
        )
    else:
        info("no verifier fail verdicts this run; verdict_reason NULL throughout")
    draft_skill_rows = [
        r for r in rows if r["role"] == "skill_curator" and r["outcome"] == "success"
    ]
    safety(
        "telemetry has skill_curator rows",
        len(draft_skill_rows) > 0,
        f"{len(draft_skill_rows)} rows",
    )


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
    print(f"repo: {REPO_URL}")
    print(f"run_id: {RUN_ID}")
    print(f"max_targets: {MAX_TARGETS}")
    print(f"seed: {SAMPLE_SEED}")
    print(f"n_candidates: {N_CANDIDATES}")
    print(f"timestamp: {datetime.now(UTC).isoformat()}")

    pre = step_preflight()
    if pre is None:
        print_summary()
        return 2
    rpcd_path, native = pre
    rpcd_work = step_setup_work(rpcd_path)

    store = step_open_store()
    if store is None:
        print_summary()
        return 2

    bridge = JSONRPCBridge([str(rpcd_work)])
    bridge.start()
    try:
        clone_result = step_clone_repo()
        if clone_result is None:
            for s in [
                "kernel parse",
                "graph summary",
                "discovered functions",
                "ranked targets",
                "blast ran on discovered targets",
                "generator ran in sandbox",
                "recon agent completed",
                "blast agent completed",
                "analyst agent completed",
                "tester agent completed",
                "writer agent completed",
                "three verifiers completed",
                "skill curator (success) completed",
                "apply",
                "characterization on new code",
                "rollback",
                "telemetry rows written",
            ]:
                skip(s, "clone failed")
            step_audit_verify(bridge)
            return print_summary()
        provider, clone = clone_result

        cpg = step_kernel_parse_repo(native, provider)
        if cpg is None:
            for s in [
                "graph summary",
                "discovered functions",
                "ranked targets",
                "recon agent completed",
                "blast agent completed",
                "analyst agent completed",
                "tester agent completed",
                "writer agent completed",
                "three verifiers completed",
                "skill curator (success) completed",
                "apply",
                "characterization on new code",
                "rollback",
                "telemetry rows written",
            ]:
                skip(s, "parse failed")
            return print_summary()
        step_graph_summary(native, cpg)

        ranked = step_discover(native, provider, clone, cpg)
        if not ranked:
            for s in [
                "generator ran in sandbox",
                "recon agent completed",
                "blast agent completed",
                "analyst agent completed",
                "tester agent completed",
                "writer agent completed",
                "three verifiers completed",
                "skill curator (success) completed",
                "apply",
                "characterization on new code",
                "rollback",
                "telemetry rows written",
            ]:
                skip(s, "no ranked targets")
        else:
            step_write_manifest_and_generator(clone, ranked)

        multi_repo = step_write_multi_lang()
        ml_cpg = step_parse_multi_lang(native, multi_repo)
        if ml_cpg is not None:
            step_blast_multi_lang(native, ml_cpg)
        else:
            skip("blast multi-lang", "parse failed")
        step_csharp_rejection(native)

        if not ranked:
            step_audit_verify(bridge)
            return print_summary()

        install_ok = step_sandbox_install(bridge, clone)
        if not install_ok:
            for s in [
                "generator ran in sandbox",
                "recon agent completed",
                "blast agent completed",
                "analyst agent completed",
                "tester agent completed",
                "writer agent completed",
                "three verifiers completed",
                "skill curator (success) completed",
                "apply",
                "characterization on new code",
                "rollback",
                "telemetry rows written",
            ]:
                skip(s, "sandbox install failed")
            step_audit_verify(bridge)
            return print_summary()

        s2 = step_sandbox_network_off(bridge, clone)
        if s2 is None:
            for s in [
                "generator ran in sandbox",
                "recon agent completed",
                "blast agent completed",
                "analyst agent completed",
                "tester agent completed",
                "writer agent completed",
                "three verifiers completed",
                "skill curator (success) completed",
                "apply",
                "characterization on new code",
                "rollback",
                "telemetry rows written",
            ]:
                skip(s, "sandbox #2 failed")
            step_audit_verify(bridge)
            return print_summary()
        backend, handle = s2

        # Set up the model router now (used by every LLM agent)
        registry = ModelRegistry.load()
        groq_keys = _get_groq_keys()
        if not groq_keys:
            raise RuntimeError(
                "GROQ_API_KEY not found in env or keyring; cannot continue"
            )
        if len(groq_keys) == 1:
            adapter = OpenAICompatibleProvider(
                api_key=groq_keys[0], base_url=GROQ_BASE
            )
        else:
            adapter = RotatingOpenAICompatibleProvider(
                keys=groq_keys, base_url=GROQ_BASE
            )
        info(f"groq adapter: {len(groq_keys)} key(s) available")
        router = FallbackModelProvider(registry, providers={"groq": adapter})

        try:
            write_launcher(clone / "run_pytest.py")
            gen_ok = step_run_generator(backend, handle)
            results: list[dict] = []
            if gen_ok:
                results = step_read_generator_results(clone)
            if not results:
                for s in [
                    "characterization green on old code",
                    "recon agent completed",
                    "blast agent completed",
                    "analyst agent completed",
                    "tester agent completed",
                    "writer agent completed",
                    "three verifiers completed",
                    "skill curator (success) completed",
                    "apply",
                    "characterization on new code",
                    "rollback",
                    "telemetry rows written",
                ]:
                    skip(s, "generator produced no results")
                step_audit_verify(bridge)
                return print_summary()

            target = pick_writer_target(ranked, results)
            if target is None:
                for s in [
                    "characterization green on old code",
                    "recon agent completed",
                    "blast agent completed",
                    "analyst agent completed",
                    "tester agent completed",
                    "writer agent completed",
                    "three verifiers completed",
                    "skill curator (success) completed",
                    "apply",
                    "characterization on new code",
                    "rollback",
                    "telemetry rows written",
                ]:
                    skip(s, "no callable target with recorded output")
                step_audit_verify(bridge)
                return print_summary()

            info(
                f"picked target: {target['module']}.{target['function']} "
                f"(direct={target['direct_count']}, "
                f"transitive={target['transitive_count']}, "
                f"deterministic={target['execution'].get('deterministic')})"
            )

            # Tool's value-based characterization test
            tool_char_path = write_characterization(clone, target)
            tool_char_filename = tool_char_path.name
            safety("tool characterization test generated", True, tool_char_filename)

            target_file = clone / target["path"]
            original_source = target_file.read_text(encoding="utf-8")

            # ------ Phase 11 agent pipeline ------
            with _store_context.active_store(store):
                # Recon
                recon_result = await step_recon_agent(clone, WORK_DIR / "cpg-out", provider)
                if recon_result is None:
                    for s in [
                        "blast agent completed",
                        "analyst agent completed",
                        "tester agent completed",
                        "writer agent completed",
                        "three verifiers completed",
                        "skill curator (success) completed",
                        "apply",
                        "characterization on new code",
                        "rollback",
                        "telemetry rows written",
                    ]:
                        skip(s, "recon agent failed")
                    step_audit_verify(bridge)
                    return print_summary()

                # Blast agent on picked target
                blast_result = await step_blast_agent(recon_result, target, clone)
                if blast_result is None:
                    for s in [
                        "analyst agent completed",
                        "tester agent completed",
                        "writer agent completed",
                        "three verifiers completed",
                        "skill curator (success) completed",
                        "apply",
                        "characterization on new code",
                        "rollback",
                        "telemetry rows written",
                    ]:
                        skip(s, "blast agent failed")
                    step_audit_verify(bridge)
                    return print_summary()

                # Analyst
                blast_result = _trim_blast_for_prompt(blast_result)
                analyst_result = await _wrapped_llm_step(
                    lambda: step_analyst_agent(blast_result, recon_result, clone, router)
                )
                if analyst_result is None or analyst_result.is_refusal:
                    for s in [
                        "tester agent completed",
                        "writer agent completed",
                        "three verifiers completed",
                        "skill curator (success) completed",
                        "apply",
                        "characterization on new code",
                        "rollback",
                        "telemetry rows written",
                    ]:
                        skip(s, "analyst agent failed or refused")
                    step_audit_verify(bridge)
                    return print_summary()

                # Tester (needs cpg_available for its context-provider tools)
                cpg_handle = native.PyCPG(recon_result.cpg_bytes)
                with cpg_available(cpg_handle, clone):
                    tester_result = await _wrapped_llm_step(
                        lambda: step_tester_agent(
                            analyst_result, blast_result, clone, router
                        )
                    )

                # Write the tester's test to disk (or fall back)
                tester_char_filename: str | None = None
                if (
                    tester_result is not None
                    and tester_result.is_valid
                    and not tester_result.is_refusal
                ):
                    tester_char_path = clone / "deep_test_tester_char.py"
                    tester_char_path.write_text(tester_result.test_file, encoding="utf-8")
                    tester_char_filename = tester_char_path.name
                    safety("tester characterization test written", True, tester_char_filename)

                # Run OLD tests: both must pass
                tool_old_ok, tester_old_ok = step_pytest_old_dual(
                    backend, handle, tool_char_filename, tester_char_filename
                )
                if not tool_old_ok:
                    for s in [
                        "writer agent completed",
                        "three verifiers completed",
                        "skill curator (success) completed",
                        "apply",
                        "characterization on new code",
                        "rollback",
                        "telemetry rows written",
                    ]:
                        skip(s, "tool test failed on old code")
                    step_audit_verify(bridge)
                    return print_summary()

                # Writer (needs cpg_available for tools)
                with (
                    cpg_available(cpg_handle, clone),
                    fresh_barrier(
                        PluginRegistry.discover_bundled(),
                        "code-skeleton",
                        mode="plan",
                        budget=_agent_budget(),
                        allowed_tools=("provide",),
                    ) as barrier,
                ):
                    skeleton_text = await barrier.call_async("provide", {"path": target["path"]})

                writer_char_input = (
                    tester_result.test_file
                    if tester_result is not None and tester_result.is_valid and tester_old_ok
                    else tool_char_path.read_text(encoding="utf-8")
                )

                with cpg_available(cpg_handle, clone):
                    writer_result = await _wrapped_llm_step(
                        lambda: step_writer_agent(
                            analyst_result,
                            blast_result,
                            skeleton_text,
                            writer_char_input,
                            clone,
                            router,
                        )
                    )

                if writer_result is None or not writer_result.candidates:
                    for s in [
                        "three verifiers completed",
                        "skill curator (success) completed",
                        "apply",
                        "characterization on new code",
                        "rollback",
                        "telemetry rows written",
                    ]:
                        skip(s, "writer agent failed")
                    step_audit_verify(bridge)
                    return print_summary()

                # Splice the best candidate into the file
                best = writer_result.candidates[0]
                new_source = _splice_candidate(original_source, target["function"], best.source)
                safety(
                    "writer candidate spliced into file",
                    new_source is not None,
                    "ok" if new_source else "splice failed",
                )
                if new_source is None:
                    for s in [
                        "three verifiers completed",
                        "skill curator (success) completed",
                        "apply",
                        "characterization on new code",
                        "rollback",
                        "telemetry rows written",
                    ]:
                        skip(s, "splice failed")
                    step_audit_verify(bridge)
                    return print_summary()

                # Diff + consent + apply
                diff_result = step_diff_and_consent(provider, new_source, target["path"])
                if diff_result is None:
                    for s in [
                        "three verifiers completed",
                        "skill curator (success) completed",
                        "apply",
                        "characterization on new code",
                        "rollback",
                        "telemetry rows written",
                    ]:
                        skip(s, "diff failed")
                    step_audit_verify(bridge)
                    return print_summary()
                diff, consent = diff_result

                apply_result = step_apply(
                    provider,
                    bridge,
                    clone,
                    diff,
                    consent,
                    target_file,
                    new_source,
                )
                if apply_result is None:
                    for s in [
                        "three verifiers completed",
                        "skill curator (success) completed",
                        "characterization on new code",
                        "rollback",
                        "telemetry rows written",
                    ]:
                        skip(s, "apply failed")
                    step_audit_verify(bridge)
                    return print_summary()
                rollback_ref, applier = apply_result

                # Run NEW tests: both must pass
                (
                    tool_new_ok,
                    tool_new_stdout,
                    tester_new_ok,
                    _tester_new_stdout,
                ) = step_pytest_new_dual(backend, handle, tool_char_filename, tester_char_filename)
                label = classify_characterization(
                    tool_new_stdout,
                    bool(target["execution"].get("deterministic")),
                )
                safety(
                    "tool test reports behavior preserved",
                    label == "pass",
                    label,
                )
                if tester_char_filename is not None:
                    safety(
                        "tester agent test still green on refactored code",
                        tester_new_ok,
                        "pass" if tester_new_ok else "fail",
                    )

                # Three verifiers in parallel
                tool_result_str = "PASS" if tool_new_ok else f"FAIL: {label}"
                corr, sec, con = await _wrapped_llm_step(
                    lambda: step_three_verifiers(
                        diff, blast_result, tool_result_str, clone, router
                    )
                )

                # Skill curator on the successful refactor
                verdict_summary = ""
                if corr is not None:
                    verdict_summary += f"correctness: {corr.verdict.verdict}\n"
                if sec is not None:
                    verdict_summary += f"security: {sec.verdict.verdict}\n"
                if con is not None:
                    verdict_summary += f"contract: {con.verdict.verdict}\n"
                test_summary = (
                    f"tool test: {'PASS' if tool_new_ok else 'FAIL'}\n"
                    f"tester test: "
                    f"{'PASS' if tester_new_ok else 'FAIL or N/A'}\n"
                )
                await _wrapped_llm_step(
                    lambda: step_skill_curator(
                        outcome="success",
                        diff=diff,
                        verifier_summary=verdict_summary,
                        test_result_summary=test_summary,
                        failure_reason="",
                        blast=blast_result,
                        clone=clone,
                        router=router,
                    )
                )

                # Rollback
                step_rollback(applier, rollback_ref, target_file, original_source)

                # Fault injection (existing mechanics)
                await step_fault_injection(
                    router,
                    clone,
                    target,
                    tool_char_filename,
                    backend,
                    handle,
                    provider,
                    bridge,
                )

                # Skill curator on the failure (if fault injection detected)
                # Snapshot the fault-injection diff if any
                # The fault-injection rollback has already restored
                # the file; we only re-run the curator on the recorded
                # failure reason.

                # Note: the fault injection section above may have
                # written a diff to the audit log; the failure reason
                # is static here. The curator still proposes a negative
                # skill from the structural signal.
                fault_diff = diff  # use the successful refactor diff
                # as a stand-in, since the injected diff was rolled back
                await _wrapped_llm_step(
                    lambda: step_skill_curator(
                        outcome="failure",
                        diff=fault_diff,
                        verifier_summary="prior refactor rejected by value check",
                        test_result_summary="tool test FAIL after injection",
                        failure_reason=(
                            "behavioral drift injected; tool value check caught the difference"
                        ),
                        blast=blast_result,
                        clone=clone,
                        router=router,
                    )
                )

            # Audit and telemetry
            expected = 4 if target["execution"].get("deterministic") else 2
            step_audit_verify(bridge, expected_min=expected)
            step_telemetry_check(store)
        finally:
            try:
                backend.destroy(handle)
            except CodeGuardianError as exc:
                note(f"destroy 2: {exc.message}")
    finally:
        bridge.stop()
        store.close()

    return print_summary()


async def step_fault_injection(
    router, clone, target, char_filename, backend, handle, provider, bridge
):
    step("Fault injection — verify tool catches a bad refactor")
    if not target["execution"].get("deterministic"):
        skip("fault injection", "picked target is non-deterministic; value check cannot fire")
        return
    target_file = clone / target["path"]
    current_source = target_file.read_text(encoding="utf-8")

    broken_source = _inject_behavioral_drift(current_source, target["function"])
    if broken_source is None:
        skip(
            "fault injection",
            f"cannot inject drift into {target['function']} (no return with value)",
        )
        return
    safety("fault injection drift injected", True, "ast behavioral drift")

    diff_result = step_diff_and_consent(
        provider, broken_source, target["path"], consent_id="deep-test-fault"
    )
    if diff_result is None:
        safety("fault injection diff", False, "could not build diff")
        return
    diff, consent = diff_result

    apply_result = step_apply(provider, bridge, clone, diff, consent, target_file, broken_source)
    if apply_result is None:
        safety("fault injection apply", False, "apply failed")
        return
    rollback_ref, applier = apply_result

    try:
        _ok, new_stdout = _run_char_test(
            backend,
            handle,
            char_filename,
            "tool value check on fault-injected code ran",
        )
        label = classify_characterization(new_stdout, True)
        if label == "pass":
            safety(
                "fault injection detected by value check",
                False,
                "drift applied but pytest passed; moat does not catch this",
            )
        else:
            safety("fault injection detected by value check", True, f"label={label}")
    finally:
        try:
            applier.rollback(rollback_ref)
            safety("fault injection rollback", True)
        except CodeGuardianError as exc:
            safety("fault injection rollback", False, f"{exc.code.value}: {exc.message}")


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
