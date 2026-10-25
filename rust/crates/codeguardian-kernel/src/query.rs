//! Query interface for the Code Property Graph.
//!
//! Every method here is O(1) after index construction. Indices are
//! built at graph finalization time and rebuilt by `apply_incremental`.
//! The methods that return `&[u32]` hand back a slice that borrows from
//! an existing index; the methods that return `Vec<u32>` allocate
//! because they filter by edge kind.
//!
//! # Declaration resolution
//!
//! Three methods answer questions about named declarations:
//!
//! - `qualified_name_of` walks the AST parent chain and joins the names
//!   of enclosing declarations with dots. A method `parse` inside
//!   `Parser` returns `Parser.parse`.
//! - `find_declaration` returns the node id of the declaration whose
//!   qualified name matches, or `None` if there is no match.
//! - `declaration_info` returns a `DeclarationInfo` record with every
//!   field a caller typically needs, or `None` if the node is not a
//!   declaration.
//!
//! The set of declaration kinds is broader than the blast crate's. The
//! blast crate recognizes `FunctionDef`, `MethodDef`, and `ClassDef`
//! only; this module also recognizes `Struct`, `Interface`, `Trait`,
//! `Enum`, and `Union`. That broader set means a Rust struct or a Java
//! interface has a qualified name in this module even though blast
//! cannot resolve it as a target. The two modules disagree on which
//! kinds count as declarations; the difference is documented so that a
//! future pass can align them deliberately rather than by accident.

use crate::ast::{NodeKind, NO_NODE};
use crate::cpg::{node_kind_wire_code, CodePropertyGraph, CpgEdge, CpgNode, EdgeKind};

/// A named declaration with the fields a caller typically needs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeclarationInfo {
    /// The CPG node id of the declaration.
    pub node_id: u32,
    /// The dot-joined qualified name, e.g. `Parser.parse`.
    pub qualified_name: String,
    /// The declaration's own name, without enclosing scopes.
    pub simple_name: String,
    /// The kind of declaration.
    pub kind: NodeKind,
    /// The path of the file the declaration lives in.
    pub file_path: String,
    /// One-based line number of the declaration's start.
    pub line: u32,
    /// Byte offset where the declaration begins.
    pub start_byte: u32,
    /// Byte offset one past the declaration's end.
    pub end_byte: u32,
}

/// Return whether a syntax kind declares a named symbol.
///
/// Broader than the blast crate's equivalent: includes class-like
/// constructs in every supported language, so that a Rust struct or a
/// Java interface has a qualified name.
pub fn is_declaration_kind(kind: NodeKind) -> bool {
    matches!(
        kind,
        NodeKind::FunctionDef
            | NodeKind::MethodDef
            | NodeKind::ClassDef
            | NodeKind::Struct
            | NodeKind::Interface
            | NodeKind::Trait
            | NodeKind::Enum
            | NodeKind::Union
    )
}

/// Return the AST parent of a node, or `None` if it is the root.
fn ast_parent(cpg: &CodePropertyGraph, node_id: u32) -> Option<u32> {
    for &edge_id in cpg.edges_to(node_id) {
        if let Some(edge) = cpg.edge(edge_id) {
            if edge.kind == EdgeKind::Ast {
                return Some(edge.src);
            }
        }
    }
    None
}

impl CodePropertyGraph {
    /// Return the number of nodes in the graph.
    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    /// Return the number of edges in the graph.
    pub fn edge_count(&self) -> usize {
        self.edges.len()
    }

    /// Return the number of files in the graph.
    pub fn file_count(&self) -> usize {
        self.files.len()
    }

    /// Return the node with the given id, or `None` if out of range.
    pub fn node(&self, id: u32) -> Option<&CpgNode> {
        self.nodes.get(id as usize)
    }

    /// Return the edge with the given id, or `None` if out of range.
    pub fn edge(&self, id: u32) -> Option<&CpgEdge> {
        self.edges.get(id as usize)
    }

    /// Return the interned string with the given id, or `None` if the
    /// id is the no-string sentinel or out of range.
    pub fn string(&self, id: u32) -> Option<&str> {
        if id == NO_NODE {
            return None;
        }
        self.strings.get(id as usize).map(String::as_str)
    }

    /// Return the path of the file with the given id, or `None`.
    pub fn file_path(&self, file_id: u32) -> Option<&str> {
        self.files.get(file_id as usize).map(String::as_str)
    }

    /// Return the id of the file with the given path, or `None`.
    pub fn file_id(&self, path: &str) -> Option<u32> {
        self.paths_to_file.get(path).copied()
    }

    /// Return the ids of every node with the given kind.
    pub fn nodes_by_kind(&self, kind: NodeKind) -> &[u32] {
        let code = node_kind_wire_code(kind);
        self.nodes_by_kind
            .get(&code)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// Return the ids of every node belonging to `file_id`.
    pub fn nodes_in_file(&self, file_id: u32) -> &[u32] {
        self.nodes_by_file
            .get(&file_id)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// Return the ids of every node whose name matches `name`.
    pub fn find_by_name(&self, name: &str) -> &[u32] {
        let string_id = match self.string_ids.get(name) {
            Some(&id) => id,
            None => return &[],
        };
        self.names_to_nodes
            .get(&string_id)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// Return the ids of every edge whose source is `node_id`.
    pub fn edges_from(&self, node_id: u32) -> &[u32] {
        self.edges_from_index
            .get(&node_id)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// Return the ids of every edge whose target is `node_id`.
    pub fn edges_to(&self, node_id: u32) -> &[u32] {
        self.edges_to_index
            .get(&node_id)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// Return the source node ids of every `Call` edge that points at
    /// `node_id`.
    pub fn callers_of(&self, node_id: u32) -> Vec<u32> {
        let mut result = Vec::new();
        for &edge_id in self.edges_to(node_id) {
            let edge = match self.edge(edge_id) {
                Some(edge) => edge,
                None => continue,
            };
            if edge.kind == EdgeKind::Call {
                result.push(edge.src);
            }
        }
        result
    }

    /// Return the target node ids of every `Call` edge from `node_id`.
    pub fn callees_of(&self, node_id: u32) -> Vec<u32> {
        let mut result = Vec::new();
        for &edge_id in self.edges_from(node_id) {
            let edge = match self.edge(edge_id) {
                Some(edge) => edge,
                None => continue,
            };
            if edge.kind == EdgeKind::Call {
                result.push(edge.dst);
            }
        }
        result
    }

    /// Return the id of the file the node belongs to, or `None`.
    pub fn file_of(&self, node_id: u32) -> Option<u32> {
        self.node(node_id).map(|node| node.file_id)
    }

    /// Reconstruct a node's qualified name by walking the AST parent
    /// chain.
    ///
    /// A top-level function `parse_config` returns `parse_config`. A
    /// method `parse` inside class `Parser` returns `Parser.parse`.
    /// Only declaration nodes contribute to the name; blocks,
    /// expressions, and other syntax nodes are skipped.
    pub fn qualified_name_of(&self, node_id: u32) -> String {
        let mut parts: Vec<String> = Vec::new();
        let mut current = Some(node_id);
        while let Some(id) = current {
            let node = match self.node(id) {
                Some(n) => n,
                None => break,
            };
            if is_declaration_kind(node.kind) {
                if let Some(name) = self.string(node.name) {
                    parts.push(name.to_owned());
                }
            }
            current = ast_parent(self, id);
        }
        parts.reverse();
        parts.join(".")
    }

    /// Return the id of the declaration in `file_id` whose qualified
    /// name matches, or `None` if no declaration matches.
    ///
    /// When more than one declaration shares the qualified name (a
    /// rare situation that requires duplicate definitions in one file),
    /// the first match by node id is returned.
    pub fn find_declaration(&self, file_id: u32, qualified_name: &str) -> Option<u32> {
        for &node_id in self.nodes_in_file(file_id) {
            let node = match self.node(node_id) {
                Some(n) => n,
                None => continue,
            };
            if !is_declaration_kind(node.kind) {
                continue;
            }
            if self.qualified_name_of(node_id) == qualified_name {
                return Some(node_id);
            }
        }
        None
    }

    /// Return the fields of a declaration as a `DeclarationInfo`
    /// record, or `None` if the node is not a declaration.
    pub fn declaration_info(&self, node_id: u32) -> Option<DeclarationInfo> {
        let node = self.node(node_id)?;
        if !is_declaration_kind(node.kind) {
            return None;
        }
        let simple_name = self.string(node.name)?.to_owned();
        let file_path = self.file_path(node.file_id)?.to_owned();
        let qualified_name = self.qualified_name_of(node_id);
        Some(DeclarationInfo {
            node_id,
            qualified_name,
            simple_name,
            kind: node.kind,
            file_path,
            line: node.start_row + 1,
            start_byte: node.start_byte,
            end_byte: node.end_byte,
        })
    }
}
