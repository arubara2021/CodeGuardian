//! Sandbox engine.
//!
//! The sandbox is the trust boundary. Nothing the pipeline does not
//! control runs outside it. This crate defines the trait every backend
//! implements and the data types that cross between the caller and the
//! backend: a spec for creation, a handle for identity, a request for
//! execution, and a result for the outcome.
//!
//! The crate also provides the four v1 backends — Bubblewrap and
//! Docker as working implementations, Seatbelt and Firecracker as
//! stubs that report they are not implemented — and a warm pool that
//! amortizes sandbox creation across a batch of executions.
//!
//! The invariant this crate protects is that every method that returns
//! a sandbox-relative error uses `SandboxError`, never a bare
//! `std::io::Error`. I/O failures are wrapped with `From`, so a caller
//! can pattern-match on the failure mode without inspecting an
//! `io::ErrorKind`.

pub mod backend;
pub mod bubblewrap;
pub mod docker;
pub mod firecracker;
pub mod network;
pub mod pool;
pub mod quotas;
pub mod seatbelt;

use thiserror::Error;

pub use backend::{
    ExecutionRequest, ExecutionResult, SandboxBackend, SandboxHandle, SandboxSpec,
};
pub use bubblewrap::BubblewrapBackend;
pub use docker::DockerBackend;
pub use firecracker::FirecrackerBackend;
pub use network::{NetworkPhase, NetworkPolicy};
pub use pool::WarmPool;
pub use quotas::ResourceQuota;
pub use seatbelt::SeatbeltBackend;

/// A sandbox identifier.
///
/// The id is opaque to the caller. It is produced by the backend at
/// creation and passed back on every subsequent operation. A backend
/// chooses the format; nothing in this crate or any caller parses it.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct SandboxId(String);

impl SandboxId {
    /// Construct a new id from any string-like value.
    pub fn new(id: impl Into<String>) -> Self {
        Self(id.into())
    }

    /// Return the id as a string slice.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for SandboxId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// Every way a sandbox operation can fail.
///
/// Each variant maps to one Python `ErrorCode` in the bridge. The
/// bridge performs the mapping; this crate does not depend on the
/// Python error vocabulary.
#[derive(Debug, Error)]
pub enum SandboxError {
    /// The sandbox could not be created. Carries the backend's reason
    /// for the failure.
    #[error("sandbox create failed: {reason}")]
    CreateFailed {
        /// A human-readable description of what went wrong.
        reason: String,
    },

    /// The sandbox image or environment is not available on this host.
    #[error("sandbox image unavailable: {image}")]
    ImageUnavailable {
        /// The image identifier that could not be found.
        image: String,
    },

    /// A resource quota was exceeded.
    #[error("resource quota exceeded: {resource}")]
    QuotaExceeded {
        /// The name of the resource, e.g. `"memory"` or `"pids"`.
        resource: String,
    },

    /// Execution did not complete within its timeout.
    #[error("sandbox execution timed out after {timeout_ms} ms")]
    Timeout {
        /// The timeout that was exceeded, in milliseconds.
        timeout_ms: u64,
    },

    /// The sandboxed process ran out of memory and was killed.
    #[error("sandbox process ran out of memory")]
    OutOfMemory,

    /// Execution failed for a reason other than a quota or a timeout.
    #[error("sandbox execution failed: {reason}")]
    ExecutionFailed {
        /// A human-readable description of what went wrong.
        reason: String,
    },

    /// A quota field is outside the range the backend accepts.
    #[error("quota field '{field}' value {value} is outside the range [{min}, {max}]")]
    QuotaOutOfRange {
        /// The field name, e.g. `"memory_mb"`.
        field: String,
        /// The value that was rejected.
        value: u32,
        /// The minimum accepted value.
        min: u32,
        /// The maximum accepted value.
        max: u32,
    },

    /// An environment variable name is a code-injection vector and
    /// cannot be set inside the sandbox.
    #[error(
        "environment variable '{key}' is a code-injection vector and \
         cannot be set inside the sandbox"
    )]
    ForbiddenEnvKey {
        /// The key that was rejected.
        key: String,
    },

    /// The working directory resolves to a protected system path or
    /// one of its subdirectories.
    #[error("working directory '{path}' is a protected system path and cannot be used")]
    WorkingDirForbidden {
        /// The path that was rejected.
        path: String,
    },

    /// The working directory does not exist, is not a directory, or
    /// cannot be resolved.
    #[error("working directory '{path}' is not usable: {reason}")]
    WorkingDirInvalid {
        /// The path that was rejected.
        path: String,
        /// A description of why it is not usable.
        reason: String,
    },

    /// The handle names a sandbox that does not exist.
    #[error("sandbox not found: {id}")]
    NotFound {
        /// The id that was not found.
        id: String,
    },

    /// An I/O operation against the sandbox failed.
    #[error("sandbox I/O error: {0}")]
    Io(#[from] std::io::Error),
}
