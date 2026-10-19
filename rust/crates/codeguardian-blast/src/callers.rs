//! Direct and transitive caller resolution.
//!
//! Direct callers are the functions with a `Call` edge into the target.
//! Transitive callers are every function reachable by walking reverse
//! `Call` edges from the direct callers, up to a cap.
//!
//! Unresolved call sites are calls that could not be attributed to a
//! specific target because the kernel could not resolve the callee
//! name. They carry `dst = NO_NODE` in the graph and are counted
//! separately so a report never says "zero callers" when the truth is
//! "zero resolved callers, N unresolved call sites".

use std::collections::{HashSet, VecDeque};

use codeguardian_kernel::{CodePropertyGraph, EdgeKind, NO_NODE};

use crate::qualified_name_of;

/// The maximum number of transitive callers returned.
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
    pub depth: u32,
    /// Whether the caller was reached directly or transitively.
    pub call_kind: CallKind,
}

/// Return every function with a direct `Call` edge into the target.
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

/// Return the number of unresolved call sites whose callee name could
/// be an alias for `target_node_id`.
///
/// An unresolved call site is a `Call` edge with `dst = NO_NODE`. The
/// count is filtered by the target's simple name so a report on
/// `Session.request` counts unresolved calls to `.request()` and not
/// unrelated unresolved calls to other methods. The target's own
/// definition is excluded: a function that calls itself recursively
/// and cannot be resolved does not count as an external caller.
///
/// The count is a lower-bound estimate. A call site whose callee name
/// happens to match a method of the target's class but is really a
/// call to a different class's method of the same name is counted. The
/// user reads this alongside the resolved direct caller list and makes
/// the final call.
pub fn count_unresolved_call_sites(
    cpg: &CodePropertyGraph,
    target_node_id: u32,
) -> usize {
    let target = match cpg.node(target_node_id) {
        Some(n) => n,
        None => return 0,
    };
    let simple_name = match cpg.string(target.name) {
        Some(s) => s,
        None => return 0,
    };
    cpg.edges_to(NO_NODE)
        .iter()
        .filter(|&&edge_id| {
            let edge = match cpg.edge(edge_id) {
                Some(e) => e,
                None => return false,
            };
            edge.kind == EdgeKind::Call
                && edge.src != target_node_id
                && cpg.string(edge.label) == Some(simple_name)
        })
        .count()
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
