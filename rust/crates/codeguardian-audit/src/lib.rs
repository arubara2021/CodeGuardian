//! Append-only audit log.
//!
//! The audit log is the tamper-evidence layer of CodeGuardian. Every
//! entry records what happened, when, and by which component. The
//! chain of entries cannot be modified without breaking every
//! subsequent entry's hash.
//!
//! # Chain structure
//!
//! Each entry carries three hashes. `payload_hash` is the SHA-256 of
//! the entry's payload, serialized canonically. `prev_hash` is the
//! entry hash of the entry before it, or the literal string `GENESIS`
//! for the first entry. `entry_hash` is the SHA-256 of the
//! concatenation of `prev_hash` and `payload_hash`.
//!
//! # Canonical payload
//!
//! Hashing a payload requires a byte representation that does not
//! depend on map iteration order. `hash_chain::canonical_json_bytes`
//! produces that representation: JSON with object keys sorted
//! lexicographically, no whitespace, and standard string escaping.
//! Two payloads that are structurally equal produce identical bytes.
//!
//! # Concurrency
//!
//! The crate assumes a single writer per file. No file locking is
//! performed; two concurrent writers would corrupt the chain silently.
//! Multi-writer coordination is tracked as a known gap.

pub mod export;
pub mod hash_chain;
pub mod verify;
pub mod writer;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

pub use export::{export_json_array, export_jsonl};
pub use hash_chain::{
    canonical_json_bytes, compute_entry_hash, compute_payload_hash, hex_encode, sha256_hex,
};
pub use verify::{verify_chain, ChainError, ChainSummary};
pub use writer::AuditWriter;

/// The value a first entry's `prev_hash` field carries.
///
/// It is the literal string `GENESIS`, not a hash. Verification
/// recognizes the string as the start of every chain.
pub const GENESIS: &str = "GENESIS";

/// One entry in the audit log.
///
/// The `payload` carries the entry's data as untyped JSON. The audit
/// crate owns the chain, not the payload schema; the caller validates
/// the payload against `schemas/audit-entry.json` before appending.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AuditEntry {
    /// The entry's data.
    pub payload: Value,
    /// The hex-encoded SHA-256 of the payload's canonical bytes.
    pub payload_hash: String,
    /// The `entry_hash` of the preceding entry, or `GENESIS`.
    pub prev_hash: String,
    /// The hex-encoded SHA-256 of `prev_hash` concatenated with
    /// `payload_hash`.
    pub entry_hash: String,
}

/// Every way an audit operation can fail.
#[derive(Debug, Error)]
pub enum AuditError {
    /// An I/O operation against the log file failed.
    #[error("audit I/O error: {0}")]
    Io(#[from] std::io::Error),

    /// A payload could not be serialized to JSON.
    #[error("audit serialization error: {0}")]
    Serialization(#[from] serde_json::Error),
}
