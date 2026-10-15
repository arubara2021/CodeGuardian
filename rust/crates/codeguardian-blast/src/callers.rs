//! Direct and transitive caller resolution.
//!
//! Direct callers are the functions with a `Call` edge into the target.
//! Transitive callers are every function reachable by walking reverse
//! `Call` edges from the direct callers, up to a cap. The cap exists
//! because a utility function called from every corner of a large
//! repository can have tens of thousands of transitive callers, and
//! the resulting report would be unusable at that size.
//!
//! The walk uses a visited set so a cyclic call graph terminates. The
//! target itself is never reported as its own caller, even if a mutual
//! recursion exists.
//!
//! The invariant this module protects is that every returned `Caller`
//! names a node that exists in the graph. A dangling edge is skipped,
//! not reported.

use std::collections::{HashSet, VecDeque};

use codeguardian_kernel::{CodePropertyGraph, EdgeKind};

use crate::qualified_name_of;

/// The maximum number of transitive callers returned.
///
/// Chosen so a report on a large repository stays within a size that a
/// CLI can render and a human can review. When the cap is reached, the
/// returned vector holds exactly this many entries and the truncated
/// flag is set.
pub const MAX_TRANSITIVE_CALLERS: usize = 2000;

/// Whether a caller was reached directly or through intermediate
/// functions.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum CallKind {
    /// The caller has a direct `Call` edge into the target.
    Direct,
    /// The caller reaches the target through one or more intermediate
    /// functions.
    Transitive,
}

/// One function that calls the target, directly or transitively.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Caller {
    /// The CPG node id of the calling function.
    pub node_id: u32,
    /// The caller's qualified name.
    pub qualified_name: String,
    /// The path of the file the caller is defined in.
    pub file_path: String,
    /// One-based line number of the caller's definition.
    pub line: u32,
    /// How many call-graph steps separate the caller from the target.
    /// Direct callers have depth 1.
    pub depth: u32,
    /// Whether the caller was reached directly or transitively.
    pub call_kind: CallKind,
}

/// Return every function with a direct `Call` edge into the target.
///
/// The result is sorted by node id so the output is deterministic. A
/// caller that appears twice through a duplicate edge is reported once.
pub fn compute_direct_callers(cpg: &CodePropertyGraph, target_node_id: u32) -> Vec<Caller> {
    let mut seen: HashSet<u32> = HashSet::new();
    let mut result: Vec<Caller> = Vec::new();

    for &edge_id in cpg.edges_to(target_node_id) {
        let edge = match cpg.edge(edge_id) {
            Some(e) => e,
            None => continue,
        };
        if edge.kind != EdgeKind::Call {
            continue;
        }
        if !seen.insert(edge.src) {
            continue;
        }
        let caller = match build_caller(cpg, edge.src, 1, CallKind::Direct) {
            Some(c) => c,
            None => continue,
        };
        result.push(caller);
    }

    result.sort_by_key(|caller| caller.node_id);
    result
}

/// Return every function reachable from the target by walking reverse
/// `Call` edges, up to `cap` entries.
///
/// The result is sorted by `(depth, node_id)` so the output is
/// deterministic. The returned bool is true when the walk stopped
/// because `cap` was reached and there were more callers to visit.
///
/// `cap` is not clamped. A caller that passes a cap of zero receives an
/// empty vector and a truncated flag of false.
pub fn compute_transitive_callers(
    cpg: &CodePropertyGraph,
    target_node_id: u32,
    cap: usize,
) -> (Vec<Caller>, bool) {
    if cap == 0 {
        return (Vec::new(), false);
    }

    let mut visited: HashSet<u32> = HashSet::new();
    visited.insert(target_node_id);

    let mut queue: VecDeque<(u32, u32)> = VecDeque::new();
    for caller in compute_direct_callers(cpg, target_node_id) {
        if visited.insert(caller.node_id) {
            queue.push_back((caller.node_id, 1));
        }
    }

    let mut result: Vec<Caller> = Vec::new();
    let mut truncated = false;

    while let Some((node_id, depth)) = queue.pop_front() {
        if result.len() >= cap {
            truncated = true;
            break;
        }

        let caller = match build_caller(cpg, node_id, depth, CallKind::Transitive) {
            Some(c) => c,
            None => continue,
        };
        result.push(caller);

        for &edge_id in cpg.edges_to(node_id) {
            let edge = match cpg.edge(edge_id) {
                Some(e) => e,
                None => continue,
            };
            if edge.kind != EdgeKind::Call {
                continue;
            }
            if visited.insert(edge.src) {
                queue.push_back((edge.src, depth + 1));
            }
        }
    }

    result.sort_by_key(|caller| (caller.depth, caller.node_id));
    (result, truncated)
}

/// Build a `Caller` from a node id. Returns `None` if the node does not
/// exist.
fn build_caller(
    cpg: &CodePropertyGraph,
    node_id: u32,
    depth: u32,
    call_kind: CallKind,
) -> Option<Caller> {
    let node = cpg.node(node_id)?;
    let qualified_name = qualified_name_of(cpg, node_id);
    let file_path = cpg.file_path(node.file_id).unwrap_or("").to_owned();
    let line = node.start_row + 1;
    Some(Caller {
        node_id,
        qualified_name,
        file_path,
        line,
        depth,
        call_kind,
    })
}
