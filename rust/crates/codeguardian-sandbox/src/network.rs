//! Network isolation.
//!
//! A sandbox has one of two network states. It is sealed, in which
//! case no outbound or inbound traffic is possible. Or it is open for
//! dependency resolution, in which case the backend permits network
//! access so the sandbox can fetch packages.
//!
//! The design document splits execution into two phases: a dependency
//! resolution phase where the network is on, and an execution phase
//! where it is off. This module makes that split explicit. A backend
//! that ignores the policy violates the isolation contract.
//!
//! No per-host allow list and no DNS restriction is modeled. The
//! backends that exist in v1 either turn the network on or off; more
//! granular policy is a future concern.

/// The network state of a sandbox.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum NetworkPhase {
    /// No network access. This is the default and the state used
    /// during execution.
    DenyAll,
    /// Network access is permitted so the sandbox can fetch
    /// dependencies. Used only during the dependency resolution phase.
    AllowForDependencies,
}

/// The network policy a backend enforces on a sandbox.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NetworkPolicy {
    /// The phase the sandbox is in.
    pub phase: NetworkPhase,
}

impl Default for NetworkPolicy {
    fn default() -> Self {
        Self::deny_all()
    }
}

impl NetworkPolicy {
    /// Return a policy that denies all network access.
    pub fn deny_all() -> Self {
        Self {
            phase: NetworkPhase::DenyAll,
        }
    }

    /// Return a policy that permits network access for dependency
    /// resolution.
    pub fn allow_for_dependencies() -> Self {
        Self {
            phase: NetworkPhase::AllowForDependencies,
        }
    }

    /// Return whether this policy permits any network access.
    pub fn is_enabled(&self) -> bool {
        matches!(self.phase, NetworkPhase::AllowForDependencies)
    }
}
