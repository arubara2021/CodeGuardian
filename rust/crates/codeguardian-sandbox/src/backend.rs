//! The sandbox backend trait and its data transfer objects.
//!
//! A backend knows how to create an isolated execution environment,
//! run a command inside it, and tear it down. The trait does not
//! prescribe how isolation is achieved; Bubblewrap, Seatbelt,
//! Firecracker, and Docker all implement the same five methods, and
//! the caller does not know which one is in use.
//!
//! The trait is async because every backend either spawns a subprocess
//! or talks to a socket. The `async_trait` attribute boxes the returned
//! futures, so the trait stays object safe and callers hold a
//! `Box<dyn SandboxBackend>`.
//!
//! # Environment safety
//!
//! `request.env` and `spec.env` are not passed to the sandbox verbatim.
//! `sanitize_env` rejects a fixed list of environment variables that
//! would let the caller run code before the command starts, load a
//! shared object into the process, or redirect the language runtime's
//! module search path. A spec that carries one of these variables is
//! rejected at create time; a request that carries one is rejected at
//! execute time.
//!
//! # Output safety
//!
//! `read_child_stream` reads at most `MAX_OUTPUT_BYTES` from a child's
//! output. A process that writes more has its extra output discarded
//! and the `output_truncated` flag set on the result. Without the cap,
//! a runaway process that writes to stdout would grow the reader's
//! buffer until the sandbox daemon is killed by the kernel OOM killer.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use async_trait::async_trait;

use crate::network::NetworkPolicy;
use crate::quotas::ResourceQuota;
use crate::{SandboxError, SandboxId};

/// The maximum number of bytes read from a child's stdout or stderr.
pub const MAX_OUTPUT_BYTES: usize = 16 * 1024 * 1024;

/// Environment variable names that are refused in a sandbox spec or
/// execution request.
///
/// Every entry is a code-injection vector. `LD_PRELOAD` and its macOS
/// equivalent load an arbitrary shared library into the process before
/// `main` runs. `PYTHONPATH`, `NODE_PATH`, and their peers redirect the
/// runtime's module search. `BASH_ENV` and `ENV` are sourced by the
/// shell on startup.
pub const FORBIDDEN_ENV_KEYS: &[&str] = &[
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "NODE_OPTIONS",
    "NODE_PATH",
    "PERL5LIB",
    "RUBYLIB",
    "GEM_HOME",
    "BASH_ENV",
    "ENV",
];

/// Everything the backend needs to construct a sandbox.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SandboxSpec {
    /// The language the sandbox will run, e.g. `"python"`.
    pub language: String,
    /// The directory the sandbox will mount or bind as its workspace.
    pub working_dir: PathBuf,
    /// Environment variables set inside the sandbox.
    pub env: HashMap<String, String>,
    /// The resource limits the backend must enforce.
    pub quota: ResourceQuota,
    /// Whether the sandbox has network access and in which phase.
    pub network: NetworkPolicy,
}

impl SandboxSpec {
    /// Construct a spec with the given language and working directory,
    /// no environment overrides, default quota, and no network.
    pub fn new(language: impl Into<String>, working_dir: impl Into<PathBuf>) -> Self {
        Self {
            language: language.into(),
            working_dir: working_dir.into(),
            env: HashMap::new(),
            quota: ResourceQuota::default(),
            network: NetworkPolicy::deny_all(),
        }
    }
}

/// The identity of a live sandbox.
///
/// The handle is returned from `create` and passed back to every
/// subsequent operation. `destroy` consumes the handle; a backend that
/// receives a handle it did not produce returns `SandboxError::NotFound`
/// rather than guessing.
///
/// `network_enabled` and the three quota fields are captured at
/// creation and travel with the handle so `execute` can reconstruct
/// the sandbox's resource state without a side table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SandboxHandle {
    /// The opaque id assigned by the backend.
    pub id: SandboxId,
    /// The name of the backend that produced this handle.
    pub backend: String,
    /// The language the sandbox was created for.
    pub language: String,
    /// The workspace directory inside the sandbox.
    pub working_dir: PathBuf,
    /// Whether the sandbox has network access.
    pub network_enabled: bool,
    /// Memory limit in megabytes.
    pub memory_mb: u32,
    /// CPU core limit.
    pub cpu_cores: u32,
    /// Process count limit.
    pub process_limit: u32,
}

/// One command to run inside a live sandbox.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecutionRequest {
    /// The command and its arguments. The first element is the program
    /// name. An empty vector is a caller bug; a backend returns
    /// `ExecutionFailed` if it cannot interpret an empty command.
    pub command: Vec<String>,
    /// The directory to run in. `None` means the sandbox's working
    /// directory.
    pub cwd: Option<PathBuf>,
    /// Additional environment variables for this run only. These are
    /// merged over the spec's environment. The same forbidden-key list
    /// that applies to the spec applies here.
    pub env: HashMap<String, String>,
    /// The timeout in milliseconds. `None` means the backend's default.
    pub timeout_ms: Option<u64>,
    /// Bytes written to the process's standard input before it is
    /// closed. `None` means standard input is closed immediately.
    pub stdin: Option<Vec<u8>>,
}

impl ExecutionRequest {
    /// Construct a request that runs `command` with no extra options.
    pub fn new(command: Vec<String>) -> Self {
        Self {
            command,
            cwd: None,
            env: HashMap::new(),
            timeout_ms: None,
            stdin: None,
        }
    }
}

/// The outcome of one execution.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecutionResult {
    /// The process's exit code.
    pub exit_code: i32,
    /// Bytes written to standard output, capped at `MAX_OUTPUT_BYTES`.
    pub stdout: Vec<u8>,
    /// Bytes written to standard error, capped at `MAX_OUTPUT_BYTES`.
    pub stderr: Vec<u8>,
    /// Wall-clock duration of the execution in milliseconds.
    pub duration_ms: u64,
    /// Whether the execution was killed because it exceeded its
    /// timeout.
    pub timed_out: bool,
    /// Whether the execution was killed because it exceeded its memory
    /// quota.
    pub oom_killed: bool,
    /// Whether either stream exceeded `MAX_OUTPUT_BYTES` and had its
    /// excess discarded.
    pub output_truncated: bool,
}

/// The isolation boundary.
#[async_trait]
pub trait SandboxBackend: Send + Sync {
    /// Create a sandbox and return a handle to it.
    async fn create(&self, spec: &SandboxSpec) -> Result<SandboxHandle, SandboxError>;

    /// Run a command inside a live sandbox.
    async fn execute(
        &self,
        handle: &SandboxHandle,
        request: &ExecutionRequest,
    ) -> Result<ExecutionResult, SandboxError>;

    /// Destroy a sandbox and release its resources.
    async fn destroy(&self, handle: SandboxHandle) -> Result<(), SandboxError>;

    /// Return whether this backend can execute the named language.
    fn supports_language(&self, language: &str) -> bool;

    /// Return the resource limits this backend will enforce.
    fn resource_limits(&self) -> ResourceQuota;
}

/// Reject any environment map that contains a forbidden key.
///
/// The check is case-insensitive on the key name. An empty map passes.
pub(crate) fn sanitize_env(env: &HashMap<String, String>) -> Result<(), SandboxError> {
    for key in env.keys() {
        let upper = key.to_ascii_uppercase();
        if FORBIDDEN_ENV_KEYS.iter().any(|k| k.to_ascii_uppercase() == upper) {
            return Err(SandboxError::CreateFailed {
                reason: format!(
                    "environment variable '{key}' is a code-injection vector \
                     and cannot be set inside the sandbox"
                ),
            });
        }
    }
    Ok(())
}

/// Return a canonical, validated working directory.
///
/// Rejects a path that does not exist, is not a directory, or resolves
/// to a top-level system directory. The canonical form is returned so
/// the caller stores the resolved path rather than the input.
pub(crate) fn validate_working_dir(path: &Path) -> Result<PathBuf, SandboxError> {
    const FORBIDDEN: &[&str] = &[
        "/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run",
        "/sbin", "/sys", "/usr", "/var",
    ];

    let canonical = path.canonicalize().map_err(|err| SandboxError::CreateFailed {
        reason: format!("working_dir cannot be resolved: {err}"),
    })?;

    if !canonical.is_dir() {
        return Err(SandboxError::CreateFailed {
            reason: format!(
                "working_dir is not a directory: {}",
                canonical.display()
            ),
        });
    }

    let text = canonical.to_string_lossy();
    if FORBIDDEN.iter().any(|f| text == *f) {
        return Err(SandboxError::CreateFailed {
            reason: format!(
                "working_dir is a top-level system directory and cannot be used: {}",
                canonical.display()
            ),
        });
    }

    Ok(canonical)
}

/// Read a child process's output stream to end or to the output cap.
///
/// Returns the bytes read and a flag that is true when the stream
/// produced more than `MAX_OUTPUT_BYTES`.
pub(crate) async fn read_child_stream<R>(mut stream: R) -> (Vec<u8>, bool)
where
    R: tokio::io::AsyncRead + Unpin,
{
    use tokio::io::AsyncReadExt;

    let mut buf: Vec<u8> = Vec::with_capacity(8 * 1024);
    let mut chunk = [0u8; 8 * 1024];
    let mut truncated = false;

    loop {
        match stream.read(&mut chunk).await {
            Ok(0) => break,
            Ok(n) => {
                if buf.len() + n > MAX_OUTPUT_BYTES {
                    let remaining = MAX_OUTPUT_BYTES.saturating_sub(buf.len());
                    buf.extend_from_slice(&chunk[..remaining]);
                    truncated = true;
                    break;
                }
                buf.extend_from_slice(&chunk[..n]);
            }
            Err(_) => break,
        }
    }

    (buf, truncated)
}
