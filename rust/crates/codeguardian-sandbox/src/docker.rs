//! Docker backend.
//!
//! Docker is the v1 fallback. It is used where Bubblewrap is not
//! available — macOS, Windows, and CI environments — and its isolation
//! is weaker than Bubblewrap's because the daemon runs outside the
//! sandbox's trust boundary.
//!
//! # Enforcement
//!
//! The backend passes the handle's quota directly to `docker run`:
//! `--memory`, `--cpus`, and `--pids-limit` all come from the handle
//! captured at create time. Capabilities are dropped to the empty set
//! with `--cap-drop ALL`. Network is disabled with `--network none`
//! unless the policy allows it. The working directory is validated at
//! create time against the same forbidden-path list the Bubblewrap
//! backend uses.
//!
//! # Timeout
//!
//! The Rust-side `tokio::time::timeout` covers the timeout. When it
//! fires, the backend runs `docker kill <name>` to stop the container
//! and then kills the `docker run` CLI process. The container name is
//! a per-call unique string so two concurrent executions cannot
//! collide.

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
pub const BACKEND_NAME: &str = "docker";

/// The default execution timeout in milliseconds.
pub const DEFAULT_TIMEOUT_MS: u64 = 300_000;

/// The languages this backend can execute.
const SUPPORTED_LANGUAGES: &[&str] = &["python", "typescript"];

/// A Docker sandbox backend.
pub struct DockerBackend {
    binary: PathBuf,
    counter: AtomicU64,
}

impl Default for DockerBackend {
    fn default() -> Self {
        Self::new()
    }
}

impl DockerBackend {
    /// Construct a backend that invokes `docker` from `PATH`.
    pub fn new() -> Self {
        Self {
            binary: PathBuf::from("docker"),
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
        SandboxId::new(format!("docker-{n}"))
    }

    /// Return the image name for a language, or `None` if unknown.
    pub fn image_for(language: &str) -> Option<String> {
        match language {
            "python" => Some("codeguardian/python-sandbox:latest".to_owned()),
            "typescript" => Some("codeguardian/typescript-sandbox:latest".to_owned()),
            _ => None,
        }
    }

    /// Return a container name unique to this execution.
    fn container_name(handle: &SandboxHandle, salt: u64) -> String {
        format!("codeguardian-{}-{}", handle.id.as_str(), salt)
    }

    /// Build the `docker run` invocation for a request.
    fn build_command(
        &self,
        image: &str,
        container_name: &str,
        handle: &SandboxHandle,
        request: &ExecutionRequest,
    ) -> Command {
        let mut command = Command::new(&self.binary);
        command.arg("run");
        command.arg("--rm");
        command.arg("--interactive");
        command.arg("--name").arg(container_name);

        if !handle.network_enabled {
            command.arg("--network").arg("none");
        }

        command
            .arg("--memory")
            .arg(format!("{}m", handle.memory_mb));
        command
            .arg("--cpus")
            .arg(handle.cpu_cores.to_string());
        command
            .arg("--pids-limit")
            .arg(handle.process_limit.to_string());
        command.arg("--cap-drop").arg("ALL");

        command
            .arg("-v")
            .arg(format!(
                "{}:{}",
                handle.working_dir.display(),
                handle.working_dir.display()
            ));
        command
            .arg("-w")
            .arg(handle.working_dir.display().to_string());

        for (key, value) in &request.env {
            command.arg("-e").arg(format!("{key}={value}"));
        }

        command.arg(image);
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
impl SandboxBackend for DockerBackend {
    async fn create(&self, spec: &SandboxSpec) -> Result<SandboxHandle, SandboxError> {
        spec.quota.validate()?;
        sanitize_env(&spec.env)?;
        if Self::image_for(&spec.language).is_none() {
            return Err(SandboxError::ImageUnavailable {
                image: spec.language.clone(),
            });
        }
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
        let image = Self::image_for(&handle.language).ok_or_else(|| {
            SandboxError::ImageUnavailable {
                image: handle.language.clone(),
            }
        })?;

        let timeout_ms = request.timeout_ms.unwrap_or(DEFAULT_TIMEOUT_MS);
        let timeout = Duration::from_millis(timeout_ms);
        let start = Instant::now();
        let salt = self.counter.fetch_add(1, Ordering::Relaxed);
        let container_name = Self::container_name(handle, salt);

        let mut command = self.build_command(&image, &container_name, handle, request);
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
                let oom_killed = exit_code == 137;
                Ok(ExecutionResult {
                    exit_code,
                    stdout,
                    stderr,
                    duration_ms,
                    timed_out: false,
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
                // Stop the container before killing the CLI. The daemon
                // stops the container asynchronously; the CLI exit
                // releases the caller either way.
                let _ = Command::new(&self.binary)
                    .args(["kill", &container_name])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .spawn();
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
