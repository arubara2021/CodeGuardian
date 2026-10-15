//! Blast radius computation.
//!
//! Given a target function and a Code Property Graph, this crate
//! answers three questions. First, which functions transitively call
//! the target. Second, which of those callers are at risk of breaking
//! because of a change to the target. Third, how much risk the change
//! carries overall, expressed as a score and a recommendation.
//!
//! The crate exposes its building blocks. `resolve_target` turns a
//! `BlastTarget` into a CPG node id. `compute_direct_callers` and
//! `compute_transitive_callers` walk the call graph.
//! `detect_contract_violations` inspects callers for the one contract
//! pattern the CPG can detect on its own. `detect_coverage_gaps`
//! reports callers that no test file reaches.
//! `compute_blast_score` combines the four counts into a single
//! integer. `recommend` maps the score and the critical-violation
//! count to a proceed, review, or block decision.
//!
//! The invariant this crate protects is that every returned `Caller`,
//! `ContractViolation`, and `CoverageGap` points at a real CPG node. A
//! node that cannot be resolved is skipped, not reported with a
//! placeholder.

pub mod callers;
pub mod contracts;
pub mod coverage;
pub mod recommendation;
pub mod score;

use thiserror::Error;

use codeguardian_kernel::{CodePropertyGraph, EdgeKind, NodeKind};

pub use callers::{
    compute_direct_callers, compute_transitive_callers, CallKind, Caller,
    MAX_TRANSITIVE_CALLERS,
};
pub use contracts::{detect_contract_violations, ContractViolation, Risk};
pub use coverage::{detect_coverage_gaps, CoverageGap, GAP_REASON};
pub use recommendation::{
    recommend, Recommendation, BLOCK_THRESHOLD, CRITICAL_FORCES_BLOCK, REVIEW_THRESHOLD,
};
pub use score::{
    compute_blast_score, W_CONTRACT_VIOLATION, W_COVERAGE_GAP, W_DIRECT_CALLER,
    W_TRANSITIVE_CALLER,
};

/// A target for blast radius analysis.
///
/// The path is the exact string passed to the kernel when the graph was
/// built. The qualified name is the dot-joined path from the outermost
/// enclosing named scope, matching the format `symbol_table.rs` uses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlastTarget {
    /// The file path recorded in the graph's file table.
    pub file_path: String,
    /// The dot-joined qualified name, e.g. `Parser.parse` or `greet`.
    pub qualified_name: String,
}

/// Every way blast radius analysis can fail to start.
#[derive(Debug, Error)]
pub enum BlastError {
    /// The file path is not present in the graph's file table.
    #[error("file not found in graph: {0}")]
    FileNotFound(String),
    /// No node in the file matches the qualified name.
    #[error("target not found: {file_path}:{qualified_name}")]
    TargetNotFound {
        /// The path that was searched.
        file_path: String,
        /// The qualified name that was searched.
        qualified_name: String,
    },
    /// More than one node matches the qualified name. This happens when
    /// the same name is defined twice in the same file.
    #[error("target is ambiguous: {file_path}:{qualified_name} matches {count} nodes")]
    TargetAmbiguous {
        /// The path that was searched.
        file_path: String,
        /// The qualified name that was searched.
        qualified_name: String,
        /// How many nodes matched.
        count: usize,
    },
}

/// Resolve a `BlastTarget` to the CPG node id of its definition.
///
/// Walks every node in the target's file whose kind is a declaration
/// and reconstructs its qualified name from the CPG's AST edges. A
/// name that matches exactly one declaration is returned. Zero matches
/// and multiple matches are errors.
pub fn resolve_target(cpg: &CodePropertyGraph, target: &BlastTarget) -> Result<u32, BlastError> {
    let file_id = cpg
        .file_id(&target.file_path)
        .ok_or_else(|| BlastError::FileNotFound(target.file_path.clone()))?;

    let mut matches: Vec<u32> = Vec::new();
    for &node_id in cpg.nodes_in_file(file_id) {
        let node = match cpg.node(node_id) {
            Some(n) => n,
            None => continue,
        };
        if !is_declaration(node.kind) {
            continue;
        }
        let qualified = qualified_name_of(cpg, node_id);
        if qualified == target.qualified_name {
            matches.push(node_id);
        }
    }

    match matches.len() {
        0 => Err(BlastError::TargetNotFound {
            file_path: target.file_path.clone(),
            qualified_name: target.qualified_name.clone(),
        }),
        1 => Ok(matches[0]),
        count => Err(BlastError::TargetAmbiguous {
            file_path: target.file_path.clone(),
            qualified_name: target.qualified_name.clone(),
            count,
        }),
    }
}

/// Return whether a syntax kind declares a named symbol.
pub(crate) fn is_declaration(kind: NodeKind) -> bool {
    matches!(
        kind,
        NodeKind::FunctionDef | NodeKind::MethodDef | NodeKind::ClassDef
    )
}

/// Reconstruct a node's qualified name by walking the CPG's AST edges
/// upward.
///
/// A top-level function `parse_config` returns `parse_config`. A method
/// `parse` inside class `Parser` returns `Parser.parse`. The name is
/// built from the enclosing declarations only; block, expression, and
/// parameter nodes contribute nothing.
pub(crate) fn qualified_name_of(cpg: &CodePropertyGraph, node_id: u32) -> String {
    let mut parts: Vec<String> = Vec::new();
    let mut current = Some(node_id);
    while let Some(id) = current {
        let node = match cpg.node(id) {
            Some(n) => n,
            None => break,
        };
        if is_declaration(node.kind) {
            if let Some(name) = cpg.string(node.name) {
                parts.push(name.to_owned());
            }
        }
        current = parent_of(cpg, id);
    }
    parts.reverse();
    parts.join(".")
}

/// Return the AST parent of a node, or `None` if it is the root.
pub(crate) fn parent_of(cpg: &CodePropertyGraph, node_id: u32) -> Option<u32> {
    for &edge_id in cpg.edges_to(node_id) {
        let edge = match cpg.edge(edge_id) {
            Some(e) => e,
            None => continue,
        };
        if edge.kind == EdgeKind::Ast {
            return Some(edge.src);
        }
    }
    None
}

/// Return the first AST child of a node, or `None` if it is a leaf.
pub(crate) fn first_ast_child(cpg: &CodePropertyGraph, node_id: u32) -> Option<u32> {
    let mut best: Option<u32> = None;
    for &edge_id in cpg.edges_from(node_id) {
        let edge = match cpg.edge(edge_id) {
            Some(e) => e,
            None => continue,
        };
        if edge.kind != EdgeKind::Ast {
            continue;
        }
        best = Some(match best {
            None => edge.dst,
            Some(current) if edge.dst < current => edge.dst,
            Some(current) => current,
        });
    }
    best
}

/// Return every AST child of a node in ascending node id order.
pub(crate) fn ast_children(cpg: &CodePropertyGraph, node_id: u32) -> Vec<u32> {
    let mut children: Vec<u32> = Vec::new();
    for &edge_id in cpg.edges_from(node_id) {
        let edge = match cpg.edge(edge_id) {
            Some(e) => e,
            None => continue,
        };
        if edge.kind == EdgeKind::Ast {
            children.push(edge.dst);
        }
    }
    children.sort_unstable();
    children
}

/// Return every node in the AST subtree rooted at `root`, in no
/// particular order. The root itself is included.
///
/// The walk follows only `Ast` edges. It does not follow call, CFG, or
/// PDG edges, which connect nodes that are not in a parent-child
/// relationship.
pub(crate) fn walk_subtree(cpg: &CodePropertyGraph, root: u32) -> Vec<u32> {
    let mut result: Vec<u32> = Vec::new();
    let mut stack: Vec<u32> = vec![root];
    while let Some(node_id) = stack.pop() {
        result.push(node_id);
        for &edge_id in cpg.edges_from(node_id) {
            let edge = match cpg.edge(edge_id) {
                Some(e) => e,
                None => continue,
            };
            if edge.kind == EdgeKind::Ast {
                stack.push(edge.dst);
            }
        }
    }
    result
}
