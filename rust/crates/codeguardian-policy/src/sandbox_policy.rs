//! Sandbox policy enforcement.
//!
//! A policy is the ceiling the pipeline accepts before it hands a
//! spec to a backend. A spec that exceeds the ceiling is rejected
//! before the backend sees it.
//!
//! The check compares three numeric fields against the policy's
//! maximum and then checks the spec's language against the policy's
//! allowed set. An empty allowed set denies every language; a policy
//! that wants to allow all languages must enumerate them. The
//! deny-by-default choice follows the safety-before-speed principle:
//! a policy with a forgotten allow list fails closed, not open.

use codeguardian_sandbox::{ResourceQuota, SandboxSpec};

use crate::PolicyError;

/// The maximum a sandbox spec may request.
///
/// `allowed_languages` is an allow list, not a deny list. An empty
/// vector denies every language. The check rejects the first field
/// that exceeds its bound and stops; a spec with two violations
/// receives one error, not two.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SandboxPolicy {
    /// The maximum quota a spec may request, field by field.
    pub max_quota: ResourceQuota,
    /// The languages a spec may run in.
    pub allowed_languages: Vec<String>,
}

impl SandboxPolicy {
    /// Return a policy that permits the given quota and languages.
    pub fn new(max_quota: ResourceQuota, allowed_languages: Vec<String>) -> Self {
        Self {
            max_quota,
            allowed_languages,
        }
    }
}

/// Return whether a spec is within the policy's bounds.
///
/// Checks the quota's three fields in order — memory, CPU, process
/// limit — then checks the language. The first failure is returned.
/// The spec is unchanged; the check does not mutate.
pub fn check_sandbox_spec(
    spec: &SandboxSpec,
    policy: &SandboxPolicy,
) -> Result<(), PolicyError> {
    if spec.quota.memory_mb > policy.max_quota.memory_mb {
        return Err(PolicyError::SandboxSpecRejected {
            reason: format!(
                "requested memory_mb {} exceeds policy maximum {}",
                spec.quota.memory_mb, policy.max_quota.memory_mb
            ),
        });
    }
    if spec.quota.cpu_cores > policy.max_quota.cpu_cores {
        return Err(PolicyError::SandboxSpecRejected {
            reason: format!(
                "requested cpu_cores {} exceeds policy maximum {}",
                spec.quota.cpu_cores, policy.max_quota.cpu_cores
            ),
        });
    }
    if spec.quota.process_limit > policy.max_quota.process_limit {
        return Err(PolicyError::SandboxSpecRejected {
            reason: format!(
                "requested process_limit {} exceeds policy maximum {}",
                spec.quota.process_limit, policy.max_quota.process_limit
            ),
        });
    }
    if !policy
        .allowed_languages
        .iter()
        .any(|language| language == &spec.language)
    {
        return Err(PolicyError::SandboxSpecRejected {
            reason: format!(
                "language '{}' is not permitted by policy",
                spec.language
            ),
        });
    }
    Ok(())
}
