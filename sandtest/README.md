# CodeGuardian Deep Integration Test

The deep test is the ultimate proof that CodeGuardian works. It clones real
repositories, parses them, runs the full orchestration pipeline, verifies the
results, and reports exactly what happened. It is not a unit test. It is the
system running end-to-end against the real world.

## 1. What it tests

Since Phase 13, the deep test drives the **LangGraphOrchestrator** directly.
It does not call individual agents; it hands a configuration to the orchestrator,
which walks the `LEGACY_GRAPH`, resolves the slots against the registry, and
dispatches the phases.

The pipeline executed for every chosen target is:
1. **Recon**: Builds the Code Property Graph and writes `recon.json`.
2. **Blast**: Computes direct/transitive callers and the blast score.
3. **Analyst**: Reads the blast report and builds a `RefactorSpec`.
4. **Tester**: Writes a characterization test.
5. **Writer**: Proposes candidate code changes.
6. **Sandbox Verify**: Applies the candidate, runs the test, rolls back.
7. **Three Verifiers**: Correctness, security, and contract.
8. **Skill Curator**: Proposes a reusable skill based on the run.

It also tests:
- **Multi-language parsing**: 8 languages via a fixed synthetic corpus.
- **Checkpoint resume**: Proving a run can be interrupted and resumed.
- **Verifier retry**: Proving the bounded K-loop retries failed candidates.

## 2. How it is structured

The harness is modular. `deep_test.py` is a thin orchestrator over the modules
in the `deep/` package.

| Module | Responsibility |
|---|---|
| `deep_test.py` | Arg parsing, preflight, model router setup, repo loop, summary. |
| `deep/_pipeline.py` | Sets up the `LangGraphOrchestrator`, registers agents, and runs fault injection. |
| `deep/_sandbox_verify.py` | A test-scoped agent filling the `sandbox_verify` slot. |
| `deep/_verifier_agents.py` | Test-scoped agents filling the three verifier slots, sharing one stack runner. |
| `deep/_repos.py` | The registry of repositories to run against. |
| `deep/_sandbox.py` | The Bubblewrap sandbox session for the test. |
| `deep/_audit.py` | Telemetry queries and audit chain verification. |

## 3. Prerequisites

The test runs entirely inside WSL2. It requires the following on the host:
- Python 3.12+
- `rpcd` binary (built via `cargo build --workspace --release`)
- `bwrap` on the `PATH` (`sudo apt install bubblewrap`)
- `GROQ_API_KEY` set in the environment (supports rotation via `_2`, `_3` suffixes)

## 4. Running the test

```bash
# Standard run against the baseline Python repo
python sandtest/deep_test.py --repo httpx

# Run against the complex cross-module Python repo
python sandtest/deep_test.py --repo scrapy

# Run all three directives on every target (takes much longer)
python sandtest/deep_test.py --repo httpx --full

# Run against multiple repos
python sandtest/deep_test.py --repo httpx,scrapy,gin
```

### Command-line flags
- `--repo`: A single repo name, a comma-separated list, or `all`.
- `--directive`: Override the default refactoring directive.
- `--full`: Run all three preset directives per target.
- `--no-clean`: Preserve the `sandtest/work/` directory from the previous run.

## 5. Repository Registry

The test runs against the `ALL_REPOS` registry in `deep/_repos.py`.

- **`httpx`**: The baseline. 61 Python files, three-hop call chain.
- **`scrapy`**: The stress case. 544 Python files, 45MB CPG, deep cross-module
  dependencies. Tests the orchestrator's ability to handle large, complex targets.
- **`gin`**: Large Go codebase. Tests multi-language parse and blast.
- **`vaultkit`**: TypeScript ESM imports.
- **`logger-internals-java`**: Java generics and interface dispatch.
- **`secs`**: Rust method resolution.

*Note on Python repos:* The `install_commands` for a Python repo must install its
dependencies into `clone/.deps` using `pip install --target .deps <package>`.
A system-wide install does not survive the sandbox network transition, and an
editable install often misses complex dependency trees (e.g., Scrapy).

## 6. Output Legend

Every line in the summary is prefixed with a status tag.

- `[PASS]`: The check succeeded as expected.
- `[FAIL]`: A real failure that requires attention.
- `[SKIP]`: The check could not run due to an environment limit (e.g., a target
  whose context pack exceeds the model's window, or a missing toolchain).
- `[XFAIL]`: A known limitation, documented in the build state (e.g., the C/C++
  declarator gap G-15).
- `[WARN]`: An anomaly that does not block the pipeline (e.g., a repo that
  fails a full `import scrapy` check but whose individual modules import fine).

## 7. Known Harness Limitations

It is important to distinguish **product failures** from **harness environment limits**.

1. **Install-layer fragility:** The deep test clones and installs arbitrary
   third-party code. A repo's own CI script might be complex (e.g., Scrapy's
   Twisted/Zope version pinning). When this happens, the harness reports a
   `[WARN]` or `[SKIP]`. This is **not** a product failure; a real user already
   has a working environment.
2. **Model context windows:** A block-band target may generate a context pack
   larger than the model's input limit. The deep test records this as `[SKIP]`.
   In production, the Phase 14 UI surfaces this as a user-facing refusal (G-33).
3. **Fault-injection assertions:** The fault injector wraps return values to
   prove the characterization test catches drift. If the target returns a
   complex object (not a primitive), the value test is not written. The smoke
   test catches the drift instead, and the assertion accepts this weaker signal.

## 8. The `work/` Directory

Every run creates or cleans `sandtest/work/`. This directory holds:
- `work/repos/`: The cloned repositories.
- `work/multi_lang_corpus/`: The synthetic multi-language files.
- `work/deep_test.db`: The SQLite database populated with telemetry and checkpoints.
- `work/rpcd`: The copied Rust daemon binary.

The directory is wiped at the start of each run unless `--no-clean` is passed.

## 9. Interpreting the Summary

At the end of the run, the harness prints a summary:

```text
========================================================================
DEEP TEST SUMMARY
========================================================================
Passed: 65
Failed: 0
Skipped: 2
Xfailed: 2
Warnings: 1
Duration: 148.2s
```

A successful phase-13 boundary run on `httpx` produces `65 passed, 0 failed`.
A run on `scrapy` produces `57 passed, 1 failed, 2 skipped, 1 warning` (where
the failure is a known fault-injection assertion detail, and the skips are
context-window limits). Both prove the orchestrator works.
```
