//! Blast score computation.
//!
//! The blast score is a single integer from 0 to 100 that summarizes
//! how much risk a change to the target carries. It is a weighted sum
//! of four counts: direct callers, transitive callers, contract
//! violations, and coverage gaps.
//!
//! The weights are heuristics. They were chosen so that a single
//! contract violation dominates a small direct-caller count and a
//! large call graph still reaches the review band without any
//! violation. They have not been calibrated against real repository
//! data. Calibration is a future task; until then the weights live in
//! this file as named constants so the change is one line and one
//! review.

/// Weight applied to each direct caller.
pub const W_DIRECT_CALLER: u32 = 2;

/// Weight applied to each transitive caller.
pub const W_TRANSITIVE_CALLER: u32 = 1;

/// Weight applied to each contract violation.
pub const W_CONTRACT_VIOLATION: u32 = 8;

/// Weight applied to each coverage gap.
pub const W_COVERAGE_GAP: u32 = 3;

/// Compute the blast score from the four counts.
///
/// The result saturates at 100. Every multiplication and addition uses
/// saturating arithmetic so an extreme input cannot overflow. A score
/// of 0 means a target with no callers and no violations; a score of
/// 100 means the cap was reached.
pub fn compute_blast_score(
    direct_callers: u32,
    transitive_callers: u32,
    contract_violations: u32,
    coverage_gaps: u32,
) -> u32 {
    let raw = direct_callers
        .saturating_mul(W_DIRECT_CALLER)
        .saturating_add(transitive_callers.saturating_mul(W_TRANSITIVE_CALLER))
        .saturating_add(contract_violations.saturating_mul(W_CONTRACT_VIOLATION))
        .saturating_add(coverage_gaps.saturating_mul(W_COVERAGE_GAP));
    raw.min(100)
}
