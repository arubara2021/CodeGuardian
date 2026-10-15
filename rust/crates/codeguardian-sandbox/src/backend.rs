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

use std::collections::HashMap;
use std::path::PathBuf;

use async_trait::async_trait;

use crate::network::NetworkPolicy;
use crate::quotas::ResourceQuota;
use crate::{SandboxError, SandboxId};

/// Everything the backend needs to construct a sandbox.
///
/// The spec is backend-agnostic. A backend that needs a mount table, an
/// image reference, or a container configuration derives those from the
/// language, the working directory, and the environment; it does not
/// receive them from the caller.
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
/// `network_enabled` is captured at creation and travels with the
/// handle so `execute` can reconstruct the sandbox's network state
/// without a side table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SandboxHandle {
    /// The opaque id assigned by the backend.
    pub id: SandboxId,
    /// The name of the backend that produced this handle, e.g.
    /// `"bubblewrap"`. Set by the backend at creation.
    pub backend: String,
    /// The language the sandbox was created for.
    pub language: String,
    /// The workspace directory inside the sandbox.
    pub working_dir: PathBuf,
    /// Whether the sandbox has network access.
    pub network_enabled: bool,
}

/// One command to run inside a live sandbox.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecutionRequest {
    /// The command and its arguments. The first element is the program
    /// name. An empty vector is a caller bug; a backend may return
    /// `ExecutionFailed` if it cannot interpret an empty command.
    pub command: Vec<String>,
    /// The directory to run in. `None` means the sandbox's working
    /// directory.
    pub cwd: Option<PathBuf>,
    /// Additional environment variables for this run only. These are
    /// merged over the spec's environment.
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
///
/// `timed_out` and `oom_killed` distinguish the two quota failures a
/// backend can produce from a process that ran to completion and
/// exited non-zero. A caller that wants to know why the process
/// stopped reads those flags before reading `exit_code`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecutionResult {
    /// The process's exit code. A backend that killed the process for a
    /// quota reason sets this to a value its implementation defines; the
    /// caller should consult `timed_out` and `oom_killed` first.
    pub exit_code: i32,
    /// Bytes written to standard output.
    pub stdout: Vec<u8>,
    /// Bytes written to standard error.
    pub stderr: Vec<u8>,
    /// Wall-clock duration of the execution in milliseconds.
    pub duration_ms: u64,
    /// Whether the execution was killed because it exceeded its
    /// timeout.
    pub timed_out: bool,
    /// Whether the execution was killed because it exceeded its memory
    /// quota.
    pub oom_killed: bool,
}

/// The isolation boundary.
///
/// A backend creates sandboxes, runs commands inside them, and destroys
/// them. The trait is the only contract the caller depends on. The
/// backend's isolation technology is not part of the contract.
///
/// Every method that can fail returns `SandboxError`. Implementations
/// must not panic on bad input; a sandbox that receives a handle it
/// does not recognize returns `NotFound`.
#[async_trait]
pub trait SandboxBackend: Send + Sync {
    /// Create a sandbox and return a handle to it.
    ///
    /// The backend validates the spec's quota before allocating
    /// resources. An invalid quota returns `CreateFailed` with a
    /// description of the constraint that was violated.
    async fn create(&self, spec: &SandboxSpec) -> Result<SandboxHandle, SandboxError>;

    /// Run a command inside a live sandbox.
    ///
    /// The backend may return `Timeout`, `OutOfMemory`, or
    /// `QuotaExceeded` if the process exceeded a limit, or
    /// `ExecutionFailed` if the process could not be started or the
    /// backend could not observe its completion. A process that runs
    /// and exits non-zero is a success from the trait's perspective;
    /// the exit code appears in the `ExecutionResult`.
    async fn execute(
        &self,
        handle: &SandboxHandle,
        request: &ExecutionRequest,
    ) -> Result<ExecutionResult, SandboxError>;

    /// Destroy a sandbox and release its resources.
    ///
    /// Consumes the handle. Destroying a sandbox that has already been
    /// destroyed is an error: the second call returns `NotFound`.
    async fn destroy(&self, handle: SandboxHandle) -> Result<(), SandboxError>;

    /// Return whether this backend can execute the named language.
    ///
    /// Backends report their supported languages from a fixed list.
    /// A stub backend that does not implement execution returns false
    /// for every language.
    fn supports_language(&self, language: &str) -> bool;

    /// Return the resource limits this backend will enforce.
    ///
    /// The returned quota is the ceiling the backend applies to every
    /// sandbox it creates, independent of the spec. A spec that
    /// requests more than the backend's ceiling is rejected at
    /// `create` with `QuotaExceeded`.
    fn resource_limits(&self) -> ResourceQuota;
}

/// Read a child process's output stream to end.
///
/// Used by every backend that spawns a process with piped standard
/// output or standard error. The read runs on the runtime, not on the
/// blocking pool, so a slow child does not stall the executor.
pub(crate) async fn read_child_stream<R>(mut stream: R) -> Vec<u8>
where
    R: tokio::io::AsyncRead + Unpin,
{
    use tokio::io::AsyncReadExt;
    let mut buf: Vec<u8> = Vec::new();
    let _ = stream.read_to_end(&mut buf).await;
    buf
}
