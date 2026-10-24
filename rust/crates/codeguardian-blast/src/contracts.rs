//! Contract violation detection.
//!
//! A contract violation exists when a caller relies on an assumption
//! the target does not guarantee. The CPG carries no type information
//! and no nullability annotations, so the only signal available is
//! syntactic. This module detects one narrow pattern:
//!
//! 1. The target has at least one bare `return`, so it can produce
//!    `None` on some path.
//! 2. A caller binds the target's return value to a variable.
//! 3. That variable is used in a `Subscript` or `Attribute` node,
//!    which is a dereference of the value.
//! 4. The dereference is not control-dependent on a guarded branch.
//!
//! Step 4 uses the PDG's `ControlDependence` edges. If the enclosing
//! statement is control-dependent on an `If` whose condition references
//! the bound variable, the caller has performed a check and the
//! dereference is not a violation.
//!
//! The signal produces false positives and false negatives. A caller
//! that checks `x is not None` and then dereferences `x` in the same
//! branch is not flagged. A caller that checks `x` for truthiness and
//! dereferences it on the else branch is not flagged because the CPG
//! cannot distinguish the condition's polarity. The signal is
//! deliberately narrow and honest about what it does not catch.

use std::collections::HashSet;

use codeguardian_kernel::ast::FLAG_NAMED;
use codeguardian_kernel::{CodePropertyGraph, EdgeKind, NodeKind};

use crate::{ast_children, first_ast_child, parent_of, walk_subtree, Caller};

/// How severe a contract violation is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Risk {
    /// Recorded for the audit trail; not acted on.
    Info,
    /// Surfaced to the user; may affect the recommendation.
    Warning,
    /// Forces the recommendation to at least `review`.
    Critical,
}

/// One caller whose assumption about the target may break.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContractViolation {
    /// The CPG node id of the calling function.
    pub caller_node_id: u32,
    /// The caller's qualified name.
    pub caller_qualified_name: String,
    /// The path of the file the caller is defined in.
    pub caller_file_path: String,
    /// One-based line of the caller's definition.
    pub caller_line: u32,
    /// The CPG node id of the target.
    pub target_node_id: u32,
    /// A sentence describing the assumption.
    pub assumption: String,
    /// How severe the violation is.
    pub risk: Risk,
}

/// Return every caller that dereferences the target's return value
/// without a guarded check, when the target has a bare return.
///
/// The result is sorted by caller node id. Duplicate callers are
/// collapsed to one violation each.
pub fn detect_contract_violations(
    cpg: &CodePropertyGraph,
    target_node_id: u32,
    callers: &[Caller],
) -> Vec<ContractViolation> {
    if !has_bare_return(cpg, target_node_id) {
        return Vec::new();
    }

    let target_node = match cpg.node(target_node_id) {
        Some(n) => n,
        None => return Vec::new(),
    };
    let target_simple_name = match cpg.string(target_node.name) {
        Some(s) => s.to_owned(),
        None => return Vec::new(),
    };

    let mut seen: HashSet<u32> = HashSet::new();
    let mut violations: Vec<ContractViolation> = Vec::new();

    for caller in callers {
        if !seen.insert(caller.node_id) {
            continue;
        }
        if let Some(violation) = check_caller(cpg, target_node_id, &target_simple_name, caller) {
            violations.push(violation);
        }
    }

    violations.sort_by_key(|violation| violation.caller_node_id);
    violations
}

/// Return whether the target contains a bare `return` — a `Return`
/// node with no named children — which means the function can produce
/// `None` on some path.
///
/// The check filters to named children. tree-sitter includes the
/// anonymous `return` keyword as a child of every `return_statement`,
/// so a naive "has no children" check returns false for the exact
/// pattern it is meant to detect.
fn has_bare_return(cpg: &CodePropertyGraph, target_node_id: u32) -> bool {
    for node_id in walk_subtree(cpg, target_node_id) {
        let node = match cpg.node(node_id) {
            Some(n) => n,
            None => continue,
        };
        if node.kind != NodeKind::Return {
            continue;
        }
        if !has_named_child(cpg, node_id) {
            return true;
        }
    }
    false
}

/// Return whether a node has at least one named AST child.
fn has_named_child(cpg: &CodePropertyGraph, node_id: u32) -> bool {
    for &edge_id in cpg.edges_from(node_id) {
        let edge = match cpg.edge(edge_id) {
            Some(e) => e,
            None => continue,
        };
        if edge.kind != EdgeKind::Ast {
            continue;
        }
        let child = match cpg.node(edge.dst) {
            Some(n) => n,
            None => continue,
        };
        if child.flags & FLAG_NAMED != 0 {
            return true;
        }
    }
    false
}

/// Check one caller for a single contract violation.
fn check_caller(
    cpg: &CodePropertyGraph,
    target_node_id: u32,
    target_simple_name: &str,
    caller: &Caller,
) -> Option<ContractViolation> {
    let subtree = walk_subtree(cpg, caller.node_id);

    for &node_id in &subtree {
        let node = match cpg.node(node_id) {
            Some(n) => n,
            None => continue,
        };
        if node.kind != NodeKind::Call {
            continue;
        }
        let callee = match last_identifier_in_subtree(cpg, node_id) {
            Some(name) => name,
            None => continue,
        };
        if callee != target_simple_name {
            continue;
        }
        let bound_names = bound_variable_names(cpg, node_id);
        if bound_names.is_empty() {
            continue;
        }
        if bound_names
            .iter()
            .any(|name| has_unguarded_dereference(cpg, &subtree, name))
        {
            return Some(ContractViolation {
                caller_node_id: caller.node_id,
                caller_qualified_name: caller.qualified_name.clone(),
                caller_file_path: caller.file_path.clone(),
                caller_line: caller.line,
                target_node_id,
                assumption: format!(
                    "caller dereferences the return value of '{target_simple_name}' \
                     without a guard; the target has a bare return that can produce None"
                ),
                risk: Risk::Warning,
            });
        }
    }
    None
}

/// Return the text of the last identifier in the subtree rooted at
/// `node_id`. This is the callee's simple name for a `Call` node.
fn last_identifier_in_subtree(cpg: &CodePropertyGraph, node_id: u32) -> Option<String> {
    let mut last: Option<String> = None;
    for id in walk_subtree(cpg, node_id) {
        let node = match cpg.node(id) {
            Some(n) => n,
            None => continue,
        };
        if node.kind != NodeKind::Identifier {
            continue;
        }
        if let Some(name) = cpg.string(node.name) {
            last = Some(name.to_owned());
        }
    }
    last
}

/// Return every variable a call site is bound to, if the call's
/// parent is an `Assignment`.
///
/// Handles two assignment shapes:
///
/// - Simple assignment: `x = get()` binds `x`.
/// - Tuple unpacking: `a, b = get()` binds both `a` and `b`.
///
/// Attribute and subscript targets (`self.cache = get()`,
/// `data[key] = get()`) are deliberately not handled here. Matching a
/// dotted path would require extending `has_unguarded_dereference` to
/// walk the full `Attribute`/`Subscript` chain rather than comparing
/// simple identifier names, which is a separate change with its own
/// false-positive profile. The narrow signature stays honest about
/// what it covers.
fn bound_variable_names(cpg: &CodePropertyGraph, call_site: u32) -> Vec<String> {
    let parent = match parent_of(cpg, call_site) {
        Some(p) => p,
        None => return Vec::new(),
    };
    let parent_node = match cpg.node(parent) {
        Some(n) => n,
        None => return Vec::new(),
    };
    if parent_node.kind != NodeKind::Assignment {
        return Vec::new();
    }
    for child in ast_children(cpg, parent) {
        let child_node = match cpg.node(child) {
            Some(n) => n,
            None => continue,
        };
        match child_node.kind {
            NodeKind::Identifier => {
                if let Some(name) = cpg.string(child_node.name) {
                    return vec![name.to_owned()];
                }
            }
            NodeKind::Tuple => {
                let mut names: Vec<String> = Vec::new();
                for tuple_child in ast_children(cpg, child) {
                    let tuple_node = match cpg.node(tuple_child) {
                        Some(n) => n,
                        None => continue,
                    };
                    if tuple_node.kind == NodeKind::Identifier {
                        if let Some(name) = cpg.string(tuple_node.name) {
                            names.push(name.to_owned());
                        }
                    }
                }
                if !names.is_empty() {
                    return names;
                }
            }
            _ => {}
        }
    }
    Vec::new()
}

/// Return whether any identifier in the subtree that matches
/// `bound_name` is used as the object of a `Subscript` or `Attribute`
/// and is not control-dependent on a guard.
fn has_unguarded_dereference(cpg: &CodePropertyGraph, subtree: &[u32], bound_name: &str) -> bool {
    for &node_id in subtree {
        let node = match cpg.node(node_id) {
            Some(n) => n,
            None => continue,
        };
        if node.kind != NodeKind::Identifier {
            continue;
        }
        if cpg.string(node.name) != Some(bound_name) {
            continue;
        }
        if !is_dereference_object(cpg, node_id) {
            continue;
        }
        if is_guarded(cpg, node_id, bound_name) {
            continue;
        }
        return true;
    }
    false
}

/// Return whether the identifier is the first child of a `Subscript` or
/// `Attribute`, which is the object of the dereference.
fn is_dereference_object(cpg: &CodePropertyGraph, node_id: u32) -> bool {
    let parent = match parent_of(cpg, node_id) {
        Some(p) => p,
        None => return false,
    };
    let parent_node = match cpg.node(parent) {
        Some(n) => n,
        None => return false,
    };
    if !matches!(
        parent_node.kind,
        NodeKind::Attribute | NodeKind::Subscript
    ) {
        return false;
    }
    first_ast_child(cpg, parent) == Some(node_id)
}

/// Return whether the dereference is control-dependent on an `If` whose
/// condition references `bound_name`.
fn is_guarded(cpg: &CodePropertyGraph, use_node_id: u32, bound_name: &str) -> bool {
    let statement = match enclosing_statement(cpg, use_node_id) {
        Some(s) => s,
        None => return false,
    };
    for &edge_id in cpg.edges_to(statement) {
        let edge = match cpg.edge(edge_id) {
            Some(e) => e,
            None => continue,
        };
        if edge.kind != EdgeKind::ControlDependence {
            continue;
        }
        let controller = match cpg.node(edge.src) {
            Some(n) => n,
            None => continue,
        };
        if controller.kind != NodeKind::If {
            continue;
        }
        if condition_references(cpg, edge.src, bound_name) {
            return true;
        }
    }
    false
}

/// Return the closest ancestor of `node_id` whose parent is a `Block`.
/// That node is the statement containing `node_id`.
fn enclosing_statement(cpg: &CodePropertyGraph, node_id: u32) -> Option<u32> {
    let mut current = node_id;
    let mut steps = 0usize;
    while steps < 100 {
        let parent = parent_of(cpg, current)?;
        let parent_node = cpg.node(parent)?;
        if parent_node.kind == NodeKind::Block {
            return Some(current);
        }
        current = parent;
        steps += 1;
    }
    None
}

/// Return whether the condition of an `If` node references
/// `bound_name`. The condition is every direct AST child of the `If`
/// whose kind is not `Block`.
fn condition_references(cpg: &CodePropertyGraph, if_node_id: u32, bound_name: &str) -> bool {
    for child in ast_children(cpg, if_node_id) {
        let child_node = match cpg.node(child) {
            Some(n) => n,
            None => continue,
        };
        if child_node.kind == NodeKind::Block {
            continue;
        }
        for descendant in walk_subtree(cpg, child) {
            let descendant_node = match cpg.node(descendant) {
                Some(n) => n,
                None => continue,
            };
            if descendant_node.kind != NodeKind::Identifier {
                continue;
            }
            if cpg.string(descendant_node.name) == Some(bound_name) {
                return true;
            }
        }
    }
    false
}
