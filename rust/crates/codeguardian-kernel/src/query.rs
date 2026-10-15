//! Query interface for the Code Property Graph.
//!
//! Every method here is O(1) after index construction. Indices are
//! built at graph finalization time and rebuilt by `apply_incremental`.
//! The methods that return `&[u32]` hand back a slice that borrows from
//! an existing index; the methods that return `Vec<u32>` allocate
//! because they filter by edge kind.

use crate::ast::{NodeKind, NO_NODE};
use crate::cpg::{node_kind_wire_code, CodePropertyGraph, CpgEdge, CpgNode, EdgeKind};

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
}
