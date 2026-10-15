//! Incremental CPG update.
//!
//! A file-level incremental update re-parses only the file that changed
//! and patches the existing graph. Unchanged files are not re-parsed;
//! their nodes and edges are preserved by compaction and remapping.
//!
//! The algorithm has seven steps. Look up the file. Collect its nodes.
//! Collect inbound edges that cross into it from other files, keeping
//! their labels. Remove the file's nodes and every edge that touched
//! them. Compact the surviving nodes and remap the surviving edges.
//! Parse the new content, append its nodes and edges, and re-resolve
//! every preserved inbound edge by label against the new file's
//! symbols. Deduplicate.
//!
//! The invariant this module protects is that no edge survives with a
//! `dst` that points at a node not in the graph. An inbound edge whose
//! target could not be re-resolved is dropped, not left dangling.

use std::collections::{HashMap, HashSet};

use crate::ast::NO_NODE;
use crate::call_graph::extract_calls;
use crate::cfg::build_cfg;
use crate::cpg::{
    cfg_edge_kind_to_edge_kind, collect_functions, intern_string_into, pdg_edge_kind_to_edge_kind,
    CodePropertyGraph, CpgEdge, CpgError, CpgNode, EdgeKind, SourceFile,
};
use crate::parser::parse_file;
use crate::pdg::build_pdg;
use crate::symbol_table::{extract_symbols, SymbolTable};

impl CodePropertyGraph {
    /// Apply a file-level incremental update.
    ///
    /// The `changed.path` must already exist in the graph's file table.
    /// Adding a new file is not supported by this method.
    ///
    /// Cross-file edges whose source is in another file and whose
    /// target was in the changed file are re-resolved against the new
    /// file's symbols by the edge's label. An edge whose label no
    /// longer resolves to a unique symbol is dropped.
    ///
    /// Returns `CpgError::Corrupt` if the path is not in the graph, or
    /// `CpgError::Parse` if the new content fails to parse.
    pub fn apply_incremental(&mut self, changed: &SourceFile) -> Result<(), CpgError> {
        let file_id = match self.paths_to_file.get(&changed.path).copied() {
            Some(id) => id,
            None => {
                return Err(CpgError::Corrupt {
                    reason: format!("file not in graph: {}", changed.path),
                });
            }
        };

        let removed: HashSet<u32> = self
            .nodes_by_file
            .get(&file_id)
            .map(|ids| ids.iter().copied().collect())
            .unwrap_or_default();

        let mut inbound: Vec<(u32, EdgeKind, u32)> = Vec::new();
        let mut kept: Vec<CpgEdge> = Vec::with_capacity(self.edges.len());
        for edge in &self.edges {
            let src_removed = removed.contains(&edge.src);
            let dst_removed = removed.contains(&edge.dst);
            match (src_removed, dst_removed) {
                (true, true) => continue,
                (true, false) => continue,
                (false, true) => inbound.push((edge.src, edge.kind, edge.label)),
                (false, false) => kept.push(edge.clone()),
            }
        }

        let mut remap: HashMap<u32, u32> = HashMap::with_capacity(self.nodes.len());
        let mut compacted: Vec<CpgNode> = Vec::with_capacity(self.nodes.len() - removed.len());
        for (old_id, node) in self.nodes.iter().enumerate() {
            let old_id = old_id as u32;
            if removed.contains(&old_id) {
                continue;
            }
            let new_id = compacted.len() as u32;
            remap.insert(old_id, new_id);
            compacted.push(node.clone());
        }

        for edge in &mut kept {
            if let Some(&new_src) = remap.get(&edge.src) {
                edge.src = new_src;
            }
            if let Some(&new_dst) = remap.get(&edge.dst) {
                edge.dst = new_dst;
            }
        }

        self.nodes = compacted;
        self.edges = kept;

        let parsed = parse_file(changed.language, &changed.content)?;
        let offset = self.nodes.len() as u32;

        let raw_symbols = extract_symbols(&parsed.arena, parsed.root, file_id);
        let mut local_symbols = SymbolTable::new();
        for mut symbol in raw_symbols {
            symbol.node_id = symbol.node_id.saturating_add(offset);
            local_symbols.insert(symbol);
        }

        let global_index = build_global_name_index(&self.nodes, &self.strings);

        for arena_id in 0..parsed.arena.len() as u32 {
            let ast_node = match parsed.arena.get(arena_id) {
                Some(node) => node,
                None => continue,
            };
            let name_id = match parsed.arena.string(ast_node.name) {
                Some(name) => intern_string_into(name, &mut self.strings, &mut self.string_ids),
                None => NO_NODE,
            };
            self.nodes.push(CpgNode {
                kind: ast_node.kind,
                flags: ast_node.flags,
                file_id,
                start_byte: ast_node.start_byte,
                end_byte: ast_node.end_byte,
                start_row: ast_node.start_row,
                start_col: ast_node.start_col,
                name: name_id,
            });

            if ast_node.parent != NO_NODE {
                self.edges.push(CpgEdge {
                    src: ast_node.parent.saturating_add(offset),
                    dst: arena_id.saturating_add(offset),
                    kind: EdgeKind::Ast,
                    label: NO_NODE,
                });
            }
        }

        let calls = extract_calls(&parsed.arena, &local_symbols, parsed.root);
        for call in calls {
            if call.caller_node_id == NO_NODE {
                continue;
            }
            let callee_id = if call.callee_node_id != NO_NODE {
                Some(call.callee_node_id)
            } else {
                resolve_global(&call.callee_name, &global_index)
            };
            let callee_id = match callee_id {
                Some(id) => id,
                None => continue,
            };
            let label =
                intern_string_into(&call.callee_name, &mut self.strings, &mut self.string_ids);
            self.edges.push(CpgEdge {
                src: call.caller_node_id.saturating_add(offset),
                dst: callee_id,
                kind: EdgeKind::Call,
                label,
            });
        }

        let functions = collect_functions(&parsed.arena, parsed.root);
        for function_node in functions {
            let cfg = build_cfg(&parsed.arena, function_node);
            for edge in &cfg.edges {
                self.edges.push(CpgEdge {
                    src: edge.from.saturating_add(offset),
                    dst: edge.to.saturating_add(offset),
                    kind: cfg_edge_kind_to_edge_kind(edge.kind),
                    label: NO_NODE,
                });
            }
            let pdg = build_pdg(&parsed.arena, &cfg);
            for edge in &pdg.edges {
                self.edges.push(CpgEdge {
                    src: edge.from.saturating_add(offset),
                    dst: edge.to.saturating_add(offset),
                    kind: pdg_edge_kind_to_edge_kind(edge.kind),
                    label: NO_NODE,
                });
            }
        }

        let new_symbols = extract_symbols(&parsed.arena, parsed.root, file_id);
        let mut new_symbol_map: HashMap<String, Vec<u32>> = HashMap::new();
        for symbol in &new_symbols {
            new_symbol_map
                .entry(symbol.simple_name.clone())
                .or_default()
                .push(symbol.node_id.saturating_add(offset));
        }

        for (external_src, kind, label) in inbound {
            if label == NO_NODE {
                continue;
            }
            let label_text = match self.strings.get(label as usize) {
                Some(text) => text.clone(),
                None => continue,
            };
            let matches = match new_symbol_map.get(&label_text) {
                Some(list) => list,
                None => continue,
            };
            if matches.len() != 1 {
                continue;
            }
            let new_src = match remap.get(&external_src) {
                Some(&id) => id,
                None => continue,
            };
            self.edges.push(CpgEdge {
                src: new_src,
                dst: matches[0],
                kind,
                label,
            });
        }

        let mut seen: HashSet<(u32, u32, u8, u32)> = HashSet::with_capacity(self.edges.len());
        self.edges
            .retain(|edge| seen.insert((edge.src, edge.dst, edge.kind.wire_code(), edge.label)));

        self.rebuild_indices();
        Ok(())
    }
}

/// Build a `simple_name -> Vec<cpg_node_id>` index for every function,
/// method, and class declared in the graph.
fn build_global_name_index(nodes: &[CpgNode], strings: &[String]) -> HashMap<String, Vec<u32>> {
    let mut index: HashMap<String, Vec<u32>> = HashMap::new();
    for (id, node) in nodes.iter().enumerate() {
        let is_declaration = matches!(
            node.kind,
            crate::ast::NodeKind::FunctionDef
                | crate::ast::NodeKind::MethodDef
                | crate::ast::NodeKind::ClassDef
        );
        if !is_declaration || node.name == NO_NODE {
            continue;
        }
        let name = match strings.get(node.name as usize) {
            Some(text) => text.clone(),
            None => continue,
        };
        index.entry(name).or_default().push(id as u32);
    }
    index
}

/// Resolve a callee name against the graph-wide name index.
///
/// Returns the id of the unique match, or `None` if the name is absent
/// or ambiguous.
fn resolve_global(name: &str, index: &HashMap<String, Vec<u32>>) -> Option<u32> {
    let matches = index.get(name)?;
    if matches.len() == 1 {
        Some(matches[0])
    } else {
        None
    }
}
