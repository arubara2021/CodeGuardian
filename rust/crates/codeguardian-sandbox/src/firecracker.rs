//! Firecracker backend (stub in v1).
//!
//! Firecracker provides hardware-level isolation with a microVM per
//! execution. It is not implemented in v1 because it requires KVM
//! access and a rootfs management layer; the v1 build targets
//! single-user Linux where Bubblewrap is the correct choice. This
//! module exists so the backend selector can treat Firecracker
//! uniformly with the implemented backends.
//!
//! `create` returns `CreateFailed` with a description of the missing
//! implementation. `execute` and `destroy` return `NotFound` because
//! they can never receive a valid handle: no handle is ever produced.
//! `supports_language` returns false for every language.

use async_trait::async_trait;

use crate::backend::{
    ExecutionRequest, ExecutionResult, SandboxBackend, SandboxHandle, SandboxSpec,
};
use crate::quotas::ResourceQuota;
use crate::SandboxError;

/// The backend name that would be recorded in a handle, if a handle
/// could be produced.
pub const BACKEND_NAME: &str = "firecracker";

/// A Firecracker sandbox backend. Every operation reports the backend
/// is not implemented in this build.
pub struct FirecrackerBackend;

impl Default for FirecrackerBackend {
    fn default() -> Self {
        Self::new()
    }
}

impl FirecrackerBackend {
    /// Construct a stub backend.
    pub fn new() -> Self {
        Self
    }
}

#[async_trait]
impl SandboxBackend for FirecrackerBackend {
    async fn create(&self, _spec: &SandboxSpec) -> Result<SandboxHandle, SandboxError> {
        Err(SandboxError::CreateFailed {
            reason: "firecracker backend is not implemented in v1".to_owned(),
        })
    }

    async fn execute(
        &self,
        handle: &SandboxHandle,
        _request: &ExecutionRequest,
    ) -> Result<ExecutionResult, SandboxError> {
        Err(SandboxError::NotFound {
            id: handle.id.as_str().to_owned(),
        })
    }

    async fn destroy(&self, handle: SandboxHandle) -> Result<(), SandboxError> {
        Err(SandboxError::NotFound {
            id: handle.id.as_str().to_owned(),
        })
    }

    fn supports_language(&self, _language: &str) -> bool {
        false
    }

    fn resource_limits(&self) -> ResourceQuota {
        ResourceQuota::default()
    }
}
