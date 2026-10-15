//! Program Dependence Graph over the CFG of one function.
//!
//! Two dependences are computed. Control dependence is derived from
//! the post-dominator tree of the CFG. Data dependence is derived from
//! intra-function reaching definitions: a definition reaches a use if
//! some CFG path from the definition to the use does not pass through
//! another definition of the same variable.
//!
//! Post-dominators are computed against a virtual exit node that is not
//! a real arena node and never appears in any emitted edge. The virtual
//! exit exists so the post-dominator lattice has a single bottom.
//!
//! # Determinism
//!
//! The algorithm uses `HashMap` and `HashSet` internally for successor
//! lists, predecessor lists, and reaching-definition sets. Iteration
//! order over those containers is not stable across processes. Before
//! the edges are returned, they are sorted by `(from, to, kind)` and
//! deduplicated. That makes the returned vector byte-stable regardless
//! of internal iteration order, which is what the CPG wire format
//! requires: two builds of the same source must produce identical
//! bytes so the cache keyed by commit hash has something stable to key
//! on.
//!
//! The invariant this module protects is that every emitted edge points
//! at a real arena node. The virtual exit is filtered out before any
//! edge is pushed, and unreachable nodes default to having the virtual
//! exit as their sole post-dominator rather than crashing.

use std::collections::{HashMap, HashSet};

use crate::ast::{AstArena, NodeKind, NO_NODE};
use crate::cfg::{Cfg, CfgEdge};

/// The virtual exit used during post-dominator computation.
///
/// It is distinct from `NO_NODE` so the code can distinguish "no
/// post-dominator" from "post-dominator is the function's exit".
const VIRTUAL_EXIT: u32 = u32::MAX - 1;

/// The kind of a PDG edge.
///
/// The variants are ordered so that `PdgEdge::sort` produces a stable
/// canonical order. The declaration order is the sort order and must
/// not change without a format version bump.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum PdgEdgeKind {
    /// The target executes only because the source's branch was taken.
    ControlDependence,
    /// The target reads a value the source defined.
    DataDependence,
}

/// One edge of the program dependence graph.
///
/// `Ord` is derived so a vector of edges can be sorted into a canonical
/// order before being returned. The order is `(from, to, kind)`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct PdgEdge {
    /// Source node id in the arena.
    pub from: u32,
    /// Target node id in the arena.
    pub to: u32,
    /// The kind of dependence this edge represents.
    pub kind: PdgEdgeKind,
}

/// The program dependence graph for one function.
#[derive(Debug)]
pub struct Pdg {
    /// The arena index of the function node.
    pub function_node: u32,
    /// Every edge, sorted by `(from, to, kind)` and deduplicated.
    pub edges: Vec<PdgEdge>,
}

/// Build the PDG for the function whose CFG is `cfg`.
///
/// The returned edge vector is sorted and deduplicated. Two calls with
/// the same CFG produce byte-identical output.
pub fn build_pdg(arena: &AstArena, cfg: &Cfg) -> Pdg {
    let (successors, all_nodes) = collect_cfg(cfg);
    let ipdom = compute_ipdom(&successors, &all_nodes);

    let mut edges: Vec<PdgEdge> = Vec::new();
    compute_control_dependence(cfg, &ipdom, &mut edges);
    compute_data_dependence(arena, cfg, &successors, &all_nodes, &mut edges);

    edges.sort();
    edges.dedup();

    Pdg {
        function_node: cfg.function_node,
        edges,
    }
}

/// Collect every node and its successors from the CFG, adding a virtual
/// exit as the single successor of every terminal node.
fn collect_cfg(cfg: &Cfg) -> (HashMap<u32, Vec<u32>>, Vec<u32>) {
    let mut successors: HashMap<u32, Vec<u32>> = HashMap::new();
    let mut nodes: HashSet<u32> = HashSet::new();
    nodes.insert(cfg.function_node);

    for edge in &cfg.edges {
        nodes.insert(edge.from);
        nodes.insert(edge.to);
        successors.entry(edge.from).or_default().push(edge.to);
    }

    let terminals: Vec<u32> = nodes
        .iter()
        .copied()
        .filter(|n| successors.get(n).is_none_or(Vec::is_empty))
        .collect();
    for terminal in terminals {
        successors.entry(terminal).or_default().push(VIRTUAL_EXIT);
    }

    nodes.insert(VIRTUAL_EXIT);
    successors.entry(VIRTUAL_EXIT).or_default();

    let mut node_list: Vec<u32> = nodes.into_iter().collect();
    node_list.sort_unstable();
    (successors, node_list)
}

/// Compute the immediate post-dominator of every CFG node.
///
/// Uses the Cooper-Harvey-Kennedy algorithm on the reversed CFG with
/// the virtual exit as the entry of the reversed graph. A node not
/// reachable from the virtual exit in the reversed graph — an infinite
/// loop with no exit — has no entry in the returned map.
fn compute_ipdom(successors: &HashMap<u32, Vec<u32>>, all_nodes: &[u32]) -> HashMap<u32, u32> {
    let mut reversed: HashMap<u32, Vec<u32>> = HashMap::new();
    for &node in all_nodes {
        reversed.entry(node).or_default();
    }
    for (&from, tos) in successors {
        for &to in tos {
            reversed.entry(to).or_default().push(from);
        }
    }
    compute_idom(&reversed, VIRTUAL_EXIT)
}

/// Compute immediate dominators of a directed graph.
fn compute_idom(successors: &HashMap<u32, Vec<u32>>, entry: u32) -> HashMap<u32, u32> {
    let rpo = compute_rpo(successors, entry);
    let rpo_index: HashMap<u32, usize> = rpo.iter().enumerate().map(|(i, &n)| (n, i)).collect();

    let mut predecessors: HashMap<u32, Vec<u32>> = HashMap::new();
    for (&from, tos) in successors {
        for &to in tos {
            predecessors.entry(to).or_default().push(from);
        }
    }

    let mut idom: HashMap<u32, u32> = HashMap::new();
    idom.insert(entry, entry);

    let mut changed = true;
    let mut iterations = 0usize;
    let max_iterations = rpo.len().saturating_mul(4).max(16);
    while changed && iterations < max_iterations {
        changed = false;
        iterations += 1;
        for &node in &rpo {
            if node == entry {
                continue;
            }
            let preds = match predecessors.get(&node) {
                Some(p) => p,
                None => continue,
            };
            let mut new_idom: Option<u32> = None;
            for &pred in preds {
                if !idom.contains_key(&pred) {
                    continue;
                }
                new_idom = Some(match new_idom {
                    None => pred,
                    Some(current) => intersect(&idom, &rpo_index, pred, current),
                });
            }
            if let Some(ni) = new_idom {
                if idom.get(&node) != Some(&ni) {
                    idom.insert(node, ni);
                    changed = true;
                }
            }
        }
    }
    idom
}

/// Walk two dominator-tree ancestors up to their common ancestor.
fn intersect(
    idom: &HashMap<u32, u32>,
    rpo_index: &HashMap<u32, usize>,
    mut a: u32,
    mut b: u32,
) -> u32 {
    let a_key = |node: u32| rpo_index.get(&node).copied().unwrap_or(usize::MAX);
    while a != b {
        while a_key(a) > a_key(b) {
            match idom.get(&a) {
                Some(&next) => a = next,
                None => return a,
            }
        }
        while a_key(b) > a_key(a) {
            match idom.get(&b) {
                Some(&next) => b = next,
                None => return b,
            }
        }
    }
    a
}

/// Reverse postorder of a directed graph reachable from `entry`.
fn compute_rpo(successors: &HashMap<u32, Vec<u32>>, entry: u32) -> Vec<u32> {
    let mut visited: HashSet<u32> = HashSet::new();
    let mut postorder: Vec<u32> = Vec::new();

    let mut stack: Vec<(u32, usize)> = vec![(entry, 0)];
    visited.insert(entry);

    while let Some((node, idx)) = stack.last().copied() {
        let succs = successors.get(&node).map(Vec::as_slice).unwrap_or(&[]);
        if idx >= succs.len() {
            postorder.push(node);
            stack.pop();
        } else {
            let child = succs[idx];
            if let Some(top) = stack.last_mut() {
                top.1 = idx + 1;
            }
            if visited.insert(child) {
                stack.push((child, 0));
            }
        }
    }

    postorder.reverse();
    postorder
}

/// Emit control-dependence edges for every CFG edge.
///
/// For an edge `a -> b` where `b` is not the immediate post-dominator
/// of `a`, every node on the path from `b` up to `ipdom[a]` in the
/// post-dominator tree is control-dependent on `a`. `a` itself is
/// excluded from the walk so a loop header is never marked
/// control-dependent on itself.
fn compute_control_dependence(cfg: &Cfg, ipdom: &HashMap<u32, u32>, edges: &mut Vec<PdgEdge>) {
    for edge in &cfg.edges {
        add_control_dependence_for_edge(edge, ipdom, edges);
    }
}

/// Emit control-dependence edges for one CFG edge.
fn add_control_dependence_for_edge(
    cfg_edge: &CfgEdge,
    ipdom: &HashMap<u32, u32>,
    edges: &mut Vec<PdgEdge>,
) {
    let a = cfg_edge.from;
    let b = cfg_edge.to;
    if b == VIRTUAL_EXIT {
        return;
    }
    let a_ipdom = ipdom.get(&a).copied().unwrap_or(VIRTUAL_EXIT);
    if b == a_ipdom {
        return;
    }

    let mut current = b;
    let mut safety = 0usize;
    let max_steps = ipdom.len().saturating_mul(2).max(64);
    while current != a_ipdom && current != VIRTUAL_EXIT && safety < max_steps {
        if current != a {
            edges.push(PdgEdge {
                from: a,
                to: current,
                kind: PdgEdgeKind::ControlDependence,
            });
        }
        current = match ipdom.get(&current) {
            Some(&next) => next,
            None => break,
        };
        safety += 1;
    }
}

/// Emit data-dependence edges from intra-function reaching definitions.
///
/// Two passes. The first extracts, for every CFG node, the variables it
/// defines and the variables it uses. The second runs the reaching-
/// definition dataflow to a fixed point and emits an edge from every
/// reaching definition to every use it reaches.
fn compute_data_dependence(
    arena: &AstArena,
    cfg: &Cfg,
    successors: &HashMap<u32, Vec<u32>>,
    all_nodes: &[u32],
    edges: &mut Vec<PdgEdge>,
) {
    let (defs_by_node, uses_by_node) = extract_def_use_map(arena, cfg, all_nodes);

    let mut predecessors: HashMap<u32, Vec<u32>> = HashMap::new();
    for (&from, tos) in successors {
        for &to in tos {
            predecessors.entry(to).or_default().push(from);
        }
    }

    type DefSet = HashSet<(u32, String)>;
    let mut in_sets: HashMap<u32, DefSet> = HashMap::new();
    let mut out_sets: HashMap<u32, DefSet> = HashMap::new();
    for &node in all_nodes {
        in_sets.insert(node, DefSet::new());
        out_sets.insert(node, DefSet::new());
    }

    let max_iterations = all_nodes.len().saturating_mul(4).max(16);
    let mut iterations = 0usize;
    let mut changed = true;
    while changed && iterations < max_iterations {
        changed = false;
        iterations += 1;
        for &node in all_nodes {
            if node == VIRTUAL_EXIT {
                continue;
            }
            let new_in = union_predecessor_outs(&predecessors, &out_sets, node);
            let new_out = transfer(&new_in, defs_by_node.get(&node), node);
            if in_sets.get(&node) != Some(&new_in) {
                in_sets.insert(node, new_in);
                changed = true;
            }
            if out_sets.get(&node) != Some(&new_out) {
                out_sets.insert(node, new_out);
                changed = true;
            }
        }
    }

    for (&stmt, uses) in &uses_by_node {
        let empty: DefSet = DefSet::new();
        let reaching = in_sets.get(&stmt).unwrap_or(&empty);
        for (def_stmt, def_var) in reaching {
            if uses.contains(def_var) && *def_stmt != stmt {
                edges.push(PdgEdge {
                    from: *def_stmt,
                    to: stmt,
                    kind: PdgEdgeKind::DataDependence,
                });
            }
        }
    }
}

/// Union of the OUT sets of every predecessor of `node`.
fn union_predecessor_outs(
    predecessors: &HashMap<u32, Vec<u32>>,
    out_sets: &HashMap<u32, HashSet<(u32, String)>>,
    node: u32,
) -> HashSet<(u32, String)> {
    let mut result: HashSet<(u32, String)> = HashSet::new();
    if let Some(preds) = predecessors.get(&node) {
        for pred in preds {
            if let Some(out) = out_sets.get(pred) {
                result.extend(out.iter().cloned());
            }
        }
    }
    result
}

/// Apply a node's transfer function: OUT = GEN union (IN minus KILL).
fn transfer(
    in_set: &HashSet<(u32, String)>,
    defs: Option<&Vec<String>>,
    node: u32,
) -> HashSet<(u32, String)> {
    let mut out: HashSet<(u32, String)> = HashSet::new();
    let empty: Vec<String> = Vec::new();
    let defs = defs.unwrap_or(&empty);
    for var in defs {
        out.insert((node, var.clone()));
    }
    for entry in in_set {
        if !defs.contains(&entry.1) {
            out.insert(entry.clone());
        }
    }
    out
}

/// Extract the definition and use sets for every CFG node.
fn extract_def_use_map(
    arena: &AstArena,
    cfg: &Cfg,
    all_nodes: &[u32],
) -> (HashMap<u32, Vec<String>>, HashMap<u32, Vec<String>>) {
    let mut defs_by_node: HashMap<u32, Vec<String>> = HashMap::new();
    let mut uses_by_node: HashMap<u32, Vec<String>> = HashMap::new();

    let params = extract_parameters(arena, cfg.function_node);
    if !params.is_empty() {
        defs_by_node.insert(cfg.function_node, params);
    }

    for &node in all_nodes {
        if node == VIRTUAL_EXIT || node == cfg.function_node || node == NO_NODE {
            continue;
        }
        let (defs, uses) = extract_def_use(arena, node);
        if !defs.is_empty() {
            defs_by_node.insert(node, defs);
        }
        if !uses.is_empty() {
            uses_by_node.insert(node, uses);
        }
    }

    (defs_by_node, uses_by_node)
}

/// Extract the parameter names of a function.
fn extract_parameters(arena: &AstArena, function_node: u32) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    for child in arena.children_of(function_node) {
        let kind = match arena.get(child) {
            Some(n) => n.kind,
            None => continue,
        };
        if kind != NodeKind::Parameter {
            continue;
        }
        for id in arena.walk_preorder(child) {
            let node = match arena.get(id) {
                Some(n) => n,
                None => continue,
            };
            if node.kind != NodeKind::Identifier {
                continue;
            }
            if let Some(name) = arena.string(node.name) {
                if !names.iter().any(|existing| existing == name) {
                    names.push(name.to_owned());
                }
            }
        }
    }
    names
}

/// Extract the variables a statement defines and the variables it uses.
fn extract_def_use(arena: &AstArena, node_id: u32) -> (Vec<String>, Vec<String>) {
    let mut def_nodes: HashSet<u32> = HashSet::new();
    collect_all_targets(arena, node_id, &mut def_nodes);

    let mut defs: Vec<String> = Vec::new();
    let mut uses: Vec<String> = Vec::new();

    for id in arena.walk_preorder(node_id) {
        let node = match arena.get(id) {
            Some(n) => n,
            None => continue,
        };
        if node.kind != NodeKind::Identifier {
            continue;
        }
        let name = match arena.string(node.name) {
            Some(s) => s,
            None => continue,
        };
        if def_nodes.contains(&id) {
            push_unique(&mut defs, name);
        } else {
            push_unique(&mut uses, name);
        }
    }

    (defs, uses)
}

/// Walk a subtree and record the identifiers that are definition targets.
fn collect_all_targets(arena: &AstArena, node_id: u32, targets: &mut HashSet<u32>) {
    for id in arena.walk_preorder(node_id) {
        let kind = match arena.get(id) {
            Some(n) => n.kind,
            None => continue,
        };
        match kind {
            NodeKind::Assignment | NodeKind::AugmentedAssignment => {
                if let Some(lhs) = arena.children_of(id).next() {
                    collect_target(arena, lhs, targets);
                }
            }
            NodeKind::For => {
                if let Some(target) = arena.children_of(id).next() {
                    collect_target(arena, target, targets);
                }
            }
            _ => {}
        }
    }
}

/// Record the identifiers in one definition target.
fn collect_target(arena: &AstArena, node_id: u32, targets: &mut HashSet<u32>) {
    let kind = match arena.get(node_id) {
        Some(n) => n.kind,
        None => return,
    };
    match kind {
        NodeKind::Identifier => {
            targets.insert(node_id);
        }
        NodeKind::Tuple | NodeKind::List => {
            for child in arena.children_of(node_id) {
                collect_target(arena, child, targets);
            }
        }
        _ => {}
    }
}

/// Push `value` onto `target` if it is not already present.
fn push_unique(target: &mut Vec<String>, value: &str) {
    if !target.iter().any(|existing| existing == value) {
        target.push(value.to_owned());
    }
}
