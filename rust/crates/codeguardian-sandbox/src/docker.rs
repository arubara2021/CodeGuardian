//! Docker backend.
//!
//! Docker is the v1 fallback. It is used where Bubblewrap is not
//! available — macOS, Windows, and CI environments — and its isolation
//! is weaker than Bubblewrap's because the daemon runs outside the
//! sandbox's trust boundary.
//!
//! # Image selection
//!
//! The caller supplies the container image via `SandboxSpec::image`.
//! The backend does not maintain a language-to-image map; any image
//! the caller names runs, whether it is a CodeGuardian-provided image
//! for Python or TypeScript, an upstream base image such as
//! `python:3.12-slim` or `node:22-alpine`, or a project-specific image
//! built from a `Dockerfile`. `create` fails with `ImageUnavailable` if
//! no image is given or if the image is not present locally.
//!
//! No pull is attempted. A host that has Docker but not the requested
//! image returns the error, and the caller decides whether to build,
//! pull, or supply a different image. Implicit pulls would make
//! `create` unbounded in time and dependent on the network.
//!
//! # Availability check
//!
//! `create` performs two probes before returning a handle. The first,
//! `docker info`, proves the CLI can reach a running daemon. The
//! second, `docker image inspect`, proves the requested image exists
//! locally. Either probe failing produces a distinct error that names
//! what is actually wrong.
//!
//! # Enforcement
//!
//! The backend passes the handle's quota directly to `docker run`:
//! `--memory`, `--cpus`, and `--pids-limit` all come from the handle
//! captured at create time. Capabilities are dropped to the empty set
//! with `--cap-drop ALL`. Network is disabled with `--network none`
//! unless the policy allows it.

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

    /// Verify that the docker CLI can reach a running daemon.
    async fn verify_available(&self) -> Result<(), SandboxError> {
        let output = Command::new(&self.binary)
            .args(["info", "--format", "{{.ServerVersion}}"])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .output()
            .await
            .map_err(|err| SandboxError::CreateFailed {
                reason: format!("docker binary not found: {err}"),
            })?;

        if output.status.success() {
            return Ok(());
        }

        let stderr = String::from_utf8_lossy(&output.stderr);
        let stdout = String::from_utf8_lossy(&output.stdout);
        let combined = format!("{stderr}\n{stdout}");
        let combined_lower = combined.to_ascii_lowercase();

        let message = if combined_lower.contains("cannot connect to the docker daemon")
            || combined_lower.contains("is the docker daemon running")
            || combined_lower.contains("error during connect")
        {
            format!("docker daemon not reachable: {}", first_non_empty_line(&combined))
        } else if combined_lower.contains("wsl")
            || combined_lower.contains("could not be found")
            || combined_lower.contains("not found in this")
        {
            format!("docker is not available on this host: {}", first_non_empty_line(&combined))
        } else {
            format!("docker CLI exited with status {}: {}", output.status, first_non_empty_line(&combined))
        };

        Err(SandboxError::CreateFailed { reason: message })
    }

    /// Verify that the given image is present locally.
    async fn verify_image(&self, image: &str) -> Result<(), SandboxError> {
        let output = Command::new(&self.binary)
            .args(["image", "inspect", image])
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .output()
            .await
            .map_err(|err| SandboxError::CreateFailed {
                reason: format!("docker binary not found: {err}"),
            })?;

        if output.status.success() {
            return Ok(());
        }

        Err(SandboxError::ImageUnavailable {
            image: image.to_owned(),
        })
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

        let image = match &spec.image {
            Some(img) => img.clone(),
            None => {
                return Err(SandboxError::ImageUnavailable {
                    image: format!(
                        "(no image specified for language '{}'); the docker backend \
                         requires an explicit image",
                        spec.language
                    ),
                });
            }
        };

        self.verify_available().await?;
        self.verify_image(&image).await?;

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
            image: Some(image),
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

        let image = match &handle.image {
            Some(img) => img.clone(),
            None => {
                return Err(SandboxError::ImageUnavailable {
                    image: format!(
                        "(no image recorded on handle for language '{}')",
                        handle.language
                    ),
                });
            }
        };

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
                let _ = child.start_kill();
                let _ = child.wait().await;
                let _ = stop_container(&self.binary, &container_name).await;
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

    fn supports_language(&self, _language: &str) -> bool {
        true
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

/// Stop a running container, awaiting the kill and escalating to
/// `docker rm -f` if the kill does not complete in five seconds.
///
/// The previous implementation spawned `docker kill` and moved on
/// without waiting for it. If the kill subprocess failed to start, or
/// if the Docker daemon did not act on it quickly enough, the
/// container kept running with `--rm` unable to help because the
/// container was still alive. Awaiting the kill with a bounded
/// timeout, then falling back to a forced remove, is the only way to
/// guarantee that a timed-out container is actually stopped.
async fn stop_container(binary: &PathBuf, name: &str) -> std::io::Result<()> {
    let kill = Command::new(binary)
        .args(["kill", name])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    match tokio::time::timeout(Duration::from_secs(5), kill).await {
        Ok(Ok(status)) if status.success() => Ok(()),
        _ => {
            let _ = Command::new(binary)
                .args(["rm", "-f", name])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .await;
            Ok(())
        }
    }
}

/// Return the first non-empty line of `text`, trimmed.
fn first_non_empty_line(text: &str) -> String {
    text.lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .unwrap_or("")
        .to_owned()
}
