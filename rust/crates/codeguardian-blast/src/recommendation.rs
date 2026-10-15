//! Proceed, review, or block.
//!
//! The recommendation is derived from the blast score with two
//! overrides. The score alone chooses the base band. Critical contract
//! violations raise the band regardless of score, because a single
//! critical violation on a small call graph should not read as
//! "proceed".
//!
//! The thresholds are heuristics, not measurements. They are chosen so
//! a small change passes without friction and a large change forces a
//! human review. The override rules are the safety net that keeps a
//! low score from hiding a high-severity signal.

/// What the pipeline should do with the target.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Recommendation {
    /// The change is small enough to proceed.
    Proceed,
    /// The change needs human review before proceeding.
    Review,
    /// The change is too risky to proceed without explicit override.
    Block,
}

impl Recommendation {
    /// Return the string the schema uses for this variant.
    pub fn as_str(self) -> &'static str {
        match self {
            Recommendation::Proceed => "proceed",
            Recommendation::Review => "review",
            Recommendation::Block => "block",
        }
    }
}

/// The score above which the base recommendation becomes `Review`.
pub const REVIEW_THRESHOLD: u32 = 31;

/// The score above which the base recommendation becomes `Block`.
pub const BLOCK_THRESHOLD: u32 = 71;

/// The number of critical contract violations that forces `Block`
/// regardless of score.
pub const CRITICAL_FORCES_BLOCK: u32 = 3;

/// Compute the recommendation from the blast score and the count of
/// critical contract violations.
///
/// The base band comes from the score. If the score is below the
/// review threshold and there is at least one critical violation, the
/// recommendation is raised to `Review`. If there are at least
/// `CRITICAL_FORCES_BLOCK` critical violations, the recommendation is
/// `Block` regardless of score.
pub fn recommend(score: u32, critical_violation_count: u32) -> Recommendation {
    let base = if score >= BLOCK_THRESHOLD {
        Recommendation::Block
    } else if score >= REVIEW_THRESHOLD {
        Recommendation::Review
    } else {
        Recommendation::Proceed
    };

    if critical_violation_count >= CRITICAL_FORCES_BLOCK {
        return Recommendation::Block;
    }
    if critical_violation_count >= 1 && base == Recommendation::Proceed {
        return Recommendation::Review;
    }
    base
}
