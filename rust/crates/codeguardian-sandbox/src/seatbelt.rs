//! Seatbelt backend (stub in v1).
//!
//! Seatbelt is the macOS sandbox technology. It is not implemented in
//! v1 because the build target is Linux first; macOS support is a
//! future concern. This module exists so the backend selector can
//! treat Seatbelt uniformly with the implemented backends.
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
pub const BACKEND_NAME: &str = "seatbelt";

/// A Seatbelt sandbox backend. Every operation reports the backend is
/// not implemented in this build.
pub struct SeatbeltBackend;

impl Default for SeatbeltBackend {
    fn default() -> Self {
        Self::new()
    }
}

impl SeatbeltBackend {
    /// Construct a stub backend.
    pub fn new() -> Self {
        Self
    }
}

#[async_trait]
impl SandboxBackend for SeatbeltBackend {
    async fn create(&self, _spec: &SandboxSpec) -> Result<SandboxHandle, SandboxError> {
        Err(SandboxError::CreateFailed {
            reason: "seatbelt backend is not implemented in v1".to_owned(),
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
