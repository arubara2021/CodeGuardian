//! Docker backend.
//!
//! Docker is the v1 fallback. It is used where Bubblewrap is not
//! available — macOS, Windows, and CI environments — and its isolation
//! is weaker than Bubblewrap's because the daemon runs outside the
//! sandbox's trust boundary. The trade-off is portability.
//!
//! `create` does no work beyond validation. The image is named per
//! language. Every `execute` runs `docker run --rm`, which creates a
//! fresh container, runs the command, and removes the container when
//! it exits. `destroy` is a no-op.
//!
//! The invariant this module protects is that no container outlives
//! the `execute` call that created it. The `--rm` flag is mandatory;
//! a change that removes it leaks containers and violates the design's
//! "no unbounded growth" rule.

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
pub const BACKEND_NAME: &str = "docker";

/// The default execution timeout in milliseconds when a request does
/// not supply one.
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

    /// Build the `docker run` invocation for a request.
    fn build_command(
        &self,
        image: &str,
        handle: &SandboxHandle,
        request: &ExecutionRequest,
    ) -> Command {
        let mut command = Command::new(&self.binary);
        command.arg("run");
        command.arg("--rm");
        command.arg("--interactive");

        if !handle.network_enabled {
            command.arg("--network").arg("none");
        }

        command
            .arg("--memory")
            .arg(format!("{}m", handle.working_dir_capacity_mb_hint()));
        command.arg("--cpus").arg("2");
        command.arg("--pids-limit").arg("256");

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
        if Self::image_for(&spec.language).is_none() {
            return Err(SandboxError::ImageUnavailable {
                image: spec.language.clone(),
            });
        }
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
        let image = Self::image_for(&handle.language).ok_or_else(|| {
            SandboxError::ImageUnavailable {
                image: handle.language.clone(),
            }
        })?;

        let timeout_ms = request.timeout_ms.unwrap_or(DEFAULT_TIMEOUT_MS);
        let timeout = Duration::from_millis(timeout_ms);
        let start = Instant::now();

        let mut command = self.build_command(&image, handle, request);
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
                let oom_killed = exit_code == 137;
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
/// vector.
async fn collect(task: Option<tokio::task::JoinHandle<Vec<u8>>>) -> Vec<u8> {
    match task {
        Some(handle) => handle.await.unwrap_or_default(),
        None => Vec::new(),
    }
}

/// Helper trait method placeholder — provides the memory hint the
/// Docker backend uses for `--memory`. The value comes from the
/// handle's language; the memory limit comes from the spec's quota at
/// creation and is not carried on the handle, so the backend uses the
/// default. A future block that persists the spec with the handle will
/// replace this.
trait WorkingDirCapacityHint {
    fn working_dir_capacity_mb_hint(&self) -> u32;
}

impl WorkingDirCapacityHint for SandboxHandle {
    fn working_dir_capacity_mb_hint(&self) -> u32 {
        crate::quotas::DEFAULT_MEMORY_MB
    }
}
