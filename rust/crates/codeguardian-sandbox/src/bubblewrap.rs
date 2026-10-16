//! Bubblewrap backend.
//!
//! Bubblewrap is the v1 Linux default. It is a user-namespace
//! sandbox built on `bwrap`, a small setuid-free launcher that
//! constructs a mount namespace, a PID namespace, and a network
//! namespace for the child.
//!
//! # Mount order
//!
//! The order in which bwrap applies mount options matters. `--tmpfs
//! /tmp` replaces the entire `/tmp` subtree; if it is applied after a
//! `--bind` whose destination is under `/tmp`, the earlier bind is
//! shadowed and the destination path stops existing. The command
//! builder therefore mounts the tmpfs first and the working directory
//! second, so bwrap creates the working directory's parents inside the
//! fresh tmpfs and the bind lands on top.
//!
//! # Enforcement
//!
//! The backend enforces six properties. The workspace is the only
//! writable mount; everything else is bound read-only. Capabilities
//! are dropped to the empty set. Network is unshared unless the
//! policy allows it. The command runs under `timeout` so a runaway
//! process is terminated by the sandbox itself rather than by the
//! host. Environment variables are filtered against the forbidden-key
//! list before the command is built. A working directory that resolves
//! to a top-level system path is rejected at create time.

use std::path::PathBuf;
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use tokio::process::Command;

use crate::backend::{
    read_child_stream, sanitize_env, validate_working_dir, ExecutionRequest, ExecutionResult,
    SandboxBackend, SandboxHandle, SandboxSpec,
};
use crate::quotas::ResourceQuota;
use crate::{SandboxError, SandboxId};

/// The backend name recorded in every handle.
pub const BACKEND_NAME: &str = "bubblewrap";

/// The default execution timeout in milliseconds.
pub const DEFAULT_TIMEOUT_MS: u64 = 300_000;

/// The grace period `timeout` gives the sandboxed process between
/// SIGTERM and SIGKILL, in seconds.
const KILL_AFTER_SECONDS: u64 = 5;

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
    /// Mounts are applied in the order they appear. The root is bound
    /// read-only first, then a fresh tmpfs replaces `/tmp`, then the
    /// working directory is bound on top of the tmpfs, then `/proc` and
    /// `/dev` are mounted. Reversing the tmpfs and working-directory
    /// steps causes the working directory to be shadowed when its path
    /// is under `/tmp`.
    fn build_command(&self, handle: &SandboxHandle, request: &ExecutionRequest) -> Command {
        let mut command = Command::new(&self.binary);

        command.arg("--die-with-parent");
        command.arg("--new-session");
        command.arg("--unshare-pid");
        command.arg("--unshare-ipc");
        command.arg("--unshare-uts");
        command.arg("--cap-drop").arg("ALL");
        if !handle.network_enabled {
            command.arg("--unshare-net");
        }

        command.arg("--ro-bind").arg("/").arg("/");
        command.arg("--tmpfs").arg("/tmp");
        command.arg("--bind").arg(&handle.working_dir).arg(&handle.working_dir);
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

        let timeout_seconds = request.timeout_ms.unwrap_or(DEFAULT_TIMEOUT_MS) / 1000;
        command.arg("timeout");
        command
            .arg("--signal=TERM")
            .arg(format!("--kill-after={KILL_AFTER_SECONDS}s"))
            .arg(timeout_seconds.max(1).to_string());

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
        sanitize_env(&spec.env)?;
        let working_dir = validate_working_dir(&spec.working_dir)?;
        Ok(SandboxHandle {
            id: self.next_id(),
            backend: BACKEND_NAME.to_owned(),
            language: spec.language.clone(),
            working_dir,
            network_enabled: spec.network.is_enabled(),
            memory_mb: spec.quota.memory_mb,
            cpu_cores: spec.quota.cpu_cores,
            process_limit: spec.quota.process_limit,
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
        sanitize_env(&request.env)?;

        let timeout_ms = request.timeout_ms.unwrap_or(DEFAULT_TIMEOUT_MS);
        let timeout = Duration::from_millis(timeout_ms + KILL_AFTER_SECONDS * 1000 + 1000);
        let start = Instant::now();

        let mut command = self.build_command(handle, request);
        let mut child = command.spawn().map_err(|err| SandboxError::ExecutionFailed {
            reason: format!("spawn failed: {err}"),
        })?;

        if let Some(stdin_bytes) = &request.stdin {
            if let Some(mut stdin) = child.stdin.take() {
                use tokio::io::AsyncWriteExt;
                let _ = stdin.write_all(stdin_bytes).await;
                let _ = stdin.shutdown().await;
            }
        } else {
            drop(child.stdin.take());
        }

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
                let (stdout, stdout_truncated) = collect(stdout_task).await;
                let (stderr, stderr_truncated) = collect(stderr_task).await;
                let duration_ms = start.elapsed().as_millis() as u64;
                let exit_code = status.code().unwrap_or(-1);
                let timed_out = exit_code == 124 || exit_code == 137;
                let oom_killed = exit_code == 137 && !timed_out;
                Ok(ExecutionResult {
                    exit_code,
                    stdout,
                    stderr,
                    duration_ms,
                    timed_out,
                    oom_killed,
                    output_truncated: stdout_truncated || stderr_truncated,
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
/// tuple with no truncation flag.
async fn collect(task: Option<tokio::task::JoinHandle<(Vec<u8>, bool)>>) -> (Vec<u8>, bool) {
    match task {
        Some(handle) => handle.await.unwrap_or_default(),
        None => (Vec::new(), false),
    }
}
