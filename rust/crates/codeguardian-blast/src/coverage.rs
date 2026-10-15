//! Coverage gap detection.
//!
//! A caller has a coverage gap when no test file reaches it through the
//! call graph. The test is static: a file counts as a test file when
//! its path matches a filename pattern, and a caller counts as covered
//! when a call edge from a test file reaches it, directly or through
//! intermediate functions.
//!
//! The signal is a heuristic. It cannot verify that a test executes,
//! that a test asserts anything, or that the code path through the
//! caller is exercised at runtime. It answers a narrower question: does
//! any file that looks like a test file call this function or call
//! something that calls it. The schema field is named `reason` rather
//! than `coverage` because the honest label for the value is "why this
//! caller is not touched by tests", not "this caller is covered".
//!
//! The invariant this module protects is that every returned
//! `CoverageGap` names a node that exists in the graph. A caller whose
//! node has been removed is skipped, not reported.

use std::collections::HashSet;

use codeguardian_kernel::{CodePropertyGraph, EdgeKind};

use crate::Caller;

/// The fixed sentence written into every gap's `reason` field.
///
/// The schema permits any string. One honest sentence is enough, and a
/// constant prevents drift between what the code says and what the
/// report says.
pub const GAP_REASON: &str = "no test file references this function";

/// One caller that no test file reaches through the call graph.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CoverageGap {
    /// The CPG node id of the caller.
    pub caller_node_id: u32,
    /// The caller's qualified name.
    pub caller_qualified_name: String,
    /// The path of the file the caller is defined in.
    pub caller_file_path: String,
    /// One-based line of the caller's definition.
    pub caller_line: u32,
    /// Why the caller is considered uncovered.
    pub reason: String,
}

/// Return every caller that no test file reaches through the call graph.
///
/// The result is sorted by caller node id. Duplicate callers are
/// collapsed to one gap each.
pub fn detect_coverage_gaps(cpg: &CodePropertyGraph, callers: &[Caller]) -> Vec<CoverageGap> {
    let covered = collect_covered_nodes(cpg);

    let mut seen: HashSet<u32> = HashSet::new();
    let mut gaps: Vec<CoverageGap> = Vec::new();

    for caller in callers {
        if !seen.insert(caller.node_id) {
            continue;
        }
        if covered.contains(&caller.node_id) {
            continue;
        }
        gaps.push(CoverageGap {
            caller_node_id: caller.node_id,
            caller_qualified_name: caller.qualified_name.clone(),
            caller_file_path: caller.file_path.clone(),
            caller_line: caller.line,
            reason: GAP_REASON.to_owned(),
        });
    }

    gaps.sort_by_key(|gap| gap.caller_node_id);
    gaps
}

/// Return every node reachable through `Call` edges from any function
/// in a test file.
fn collect_covered_nodes(cpg: &CodePropertyGraph) -> HashSet<u32> {
    let mut covered: HashSet<u32> = HashSet::new();
    let file_count = cpg.file_count() as u32;
    for file_id in 0..file_count {
        let path = match cpg.file_path(file_id) {
            Some(path) => path,
            None => continue,
        };
        if !is_test_file(path) {
            continue;
        }
        for &node_id in cpg.nodes_in_file(file_id) {
            for &edge_id in cpg.edges_from(node_id) {
                let edge = match cpg.edge(edge_id) {
                    Some(edge) => edge,
                    None => continue,
                };
                if edge.kind != EdgeKind::Call {
                    continue;
                }
                mark_reachable(cpg, edge.dst, &mut covered);
            }
        }
    }
    covered
}

/// Walk forward through `Call` edges from `start`, inserting every
/// reached node into `visited`. Terminates on cycles.
fn mark_reachable(cpg: &CodePropertyGraph, start: u32, visited: &mut HashSet<u32>) {
    if !visited.insert(start) {
        return;
    }
    let mut stack: Vec<u32> = vec![start];
    while let Some(node_id) = stack.pop() {
        for &edge_id in cpg.edges_from(node_id) {
            let edge = match cpg.edge(edge_id) {
                Some(edge) => edge,
                None => continue,
            };
            if edge.kind != EdgeKind::Call {
                continue;
            }
            if visited.insert(edge.dst) {
                stack.push(edge.dst);
            }
        }
    }
}

/// Return whether a file path matches a test-file pattern.
///
/// The rules are per-language and path-based. Python: the basename
/// starts with `test_`, ends with `_test.py`, or the path contains a
/// `/tests/` segment. TypeScript: the basename contains `.test.` or
/// `.spec.`, or the path contains a `/__tests__/` segment.
///
/// These patterns are heuristics, not an exhaustive taxonomy of test
/// layouts. A project that puts its tests in `spec/` or names its
/// TypeScript tests `*.test.ts` without the segment will not be
/// recognized by these rules; widening the pattern list is a code
/// change, deliberately not a runtime configuration.
fn is_test_file(path: &str) -> bool {
    let base = basename(path);
    if path.ends_with(".py") {
        return base.starts_with("test_")
            || base.ends_with("_test.py")
            || path.contains("/tests/");
    }
    if path.ends_with(".ts") || path.ends_with(".tsx") {
        return base.contains(".test.")
            || base.contains(".spec.")
            || path.contains("/__tests__/");
    }
    false
}

/// Return the last path segment, handling both `/` and `\` separators.
fn basename(path: &str) -> &str {
    match path.rfind(['/', '\\']) {
        Some(index) => &path[index + 1..],
        None => path,
    }
}
