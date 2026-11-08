# Modularity Audit — Phases 0 through 9

**Snapshot date:** 2026-10-20
**Scope:** Python and Rust code written through Phase 9
**Purpose:** An honest assessment of what is modular, what is not, and what must change before v2 (greenfield + project upgrade).

This document is not aspirational. Every claim is grounded in a specific file, a specific function, or a specific gap logged in `BUILD_STATE.md`. Where the answer is "modular in theory, narrow in practice," it says so. Where the answer is "not modular, but the cost is small," it quantifies the cost.

---

## 1. Executive Summary

| Dimension | Verdict |
|---|---|
| **Interface layer (Phase 4)** | Genuinely modular. 12 Protocols, no concrete imports across boundaries, dynamic re-export with `TYPE_CHECKING` mirror. |
| **Plugin layer (Phase 9)** | Genuinely modular for the seven extensible categories. Registry, discovery, fault barrier, permission enforcement all correct. |
| **Kernel layer (Phase 6)** | Modular in the language dimension via `LanguageModule` trait and runtime grammar loading. Not modular in the enum dimension — the `Language` enum requires an edit per built-in language. |
| **Provider layer (Phase 8)** | Modular at the interface. Not modular at the adapter selection — `_ADAPTER_CLASSES` is a hardcoded dict. |
| **Storage layer (Phase 3)** | Modular — five separate stores. Migration runner versioned. |
| **Pipeline (Phase 13, not yet built)** | Will be built refactor-shaped unless explicitly designed otherwise. This is the single largest v2 risk. |
| **Agents (Phase 11, not yet built)** | Will be built refactor-shaped unless explicitly designed otherwise. This is the second-largest v2 risk. |
| **Narrow by design** | Blast contract detection, coverage gaps, blast score weights, sandbox backends, SQLite concurrency. All documented. None hidden. |
| **Hidden narrowness** | G-16 partial resolution. The blast radius claim is weaker than advertised. This is the single most important pre-v2 item. |
| **Total extension points requiring Rule 1 edit** | 12 specific places. All small. None fatal. All documented in this audit. |

**Overall honest assessment:** the substrate is right. The interfaces survive future change. Twelve specific implementation files will require edits when adding new dimensions, and none of those edits is a rewrite. The pipeline and agents — the two largest remaining phases — are the risk.

---

## 2. Methodology

This audit assessed every Python and Rust file written through Phase 9. For each module, four questions were asked:

1. **Does adding a new instance of the thing this module abstracts require editing this file?**
2. **Does changing a dependency of this module require editing files that import it?**
3. **Is the dispatch mechanism data-driven or code-driven?**
4. **Does the module have a clear public surface?**

An answer of "yes, code-driven" was recorded as non-modular regardless of whether the edit is small. A small edit is still a Rule 1 escalation, and Rule 1 escalations across phases are the tax that compounds.

Files reviewed in full:

- Python: `core/errors/`, `core/config/`, `core/storage/`, `core/bridges/`, `core/providers/`, `core/sandbox/`, `core/plugins/`, `plugins_bundled/`, `interfaces/`
- Rust: `codeguardian-kernel/`, `codeguardian-blast/`, `codeguardian-audit/`, `codeguardian-policy/`, `codeguardian-sandbox/`, `codeguardian-rpc/`, `codeguardian-rpcd/`, `codeguardian-pyo3/`

Files read from BUILD_STATE and phase summaries where the code itself was not present in this session. Those are marked "(per BUILD_STATE)".

---

## 3. Modular by Design

### 3.1 The interface layer — 12 Protocols

The single strongest piece of architecture in the project. Every cross-component boundary is a `typing.Protocol`. No core module imports a concrete provider, sandbox, plugin, or reporter.

| Protocol | File | Purpose |
|---|---|---|
| `RepositoryProvider` | `interfaces/repository.py` | Clone, read, list, diff, metadata |
| `ApplyProvider` | `interfaces/apply.py` | Consent-gated apply and rollback |
| `ModelProvider` | `interfaces/model.py` | Complete, stream, count, list models |
| `SandboxBackend` | `interfaces/sandbox.py` | Create, execute, destroy |
| `Orchestrator` | `interfaces/orchestrator.py` | Run, resume, pause, get_state |
| `Agent[InputT, OutputT]` | `interfaces/agent.py` | Generic agent run |
| `ToolPlugin` | `interfaces/tool.py` | MCP-style tool call |
| `VerifierPlugin` | `interfaces/verifier.py` | Verdict from a diff and context |
| `ReporterPlugin` | `interfaces/reporter.py` | Render a run as a string |
| `LanguagePlugin` | `interfaces/language.py` | Detect, parse, generate_test, run_tests |
| `MCPClient` | `interfaces/mcp_client.py` | Discover tools, call tools, list servers |
| `MCPServer` | `interfaces/mcp_server.py` | Start, stop, list tools |

**Why this is modular:**

- **No inheritance.** A new provider implements the shape structurally. It does not subclass a base class, does not import the Protocol, does not register anywhere.
- **Runtime checkable only where required.** Four Protocols (`ToolPlugin`, `VerifierPlugin`, `ReporterPlugin`, `LanguagePlugin`) are `@runtime_checkable`. The rest are checked by mypy only, which keeps runtime overhead at zero for the hot paths.
- **`Agent` is generic.** `Agent[InputT, OutputT]` means every agent can have its own input and output type. A new agent type does not force a shared bag of arguments.
- **`interfaces/__init__.py` is discovered dynamically.** Adding a new Protocol file requires no edit to `__init__.py`. The `pkgutil.iter_modules` loop re-exports every symbol from every module automatically.
- **`TYPE_CHECKING` mirror.** The same names are declared statically in a `if TYPE_CHECKING:` block so mypy sees them. This is a Phase 4 deviation (deviation #21 in BUILD_STATE) but it is the correct one.

**Adding a new Protocol:** one new file in `interfaces/`. Zero edits elsewhere.

**Adding a new implementation of an existing Protocol:** one new file. Zero edits to `interfaces/`.

### 3.2 The plugin layer — seven extensible categories

| Extension point | How it is added | Files touched |
|---|---|---|
| **Language** | New directory under `plugins_bundled/languages/` with `plugin.py` | 0 |
| **Language (runtime grammar)** | New `.so` file + `_native.load_grammar` call | 0 |
| **Tool** | New directory under `plugins_bundled/tools/` with `plugin.py` | 0 |
| **Reporter** | New directory under `plugins_bundled/reporters/` with `plugin.py` | 0 |
| **Verifier** | New directory under `plugins_bundled/verifiers/` with `plugin.py` (Phase 11) | 0 |
| **Provider (via entry point)** | New Python package with `codeguardian.provider` entry point | 0 |
| **Third-party plugin** | `pip install` a package with the correct entry point group | 0 |

**Why this is modular:**

- **`PluginSource` protocol.** Two implementations exist today (`BundledSource`, `EntryPointSource`) and both produce identical `PluginSpec` objects. Adding a third source (e.g. `LocalDirectorySource` for development) is one new class. Zero registry changes.
- **`BundledSource` walks directories.** The `_TYPE_DIRS` table maps a directory name to a plugin type. Adding a plugin is adding a directory. No code edit.
- **`EntryPointSource` uses `importlib.metadata`.** Third-party plugins register under entry point groups (`codeguardian.language`, `codeguardian.tool`, etc.). Discovery is metadata-driven. No code edit.
- **Fault barrier wraps every call.** Sync and async paths both isolate exceptions and enforce timeouts. A new plugin cannot crash the run.
- **Audit hook enforces permissions.** A plugin that declares `network: false` cannot open a socket (pure-Python plugins only — see §5.4). A new plugin cannot exceed its manifest.

**Adding a new language:** create `python/codeguardian/plugins_bundled/languages/<name>/plugin.py` with a `LanguagePlugin`-shaped class and a `__manifest__` dict. Reload. The registry finds it. Zero core edits.

**Adding a new tool:** same shape under `tools/`. Zero core edits.

### 3.3 The kernel layer — language modules and runtime grammar loading

The kernel's language dimension is modular in a specific and deliberate way.

**Two paths to a new language exist today.**

**Path A — Runtime grammar loading.** A third-party plugin ships `libtree_sitter_<name>.so` (or `.dylib` / `.dll`) and calls `_native.load_grammar(name, path)`. The kernel loads the grammar, wraps it in a `RuntimeLanguageModule`, registers it in a global registry, and makes it available via `langs::dispatch_by_name`. No Rust rebuild. No Python code edit. Handled by `grammar_loader.rs`.

**Path B — Compiled-in language.** A new file in `rust/crates/codeguardian-kernel/src/langs/` implementing `LanguageModule`, plus edits to `langs/mod.rs` (`Language` enum, `dispatch`, `dispatch_by_name`), plus a rebuild. Used when the language is bundled.

**Why path A makes the kernel modular:** eight languages are compiled in. Every additional language can use path A. The eight-language ceiling is not architectural — it is a convenience choice for the most common cases.

**Why path B is not modular:** adding a ninth compiled-in language requires:

1. New `languages/<name>.rs` module
2. New `Language::<Name>` variant in `langs/mod.rs`
3. New `dispatch` arm in `langs/mod.rs`
4. New `dispatch_by_name` arm in `langs/mod.rs`
5. New grammar dependency in `Cargo.toml`
6. Rebuild

That is a six-step Rule 1 escalation per compiled-in language. The runtime path avoids it entirely.

### 3.4 The provider layer — interface modularity, adapter non-modularity

The provider layer has a split personality.

**Modular: the interface.**

`ModelProvider` (in `interfaces/model.py`) is the contract. Any provider that implements `complete`, `stream`, `count_tokens`, and `get_available_models` is a valid provider. The `FallbackModelProvider` in `core/providers/fallback_router.py` takes any provider instance and routes to it. There is no import of `AnthropicProvider` from the router, only of `ModelProvider`.

**Modular: model discovery.**

`ModelRegistry.discover(provider)` calls the provider's `list_models()` method. Whatever the provider's `/models` endpoint returns, that becomes the eligible set. Adding a new model (e.g. `gpt-5.5`) requires no code change — it appears in the next discovery call.

**Modular: role configuration.**

`registry.json` declares roles with constraints and fallback chains. Adding a role (e.g. `upgrade_verifier` in v2) is a JSON edit. No Python change.

**Non-modular: adapter selection.**

`model_registry.py` contains an `_ADAPTER_CLASSES` dict that maps provider name to adapter class. Adding a new provider adapter (e.g. `GeminiProvider`) requires:

1. New `core/providers/gemini_model.py` file
2. New entry in `_ADAPTER_CLASSES`
3. New provider config in `schema.py` (`ProvidersConfig` needs a `gemini` field, or a generic `extra` field)
4. Rebuild of the discovery path

That is a four-step Rule 1 escalation on `model_registry.py` and `schema.py`. Small in absolute terms, but not "add a plugin and it works."

**See §4.3 for the specific fix.**

### 3.5 The storage layer — separated concerns, versioned migrations

Five distinct stores, each with its own file:

| Store | File | Responsibility |
|---|---|---|
| `SQLiteStore` | `core/storage/sqlite_store.py` | Connection, PRAGMAs, execute/query |
| `FilesystemStore` | `core/storage/filesystem_store.py` | Artifacts on disk |
| `GraphStore` | `core/storage/graph_store.py` | CPG binary files |
| `Cache` | `core/storage/cache.py` | Two-tier LRU + disk |
| `SkillStore` | `core/storage/skill_store.py` | Markdown + FTS5 + vec0 |

**Why this is modular:**

- Each store has its own public surface. Replacing `Cache` with a Redis-backed cache does not touch `SQLiteStore`.
- The `MigrationRunner` reads `.sql` files sorted by name. Adding a migration is adding a file. No code edit.
- `_schema_migrations` records applied migrations. Idempotent.

**Adding a new artifact type:**

1. New JSON Schema in `schemas/`
2. Call `FilesystemStore.write_artifact(...)` with the schema filename
3. Optional: new table in a new migration

That is additive. No Rule 1 escalation. Storage is the second-most modular layer after the interface layer.

---

## 4. Non-Modular — The Specific Edit Points

Twelve places where adding a new instance of the abstracted thing requires editing a specific file. All are documented. None is a rewrite. All are Rule 1 escalations.

### 4.1 `Language` enum — Rust kernel

**File:** `rust/crates/codeguardian-kernel/src/langs/mod.rs`

**Current shape:** `Language` is a `Copy` enum with eight variants (Python, TypeScript, JavaScript, Java, Go, Rust, C, Cpp).

**What breaks when you add a ninth compiled-in language:** every `match language` statement across the kernel must add an arm. Current match sites:

- `langs::dispatch` — one match per language
- `langs::dispatch_by_name` — one match per language (plus aliases)
- `langs::Language::name` — one match per language
- `langs::Language::from_name` — one match per language (plus aliases)
- `pyo3/src/kernel.rs::build_graph` — one match per language name (indirect)

**Cost:** six-step Rule 1 escalation, ~1 hour of mechanical work.

**Alternative already shipped:** runtime grammar loading. A ninth language via `_native.load_grammar(name, path)` requires zero edits. The enum is only a convenience for the eight compiled-in languages.

**v2 impact:** none. Greenfield and upgrade modes do not require new compiled-in languages. They use the eight that exist, or a runtime grammar loaded from a plugin.

### 4.2 `_ADAPTER_CLASSES` — provider adapter map

**File:** `python/codeguardian/core/providers/model_registry.py`

**Current shape:** a hardcoded dict mapping provider name (`"anthropic"`, `"openai"`, `"deepseek"`, `"local"`) to adapter class.

**What breaks when you add a new provider adapter:** new file + new dict entry + provider config schema update + rebuild.

**Cost:** four-step Rule 1 escalation on `model_registry.py` and `config/schema.py`.

**Alternative not yet built:** entry-point discovery for providers. A new provider would register under `codeguardian.provider` entry point group. `ModelRegistry` would discover it like a plugin. One line in `pyproject.toml`, no core edit.

**v2 impact:** medium. Upgrade mode may want per-mode providers (a migration-specialist model for the planner). Today that means touching the hardcoded dict. Tomorrow it would be a plugin.

### 4.3 `_IMPLEMENTED` set — sandbox backend selector

**File:** `python/codeguardian/core/sandbox/backend_selector.py`

**Current shape:** a hardcoded set `_IMPLEMENTED = {"bubblewrap", "docker"}` telling the selector which backends are real vs stubs.

**What breaks when you add a new sandbox backend:** new file + new set entry + backend-specific config in `SandboxSpec` + Rust side changes if the backend is a new isolation primitive.

**Cost:** Rule 1 on `backend_selector.py`, and potentially on the Rust `codeguardian-sandbox` crate.

**Alternative not yet built:** register backends via entry point, same shape as tools.

**v2 impact:** low for greenfield (uses the same bubblewrap sandbox). Medium for upgrade mode if it needs a sandbox with different network policy (e.g. allow network during dependency-resolution steps in a multi-phase migration). That is a config change to `SandboxSpec`, not a backend change.

### 4.4 `_TYPE_DIRS` — bundled plugin type table

**File:** `python/codeguardian/core/plugins/loader.py`

**Current shape:** a 3-tuple table mapping directory name (`"languages"`, `"tools"`, `"reporters"`) to plugin type string.

**What breaks when you add a new plugin type** (e.g. `"transformers"` for v2 migration rules): the table grows one entry, and `_PROTOCOLS` grows one entry (§4.5).

**Cost:** two-line edit, Rule 1 on `loader.py` and `registry.py`.

**Alternative not yet built:** consolidate `_TYPE_DIRS`, `_PROTOCOLS`, and `_EXPECTED_HIGH_TIER` into one `plugin-types.json` schema. One source of truth. See §6.2.

**v2 impact:** medium. Adding "migration rule" as a plugin type is exactly this shape. Today it is a three-file edit. Tomorrow it could be a JSON edit.

### 4.5 `_PROTOCOLS` — plugin type to Protocol map

**File:** `python/codeguardian/core/plugins/registry.py`

**Current shape:** a dict mapping plugin type string (`"language"`, `"tool"`, `"reporter"`, `"verifier"`) to Protocol class.

**What breaks when you add a new plugin type:** new dict entry. Same story as §4.4.

**Cost:** one-line edit, Rule 1 on `registry.py`.

**Alternative:** same consolidation as §4.4.

### 4.6 `codes.py` + `taxonomy.py` — error code registry

**Files:** `python/codeguardian/core/errors/codes.py` and `python/codeguardian/core/errors/taxonomy.py`

**Current shape:** every error code is a string constant in `codes.py` and an `ErrorCode` enum member in `taxonomy.py`. Every member appears exactly once in `CATEGORY_MAP`. Category defaults are in `RETRY_POLICY`. Per-code overrides are in `RETRY_POLICY_OVERRIDES`.

**What breaks when you add a new error code** with a new category: two-file edit plus possibly a new category member, which ripples to every raise site that uses the new category.

**Cost:** two-line edit for a code in an existing category. Larger for a new category.

**This happened during Phase 9.** Five new codes (`PLUGIN_TIMEOUT`, four `GRAMMAR_*`) required adding to both files. That was approved as an additive Rule 1 escalation and it was the correct call — the taxonomy's bijection invariant (every code has exactly one category) is what makes the error handling work.

**Alternative not yet built:** load the taxonomy from JSON at import time. That would make adding a code a data edit, not a code edit. It would also break the "raise sites use `ErrorCode.MEMBER`" pattern that gives IDE autocomplete and typo safety.

**Trade-off:** the current design wins on type safety. The JSON design wins on extensibility. For a v1 codebase, type safety is the right choice. For v2, if error codes multiply (every migration step needs its own code), JSON wins.

**v2 impact:** low. v2 will likely add a handful of codes (`MIGRATION_PLAN_INVALID`, `MIGRATION_STEP_FAILED`, `SPEC_INVALID`, `TARGET_STATE_UNREACHABLE`), not dozens. Two-file edit per code is fine.

### 4.7 pyo3 binding registration — `add_function` calls

**File:** `rust/crates/codeguardian-pyo3/src/lib.rs`

**Current shape:** the `_native` module initializer explicitly registers every PyO3 function with `module.add_function(wrap_pyfunction!(...))`. Current functions: `echo`, `kernel_version`, `build_graph`, `graph_summary`, `compute_blast`, `check_sandbox_spec`, `check_plugin_permissions`, `detect_host_limits`, `load_grammar`, `list_languages`.

**What breaks when you add a new PyO3-exposed function:** two edits — the `#[pyfunction]` definition in a module file, and one `add_function` line in `lib.rs`.

**Cost:** two-line edit per function. No signature changes elsewhere. Trivial.

**v2 impact:** medium. Upgrade mode will likely expose new functions like `diff_api_surface`, `parse_spec`, `plan_migration_steps`. Each is a two-line edit. Acceptable.

### 4.8 `SandboxSpec.language` — sandbox language string

**File:** `python/codeguardian/interfaces/sandbox.py` (DTO) plus `rust/crates/codeguardian-sandbox/src/backend.rs` (backend `supports_language` check).

**Current shape:** language is a free-form string; backends check against their supported set. Bubblewrap supports all languages (it is just a Linux namespace wrapper). Docker checks against an image mapping.

**What breaks when you add a new language to the sandbox:** add to the image mapping in `DockerBackend`, verify bubblewrap accepts it. Small, local.

**Cost:** one-file edit for the sandbox backend that needs the mapping. No core edit.

**v2 impact:** none. Greenfield uses the same languages. Upgrade uses the same languages plus target-state package names (a different concept, not a language).

### 4.9 `_EXPECTED_HIGH_TIER` — reliability tier expectations in tests

**File:** `tests/contracts/test_language_contract.py`

**Current shape:** a frozenset of language names that the test expects to declare `reliability_tier = "high"`.

**What breaks when you change a language's tier:** both the plugin's `__manifest__` and this frozenset must update in sync.

**Cost:** two files, one line each. Small. But the fact that the tier is declared in two places is itself the smell.

**See §6.3 for the fix.**

### 4.10 `MULTI_LANG_FILES` — synthetic fixture in deep test

**File:** `sandtest/deep_test.py`

**Current shape:** an eight-tuple list with one synthetic file per language.

**What breaks when you add a ninth language to the deep test:** one more tuple. Local, additive.

**v2 impact:** none.

### 4.11 `deep_test.py` per-repo configuration

**File:** `sandtest/deep_test.py`

**Current shape:** `REPO_URL` env-overridable, but every other aspect of the run (target discovery, arg synthesis, characterization test) assumes Python source.

**What breaks for a non-Python deep test:** the entire target discovery pipeline is Python AST-based. A TypeScript or Java deep test needs a new discovery function.

**Cost:** substantial — a new `discover_<language>_targets` function per supported language in the deep test.

**Alternative not yet built:** since the deep test lives in the sandbox, it could parse with the kernel and walk the CPG instead of using Python AST. That would make it language-agnostic. Currently it does not.

**v2 impact:** medium. v2 = multi-mode, and modes will need to prove themselves on at least one non-Python repo. This becomes urgent.

### 4.12 The phase pipeline — not yet built, still refactor-shaped in the plan

**File:** `python/codeguardian/core/orchestrator/langgraph_orchestrator.py` (Phase 13, not yet written)

**Current shape:** per BUILD_ORDER, this will be a linear 8-phase pipeline. Recon → Blast → Context → Tests → Refactor → Sandbox Verify → Independent Verify → Output.

**What breaks in v2:** greenfield has no Blast and no characterization tests. Upgrade mode has a Migration Planner between Recon and Context, and per-step verification. The linear pipeline cannot represent either.

**Cost:** full pipeline refactor. This is the single largest v2 item.

**Alternative to adopt during Phase 13:** a phase graph as data. `legacy` mode = one list of phases. `greenfield` mode = another. `upgrade` mode = a third. The orchestrator interprets a graph, not a hardcoded sequence. **If this decision is made in Phase 13, v2 pipeline work goes from "refactor" to "add graph definition."**

**v2 impact:** critical. See §7.

---

## 5. Narrow by Design

These are deliberate limitations. They are documented in `BUILD_STATE.md` and in the relevant doc sections. They are not bugs.

### 5.1 Blast radius contract detection

**What it detects:** a caller that binds a target's return value to a variable, then dereferences that variable, where the target has at least one bare `return` (can produce `None`), and the dereference is not control-dependent on a guard.

**What it misses:** every other contract violation shape. A caller that assumes the return value is a list; a caller that assumes a specific exception type; a caller that relies on a side effect ordering; a caller that relies on idempotency.

**Why this is narrow:** the alternative is points-to analysis with aliasing, which is not a v1 or v2 concern. The narrow detector is honest: it finds some violations with high precision. It does not find all of them.

**Source:** `rust/crates/codeguardian-blast/src/contracts.rs`.

### 5.2 Coverage gaps

**What it detects:** callers not reached from a file whose filename matches a test pattern (`test_*.py`, `*_test.py`, `/tests/`, `*.test.*`, `__tests__/`).

**What it misses:** real coverage data. A caller that is reached by a test file may not actually be executed. A caller that is not reached by a test file may be exercised at runtime through a fixture that is not named like a test.

**Why this is narrow:** real coverage requires running the test suite under instrumentation and mapping the execution trace back to the call graph. That is the `runtime_contract_map` artifact in the design, but v1's implementation is filename-based.

**Source:** `rust/crates/codeguardian-blast/src/coverage.rs`.

### 5.3 Blast score weights

**Current weights:** `W_DIRECT_CALLER = 2`, `W_TRANSITIVE_CALLER = 1`, `W_CONTRACT_VIOLATION = 8`, `W_COVERAGE_GAP = 3`, cap at 100.

**Why this is narrow:** the weights are heuristics. They have never been calibrated against real repository data. On `psf/requests`, `get_environ_proxies` reports `direct=6, transitive=25`, score 72 in review range. That is plausible but not validated against actual breakage.

**Source:** `rust/crates/codeguardian-blast/src/score.rs`.

### 5.4 G-16 partial resolution

This is the honest gap. Every other "narrow" item is deliberate. G-16 partial is the only one where the narrowness is a risk.

**What works:** `self.foo()` inside a class resolves to `EnclosingClass.foo`. `Class.foo()` resolves to `Class.foo` when `Class` is in the symbol table. Simple `foo()` resolves to a unique symbol.

**What does not work:** `mod.foo()` where `mod` is imported. `pkg.mod.foo()` chained qualified names. Any call where the callee name is ambiguous across modules (`parse` defined in three files) and the receiver is a module reference.

**Consequence:** a function named `parse` called as `utils.parse()` from another file reports zero cross-file callers when the truth may be dozens. The blast report says "safe to change" when the change is dangerous.

**Why this matters more than the other narrow items:** the blast report's promise is "we tell you what will break before we touch it." Every other narrow item weakens a secondary signal. This one weakens the primary signal.

**Source:** `rust/crates/codeguardian-kernel/src/call_graph.rs`, `symbol_table.rs`, `cpg.rs`. Also `G-16` in `BUILD_STATE.md`.

### 5.5 Sandbox backends

| Backend | Status | What is missing |
|---|---|---|
| Bubblewrap | Real, proven | cgroups v2 writing for memory/CPU enforcement (uses simple `--die-with-parent` + outer timeout) |
| Docker | Real, partial | Full container lifecycle tested only on CI, not on dev environment |
| Seatbelt | Stub | `SANDBOX_CREATE_FAILED` from every method |
| Firecracker | Stub | `SANDBOX_CREATE_FAILED` from every method |

**Why this is narrow:** bubblewrap is sufficient for v1 (Linux CLI). Docker is the fallback. Seatbelt and Firecracker are documented stubs.

**Source:** `python/codeguardian/core/sandbox/`.

### 5.6 SQLite single-writer

**Current shape:** `busy_timeout = 5000` handles two concurrent writers. A third times out.

**Why this is narrow:** v1 is a single-process CLI. The cleanup thread is a second writer. Orchestrator + cleanup thread = 2 writers, safe. A third (MCP server process, or a second CLI invocation) breaks.

**Mitigation path:** per `BUILD_STATE` G-1, this needs either a write lock at the application level or a move to PostgreSQL. Neither is v1 or v2 scope.

### 5.7 Schema N-1 read

**What `05-data-model.md §8.2` promises:** "the core reads version N and N-1 of every schema. Older versions are rejected."

**What the code does:** validates against the exact schema in `schemas/`. No N-1 fallback.

**Why this is narrow:** every schema bump is a breaking change today. This is fine while the project is pre-1.0 with no persistent state. It becomes a problem the moment user runs are on disk from a previous version.

**v2 impact:** medium. v2 adds new artifacts (`spec.json`, `migration-plan.json`). Old runs on N, new runs on N+1. Without N-1 read, existing runs become unreadable.

### 5.8 Checkpoint durability

**What `13-risks R-T9` says:** "LangGraph saves state but does not detect whether the process running the next node is still active, restart the run if that process terminates, or prevent duplicate execution. A process crash during an OOM kill leaves checkpoints stranded without automated resume."

**Current state:** correct. Phase 13 has not been built, so this risk is unmitigated today.

**Why this is narrow:** a supervisor/watchdog process was planned but not built. It goes in Phase 13.

---

## 6. What Must Be Hardened Before v2

Ordered by priority. Not all are required for v1.0 ship; all are required before v2 begins.

### 6.1 Priority 0 — Close G-16

**Why:** every v2 mode — greenfield and upgrade — relies on accurate blast radius.

- **Greenfield:** blast radius determines how much of the existing codebase depends on the new module's integration points. A greenfield module dropped into a codebase without accurate blast means the verifiers cannot check what the new module broke in existing callers.
- **Upgrade:** blast radius determines the migration step order. If the tool thinks `parse()` has 3 callers when it has 30, it will plan 3 replacements and leave 27 broken.

**What closing means:**

1. Build `ImportTable` per file from `Import` and `ImportFrom` AST nodes.
2. Extend `call_graph.rs::resolve_callee` to consult the import table when the receiver is a module identifier.
3. Add qualified-name resolution to `symbol_table.rs::lookup_in_module(module, name)`.
4. Extend tests. Add a `test_kernel_imports.py` covering `mod.foo()`, `pkg.mod.foo()`, ambiguous names.

**Cost estimate:** 4-6 days of focused work. One new Rust module, three file edits, one test file.

**Why before v2:** because v2 builds on blast radius. Fixing it after v2 means v2 is written against a foundation known to be wrong.

### 6.2 Priority 1 — Consolidate plugin type dispatch

**Current state:** three dicts must agree — `_TYPE_DIRS` in `loader.py`, `_PROTOCOLS` in `registry.py`, `_EXPECTED_HIGH_TIER` in `test_language_contract.py`. Plus `_ENTRY_POINT_GROUPS` in `loader.py`.

**Problem:** adding a new plugin type (e.g. `"migration_rule"` in v2) means touching four files. Any disagreement is a silent bug.

**Recommended fix:** single `plugin-types.json` schema at `schemas/plugin-types.json`. Contents: list of plugin types, each with `directory_name`, `entry_point_group`, `protocol_path`, optional tier default. Loaded at plugin registry init.

**Cost estimate:** 2-3 days. One new schema, one loader change, one registry change, migration of the three test files to read from the schema.

**Why before v2:** v2 adds at least two new plugin types (`migration_rule`, possibly `spec_parser`). Cleaner now than after.

### 6.3 Priority 1 — Move provider and sandbox backend selection to entry points

**Current state:** `_ADAPTER_CLASSES` (providers) and `_IMPLEMENTED` (sandbox backends) are hardcoded dicts.

**Problem:** adding a new provider or sandbox backend is a Rule 1 escalation on core dispatch files.

**Recommended fix:** entry-point discovery for both. New provider registers under `codeguardian.provider` entry point group. New sandbox backend registers under `codeguardian.sandbox`. Both discovered at startup by the same pattern as tool plugins.

**Cost estimate:** 3-5 days. Two new discovery paths, migration of four bundled providers and two bundled sandbox backends to entry points, update `pyproject.toml`.

**Why before v2:** v2 needs more model flexibility (per-mode providers, per-migration-step models). Hardcoded dict blocks that.

### 6.4 Priority 2 — Implement schema N-1 read

**Current state:** `05-data-model.md §8.2` promises it, code does not do it.

**Problem:** every schema bump is a breaking change today. v2 adds new schemas.

**Recommended fix:** each schema declares `version` and `previous_version`. The artifact reader tries the current schema; on failure, tries the previous; if both fail, rejects. Straightforward, additive.

**Cost estimate:** 1-2 days. New helper in `FilesystemStore.read_artifact`, one field added to every schema, one test.

**Why before v2:** v2 adds `spec.json`, `migration-plan.json`, `migration-step.json`. Without N-1 read, existing runs break on the first schema bump.

### 6.5 Priority 2 — Language-agnostic deep test target discovery

**Current state:** `discover_python_targets` uses Python AST. Deep test only proves Python end-to-end.

**Problem:** v2 modes are mode-agnostic. Proving them on one language is not enough.

**Recommended fix:** rewrite target discovery to walk the CPG instead of the AST. The CPG already exists for every language. Enumerate `FunctionDef` nodes, check fan-in, filter. Language-agnostic.

**Cost estimate:** 2-3 days. One function rewrite in `sandtest/deep_test.py`.

**Why before v2:** v1.5 greenfield and v2 upgrade need proof beyond Python.

### 6.6 Priority 3 — Checkpoint durability supervisor

**Current state:** Phase 13 has not been built. R-T9 mitigation is deferred.

**Problem:** a crash mid-run strands the checkpoint. Every failed long run wastes the full token cost.

**Recommended fix:** supervisor process that watches the LangGraph checkpointer table for stale runs (heartbeat missing for N seconds), marks them resumable, and re-enters the pipeline from the last complete checkpoint.

**Cost estimate:** 3-5 days as part of Phase 13.

**Why before v2:** upgrade mode runs longer than refactor mode. A crash on a 40-step migration with 500k tokens spent is much worse than a crash on one refactor with 30k spent.

### 6.7 Priority 3 — Real coverage data in blast

**Current state:** filename heuristic.

**Problem:** for migration, "does a test cover this code path" determines risk. Filename heuristics are wrong by 40-60% on real codebases.

**Recommended fix:** instrumented test run producing a coverage map, fed into blast. This is the `runtime_contract_map` artifact from the design — it exists in the schema, not in the implementation.

**Cost estimate:** 5-7 days. New step in recon, new field in blast report, new schema field.

**Why before v2:** higher accuracy needed for migration planning than for refactoring.

---

## 7. v2 Readiness — The Pipeline and Agent Question

The single most important non-modular decision is not in any file yet. It is in Phase 13.

**Phase 13 will be the orchestrator.** If it is built as a linear 8-phase pipeline, v2 requires:

- New orchestrator
- New agent implementations per mode
- New verifiers per mode
- New schemas per mode
- New CLI commands per mode
- Rewrite the deep test per mode

That is a **full pipeline refactor**, not an extension.

**If instead the orchestrator interprets a phase graph as data**, v2 becomes:

- New graph definition per mode
- New agent branches per mode (each agent gains a `mode` parameter and a `run(mode)` method)
- New schemas per mode (already additive)
- New CLI commands per mode (subcommand registration)
- New deep test variant per mode (or language-agnostic discovery per §6.5)

That is **additive**, not a refactor.

**The specific change:** `orchestrator/langgraph_orchestrator.py` builds its LangGraph from a `PhaseGraph` object, not from hardcoded node insertions. `PhaseGraph` is defined per mode in `orchestrator/phase_graphs/legacy.py`, `greenfield.py`, `upgrade.py`. Each graph is a list of phases with entry conditions, exit conditions, and agent assignments.

**Cost of doing this in Phase 13:** roughly 20% extra effort on the orchestrator phase. Roughly 3-5 days.

**Cost of NOT doing this in Phase 13:** roughly 3-4 weeks of refactor work before v2 can start.

**Decision belongs in Phase 13, not now.** But this audit is the record that it must be made.

The same logic applies to Phase 11 (agents) and Phase 14 (CLI). Both are additive if built with mode as a first-class concept, both are refactor-shaped if built without it. This is the leverage point.

---

## 8. The Complete Extension Checklist

When adding a new instance of each extensible thing, use this table.

| Adding... | Files touched | Rule 1 escalations | Reference |
|---|---|---|---|
| A new language via runtime grammar | 1 (new `.so`) | 0 | §3.3 |
| A new language as compiled-in | 6 | 6 | §4.1 |
| A new tool plugin | 1 | 0 | §3.2 |
| A new reporter plugin | 1 | 0 | §3.2 |
| A new verifier plugin | 1 | 0 | §3.2 |
| A new provider via entry point (after §6.3) | 1 | 0 | §6.3 |
| A new provider via current `_ADAPTER_CLASSES` | 4 | 3 | §4.2 |
| A new sandbox backend | 3 | 2 | §4.3 |
| A new plugin type | 4 | 3 | §4.4, §4.5 |
| A new error code (existing category) | 2 | 2 | §4.6 |
| A new error category | 3 | 3 | §4.6 |
| A new PyO3 function | 2 | 1 | §4.7 |
| A new JSON schema | 1 | 0 | §3.5 |
| A new SQL migration | 1 | 0 | §3.5 |
| A new model (no adapter needed) | 0 | 0 | §3.4 |
| A new MCP tool | 1 | 0 | §3.2 |

The two columns that matter: **files touched** and **Rule 1 escalations**. Everything with 0 Rule 1 escalations is genuinely modular. Everything with 1 or more requires an escalation and belongs in this audit.

---

## 9. What v1.0 Code Is Not Designed For

Explicitly out of scope. These are not gaps to fix. They are statements of what v1.0 is.

| Not supported | Where it lives | When it matters |
|---|---|---|
| Greenfield mode | v1.5 (M6) | Already on roadmap |
| Project upgrade mode (dependency, framework, pattern) | v2+ | Post-v1.0 strategy |
| Architecture migration | Not on roadmap | Different product |
| Real-time repository monitoring | Non-goal (`02 §4.2` NG-S9) | Never |
| Multi-tenant isolation | v2.0 (M7) | When serving multiple users |
| Cryptographic audit signing | v2.0 (M7) | Regulated industries |
| Bitbucket, Azure DevOps providers | v2.0 (M7) | Enterprise shops |
| Cross-repo refactoring | Post-v2 (explicit non-goal) | Never in v1 scope |
| Dependency upgrade automation | Not on roadmap | v2 scope per §7 |

---

## 10. Summary Judgment

**Modular (do not touch):**
- 12 Protocols in `interfaces/`
- Plugin discovery, fault barrier, permission enforcement
- Runtime grammar loading
- Model discovery and role configuration
- Storage layer with versioned migrations
- Error taxonomy with bijection invariant

**Non-modular but cheap (edit when needed):**
- `_ADAPTER_CLASSES`, `_IMPLEMENTED`, `_TYPE_DIRS`, `_PROTOCOLS`, `_EXPECTED_HIGH_TIER`
- pyo3 binding registration
- `Language` enum (for compiled-in languages; runtime path avoids this)

**Non-modular and expensive (must be addressed):**
- G-16 partial resolution — the blast promise is weaker than advertised
- Pipeline phase graph — not yet built; must be data-driven in Phase 13
- Agent mode parameterization — not yet built; must be mode-aware in Phase 11

**Narrow by design (documented, honest):**
- Blast contract detection
- Coverage gaps
- Blast score weights
- Sandbox backends (bubblewrap real; seatbelt/firecracker stubs)
- SQLite single-writer
- Schema N-1 read

**The one risk to the whole promise:**

G-16 partial resolution is the only narrowness that undermines the tool's central claim. The blast report is the differentiator. It is not accurate today for the most common code shape in real projects.

Everything else is either modular, or narrow but honest, or a Phase 13/11 decision that has not yet been made.

**Recommended order before v2 starts:**

1. Close G-16 (§6.1) — 4-6 days
2. Consolidate plugin type dispatch (§6.2) — 2-3 days
3. Move provider and sandbox selection to entry points (§6.3) — 3-5 days
4. Implement schema N-1 read (§6.4) — 1-2 days
5. Language-agnostic deep test discovery (§6.5) — 2-3 days
6. Checkpoint durability (§6.6) — 3-5 days
7. Real coverage data in blast (§6.7) — 5-7 days

Total: roughly 4-6 weeks of work. All of it is useful for v1.0/v1.5 anyway. None of it is scope creep into v2 territory — every item is a completion of something already promised in the design.

Do this before v2 begins. Not during. Not after.

---

## 11. Related Documents

- `docs/04-architecture.md` — the system design this audit assesses
- `docs/05-data-model.md` — schemas and storage architecture
- `BUILD_STATE.md` — known gaps, particularly G-15, G-16, G-20, G-21
- `BUILD_ORDER.md` — phase plan, particularly Phase 10-16
- `docs/13-risks-assumptions-decisions.md` — risk register for cross-reference
- `docs/12-roadmap.md` — milestone plan this audit informs
- `skills.md` — the operating manual this audit was written under
