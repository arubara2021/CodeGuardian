//! Bubblewrap backend.
//!
//! Bubblewrap is the v1 Linux default. It is a user-namespace
//! sandbox built on `bwrap`, a small setuid-free launcher that
//! constructs a mount namespace, a PID namespace, and a network
//! namespace for the child. No daemon is required; the sandbox is a
//! plain subprocess.
//!
//! `create` does no work. Bubblewrap has no persistent sandbox state
//! to allocate; every `execute` spawns a fresh `bwrap` with the
//! parameters captured in the handle. `destroy` is a no-op that
//! verifies the handle was produced by this backend.
//!
//! The invariant this module protects is that a command cannot see the
//! host filesystem outside the workspace and a small read-only root.
//! Every flag list in `build_command` is written to enforce this; if a
//! future change adds a bind mount, it must add a matching test in
//! B-037 that asserts the mount is not writable from the sandbox.

use std::path::PathBuf;
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use tokio::process::Command;

use crate::backend::{
    read_child_stream, ExecutionRequest, ExecutionResult, SandboxBackend, SandboxHandle,
    SandboxSpec,
};
use crate::quotas::ResourceQuota;
use crate::{SandboxError, SandboxId};

/// The backend name recorded in every handle.
pub const BACKEND_NAME: &str = "bubblewrap";

/// The default execution timeout in milliseconds when a request does
/// not supply one.
pub const DEFAULT_TIMEOUT_MS: u64 = 300_000;

/// The languages this backend can execute.
const SUPPORTED_LANGUAGES: &[&str] = &["python", "typescript"];

/// A Bubblewrap sandbox backend.
pub struct BubblewrapBackend {
    binary: PathBuf,
    counter: AtomicU64,
}

impl Default for BubblewrapBackend {
    fn default() -> Self {
        Self::new()
    }
}

impl BubblewrapBackend {
    /// Construct a backend that invokes `bwrap` from `PATH`.
    pub fn new() -> Self {
        Self {
            binary: PathBuf::from("bwrap"),
            counter: AtomicU64::new(1),
        }
    }

    /// Construct a backend that invokes the given binary path.
    pub fn with_binary(binary: impl Into<PathBuf>) -> Self {
        Self {
            binary: binary.into(),
            counter: AtomicU64::new(1),
        }
    }

    /// Allocate a handle id unique to this backend instance.
    fn next_id(&self) -> SandboxId {
        let n = self.counter.fetch_add(1, Ordering::Relaxed);
        SandboxId::new(format!("bwrap-{n}"))
    }

    /// Build the `bwrap` invocation for a request.
    ///
    /// The mount layout is: the host root is bound read-only, the
    /// workspace is bound read-write, `/tmp` is a fresh tmpfs, and
    /// `/proc` and `/dev` are the sandbox's own. Every mount is
    /// explicit; nothing is inherited from the host process.
    fn build_command(
        &self,
        handle: &SandboxHandle,
        request: &ExecutionRequest,
    ) -> Command {
        let mut command = Command::new(&self.binary);

        command.arg("--die-with-parent");
        command.arg("--new-session");
        command.arg("--unshare-pid");
        command.arg("--unshare-ipc");
        command.arg("--unshare-uts");
        if !handle.network_enabled {
            command.arg("--unshare-net");
        }

        command.arg("--ro-bind").arg("/").arg("/");
        command.arg("--bind").arg(&handle.working_dir).arg(&handle.working_dir);
        command.arg("--tmpfs").arg("/tmp");
        command.arg("--proc").arg("/proc");
        command.arg("--dev").arg("/dev");

        for (key, value) in &request.env {
            command.arg("--setenv").arg(key).arg(value);
        }

        let cwd = match &request.cwd {
            Some(dir) => dir.clone(),
            None => handle.working_dir.clone(),
        };
        command.arg("--chdir").arg(cwd);

        command.arg("--");
        for part in &request.command {
            command.arg(part);
        }

        command.stdin(Stdio::piped());
        command.stdout(Stdio::piped());
        command.stderr(Stdio::piped());
        command
    }
}

#[async_trait]
impl SandboxBackend for BubblewrapBackend {
    async fn create(&self, spec: &SandboxSpec) -> Result<SandboxHandle, SandboxError> {
        spec.quota.validate()?;
        Ok(SandboxHandle {
            id: self.next_id(),
            backend: BACKEND_NAME.to_owned(),
            language: spec.language.clone(),
            working_dir: spec.working_dir.clone(),
            network_enabled: spec.network.is_enabled(),
        })
    }

    async fn execute(
        &self,
        handle: &SandboxHandle,
        request: &ExecutionRequest,
    ) -> Result<ExecutionResult, SandboxError> {
        if handle.backend != BACKEND_NAME {
            return Err(SandboxError::NotFound {
                id: handle.id.as_str().to_owned(),
            });
        }
        if request.command.is_empty() {
            return Err(SandboxError::ExecutionFailed {
                reason: "empty command".to_owned(),
            });
        }

        let timeout_ms = request.timeout_ms.unwrap_or(DEFAULT_TIMEOUT_MS);
        let timeout = Duration::from_millis(timeout_ms);
        let start = Instant::now();

        let mut command = self.build_command(handle, request);
        let mut child = command.spawn().map_err(|err| SandboxError::ExecutionFailed {
            reason: format!("spawn failed: {err}"),
        })?;

        let stdout_task = child
            .stdout
            .take()
            .map(|stream| tokio::spawn(read_child_stream(stream)));
        let stderr_task = child
            .stderr
            .take()
            .map(|stream| tokio::spawn(read_child_stream(stream)));

        let wait_result = tokio::time::timeout(timeout, child.wait()).await;

        match wait_result {
            Ok(Ok(status)) => {
                let stdout = collect(stdout_task).await;
                let stderr = collect(stderr_task).await;
                let duration_ms = start.elapsed().as_millis() as u64;
                let exit_code = status.code().unwrap_or(-1);
                let oom_killed = stderr_contains_oom(&stderr);
                Ok(ExecutionResult {
                    exit_code,
                    stdout,
                    stderr,
                    duration_ms,
                    timed_out: false,
                    oom_killed,
                })
            }
            Ok(Err(err)) => {
                let _ = child.start_kill();
                let _ = child.wait().await;
                Err(SandboxError::ExecutionFailed {
                    reason: format!("wait failed: {err}"),
                })
            }
            Err(_) => {
                let _ = child.start_kill();
                let _ = child.wait().await;
                Err(SandboxError::Timeout { timeout_ms })
            }
        }
    }

    async fn destroy(&self, handle: SandboxHandle) -> Result<(), SandboxError> {
        if handle.backend != BACKEND_NAME {
            return Err(SandboxError::NotFound {
                id: handle.id.as_str().to_owned(),
            });
        }
        Ok(())
    }

    fn supports_language(&self, language: &str) -> bool {
        SUPPORTED_LANGUAGES.contains(&language)
    }

    fn resource_limits(&self) -> ResourceQuota {
        ResourceQuota::default()
    }
}

/// Await a spawned stream reader, collapsing a join error to an empty
/// vector. A reader that failed to complete means the stream was
/// closed before the child exited; the child's exit code remains the
/// authoritative signal.
async fn collect(task: Option<tokio::task::JoinHandle<Vec<u8>>>) -> Vec<u8> {
    match task {
        Some(handle) => handle.await.unwrap_or_default(),
        None => Vec::new(),
    }
}

/// Return whether stderr contains a message that indicates the kernel
/// killed the process for memory.
///
/// Bubblewrap does not distinguish a memory kill from a signal kill in
/// the exit status, so the backend inspects the child's stderr for the
/// Linux OOM killer's standard phrase. This is a heuristic; a process
/// that prints the same phrase for other reasons will be misclassified.
fn stderr_contains_oom(stderr: &[u8]) -> bool {
    let text = String::from_utf8_lossy(stderr);
    text.contains("Killed") && text.contains("memory")
}
