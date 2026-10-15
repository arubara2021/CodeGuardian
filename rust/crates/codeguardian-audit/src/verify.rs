//! Audit chain verification.
//!
//! Verification walks the log from the first entry to the last and
//! checks three invariants per line. The `payload_hash` field must
//! equal the SHA-256 of the payload's canonical bytes. The `prev_hash`
//! field must equal the preceding entry's `entry_hash`, or `GENESIS`
//! for the first line. The `entry_hash` field must equal the SHA-256
//! of `prev_hash` concatenated with `payload_hash`.
//!
//! The walk stops at the first failure. The returned summary counts
//! entries that passed before the failure, not entries in the file.

use std::path::PathBuf;

use thiserror::Error;
use tokio::fs::OpenOptions;
use tokio::io::{AsyncBufReadExt, BufReader};

use crate::hash_chain::{compute_entry_hash, compute_payload_hash};
use crate::{AuditEntry, GENESIS};

/// The result of a successful verification.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChainSummary {
    /// The number of entries that passed every check.
    pub entries_checked: usize,
    /// The `entry_hash` of the last entry, or `GENESIS` if the file was
    /// empty.
    pub head_hash: String,
}

/// Every way verification can fail.
#[derive(Debug, Error)]
pub enum ChainError {
    /// An I/O operation against the log file failed.
    #[error("audit I/O error: {0}")]
    Io(#[from] std::io::Error),

    /// A line is not valid JSON.
    #[error("line {line} is not valid JSON: {source}")]
    Parse {
        /// One-based line number in the file.
        line: usize,
        /// The underlying parse error.
        #[source]
        source: serde_json::Error,
    },

    /// A line's payload hash does not match the payload.
    #[error("line {line}: payload hash mismatch (expected {expected}, got {actual})")]
    PayloadHashMismatch {
        /// One-based line number in the file.
        line: usize,
        /// The hash recorded in the entry.
        expected: String,
        /// The hash computed from the payload.
        actual: String,
    },

    /// A line's prev_hash does not match the preceding entry's
    /// entry_hash.
    #[error("line {line}: prev_hash mismatch (expected {expected}, got {actual})")]
    PrevHashMismatch {
        /// One-based line number in the file.
        line: usize,
        /// The chain's head at this position.
        expected: String,
        /// The hash recorded in the entry.
        actual: String,
    },

    /// A line's entry hash does not match the recomputed value.
    #[error("line {line}: entry hash mismatch (expected {expected}, got {actual})")]
    EntryHashMismatch {
        /// One-based line number in the file.
        line: usize,
        /// The hash computed from prev_hash + payload_hash.
        expected: String,
        /// The hash recorded in the entry.
        actual: String,
    },
}

/// Verify the audit chain at `path`.
///
/// Returns a summary of the entries that passed. If any line fails a
/// check, the walk stops and returns the failure.
pub async fn verify_chain(path: impl Into<PathBuf>) -> Result<ChainSummary, ChainError> {
    let path: PathBuf = path.into();
    let file = match OpenOptions::new().read(true).open(&path).await {
        Ok(file) => file,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return Ok(ChainSummary {
                entries_checked: 0,
                head_hash: GENESIS.to_owned(),
            });
        }
        Err(err) => return Err(ChainError::Io(err)),
    };

    let reader = BufReader::new(file);
    let mut lines = reader.lines();

    let mut head = GENESIS.to_owned();
    let mut entries_checked: usize = 0;
    let mut line_number: usize = 0;

    while let Some(line) = lines.next_line().await? {
        line_number += 1;
        if line.trim().is_empty() {
            continue;
        }

        let entry: AuditEntry =
            serde_json::from_str(&line).map_err(|source| ChainError::Parse {
                line: line_number,
                source,
            })?;

        let computed_payload = compute_payload_hash(&entry.payload);
        if computed_payload != entry.payload_hash {
            return Err(ChainError::PayloadHashMismatch {
                line: line_number,
                expected: entry.payload_hash,
                actual: computed_payload,
            });
        }

        if entry.prev_hash != head {
            return Err(ChainError::PrevHashMismatch {
                line: line_number,
                expected: head,
                actual: entry.prev_hash,
            });
        }

        let computed_entry = compute_entry_hash(&entry.prev_hash, &entry.payload_hash);
        if computed_entry != entry.entry_hash {
            return Err(ChainError::EntryHashMismatch {
                line: line_number,
                expected: computed_entry,
                actual: entry.entry_hash,
            });
        }

        head = entry.entry_hash;
        entries_checked += 1;
    }

    Ok(ChainSummary {
        entries_checked,
        head_hash: head,
    })
}
