//! Call edge extraction.
//!
//! Walks the AST, finds every call site, identifies the enclosing
//! function, extracts the callee name, and resolves it against the
//! symbol table with receiver awareness.
//!
//! Three callee shapes are distinguished:
//!
//! - `foo()` — simple call. Resolves only if exactly one symbol in
//!   the table has the simple name. Zero matches means external; two
//!   or more means ambiguous, and the edge is emitted unresolved.
//! - `self.foo()` — receiver is the current class. Resolves to
//!   `<enclosing_class>.foo` if that qualified name exists. Falls
//!   back to simple-name uniqueness when it does not.
//! - `Class.foo()` — receiver names a class in the symbol table.
//!   Resolves to `Class.foo`. Falls back to simple-name uniqueness
//!   when the receiver is not a known class or the qualified name
//!   does not exist.
//!
//! Every call site that has an enclosing function produces a
//! `CallEdge`. Unresolved edges carry `callee_node_id = NO_NODE` and
//! `resolved = false`. Keeping them in the graph is what lets blast
//! report "unresolved calls we could not attribute" instead of "zero
//! callers".
//!
//! Import tracking is not performed. A call to `mod.foo()` where `mod`
//! was imported is treated as a simple `foo` lookup, which resolves
//! only if `foo` is unique in the repository. Full import resolution
//! is deferred to a later phase.

use crate::ast::{AstArena, NodeKind, NO_NODE};
use crate::symbol_table::{SymbolKind, SymbolTable};

/// The outcome of attempting to resolve a callee name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Resolution {
    /// Resolved to a specific symbol's definition node.
    Unique(u32),
    /// Multiple candidates exist and no receiver narrowed the choice.
    Ambiguous {
        /// The number of candidates that matched the simple name.
        candidates: usize,
    },
    /// No candidate matched the name.
    NotFound,
}

impl Resolution {
    /// Return the resolved node id, or `NO_NODE` if unresolved.
    pub fn node_id(self) -> u32 {
        match self {
            Resolution::Unique(id) => id,
            _ => NO_NODE,
        }
    }

    /// Return whether this resolution identified a specific symbol.
    pub fn is_resolved(self) -> bool {
        matches!(self, Resolution::Unique(_))
    }
}

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
        let resolution = resolve_callee(arena, symbols, node_id, &callee_name);
        edges.push(CallEdge {
            caller_node_id: caller,
            callee_node_id: resolution.node_id(),
            site_node_id: node_id,
            callee_name,
            resolved: resolution.is_resolved(),
        });
    }
    edges
}

/// Resolve a call site to a symbol using receiver-aware logic.
///
/// Tries `self.foo` via enclosing class, `Class.foo` via receiver
/// class check, and finally falls back to simple-name uniqueness.
pub fn resolve_callee(
    arena: &AstArena,
    symbols: &SymbolTable,
    call_node: u32,
    callee_name: &str,
) -> Resolution {
    let callee_node_id = match arena.get(call_node) {
        Some(n) if n.first_child != NO_NODE => n.first_child,
        _ => return resolve_simple(symbols, callee_name),
    };

    let callee_kind = match arena.get(callee_node_id) {
        Some(n) => n.kind,
        None => return resolve_simple(symbols, callee_name),
    };

    if callee_kind != NodeKind::Attribute {
        return resolve_simple(symbols, callee_name);
    }

    let receiver_id = match arena.get(callee_node_id) {
        Some(n) if n.first_child != NO_NODE => n.first_child,
        _ => return resolve_simple(symbols, callee_name),
    };

    let receiver_text = match receiver_text_of(arena, receiver_id) {
        Some(text) => text,
        None => return resolve_simple(symbols, callee_name),
    };

    resolve_attribute(arena, symbols, call_node, &receiver_text, callee_name)
}

/// Resolve a simple call (`foo()`) by simple-name uniqueness.
fn resolve_simple(symbols: &SymbolTable, name: &str) -> Resolution {
    let count = symbols.count_simple_name(name);
    match count {
        0 => Resolution::NotFound,
        1 => match symbols.lookup_by_simple_name(name).first() {
            Some(symbol) => Resolution::Unique(symbol.node_id),
            None => Resolution::NotFound,
        },
        n => Resolution::Ambiguous { candidates: n },
    }
}

/// Resolve an attribute call (`receiver.method()`) using the receiver.
fn resolve_attribute(
    arena: &AstArena,
    symbols: &SymbolTable,
    call_node: u32,
    receiver: &str,
    method_name: &str,
) -> Resolution {
    if receiver == "self" {
        if let Some(class_name) = enclosing_class_name(arena, call_node) {
            if let Some(symbol) = symbols.lookup_in_class(&class_name, method_name) {
                return Resolution::Unique(symbol.node_id);
            }
        }
        return resolve_simple(symbols, method_name);
    }

    if symbols.count_simple_name(receiver) > 0 {
        let is_class = symbols
            .lookup_by_simple_name(receiver)
            .iter()
            .any(|s| matches!(s.kind, SymbolKind::Class));
        if is_class {
            if let Some(symbol) = symbols.lookup_in_class(receiver, method_name) {
                return Resolution::Unique(symbol.node_id);
            }
        }
    }

    resolve_simple(symbols, method_name)
}

/// Return the text of a receiver expression when it is a plain
/// identifier, otherwise `None`.
fn receiver_text_of(arena: &AstArena, receiver_id: u32) -> Option<String> {
    let node = arena.get(receiver_id)?;
    if node.kind == NodeKind::Identifier {
        arena.string(node.name).map(str::to_owned)
    } else {
        None
    }
}

/// Walk up from `node_id` and return the name of the first enclosing
/// class-like scope.
///
/// Does not stop at function boundaries. A nested function inside a
/// method still has access to the method's `self`, which refers to
/// the enclosing class. The first class-like ancestor is the correct
/// binding.
pub fn enclosing_class_name(arena: &AstArena, node_id: u32) -> Option<String> {
    let mut current = arena.parent_of(node_id);
    while let Some(id) = current {
        let node = match arena.get(id) {
            Some(n) => n,
            None => break,
        };
        if matches!(
            node.kind,
            NodeKind::ClassDef
                | NodeKind::Struct
                | NodeKind::Interface
                | NodeKind::Trait
                | NodeKind::Enum
                | NodeKind::Union
        ) {
            if let Some(name) = arena.string(node.name) {
                return Some(name.to_owned());
            }
        }
        current = arena.parent_of(id);
    }
    None
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
/// the callee expression.
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
