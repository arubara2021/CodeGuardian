//! Plugin permission enforcement.
//!
//! A plugin declares what it needs. A policy declares what the host
//! grants. The check rejects any plugin whose declared needs exceed
//! the grant, field by field. Every field is checked independently,
//! and the first failure is returned.
//!
//! The permission fields mirror the `permissions` object in
//! `schemas/plugin-manifest.json` exactly. `filesystem` is one of four
//! levels. `network` and `process_spawn` are booleans. `env_read` is a
//! list of environment variable names the plugin may read.
//!
//! The filesystem levels are ordered so that a single comparison
//! answers "does the request exceed the grant?". A request for a
//! higher level than the policy allows is rejected; a request for an
//! equal or lower level is accepted.

use crate::PolicyError;

/// The level of filesystem access a plugin requests or a policy
/// grants.
///
/// Variants are ordered from least to most permissive. The derived
/// `Ord` places `None` before `SandboxOnly`, `SandboxOnly` before
/// `ReadOnly`, and `ReadOnly` before `Full`. The ordering is what
/// makes "requested exceeds granted" a single comparison.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum FilesystemLevel {
    /// No filesystem access.
    None,
    /// Read and write inside the sandbox's workspace only.
    SandboxOnly,
    /// Read anywhere, write nowhere outside the sandbox.
    ReadOnly,
    /// Read and write anywhere.
    Full,
}

impl FilesystemLevel {
    /// Return the string the schema uses for this level.
    pub fn as_str(self) -> &'static str {
        match self {
            FilesystemLevel::None => "none",
            FilesystemLevel::SandboxOnly => "sandbox_only",
            FilesystemLevel::ReadOnly => "read_only",
            FilesystemLevel::Full => "full",
        }
    }
}

/// What a plugin declares it needs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PluginPermissions {
    /// The level of filesystem access the plugin requests.
    pub filesystem: FilesystemLevel,
    /// Whether the plugin needs network access.
    pub network: bool,
    /// Whether the plugin spawns subprocesses.
    pub process_spawn: bool,
    /// The environment variable names the plugin reads.
    pub env_read: Vec<String>,
}

/// What the host grants to a plugin.
///
/// `allowed_env_keys` is an allow list. A plugin that reads an
/// environment variable not in the list is rejected. An empty list
/// grants access to no variables.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PluginPolicy {
    /// The highest filesystem level any plugin may request.
    pub allowed_filesystem: FilesystemLevel,
    /// Whether plugins may use the network.
    pub allow_network: bool,
    /// Whether plugins may spawn subprocesses.
    pub allow_process_spawn: bool,
    /// The environment variable names plugins may read.
    pub allowed_env_keys: Vec<String>,
}

/// Return whether a plugin's declared permissions are within the
/// policy's grant.
///
/// Checks the filesystem level first, then network, then process
/// spawning, then every environment key the plugin declares. The
/// first failure is returned. The plugin's permissions are unchanged;
/// the check does not mutate.
pub fn check_plugin_permissions(
    plugin_name: &str,
    permissions: &PluginPermissions,
    policy: &PluginPolicy,
) -> Result<(), PolicyError> {
    if permissions.filesystem > policy.allowed_filesystem {
        return Err(PolicyError::PluginPermissionDenied {
            plugin: plugin_name.to_owned(),
            permission: format!(
                "filesystem: requested '{}', policy allows '{}'",
                permissions.filesystem.as_str(),
                policy.allowed_filesystem.as_str()
            ),
        });
    }
    if permissions.network && !policy.allow_network {
        return Err(PolicyError::PluginPermissionDenied {
            plugin: plugin_name.to_owned(),
            permission: "network: requested true, policy allows false".to_owned(),
        });
    }
    if permissions.process_spawn && !policy.allow_process_spawn {
        return Err(PolicyError::PluginPermissionDenied {
            plugin: plugin_name.to_owned(),
            permission: "process_spawn: requested true, policy allows false".to_owned(),
        });
    }
    for key in &permissions.env_read {
        if !policy.allowed_env_keys.iter().any(|allowed| allowed == key) {
            return Err(PolicyError::PluginPermissionDenied {
                plugin: plugin_name.to_owned(),
                permission: format!("env_read: '{key}' is not in the allowed set"),
            });
        }
    }
    Ok(())
}
