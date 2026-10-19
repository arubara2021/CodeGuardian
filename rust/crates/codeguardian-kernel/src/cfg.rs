//! Per-function Control Flow Graph over statement-level AST nodes.
//!
//! A CFG is built for one function at a time. The function node is the
//! entry. Every `return` and every `raise` is a terminator — a node with
//! no outgoing CFG edge. No synthetic entry or exit nodes are stored;
//! the "function exit" is a virtual concept that exists only during
//! post-dominator computation in `pdg.rs`.
//!
//! The builder models the tree-sitter structure directly. A block is a
//! sequence of statements. Control-flow statements (`If`, `While`,
//! `For`, `Loop`, `SelectStatement`, `Try`, `With`, `Synchronized`,
//! `Lock`) each produce their own sub-CFG, and their exits are threaded
//! into the enclosing sequence.
//!
//! The invariant this module protects is that every edge's source and
//! target are real arena node ids. The `Block` internal type carries
//! exits as `(node_id, ExitKind)` pairs; a `Break` exit is not
//! resolved until the enclosing sequence threads it to the next
//! statement, so break's target is always a real node and never a
//! placeholder.

use std::collections::HashMap;

use crate::ast::{AstArena, NodeKind, NO_NODE};

/// The kind of a CFG edge.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum CfgEdgeKind {
    /// Sequential flow from one statement to the next.
    Normal,
    /// The conditional's true branch was taken.
    True,
    /// The conditional's false branch was taken.
    False,
    /// A loop body returned to its header.
    LoopBack,
    /// A break transfer: control left the innermost enclosing loop.
    Break,
    /// A continue transfer: control returned to the innermost
    /// enclosing loop header.
    Continue,
    /// A try region's syntactic entry into an except handler.
    Exception,
}

/// One edge of the control flow graph.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CfgEdge {
    /// Source node id in the arena.
    pub from: u32,
    /// Target node id in the arena.
    pub to: u32,
    /// The kind of control flow this edge represents.
    pub kind: CfgEdgeKind,
}

/// The control flow graph for one function.
#[derive(Debug)]
pub struct Cfg {
    /// The arena index of the function node. The entry of the CFG.
    pub function_node: u32,
    /// Every edge, in insertion order.
    pub edges: Vec<CfgEdge>,
}

/// Build the CFG for the function rooted at `function_node`.
///
/// Returns a graph whose entry is the function node itself. If the
/// function has no body block, the returned CFG contains only the
/// function node and no edges.
pub fn build_cfg(arena: &AstArena, function_node: u32) -> Cfg {
    let mut builder = CfgBuilder::new(arena);
    let body = find_function_body(arena, function_node);
    if let Some(body_node) = body {
        let body_cfg = builder.build_block(body_node);
        if body_cfg.entry != NO_NODE {
            builder.add_edge(function_node, body_cfg.entry, CfgEdgeKind::Normal);
        }
    }
    Cfg {
        function_node,
        edges: builder.edges,
    }
}

/// Return the direct `Block` child of a function node, if any.
///
/// A Python `function_definition` and a TypeScript `function_declaration`
/// both place the body block as a direct child. An arrow function with
/// an expression body has no block and returns `None`.
fn find_function_body(arena: &AstArena, function_node: u32) -> Option<u32> {
    arena
        .children_of(function_node)
        .find(|&child| is_block(arena, child))
}

/// Return whether the node's kind is `Block`.
fn is_block(arena: &AstArena, node_id: u32) -> bool {
    matches!(arena.get(node_id).map(|n| n.kind), Some(NodeKind::Block))
}

/// Return whether the node's kind is `Except`.
fn is_except(arena: &AstArena, node_id: u32) -> bool {
    matches!(arena.get(node_id).map(|n| n.kind), Some(NodeKind::Except))
}

/// Return whether the node's kind is `Finally`.
fn is_finally(arena: &AstArena, node_id: u32) -> bool {
    matches!(arena.get(node_id).map(|n| n.kind), Some(NodeKind::Finally))
}

/// How a `Block`'s execution can leave.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum ExitKind {
    /// Falls through to the next statement.
    Normal,
    /// Transfers to the innermost enclosing loop's after-point.
    Break,
    /// Transfers to the innermost enclosing loop's header.
    Continue,
    /// Returns from the enclosing function.
    Return,
    /// Raises out of the enclosing function.
    Raise,
}

/// A sub-CFG: a single entry and a set of exits.
///
/// `entry` is `NO_NODE` when the block is empty. Each exit carries the
/// node id and how control leaves it.
#[derive(Debug)]
struct Block {
    entry: u32,
    exits: Vec<(u32, ExitKind)>,
}

impl Block {
    fn empty() -> Self {
        Block {
            entry: NO_NODE,
            exits: Vec::new(),
        }
    }
}

/// State carried through the recursive build.
struct CfgBuilder<'a> {
    arena: &'a AstArena,
    edges: Vec<CfgEdge>,
}

impl<'a> CfgBuilder<'a> {
    fn new(arena: &'a AstArena) -> Self {
        Self {
            arena,
            edges: Vec::new(),
        }
    }

    fn add_edge(&mut self, from: u32, to: u32, kind: CfgEdgeKind) {
        if from == NO_NODE || to == NO_NODE {
            return;
        }
        self.edges.push(CfgEdge { from, to, kind });
    }

    /// Return the first direct child of `node_id` that is a `Block`.
    fn first_block_child(&self, node_id: u32) -> Option<u32> {
        self.arena
            .children_of(node_id)
            .find(|&child| is_block(self.arena, child))
    }

    /// Return the first direct child of `node_id` that is an `If`.
    fn first_if_child(&self, node_id: u32) -> Option<u32> {
        for child in self.arena.children_of(node_id) {
            if matches!(self.arena.get(child).map(|n| n.kind), Some(NodeKind::If)) {
                return Some(child);
            }
        }
        None
    }

    /// Return whether a node is a statement the CFG should include.
    fn is_statement(&self, node_id: u32) -> bool {
        match self.arena.get(node_id) {
            Some(n) => !n.is_extra(),
            None => false,
        }
    }

    /// Build the CFG for a sequence of statements inside `block_node`.
    fn build_block(&mut self, block_node: u32) -> Block {
        let statements: Vec<u32> = self
            .arena
            .children_of(block_node)
            .filter(|&c| self.is_statement(c))
            .collect();

        let mut result = Block::empty();
        for stmt in statements {
            let sub = self.build_statement(stmt);
            result = self.thread(result, sub);
        }
        result
    }

    /// Build the CFG for one statement.
    fn build_statement(&mut self, node_id: u32) -> Block {
        let kind = match self.arena.get(node_id) {
            Some(n) => n.kind,
            None => return Block::empty(),
        };
        match kind {
            NodeKind::If => self.build_if(node_id),
            NodeKind::While
            | NodeKind::For
            | NodeKind::Loop
            | NodeKind::SelectStatement => self.build_loop(node_id),
            NodeKind::Try => self.build_try(node_id),
            NodeKind::With | NodeKind::Synchronized | NodeKind::Lock => {
                self.build_with(node_id)
            }
            NodeKind::Return | NodeKind::Yield => {
                self.build_terminator(node_id, ExitKind::Return)
            }
            NodeKind::Raise => self.build_terminator(node_id, ExitKind::Raise),
            NodeKind::Break => self.build_terminator(node_id, ExitKind::Break),
            NodeKind::Continue => self.build_terminator(node_id, ExitKind::Continue),
            _ => self.build_simple(node_id),
        }
    }

    /// A statement that runs and falls through.
    fn build_simple(&mut self, node_id: u32) -> Block {
        Block {
            entry: node_id,
            exits: vec![(node_id, ExitKind::Normal)],
        }
    }

    /// A statement that terminates its enclosing function or loop.
    fn build_terminator(&mut self, node_id: u32, kind: ExitKind) -> Block {
        Block {
            entry: node_id,
            exits: vec![(node_id, kind)],
        }
    }

    /// Thread `b` after `a`.
    ///
    /// `a`'s `Normal` and `Break` exits are connected to `b`'s entry.
    /// A `Break` exit becomes a `Break` edge here because break
    /// transfers control out of the innermost loop, and once the loop
    /// is left the next statement is its target.
    fn thread(&mut self, a: Block, b: Block) -> Block {
        if a.entry == NO_NODE {
            return b;
        }
        if b.entry == NO_NODE {
            return a;
        }

        let mut new_exits: Vec<(u32, ExitKind)> = Vec::new();
        let mut a_flows = false;

        for (exit, kind) in a.exits {
            match kind {
                ExitKind::Normal => {
                    self.add_edge(exit, b.entry, CfgEdgeKind::Normal);
                    a_flows = true;
                }
                ExitKind::Break => {
                    self.add_edge(exit, b.entry, CfgEdgeKind::Break);
                    a_flows = true;
                }
                other => {
                    new_exits.push((exit, other));
                }
            }
        }

        if !a_flows {
            return Block {
                entry: a.entry,
                exits: new_exits,
            };
        }

        new_exits.extend(b.exits);
        Block {
            entry: a.entry,
            exits: new_exits,
        }
    }

    /// Build the CFG for an `if`/`elif`/`else` chain.
    fn build_if(&mut self, if_node: u32) -> Block {
        let (then_block, else_chain) = self.partition_if_children(if_node);

        let then_cfg = match then_block {
            Some(b) => self.build_block(b),
            None => Block::empty(),
        };
        if then_cfg.entry != NO_NODE {
            self.add_edge(if_node, then_cfg.entry, CfgEdgeKind::True);
        }

        let else_result = self.build_else_chain(&else_chain);

        let mut exits = then_cfg.exits;
        match else_result {
            Some((else_entry, else_exits)) => {
                self.add_edge(if_node, else_entry, CfgEdgeKind::False);
                exits.extend(else_exits);
            }
            None => {
                exits.push((if_node, ExitKind::Normal));
            }
        }

        Block {
            entry: if_node,
            exits,
        }
    }

    /// Split an `if` node's direct children into the then-block and the
    /// chain of `elif`/`else` clauses that follow it.
    fn partition_if_children(&self, if_node: u32) -> (Option<u32>, Vec<u32>) {
        let mut then_block = None;
        let mut else_chain = Vec::new();
        let children: Vec<u32> = self.arena.children_of(if_node).collect();
        for child in children {
            let kind = match self.arena.get(child) {
                Some(n) => n.kind,
                None => continue,
            };
            match kind {
                NodeKind::Block if then_block.is_none() => then_block = Some(child),
                NodeKind::Elif | NodeKind::Else => else_chain.push(child),
                _ => {}
            }
        }
        (then_block, else_chain)
    }

    /// Build the CFG for a chain of `elif` and `else` clauses.
    ///
    /// Returns `None` for an empty chain. Otherwise returns the entry
    /// node of the first clause and the combined exits of every clause.
    fn build_else_chain(&mut self, chain: &[u32]) -> Option<(u32, Vec<(u32, ExitKind)>)> {
        if chain.is_empty() {
            return None;
        }
        let first = chain[0];
        let rest = &chain[1..];
        let kind = self.arena.get(first)?.kind;

        match kind {
            NodeKind::Elif => self.build_elif(first, rest),
            NodeKind::Else => self.build_else(first),
            _ => None,
        }
    }

    /// Build one `elif` clause.
    fn build_elif(&mut self, elif_node: u32, rest: &[u32]) -> Option<(u32, Vec<(u32, ExitKind)>)> {
        let body = self.first_block_child(elif_node);
        let body_cfg = match body {
            Some(b) => self.build_block(b),
            None => Block::empty(),
        };

        let mut exits: Vec<(u32, ExitKind)> = Vec::new();
        if body_cfg.entry != NO_NODE {
            self.add_edge(elif_node, body_cfg.entry, CfgEdgeKind::True);
            exits.extend(body_cfg.exits);
        } else {
            exits.push((elif_node, ExitKind::Normal));
        }

        match self.build_else_chain(rest) {
            Some((next_entry, next_exits)) => {
                self.add_edge(elif_node, next_entry, CfgEdgeKind::False);
                exits.extend(next_exits);
            }
            None => {
                exits.push((elif_node, ExitKind::Normal));
            }
        }

        dedup_exits(&mut exits);
        Some((elif_node, exits))
    }

    /// Build one `else` clause.
    fn build_else(&mut self, else_node: u32) -> Option<(u32, Vec<(u32, ExitKind)>)> {
        let body = self.first_block_child(else_node);
        let body_cfg = match body {
            Some(b) => self.build_block(b),
            None => {
                if let Some(nested_if) = self.first_if_child(else_node) {
                    let nested_cfg = self.build_if(nested_if);
                    self.add_edge(else_node, nested_cfg.entry, CfgEdgeKind::Normal);
                    return Some((else_node, nested_cfg.exits));
                }
                Block::empty()
            }
        };

        if body_cfg.entry != NO_NODE {
            self.add_edge(else_node, body_cfg.entry, CfgEdgeKind::Normal);
        }

        let exits = if body_cfg.exits.is_empty() {
            vec![(else_node, ExitKind::Normal)]
        } else {
            body_cfg.exits
        };
        Some((else_node, exits))
    }

    /// Build the CFG for a loop: `while`, `for`, Rust's `loop`, or Go's
    /// `select`.
    ///
    /// All four statements share the same control-flow shape: a header,
    /// a body, a back edge from every normal body exit to the header,
    /// and a normal exit representing the loop terminating.
    fn build_loop(&mut self, loop_node: u32) -> Block {
        let body = self.first_block_child(loop_node);
        let body_cfg = match body {
            Some(b) => self.build_block(b),
            None => Block::empty(),
        };

        if body_cfg.entry != NO_NODE {
            self.add_edge(loop_node, body_cfg.entry, CfgEdgeKind::True);
        }

        let mut exits: Vec<(u32, ExitKind)> = Vec::new();
        for (exit, kind) in body_cfg.exits {
            match kind {
                ExitKind::Normal => {
                    self.add_edge(exit, loop_node, CfgEdgeKind::LoopBack);
                }
                ExitKind::Continue => {
                    self.add_edge(exit, loop_node, CfgEdgeKind::Continue);
                }
                other => {
                    exits.push((exit, other));
                }
            }
        }

        exits.push((loop_node, ExitKind::Normal));
        Block {
            entry: loop_node,
            exits,
        }
    }

    /// Build the CFG for a `try`/`except`/`finally` statement.
    ///
    /// The try body's entry carries `Exception` edges to each handler,
    /// matching the syntactic model approved for this block. The try
    /// body and each handler flow normally to the finally block when
    /// one exists, or become exits of the whole construct when none
    /// does.
    fn build_try(&mut self, try_node: u32) -> Block {
        let children: Vec<u32> = self.arena.children_of(try_node).collect();

        let body_node = children.iter().copied().find(|&c| is_block(self.arena, c));
        let handlers: Vec<u32> = children
            .iter()
            .copied()
            .filter(|&c| is_except(self.arena, c))
            .collect();
        let finally_node = children
            .iter()
            .copied()
            .find(|&c| is_finally(self.arena, c));

        let body_cfg = match body_node {
            Some(b) => self.build_block(b),
            None => Block::empty(),
        };
        if body_cfg.entry != NO_NODE {
            self.add_edge(try_node, body_cfg.entry, CfgEdgeKind::Normal);
            for &handler in &handlers {
                self.add_edge(body_cfg.entry, handler, CfgEdgeKind::Exception);
            }
        }

        let mut handler_cfgs: Vec<Block> = Vec::new();
        for &handler in &handlers {
            let handler_body = self.first_block_child(handler);
            let handler_cfg = match handler_body {
                Some(b) => self.build_block(b),
                None => Block::empty(),
            };
            if handler_cfg.entry != NO_NODE {
                self.add_edge(handler, handler_cfg.entry, CfgEdgeKind::Normal);
            }
            handler_cfgs.push(handler_cfg);
        }

        let finally_cfg = match finally_node {
            Some(f) => match self.first_block_child(f) {
                Some(b) => self.build_block(b),
                None => Block::empty(),
            },
            None => Block::empty(),
        };
        let merge_target = if finally_cfg.entry != NO_NODE {
            Some(finally_cfg.entry)
        } else {
            None
        };

        let mut exits: Vec<(u32, ExitKind)> = Vec::new();
        self.merge_into(&body_cfg.exits, merge_target, &mut exits);
        for handler_cfg in &handler_cfgs {
            self.merge_into(&handler_cfg.exits, merge_target, &mut exits);
        }
        exits.extend(finally_cfg.exits);

        Block {
            entry: try_node,
            exits,
        }
    }

    /// Route a set of exits to the merge target, or pass them through.
    fn merge_into(
        &mut self,
        from: &[(u32, ExitKind)],
        merge_target: Option<u32>,
        into: &mut Vec<(u32, ExitKind)>,
    ) {
        for &(exit, kind) in from {
            if kind == ExitKind::Normal {
                match merge_target {
                    Some(target) => self.add_edge(exit, target, CfgEdgeKind::Normal),
                    None => into.push((exit, ExitKind::Normal)),
                }
            } else {
                into.push((exit, kind));
            }
        }
    }

    /// Build the CFG for a pass-through block construct: `with`,
    /// `synchronized`, or `lock`.
    ///
    /// Each is a pass-through: it flows to its body, and the body's
    /// exits become the exits of the whole statement.
    fn build_with(&mut self, with_node: u32) -> Block {
        let body = self.first_block_child(with_node);
        let body_cfg = match body {
            Some(b) => self.build_block(b),
            None => Block::empty(),
        };
        if body_cfg.entry != NO_NODE {
            self.add_edge(with_node, body_cfg.entry, CfgEdgeKind::Normal);
        }
        let exits = if body_cfg.exits.is_empty() {
            vec![(with_node, ExitKind::Normal)]
        } else {
            body_cfg.exits
        };
        Block {
            entry: with_node,
            exits,
        }
    }
}

/// Remove duplicate exits from a vector while preserving order.
fn dedup_exits(exits: &mut Vec<(u32, ExitKind)>) {
    let mut seen: HashMap<(u32, ExitKind), ()> = HashMap::new();
    exits.retain(|entry| seen.insert(*entry, ()).is_none());
}