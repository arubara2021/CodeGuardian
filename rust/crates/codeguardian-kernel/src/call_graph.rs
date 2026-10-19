//! Call edge extraction.
//!
//! Walks the AST, finds every call site, identifies the enclosing
//! function, extracts the callee name, and resolves it against the
//! symbol table.
//!
//! Resolution is deliberately conservative. A callee resolves only if
//! exactly one symbol in the file matches by simple name. Zero matches
//! means the callee is external (imported, dynamically dispatched, or
//! defined in another file). More than one match means the name is
//! ambiguous. Both cases produce an unresolved edge with
//! `callee_node_id = NO_NODE`. The edge is still emitted: the graph
//! must show that a call happened even if the target is unknown.
//!
//! The invariant this module protects is that every `CallEdge` with
//! `resolved = true` has both endpoints pointing at live nodes. An
//! unresolved edge carries `callee_node_id = NO_NODE` and is
//! distinguishable by the `resolved` flag.
//!
//! `default_extract_callee_name` is the language-agnostic callee-name
//! extractor that `LanguageModule::extract_callee` delegates to by
//! default. It is public so a language module that needs to override
//! the default can call it for the cases where its own logic does not
//! apply.

use crate::ast::{AstArena, NodeKind, NO_NODE};
use crate::symbol_table::SymbolTable;

/// One call site.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallEdge {
    /// The arena index of the enclosing function, or `NO_NODE` if the
    /// call appears at module scope.
    pub caller_node_id: u32,
    /// The arena index of the resolved callee, or `NO_NODE` if the call
    /// could not be resolved.
    pub callee_node_id: u32,
    /// The arena index of the call expression itself.
    pub site_node_id: u32,
    /// The textual name of the callee as it appears at the call site.
    pub callee_name: String,
    /// Whether the callee was resolved to a symbol.
    pub resolved: bool,
}

/// Extract every call edge from the subtree rooted at `root`.
pub fn extract_calls(arena: &AstArena, symbols: &SymbolTable, root: u32) -> Vec<CallEdge> {
    let mut edges = Vec::new();
    for node_id in arena.walk_preorder(root) {
        let node = match arena.get(node_id) {
            Some(n) => n,
            None => continue,
        };
        if node.kind != NodeKind::Call {
            continue;
        }
        let callee_name = match default_extract_callee_name(arena, node_id) {
            Some(name) => name,
            None => continue,
        };
        let caller = enclosing_function(arena, node_id);
        let resolved_id = resolve_callee(symbols, &callee_name);
        edges.push(CallEdge {
            caller_node_id: caller,
            callee_node_id: resolved_id.unwrap_or(NO_NODE),
            site_node_id: node_id,
            callee_name,
            resolved: resolved_id.is_some(),
        });
    }
    edges
}

/// Return the enclosing function or method of a node, or `NO_NODE` if
/// none exists.
fn enclosing_function(arena: &AstArena, node_id: u32) -> u32 {
    let mut current = arena.parent_of(node_id);
    while let Some(id) = current {
        let node = match arena.get(id) {
            Some(n) => n,
            None => break,
        };
        if matches!(
            node.kind,
            NodeKind::FunctionDef | NodeKind::MethodDef | NodeKind::Lambda
        ) {
            return id;
        }
        current = arena.parent_of(id);
    }
    NO_NODE
}

/// Return the textual name of the callee at a call site.
///
/// For `foo()` the callee name is `foo`. For `foo.bar()` it is `bar`.
/// For `foo.bar.baz()` it is `baz`. The name is the last identifier in
/// the callee expression. This is a simplification: a fully-qualified
/// callee chain would require import resolution, which this block does
/// not perform.
///
/// This function is public because it is the default implementation
/// `LanguageModule::extract_callee` delegates to. A language module
/// that needs to handle call syntax this function does not cover can
/// override `extract_callee` and still call this function for the
/// straightforward cases.
pub fn default_extract_callee_name(arena: &AstArena, call_node: u32) -> Option<String> {
    let node = arena.get(call_node)?;
    let first = node.first_child;
    if first == NO_NODE {
        return None;
    }
    let callee = arena.get(first)?;
    match callee.kind {
        NodeKind::Identifier => arena.string(callee.name).map(str::to_owned),
        NodeKind::Attribute | NodeKind::Subscript => last_identifier_text(arena, first),
        _ => None,
    }
}

/// Return the text of the last identifier in the subtree rooted at
/// `node_id`.
fn last_identifier_text(arena: &AstArena, node_id: u32) -> Option<String> {
    let mut last: Option<String> = None;
    for id in arena.walk_preorder(node_id) {
        let node = match arena.get(id) {
            Some(n) => n,
            None => continue,
        };
        if node.kind == NodeKind::Identifier {
            if let Some(text) = arena.string(node.name) {
                last = Some(text.to_owned());
            }
        }
    }
    last
}

/// Resolve a callee name against the symbol table.
///
/// Returns the arena index of the resolved symbol's definition node, or
/// `None` if the name is absent or ambiguous.
fn resolve_callee(symbols: &SymbolTable, name: &str) -> Option<u32> {
    let matches = symbols.lookup_by_simple_name(name);
    if matches.len() == 1 {
        Some(matches[0].node_id)
    } else {
        None
    }
}