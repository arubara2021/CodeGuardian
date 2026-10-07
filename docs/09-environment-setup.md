# 09 — Environment Setup

---

## 1. Purpose

This document tells a new user exactly what to install and how to run CodeGuardian for the first time. It covers prerequisites, installation, configuration, first run, platform-specific notes, troubleshooting, and uninstall.

The goal is simple: a new user should be able to go from zero to a working CodeGuardian run without reading any other document.

---

## 2. Prerequisites

### 2.1 Required

| Prerequisite | Minimum Version | Why |
|---|---|---|
| **Python** | 3.12+ | The orchestration, CLI, and plugin layer. |
| **Git** | 2.40+ | Required for cloning repositories. |
| **Rust toolchain** | 1.83+ | Only required for building from source. Binary distributions include pre-built Rust libraries. |
| **Sandbox runtime** | Platform-dependent | See §2.2. |

### 2.2 Sandbox Runtime by Platform

The sandbox runtime depends on the platform and the chosen backend.

| Platform | Default Backend | Runtime | Install |
|---|---|---|---|
| **Linux** | Bubblewrap | `bwrap` | `apt install bubblewrap` / `dnf install bubblewrap` / `pacman -S bubblewrap` |
| **macOS** | Seatbelt | `sandbox-exec` (built in) | No install required. |
| **Windows** | Docker | Docker Desktop | Docker Desktop with WSL2 backend. |

Bubblewrap is Linux-only and requires unprivileged user namespaces to be enabled in the kernel. Most modern distributions enable this by default. If not, the setup command detects it and warns.

Seatbelt is built into macOS. It uses the `sandbox-exec` command and a Scheme-like profile generated at runtime. No install is required.

Firecracker is optional and only needed for cloud or multi-tenant deployments. It requires a Linux host with hardware virtualization (KVM). It is not part of the default local setup.

### 2.3 Optional

| Prerequisite | Why |
|---|---|
| **Local model runtime** | Ollama, vLLM, llama.cpp, or LM Studio for offline use. |
| **Docker** | For Windows users or as a fallback backend. |
| **`uv`** | Faster Python package installation. |

---

## 3. Installation

There are three installation paths. Choose the one that fits your use case.

### 3.1 Standard Install (Recommended)

```bash
pip install codeguardian
```

This installs the Python package and the pre-built Rust library. It works on Linux, macOS, and Windows.

### 3.2 Isolated Install

```bash
pipx install codeguardian
```

This installs CodeGuardian in its own isolated environment. Recommended if you use multiple Python projects and do not want dependency conflicts.

### 3.3 Build from Source

For contributors and users who want the latest development version.

```bash
git clone https://github.com/<org>/codeguardian.git
cd codeguardian

# Install Rust toolchain (if not already installed)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Install maturin (the build tool for PyO3)
pip install maturin

# Build the Rust layer and install the Python package
maturin develop --release

# Or install in editable mode
pip install -e ".[dev]"
```

`maturin` is the recommended build tool for PyO3 projects. It handles the Rust compilation, the Python packaging, and the linkage between the two layers.

### 3.4 Verify Installation

```bash
codeguardian --version
codeguardian doctor
```

The `doctor` command checks every prerequisite and reports what is available.

```
CodeGuardian Doctor
───────────────────
✓ Python 3.12.4
✓ Git 2.43.0
✓ Sandbox: Bubblewrap 0.8.0
✓ SQLite 3.45.0 (WAL mode)
✓ Tree-sitter grammars: python, typescript, javascript
✗ Rust toolchain: not found (only needed for source builds)
✓ Config: ~/.codeguardian/config.toml
✓ Database: ~/.codeguardian/codeguardian.db
```

---

## 4. Configuration

### 4.1 Config File Location

CodeGuardian reads its configuration from:

```
~/.codeguardian/config.toml
```

If the file does not exist, CodeGuardian uses built-in defaults. Create a default config with:

```bash
codeguardian config init
```

### 4.2 Configuration Schema

```toml
# ~/.codeguardian/config.toml

[general]
# Default mode: "legacy" or "greenfield"
mode = "legacy"

# Default sandbox backend: "auto", "bubblewrap", "seatbelt", "firecracker", "docker"
sandbox_backend = "auto"

# Log level: "debug", "info", "warning", "error"
log_level = "info"


[models]
# Per-role model selection. Any OpenAI-compatible provider works.

[models.analyst]
provider = "deepseek"
model = "deepseek-v4-flash"

[models.tester]
provider = "anthropic"
model = "claude-opus-5.5"

[models.writer]
provider = "anthropic"
model = "claude-opus-5.5"

[models.correctness_verifier]
provider = "openai"
model = "gpt-6-astra"

[models.security_verifier]
provider = "openai"
model = "gpt-6-astra"

[models.contract_verifier]
provider = "openai"
model = "gpt-6-astra"

[models.skill_curator]
provider = "deepseek"
model = "deepseek-v4-flash"


[providers.anthropic]
api_key_source = "keychain"          # API keys stored in OS keychain

[providers.openai]
api_key_source = "keychain"

[providers.deepseek]
api_key_source = "keychain"

[providers.local]
base_url = "http://localhost:11434/v1"   # Ollama default
# base_url = "http://localhost:8000/v1"  # vLLM default
# base_url = "http://localhost:8080/v1"  # llama.cpp default
# base_url = "http://localhost:1234/v1"  # LM Studio default


[storage]
database = "~/.codeguardian/codeguardian.db"
artifacts_retention_days = 30
graph_retention_days = 90
audit_retention_days = 0              # 0 = forever


[sandbox]
memory_limit_mb = 1024
cpu_limit_cores = 2
process_limit = 256
network_during_deps = true             # Network enabled only during dependency resolution


[mcp]
# External MCP servers to consume
servers = [
  { name = "ast-grep", command = "npx", args = ["-y", "ast-grep-mcp"] },
]


[exclusions]
# Additional exclusion patterns beyond Tier 1 defaults.
# Uses .gitignore syntax.
patterns = [
  "docs/generated/",
  "*.generated.py",
]
```

### 4.3 Setting API Keys

API keys are stored in the OS keychain, never in the config file.

```bash
codeguardian config set-key anthropic
# Prompts for the key, stores it in the OS keychain

codeguardian config set-key openai
codeguardian config set-key deepseek
```

On Linux, the keychain uses `libsecret`. On macOS, it uses Keychain Access. On Windows, it uses Windows Credential Manager.

### 4.4 Sandbox Backend Selection

The default backend is `auto`. CodeGuardian detects the platform and available runtimes.

```bash
# Override the backend for a specific run
codeguardian run --sandbox bubblewrap ...

# Or set it permanently in config.toml
[sandbox]
backend = "bubblewrap"
```

### 4.5 Local Model Setup

If you want to run CodeGuardian with local models, start your local runtime and point CodeGuardian at its OpenAI-compatible endpoint.

```bash
# Example: Ollama
ollama pull llama3.3
ollama serve
```

Then set the local provider as the default in `config.toml`:

```toml
[models.writer]
provider = "local"
model = "llama3.3"
```

No API key is required for local models. Cost is electricity, not tokens.

---

## 5. First Run

### 5.1 Initialize

```bash
codeguardian config init
```

This creates `~/.codeguardian/config.toml`, the SQLite database, and the directory structure.

### 5.2 Run Reconnaissance

```bash
codeguardian recon --url https://github.com/example/legacy-project
```

This clones the repository into the sandbox, builds the Code Property Graph, runs the smoke test, and produces the reconnaissance manifest.

**What you see:**

```
CodeGuardian Reconnaissance
───────────────────────────
Cloning repository...                    ✓
Detecting languages...                   ✓ python, typescript
Detecting frameworks...                  ✓ django, react
Building Code Property Graph...           ✓ 12,847 nodes, 34,291 edges
Building dependency graph...              ✓ 1,203 files, 4,891 edges
Building hierarchical index...            ✓ 187 directories
Extracting declared versions...           ✓ 14 packages
Running instrumented smoke test...        ✓ 142 passed, 3 failed, 2 errored
Generating Truth Report...                ✓ 2 mismatches found
Generating Drift Report...                ✓ 5 drifts found

Reconnaissance complete.
  → ~/.codeguardian/repos/<repo-id>/cache/recon-<commit>.json
  → ~/.codeguardian/repos/<repo-id>/cache/runtime-contract-<commit>.json
  → ~/.codeguardian/repos/<repo-id>/graph/cpg-<commit>.bin
```

### 5.3 Compute Blast Radius

```bash
codeguardian blast \
  --url https://github.com/example/legacy-project \
  --target utils/parser.py:parse_config
```

**What you see:**

```
Blast Radius Report
───────────────────
Target:              utils/parser.py:parse_config
Change kind:         semantic

Direct callers:      14
Transitive callers:  47
Contract violations: 2
Coverage gaps:       1

Blast score:         72 / 100
Recommendation:      REVIEW

Contract violations:
  ⚠ services/loader.py:load_settings
    Assumption: parse_config never returns None
    Risk: critical

  ⚠ controllers/api.py:handle_request
    Assumption: parse_config always strips null bytes
    Risk: warning

Coverage gaps:
  ⚠ controllers/api.py:handle_request has no test covering this path
```

### 5.4 Run the Full Pipeline

```bash
codeguardian run \
  --url https://github.com/example/legacy-project \
  --target utils/parser.py:parse_config \
  --directive "add type hints and extract long functions"
```

**What you see:**

```
CodeGuardian Run
────────────────
Pre-flight brief:
  Scope:      single target + 14 callers
  Target:     utils/parser.py:parse_config
  Directive:  add type hints and extract long functions
  Blast:      72 / 100 (review)

Approve? (y/N) y

Phase 0 — Reconnaissance.............. ✓ (cached)
Phase 1 — Blast Radius Analysis....... ✓ (cached)
Phase 2 — Context Gathering........... ✓ 4,200 tokens
Phase 3 — Safety Net.................. ✓ 18 tests, 2 contract assertions
Phase 4 — Refactor.................... ✓ diff produced
Phase 5 — Sandbox Verification........ ✓ all tests pass
Phase 6 — Independent Verification.... 
  Verify with a second model? (y/N) y
  Correctness verifier................ ✓ pass
  Security verifier................... ✓ pass
  Contract verifier................... ✓ pass
Phase 7 — Output...................... ✓

Apply changes? (y/N)
```

### 5.5 Apply or Discard

If you type `y`, the diff is applied to the repository. If you type `N`, the diff is discarded and the sandbox is destroyed.

The audit log is written either way.

---

## 6. Platform-Specific Notes

### 6.1 Linux

**Bubblewrap setup:**

```bash
# Debian / Ubuntu
sudo apt install bubblewrap

# Fedora / RHEL
sudo dnf install bubblewrap

# Arch
sudo pacman -S bubblewrap
```

Verify that unprivileged user namespaces are enabled:

```bash
cat /proc/sys/kernel/unprivileged_userns_clone
# Should be 1
```

If it is 0, enable it:

```bash
sudo sysctl -w kernel.unprivileged_userns_clone=1
```

Some distributions (such as Ubuntu 24.04+) restrict unprivileged user namespaces via AppArmor. If Bubblewrap fails, check `kernel.apparmor_restrict_unprivileged_userns` and disable the restriction if appropriate for your environment.

**Firecracker (optional):**

Firecracker requires KVM. Verify:

```bash
lsmod | grep kvm
ls -la /dev/kvm
```

If `/dev/kvm` is missing, load the module:

```bash
sudo modprobe kvm
sudo modprobe kvm_intel   # or kvm_amd
```

Add your user to the `kvm` group:

```bash
sudo usermod -aG kvm $(whoami)
```

### 6.2 macOS

Seatbelt is built into macOS. No install is required. CodeGuardian generates a Seatbelt profile at runtime and applies it via `sandbox-exec`.

**Apple Silicon note:** CodeGuardian ships universal binaries for both Apple Silicon and Intel. The Rust layer is compiled for both architectures.

### 6.3 Windows

Windows requires Docker Desktop with the WSL2 backend.

1. Install Docker Desktop.
2. Enable the WSL2 backend in Docker Desktop settings.
3. Install CodeGuardian via `pip install codeguardian`.

Docker Desktop is the only supported sandbox backend on Windows. Bubblewrap and Seatbelt are not available.

**WSL2 recommended:** For the best experience, run CodeGuardian inside WSL2 instead of native Windows. This gives access to Bubblewrap and the full Linux toolchain.

---

## 7. Troubleshooting

### 7.1 Sandbox Backend Unavailable

```
Error: No sandbox backend available.
Tried: bubblewrap (not found), docker (not running)
```

**Fix:** Install a supported backend for your platform. See §2.2.

### 7.2 User Namespaces Disabled

```
Error: Bubblewrap failed to create sandbox.
Reason: unprivileged user namespaces are disabled.
```

**Fix:** Enable user namespaces. See §6.1.

### 7.3 Missing API Key

```
Error: No API key configured for provider 'anthropic'.
```

**Fix:**

```bash
codeguardian config set-key anthropic
```

### 7.4 Tree-sitter Grammar Not Found

```
Error: No grammar registered for language 'kotlin'.
```

**Fix:** Install the language plugin:

```bash
pip install codeguardian-lang-kotlin
```

### 7.5 PyO3 Build Failure

```
Error: Failed to build codeguardian-kernel.
Reason: Rust toolchain not found.
```

**Fix:** Install the Rust toolchain:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

### 7.6 Database Locked

```
Error: database is locked.
```

**Fix:** Ensure no other CodeGuardian process is running. SQLite WAL mode allows concurrent reads but only one writer.

### 7.7 MCP Server Not Responding

```
Error: MCP server 'ast-grep' did not respond within 30 seconds.
```

**Fix:** Verify the server is installed and the command is correct:

```bash
npx -y ast-grep-mcp --help
```

### 7.8 Out of Memory During CPG Build

```
Error: CPG build exceeded memory limit.
```

**Fix:** For large repositories, use a scoped graph instead of a full-repo graph:

```bash
codeguardian blast --url <repo> --target <file>
```

This builds the graph only for the target and its transitive callers.

---

## 8. Uninstall

### 8.1 Remove the Package

```bash
pip uninstall codeguardian
```

### 8.2 Remove Data

```bash
rm -rf ~/.codeguardian
```

This removes the database, cached graphs, run artifacts, skills, and plugins.

### 8.3 Remove API Keys

```bash
codeguardian config remove-key anthropic
codeguardian config remove-key openai
codeguardian config remove-key deepseek
```

Or remove them manually from your OS keychain.

### 8.4 Remove Rust Toolchain (Optional)

Only if you installed Rust specifically for CodeGuardian:

```bash
rustup self uninstall
```

---

## 9. Environment Variables

CodeGuardian reads the following environment variables. All are optional; they override config file values.

| Variable | Purpose |
|---|---|
| `CODEGUARDIAN_CONFIG` | Path to an alternate config file. |
| `CODEGUARDIAN_HOME` | Override the default `~/.codeguardian` directory. |
| `CODEGUARDIAN_LOG_LEVEL` | Override the log level. |
| `CODEGUARDIAN_SANDBOX_BACKEND` | Override the sandbox backend. |
| `ANTHROPIC_API_KEY` | Fallback API key if the keychain is unavailable. |
| `OPENAI_API_KEY` | Fallback API key if the keychain is unavailable. |
| `DEEPSEEK_API_KEY` | Fallback API key if the keychain is unavailable. |
| `LANGSMITH_API_KEY` | Enable LangSmith tracing. |
| `LANGSMITH_PROJECT` | LangSmith project name. |

API keys in environment variables are less secure than the OS keychain. Use them only in CI environments where the keychain is unavailable.

---

## 10. Related Documents

- `04-architecture.md` — components that these prerequisites support
- `07-tech-stack.md` — language and framework decisions
- `08-repository-structure.md` — directory layout
- `10-testing-cicd-deployment.md` — CI pipeline setup
- `14-model-strategy.md` — model roles and provider selection
