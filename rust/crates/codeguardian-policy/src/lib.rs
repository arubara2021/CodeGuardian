//! Policy enforcement.
//!
//! Three policies are enforced before an action runs. The sandbox
//! policy bounds what a sandbox spec may request: memory, CPU, process
//! count, and the set of languages. The plugin policy bounds what a
//! plugin may do: filesystem access, network, process spawning, and
//! which environment variables it may read. The host check bounds a
//! quota against what the machine can actually enforce.
//!
//! Every check is pre-flight. It runs before the backend sees the spec
//! and before the plugin loader honors the plugin's manifest. A
//! rejected action never reaches the component that would carry it
//! out.
//!
//! Enforcement during execution is the sandbox's job, not this
//! crate's. Policy answers the question "may this run"; the sandbox
//! answers "did it stay inside the bounds while it ran".

pub mod plugin_permissions;
pub mod resource_limits;
pub mod sandbox_policy;

use thiserror::Error;

pub use plugin_permissions::{
    check_plugin_permissions, FilesystemLevel, PluginPermissions, PluginPolicy,
};
pub use resource_limits::{check_against_host, HostLimits};
pub use sandbox_policy::{check_sandbox_spec, SandboxPolicy};

/// Every way a policy check can fail.
///
/// Each variant maps to one Python `ErrorCode` in the bridge. The
/// bridge performs the mapping; this crate does not depend on the
/// Python error vocabulary.
#[derive(Debug, Error)]
pub enum PolicyError {
    /// A sandbox spec exceeded the policy's bounds. Maps to
    /// `SANDBOX_QUOTA_EXCEEDED`.
    #[error("sandbox spec rejected: {reason}")]
    SandboxSpecRejected {
        /// A description of the constraint that was violated.
        reason: String,
    },

    /// A plugin requested a permission the policy does not grant. Maps
    /// to `PLUGIN_PERMISSION_DENIED`.
    #[error("plugin '{plugin}' denied: {permission}")]
    PluginPermissionDenied {
        /// The plugin's name.
        plugin: String,
        /// The permission that was denied.
        permission: String,
    },

    /// A plugin manifest is not valid. Maps to
    /// `PLUGIN_MANIFEST_INVALID`.
    ///
    /// No caller in the current block. Reserved for the plugin loader
    /// in Phase 9, which validates a manifest against the schema and
    /// returns this error when it fails.
    #[error("plugin manifest for '{plugin}' invalid: {reason}")]
    ManifestInvalid {
        /// The plugin's name.
        plugin: String,
        /// A description of the schema violation.
        reason: String,
    },
}
