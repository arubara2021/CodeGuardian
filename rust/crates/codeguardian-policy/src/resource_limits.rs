//! Host resource limits.
//!
//! A quota is enforceable only if the host has the resources to
//! enforce it. A request for 128 GB of memory on a 16 GB host cannot
//! be honored, and the check should happen before the backend attempts
//! to create the sandbox.
//!
//! `HostLimits::detect` reads the host's capabilities from the Linux
//! `/proc` filesystem. On non-Linux platforms, detection fails and the
//! caller must supply limits explicitly via `HostLimits::new`.
//!
//! The invariant this module protects is that a quota which passes
//! `check_against_host` is one the host can actually enforce. A quota
//! that exceeds the host's total memory, total CPU cores, or process
//! count is rejected before the sandbox is created.

use std::fs;
use std::io;

use codeguardian_sandbox::ResourceQuota;

use crate::PolicyError;

/// What this host can enforce.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HostLimits {
    /// Total physical memory in megabytes.
    pub total_memory_mb: u64,
    /// Number of logical CPU cores.
    pub total_cpu_cores: u32,
    /// Maximum number of processes the kernel allows.
    pub max_processes: u32,
}

impl HostLimits {
    /// Construct limits from explicit values.
    pub fn new(total_memory_mb: u64, total_cpu_cores: u32, max_processes: u32) -> Self {
        Self {
            total_memory_mb,
            total_cpu_cores,
            max_processes,
        }
    }

    /// Detect the host's limits by reading `/proc`.
    ///
    /// Returns an I/O error if a needed file is missing or a parse
    /// error if a value cannot be interpreted. On platforms without
    /// `/proc` — macOS, Windows — this always fails and the caller
    /// must use `new` with explicit values.
    pub fn detect() -> io::Result<Self> {
        let total_memory_mb = read_meminfo_total_mb()?;
        let total_cpu_cores = read_cpu_cores()?;
        let max_processes = read_pid_max()?;
        Ok(Self {
            total_memory_mb,
            total_cpu_cores,
            max_processes,
        })
    }
}

/// Return whether a quota is within the host's capability.
pub fn check_against_host(quota: &ResourceQuota, limits: &HostLimits) -> Result<(), PolicyError> {
    if u64::from(quota.memory_mb) > limits.total_memory_mb {
        return Err(PolicyError::SandboxSpecRejected {
            reason: format!(
                "requested memory_mb {} exceeds host total {}",
                quota.memory_mb, limits.total_memory_mb
            ),
        });
    }
    if quota.cpu_cores > limits.total_cpu_cores {
        return Err(PolicyError::SandboxSpecRejected {
            reason: format!(
                "requested cpu_cores {} exceeds host total {}",
                quota.cpu_cores, limits.total_cpu_cores
            ),
        });
    }
    if quota.process_limit > limits.max_processes {
        return Err(PolicyError::SandboxSpecRejected {
            reason: format!(
                "requested process_limit {} exceeds host total {}",
                quota.process_limit, limits.max_processes
            ),
        });
    }
    Ok(())
}

/// Read `MemTotal` from `/proc/meminfo` and convert to megabytes.
fn read_meminfo_total_mb() -> io::Result<u64> {
    let content = fs::read_to_string("/proc/meminfo")?;
    for line in content.lines() {
        if let Some(rest) = line.strip_prefix("MemTotal:") {
            let kb = parse_first_u64(rest).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "cannot parse MemTotal value",
                )
            })?;
            return Ok(kb / 1024);
        }
    }
    Err(io::Error::new(
        io::ErrorKind::InvalidData,
        "MemTotal not found in /proc/meminfo",
    ))
}

/// Count logical CPU cores from `/proc/cpuinfo`.
fn read_cpu_cores() -> io::Result<u32> {
    let content = fs::read_to_string("/proc/cpuinfo")?;
    let count = content
        .lines()
        .filter(|line| line.starts_with("processor"))
        .count();
    if count == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "no processor entries in /proc/cpuinfo",
        ));
    }
    Ok(count as u32)
}

/// Read the kernel's maximum PID from `/proc/sys/kernel/pid_max`.
fn read_pid_max() -> io::Result<u32> {
    let content = fs::read_to_string("/proc/sys/kernel/pid_max")?;
    content.trim().parse::<u32>().map_err(|err| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("cannot parse pid_max: {err}"),
        )
    })
}

/// Return the first whitespace-separated unsigned integer in `text`.
fn parse_first_u64(text: &str) -> Option<u64> {
    text.split_whitespace().find_map(|token| token.parse().ok())
}
