//! Resource quotas.
//!
//! A quota is the set of limits the sandbox engine enforces on a
//! running process. The three limits correspond to the three resources
//! the design document names as mandatory: memory, CPU, and process
//! count. Disk is deliberately not part of the quota; a backend that
//! needs a disk limit derives it from the workspace mount, not from a
//! numeric field.
//!
//! The defaults match the Phase 1 configuration so a spec constructed
//! without an explicit quota behaves the same as a configuration
//! loaded from `config.toml`.
//!
//! Validation rejects values that no host can honor. The bounds are
//! wide: 64 MB to 64 GB of memory, 1 to 64 CPU cores, 1 to 65536
//! processes. A value outside the bounds is a caller mistake, not a
//! runtime condition.

use crate::SandboxError;

/// The default memory limit in megabytes.
pub const DEFAULT_MEMORY_MB: u32 = 1024;

/// The default CPU core limit.
pub const DEFAULT_CPU_CORES: u32 = 2;

/// The default process count limit.
pub const DEFAULT_PROCESS_LIMIT: u32 = 256;

/// The smallest memory limit a backend will accept, in megabytes.
pub const MIN_MEMORY_MB: u32 = 64;

/// The largest memory limit a backend will accept, in megabytes.
pub const MAX_MEMORY_MB: u32 = 65536;

/// The smallest CPU core limit a backend will accept.
pub const MIN_CPU_CORES: u32 = 1;

/// The largest CPU core limit a backend will accept.
pub const MAX_CPU_CORES: u32 = 64;

/// The smallest process count limit a backend will accept.
pub const MIN_PROCESS_LIMIT: u32 = 1;

/// The largest process count limit a backend will accept.
pub const MAX_PROCESS_LIMIT: u32 = 65536;

/// The resource limits a backend enforces on a sandbox.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ResourceQuota {
    /// Memory limit in megabytes.
    pub memory_mb: u32,
    /// CPU core limit.
    pub cpu_cores: u32,
    /// Maximum number of processes inside the sandbox.
    pub process_limit: u32,
}

impl Default for ResourceQuota {
    fn default() -> Self {
        Self {
            memory_mb: DEFAULT_MEMORY_MB,
            cpu_cores: DEFAULT_CPU_CORES,
            process_limit: DEFAULT_PROCESS_LIMIT,
        }
    }
}

impl ResourceQuota {
    /// Construct a quota from explicit values.
    pub fn new(memory_mb: u32, cpu_cores: u32, process_limit: u32) -> Self {
        Self {
            memory_mb,
            cpu_cores,
            process_limit,
        }
    }

    /// Return whether the quota's values are within the accepted range.
    pub fn is_valid(&self) -> bool {
        self.validate().is_ok()
    }

    /// Validate the quota against the accepted bounds.
    ///
    /// Returns `SandboxError::CreateFailed` with a description of the
    /// violated constraint. The error variant is `CreateFailed`
    /// because a quota that fails validation prevents a sandbox from
    /// being created; there is no separate "invalid quota" error
    /// because the caller can act on the description string.
    pub fn validate(&self) -> Result<(), SandboxError> {
        if self.memory_mb < MIN_MEMORY_MB || self.memory_mb > MAX_MEMORY_MB {
            return Err(SandboxError::CreateFailed {
                reason: format!(
                    "memory_mb {} is outside the accepted range [{}, {}]",
                    self.memory_mb, MIN_MEMORY_MB, MAX_MEMORY_MB
                ),
            });
        }
        if self.cpu_cores < MIN_CPU_CORES || self.cpu_cores > MAX_CPU_CORES {
            return Err(SandboxError::CreateFailed {
                reason: format!(
                    "cpu_cores {} is outside the accepted range [{}, {}]",
                    self.cpu_cores, MIN_CPU_CORES, MAX_CPU_CORES
                ),
            });
        }
        if self.process_limit < MIN_PROCESS_LIMIT || self.process_limit > MAX_PROCESS_LIMIT {
            return Err(SandboxError::CreateFailed {
                reason: format!(
                    "process_limit {} is outside the accepted range [{}, {}]",
                    self.process_limit, MIN_PROCESS_LIMIT, MAX_PROCESS_LIMIT
                ),
            });
        }
        Ok(())
    }
}
