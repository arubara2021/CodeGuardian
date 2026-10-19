//! Code Property Graph — the merged, serializable graph.
//!
//! The CPG merges four layers into one node-and-edge graph: the AST
//! tree structure, the call graph, the per-function Control Flow Graph,
//! and the Program Dependence Graph. Nodes are `CpgNode` records; edges
//! are `CpgEdge` records. All structure, including the tree structure,
//! lives in edges. `CpgNode` carries only what is intrinsic to a node:
//! its syntax kind, its file, its source position, and its name.
//!
//! # Wire format
//!
//! The graph serializes to a byte layout that matches the header Phase 3
//! fixed in `graph_store.py`. The first 64 bytes are the header:
//!
//! ```text
//! offset  size  field
//! 0       4     magic, b"CGCP"
//! 4       2     format_version, u16 = 1
//! 6       2     header_size, u16 = 64
//! 8       4     flags, u32, bit 0 = incremental
//! 12      8     node_count, u64
//! 20      8     edge_count, u64
//! 28      8     payload_offset, u64 = 64
//! 36      8     payload_length, u64
//! 44      8     payload_checksum, u64 (FNV-1a of the payload)
//! 52      12    reserved, all zero
//! ```
//!
//! The payload follows the header and is laid out as four sections: a
//! node section, an edge section, a string table, and a file path table.
//! Each section is preceded by a u32 count. The counts are redundant
//! with the header counts; the reader validates that they agree, and a
//! mismatch raises `CpgError::Corrupt`.
//!
//! # Wire codes
//!
//! `NodeKind` and `EdgeKind` are serialized as fixed `u16` and `u8`
//! codes, not as Rust discriminants. The codes are listed once in this
//! file and never change. Adding a new variant is a minor format
//! version bump; renaming a code is a major one.
//!
//! Codes 0 through 54 predate the multi-language extension. Codes 55
//! through 82 were added when the kernel grew from two languages to
//! nine. Codes 0 through 54 keep their original meanings; nothing was
//! renumbered.

use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::path::Path;

use thiserror::Error;

use crate::ast::{AstArena, NodeKind, NO_NODE};
use crate::call_graph::{extract_calls, CallEdge};
use crate::cfg::build_cfg;
use crate::langs::dispatch_by_name;
use crate::parser::{parse_file, KernelError};
use crate::pdg::build_pdg;
use crate::symbol_table::{extract_symbols, SymbolTable};

/// The four-byte magic at the start of every CPG file.
const MAGIC: [u8; 4] = *b"CGCP";
/// The only format version this build writes or reads.
const FORMAT_VERSION: u16 = 1;
/// The size in bytes of the CPG header.
const HEADER_SIZE: u16 = 64;
/// The flag bit set on files produced by an incremental update.
const FLAG_INCREMENTAL: u32 = 0x1;
/// The wire size in bytes of one `CpgNode`.
const NODE_WIRE_SIZE: usize = 32;
/// The wire size in bytes of one `CpgEdge`.
const EDGE_WIRE_SIZE: usize = 16;
/// The wire code used for `NodeKind::Other`.
const KIND_OTHER_CODE: u16 = 0xFFFF;

/// Split a `NodeKind` into its wire code and payload.
///
/// The mapping is frozen. The code for a given variant never changes for
/// as long as format version 1 is readable. Codes 55 through 82 were
/// added when the kernel grew from two languages to nine; codes 0
/// through 54 retain their original meanings.
pub fn node_kind_wire_parts(kind: NodeKind) -> (u16, u16) {
    match kind {
        NodeKind::Module => (0, 0),
        NodeKind::Block => (1, 0),
        NodeKind::FunctionDef => (2, 0),
        NodeKind::ClassDef => (3, 0),
        NodeKind::MethodDef => (4, 0),
        NodeKind::Lambda => (5, 0),
        NodeKind::Decorator => (6, 0),
        NodeKind::Parameter => (7, 0),
        NodeKind::Argument => (8, 0),
        NodeKind::Identifier => (9, 0),
        NodeKind::Attribute => (10, 0),
        NodeKind::Comment => (11, 0),
        NodeKind::TypeAnnotation => (12, 0),
        NodeKind::Assignment => (13, 0),
        NodeKind::AugmentedAssignment => (14, 0),
        NodeKind::Return => (15, 0),
        NodeKind::Yield => (16, 0),
        NodeKind::If => (17, 0),
        NodeKind::Elif => (18, 0),
        NodeKind::Else => (19, 0),
        NodeKind::For => (20, 0),
        NodeKind::While => (21, 0),
        NodeKind::Try => (22, 0),
        NodeKind::Except => (23, 0),
        NodeKind::Finally => (24, 0),
        NodeKind::With => (25, 0),
        NodeKind::Raise => (26, 0),
        NodeKind::Pass => (27, 0),
        NodeKind::Break => (28, 0),
        NodeKind::Continue => (29, 0),
        NodeKind::Global => (30, 0),
        NodeKind::Nonlocal => (31, 0),
        NodeKind::Assert => (32, 0),
        NodeKind::Del => (33, 0),
        NodeKind::Import => (34, 0),
        NodeKind::ImportFrom => (35, 0),
        NodeKind::Match => (36, 0),
        NodeKind::Case => (37, 0),
        NodeKind::Call => (38, 0),
        NodeKind::BinaryOp => (39, 0),
        NodeKind::UnaryOp => (40, 0),
        NodeKind::ComparisonOp => (41, 0),
        NodeKind::BoolOp => (42, 0),
        NodeKind::ConditionalExpression => (43, 0),
        NodeKind::Literal => (44, 0),
        NodeKind::List => (45, 0),
        NodeKind::Dict => (46, 0),
        NodeKind::Set => (47, 0),
        NodeKind::Tuple => (48, 0),
        NodeKind::Subscript => (49, 0),
        NodeKind::Slice => (50, 0),
        NodeKind::NewExpression => (51, 0),
        NodeKind::AwaitExpression => (52, 0),
        NodeKind::Error => (53, 0),
        NodeKind::Missing => (54, 0),
        NodeKind::Struct => (55, 0),
        NodeKind::Interface => (56, 0),
        NodeKind::Trait => (57, 0),
        NodeKind::Impl => (58, 0),
        NodeKind::Enum => (59, 0),
        NodeKind::Union => (60, 0),
        NodeKind::Namespace => (61, 0),
        NodeKind::Package => (62, 0),
        NodeKind::TypeParameter => (63, 0),
        NodeKind::TypeAlias => (64, 0),
        NodeKind::WhereClause => (65, 0),
        NodeKind::Lifetime => (66, 0),
        NodeKind::Annotation => (67, 0),
        NodeKind::Modifier => (68, 0),
        NodeKind::GoStatement => (69, 0),
        NodeKind::DeferStatement => (70, 0),
        NodeKind::SelectStatement => (71, 0),
        NodeKind::Loop => (72, 0),
        NodeKind::MacroDefinition => (73, 0),
        NodeKind::MacroCall => (74, 0),
        NodeKind::Preprocessor => (75, 0),
        NodeKind::TemplateDeclaration => (76, 0),
        NodeKind::Synchronized => (77, 0),
        NodeKind::Lock => (78, 0),
        NodeKind::Goto => (79, 0),
        NodeKind::TemplateLiteral => (80, 0),
        NodeKind::Property => (81, 0),
        NodeKind::TypeCast => (82, 0),
        NodeKind::Other(payload) => (KIND_OTHER_CODE, payload),
    }
}

/// Return the wire code for a `NodeKind`, ignoring any payload.
pub fn node_kind_wire_code(kind: NodeKind) -> u16 {
    node_kind_wire_parts(kind).0
}

/// Reconstruct a `NodeKind` from its wire code and payload.
pub fn node_kind_from_wire(code: u16, payload: u16) -> Option<NodeKind> {
    match code {
        0 => Some(NodeKind::Module),
        1 => Some(NodeKind::Block),
        2 => Some(NodeKind::FunctionDef),
        3 => Some(NodeKind::ClassDef),
        4 => Some(NodeKind::MethodDef),
        5 => Some(NodeKind::Lambda),
        6 => Some(NodeKind::Decorator),
        7 => Some(NodeKind::Parameter),
        8 => Some(NodeKind::Argument),
        9 => Some(NodeKind::Identifier),
        10 => Some(NodeKind::Attribute),
        11 => Some(NodeKind::Comment),
        12 => Some(NodeKind::TypeAnnotation),
        13 => Some(NodeKind::Assignment),
        14 => Some(NodeKind::AugmentedAssignment),
        15 => Some(NodeKind::Return),
        16 => Some(NodeKind::Yield),
        17 => Some(NodeKind::If),
        18 => Some(NodeKind::Elif),
        19 => Some(NodeKind::Else),
        20 => Some(NodeKind::For),
        21 => Some(NodeKind::While),
        22 => Some(NodeKind::Try),
        23 => Some(NodeKind::Except),
        24 => Some(NodeKind::Finally),
        25 => Some(NodeKind::With),
        26 => Some(NodeKind::Raise),
        27 => Some(NodeKind::Pass),
        28 => Some(NodeKind::Break),
        29 => Some(NodeKind::Continue),
        30 => Some(NodeKind::Global),
        31 => Some(NodeKind::Nonlocal),
        32 => Some(NodeKind::Assert),
        33 => Some(NodeKind::Del),
        34 => Some(NodeKind::Import),
        35 => Some(NodeKind::ImportFrom),
        36 => Some(NodeKind::Match),
        37 => Some(NodeKind::Case),
        38 => Some(NodeKind::Call),
        39 => Some(NodeKind::BinaryOp),
        40 => Some(NodeKind::UnaryOp),
        41 => Some(NodeKind::ComparisonOp),
        42 => Some(NodeKind::BoolOp),
        43 => Some(NodeKind::ConditionalExpression),
        44 => Some(NodeKind::Literal),
        45 => Some(NodeKind::List),
        46 => Some(NodeKind::Dict),
        47 => Some(NodeKind::Set),
        48 => Some(NodeKind::Tuple),
        49 => Some(NodeKind::Subscript),
        50 => Some(NodeKind::Slice),
        51 => Some(NodeKind::NewExpression),
        52 => Some(NodeKind::AwaitExpression),
        53 => Some(NodeKind::Error),
        54 => Some(NodeKind::Missing),
        55 => Some(NodeKind::Struct),
        56 => Some(NodeKind::Interface),
        57 => Some(NodeKind::Trait),
        58 => Some(NodeKind::Impl),
        59 => Some(NodeKind::Enum),
        60 => Some(NodeKind::Union),
        61 => Some(NodeKind::Namespace),
        62 => Some(NodeKind::Package),
        63 => Some(NodeKind::TypeParameter),
        64 => Some(NodeKind::TypeAlias),
        65 => Some(NodeKind::WhereClause),
        66 => Some(NodeKind::Lifetime),
        67 => Some(NodeKind::Annotation),
        68 => Some(NodeKind::Modifier),
        69 => Some(NodeKind::GoStatement),
        70 => Some(NodeKind::DeferStatement),
        71 => Some(NodeKind::SelectStatement),
        72 => Some(NodeKind::Loop),
        73 => Some(NodeKind::MacroDefinition),
        74 => Some(NodeKind::MacroCall),
        75 => Some(NodeKind::Preprocessor),
        76 => Some(NodeKind::TemplateDeclaration),
        77 => Some(NodeKind::Synchronized),
        78 => Some(NodeKind::Lock),
        79 => Some(NodeKind::Goto),
        80 => Some(NodeKind::TemplateLiteral),
        81 => Some(NodeKind::Property),
        82 => Some(NodeKind::TypeCast),
        KIND_OTHER_CODE => Some(NodeKind::Other(payload)),
        _ => None,
    }
}

/// The kind of a CPG edge. Each variant maps to a fixed wire code.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum EdgeKind {
    /// Syntactic parent-to-child edge from the tree structure.
    Ast,
    /// A call site's enclosing function to its resolved callee.
    Call,
    /// Sequential flow from one CFG statement to the next.
    CfgNormal,
    /// The conditional's true branch was taken.
    CfgTrue,
    /// The conditional's false branch was taken.
    CfgFalse,
    /// A loop body returned to its header.
    CfgLoopBack,
    /// A break transfer out of the innermost enclosing loop.
    CfgBreak,
    /// A continue transfer to the innermost enclosing loop header.
    CfgContinue,
    /// A try region's syntactic entry into an except handler.
    CfgException,
    /// PDG control dependence: target executes because source branched.
    ControlDependence,
    /// PDG data dependence: target reads a value source defined.
    DataDependence,
}

impl EdgeKind {
    /// Return the frozen wire code for this edge kind.
    pub fn wire_code(self) -> u8 {
        match self {
            EdgeKind::Ast => 0,
            EdgeKind::Call => 1,
            EdgeKind::CfgNormal => 2,
            EdgeKind::CfgTrue => 3,
            EdgeKind::CfgFalse => 4,
            EdgeKind::CfgLoopBack => 5,
            EdgeKind::CfgBreak => 6,
            EdgeKind::CfgContinue => 7,
            EdgeKind::CfgException => 8,
            EdgeKind::ControlDependence => 9,
            EdgeKind::DataDependence => 10,
        }
    }

    /// Return the edge kind for a wire code, or `None` if unknown.
    pub fn from_wire_code(code: u8) -> Option<Self> {
        match code {
            0 => Some(EdgeKind::Ast),
            1 => Some(EdgeKind::Call),
            2 => Some(EdgeKind::CfgNormal),
            3 => Some(EdgeKind::CfgTrue),
            4 => Some(EdgeKind::CfgFalse),
            5 => Some(EdgeKind::CfgLoopBack),
            6 => Some(EdgeKind::CfgBreak),
            7 => Some(EdgeKind::CfgContinue),
            8 => Some(EdgeKind::CfgException),
            9 => Some(EdgeKind::ControlDependence),
            10 => Some(EdgeKind::DataDependence),
            _ => None,
        }
    }
}

/// One node of the Code Property Graph.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CpgNode {
    /// The syntax kind.
    pub kind: NodeKind,
    /// The parser's flag word for this node. Preserves error, missing,
    /// extra, and named bits.
    pub flags: u16,
    /// The file the node belongs to.
    pub file_id: u32,
    /// Byte offset of the node's source span start.
    pub start_byte: u32,
    /// Byte offset one past the node's source span.
    pub end_byte: u32,
    /// Zero-based line of the node's start.
    pub start_row: u32,
    /// Zero-based column of the node's start.
    pub start_col: u32,
    /// String-table id of the node's name, or `NO_NODE`.
    pub name: u32,
}

/// One edge of the Code Property Graph.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CpgEdge {
    /// Source node id.
    pub src: u32,
    /// Target node id.
    pub dst: u32,
    /// The kind of relationship.
    pub kind: EdgeKind,
    /// String-table id of the edge's label, or `NO_NODE`. Carries the
    /// callee name on `Call` edges so an incremental update can
    /// re-resolve the edge after the target file changes.
    pub label: u32,
}

/// The decoded header of a CPG file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CpgHeader {
    /// Format version.
    pub format_version: u16,
    /// Flag word.
    pub flags: u32,
    /// Node count reported by the header.
    pub node_count: u64,
    /// Edge count reported by the header.
    pub edge_count: u64,
    /// Byte offset of the payload from the start of the file.
    pub payload_offset: u64,
    /// Payload length in bytes.
    pub payload_length: u64,
    /// FNV-1a checksum of the payload.
    pub payload_checksum: u64,
}

impl CpgHeader {
    /// Return whether this file was produced by an incremental update.
    pub fn incremental(&self) -> bool {
        self.flags & FLAG_INCREMENTAL != 0
    }
}

/// Every way a CPG file can be unreadable.
#[derive(Debug, Error)]
pub enum CpgError {
    /// An I/O operation failed.
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    /// The header's magic bytes are not `CGCP`.
    #[error("invalid magic bytes: expected CGCP, got {0:?}")]
    InvalidMagic([u8; 4]),
    /// The file declares a format version this build does not support.
    #[error("unsupported format version: {0}")]
    UnsupportedVersion(u16),
    /// The file declares a header size this build does not support.
    #[error("invalid header size: {0}")]
    InvalidHeaderSize(u16),
    /// The payload's checksum does not match the header.
    #[error("payload checksum mismatch: expected {expected:#x}, got {actual:#x}")]
    ChecksumMismatch {
        /// Checksum from the header.
        expected: u64,
        /// Checksum computed from the payload.
        actual: u64,
    },
    /// The file is shorter than its header claims.
    #[error("payload truncated: expected {expected} bytes, got {actual}")]
    Truncated {
        /// Byte count the header claimed.
        expected: usize,
        /// Byte count actually present.
        actual: usize,
    },
    /// A node kind wire code has no mapping.
    #[error("unknown node kind code: {0:#x}")]
    UnknownNodeKind(u16),
    /// An edge kind wire code has no mapping.
    #[error("unknown edge kind code: {0:#x}")]
    UnknownEdgeKind(u8),
    /// The file is structurally corrupt in a way not covered by a more
    /// specific variant.
    #[error("graph file is corrupt: {reason}")]
    Corrupt {
        /// Description of the corruption.
        reason: String,
    },
    /// A parse of one of the source files failed during an incremental
    /// update.
    #[error(transparent)]
    Parse(#[from] KernelError),
}

/// One source file handed to `CodePropertyGraph::build`.
#[derive(Debug, Clone)]
pub struct SourceFile {
    /// The path recorded in the graph's file table.
    pub path: String,
    /// The language the file is written in. Either a built-in name
    /// (`python`, `typescript`, `javascript`, `java`, `go`, `rust`,
    /// `c`, `cpp`) or the name of a runtime-loaded grammar.
    pub language: String,
    /// The file's full source text.
    pub content: String,
}

/// The Code Property Graph.
///
/// Nodes and edges are stored in flat `Vec`s indexed by `u32`. Strings
/// are interned into one table. Indices are precomputed at build time
/// so queries are O(1).
#[derive(Debug)]
pub struct CodePropertyGraph {
    pub(crate) nodes: Vec<CpgNode>,
    pub(crate) edges: Vec<CpgEdge>,
    pub(crate) strings: Vec<String>,
    pub(crate) files: Vec<String>,
    pub(crate) nodes_by_kind: HashMap<u16, Vec<u32>>,
    pub(crate) nodes_by_file: HashMap<u32, Vec<u32>>,
    pub(crate) names_to_nodes: HashMap<u32, Vec<u32>>,
    pub(crate) string_ids: HashMap<String, u32>,
    pub(crate) edges_from_index: HashMap<u32, Vec<u32>>,
    pub(crate) edges_to_index: HashMap<u32, Vec<u32>>,
    pub(crate) paths_to_file: HashMap<String, u32>,
}

impl CodePropertyGraph {
    /// Build a CPG from a list of source files.
    ///
    /// Each source is parsed with its declared language. Symbols are
    /// extracted and merged into one symbol table across every file.
    /// Call edges are resolved with the merged table. A CFG and PDG are
    /// built for every function. All nodes and edges are merged into
    /// one graph, deduplicated, and indexed.
    ///
    /// Returns `KernelError` if any source fails to parse. A source that
    /// parses with ERROR nodes still succeeds; the ERROR nodes appear in
    /// the graph as `NodeKind::Error` nodes.
    pub fn build(sources: &[SourceFile]) -> Result<Self, KernelError> {
        let file_count = sources.len();

        let mut paths: Vec<String> = Vec::with_capacity(file_count);
        let mut arenas: Vec<AstArena> = Vec::with_capacity(file_count);
        let mut roots: Vec<u32> = Vec::with_capacity(file_count);
        let mut offsets: Vec<u32> = Vec::with_capacity(file_count);

        let mut running: u32 = 0;
        for source in sources.iter() {
            let module = dispatch_by_name(&source.language).ok_or_else(|| {
                KernelError::LanguageNotImplemented {
                    language: source.language.clone(),
                }
            })?;
            let parsed = parse_file(module, &source.content)?;
            paths.push(source.path.clone());
            offsets.push(running);
            running = running.saturating_add(parsed.arena.len() as u32);
            roots.push(parsed.root);
            arenas.push(parsed.arena);
        }

        let mut strings: Vec<String> = Vec::new();
        let mut string_ids: HashMap<String, u32> = HashMap::new();
        let mut nodes: Vec<CpgNode> = Vec::with_capacity(running as usize);
        let mut edges: Vec<CpgEdge> = Vec::new();

        let global_symbols = build_global_symbols(&arenas, &roots, &offsets);

        for (file_index, arena) in arenas.iter().enumerate() {
            let file_id = file_index as u32;
            let offset = offsets[file_index];

            for arena_id in 0..arena.len() as u32 {
                let ast_node = match arena.get(arena_id) {
                    Some(node) => node,
                    None => continue,
                };

                let name_id = match arena.string(ast_node.name) {
                    Some(name) => intern_string_into(name, &mut strings, &mut string_ids),
                    None => NO_NODE,
                };

                nodes.push(CpgNode {
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
                    edges.push(CpgEdge {
                        src: ast_node.parent.saturating_add(offset),
                        dst: arena_id.saturating_add(offset),
                        kind: EdgeKind::Ast,
                        label: NO_NODE,
                    });
                }
            }
        }

        for (file_index, arena) in arenas.iter().enumerate() {
            let offset = offsets[file_index];
            let calls = extract_calls(arena, &global_symbols, roots[file_index]);
            for call in calls {
                push_call_edge(&call, offset, &mut strings, &mut string_ids, &mut edges);
            }
        }

        for (file_index, arena) in arenas.iter().enumerate() {
            let offset = offsets[file_index];
            let functions = collect_functions(arena, roots[file_index]);
            for function_node in functions {
                let cfg = build_cfg(arena, function_node);
                for edge in &cfg.edges {
                    edges.push(CpgEdge {
                        src: edge.from.saturating_add(offset),
                        dst: edge.to.saturating_add(offset),
                        kind: cfg_edge_kind_to_edge_kind(edge.kind),
                        label: NO_NODE,
                    });
                }

                let pdg = build_pdg(arena, &cfg);
                for edge in &pdg.edges {
                    edges.push(CpgEdge {
                        src: edge.from.saturating_add(offset),
                        dst: edge.to.saturating_add(offset),
                        kind: pdg_edge_kind_to_edge_kind(edge.kind),
                        label: NO_NODE,
                    });
                }
            }
        }

        dedupe_edges(&mut edges);

        let mut graph = CodePropertyGraph {
            nodes,
            edges,
            strings,
            files: paths,
            nodes_by_kind: HashMap::new(),
            nodes_by_file: HashMap::new(),
            names_to_nodes: HashMap::new(),
            string_ids,
            edges_from_index: HashMap::new(),
            edges_to_index: HashMap::new(),
            paths_to_file: HashMap::new(),
        };
        graph.rebuild_indices();
        Ok(graph)
    }

    /// Rebuild every lookup index from the node, edge, string, and file
    /// vectors. Called at the end of `build`, `read_from`, and
    /// `apply_incremental`.
    pub(crate) fn rebuild_indices(&mut self) {
        self.nodes_by_kind.clear();
        self.nodes_by_file.clear();
        self.names_to_nodes.clear();
        self.string_ids.clear();
        self.edges_from_index.clear();
        self.edges_to_index.clear();
        self.paths_to_file.clear();

        for (id, node) in self.nodes.iter().enumerate() {
            let id = id as u32;
            let wire = node_kind_wire_code(node.kind);
            self.nodes_by_kind.entry(wire).or_default().push(id);
            self.nodes_by_file.entry(node.file_id).or_default().push(id);
            if node.name != NO_NODE {
                self.names_to_nodes.entry(node.name).or_default().push(id);
            }
        }

        for (id, edge) in self.edges.iter().enumerate() {
            let id = id as u32;
            self.edges_from_index.entry(edge.src).or_default().push(id);
            self.edges_to_index.entry(edge.dst).or_default().push(id);
        }

        for (id, string) in self.strings.iter().enumerate() {
            self.string_ids.insert(string.clone(), id as u32);
        }
        for (id, path) in self.files.iter().enumerate() {
            self.paths_to_file.insert(path.clone(), id as u32);
        }
    }

    /// Serialize the graph to `path`.
    pub fn write_to_file<P: AsRef<Path>>(&self, path: P) -> Result<(), CpgError> {
        let file = std::fs::File::create(path)?;
        let mut writer = std::io::BufWriter::new(file);
        self.write_to(&mut writer)?;
        writer.flush()?;
        Ok(())
    }

    /// Serialize the graph to a writer.
    pub fn write_to<W: Write>(&self, writer: &mut W) -> Result<(), CpgError> {
        let payload = self.serialize_payload();
        let checksum = fnv1a_64(&payload);
        let header = build_header_bytes(
            self.nodes.len() as u64,
            self.edges.len() as u64,
            payload.len() as u64,
            checksum,
        );
        writer.write_all(&header)?;
        writer.write_all(&payload)?;
        Ok(())
    }

    /// Read a graph from a file.
    pub fn read_from_file<P: AsRef<Path>>(path: P) -> Result<Self, CpgError> {
        let file = std::fs::File::open(path)?;
        let mut reader = std::io::BufReader::new(file);
        Self::read_from(&mut reader)
    }

    /// Read a graph from a reader.
    pub fn read_from<R: Read>(reader: &mut R) -> Result<Self, CpgError> {
        let mut header_bytes = [0u8; HEADER_SIZE as usize];
        reader.read_exact(&mut header_bytes)?;
        let header = parse_header(&header_bytes)?;

        let payload_len = header.payload_length as usize;
        let mut payload = vec![0u8; payload_len];
        reader.read_exact(&mut payload)?;

        let computed = fnv1a_64(&payload);
        if computed != header.payload_checksum {
            return Err(CpgError::ChecksumMismatch {
                expected: header.payload_checksum,
                actual: computed,
            });
        }

        let mut cursor = PayloadReader::new(&payload);

        let node_section_len = cursor.u32()? as usize;
        if node_section_len as u64 != header.node_count {
            return Err(CpgError::Corrupt {
                reason: format!(
                    "node section count {node_section_len} does not match header {}",
                    header.node_count
                ),
            });
        }
        let mut nodes = Vec::with_capacity(node_section_len);
        for _ in 0..node_section_len {
            nodes.push(cursor.node()?);
        }

        let edge_section_len = cursor.u32()? as usize;
        if edge_section_len as u64 != header.edge_count {
            return Err(CpgError::Corrupt {
                reason: format!(
                    "edge section count {edge_section_len} does not match header {}",
                    header.edge_count
                ),
            });
        }
        let mut edges = Vec::with_capacity(edge_section_len);
        for _ in 0..edge_section_len {
            edges.push(cursor.edge()?);
        }

        let string_count = cursor.u32()? as usize;
        let mut strings = Vec::with_capacity(string_count);
        for _ in 0..string_count {
            strings.push(cursor.length_prefixed_string()?);
        }

        let file_count = cursor.u32()? as usize;
        let mut files = Vec::with_capacity(file_count);
        for _ in 0..file_count {
            files.push(cursor.length_prefixed_string()?);
        }

        let mut graph = CodePropertyGraph {
            nodes,
            edges,
            strings,
            files,
            nodes_by_kind: HashMap::new(),
            nodes_by_file: HashMap::new(),
            names_to_nodes: HashMap::new(),
            string_ids: HashMap::new(),
            edges_from_index: HashMap::new(),
            edges_to_index: HashMap::new(),
            paths_to_file: HashMap::new(),
        };
        graph.rebuild_indices();
        Ok(graph)
    }

    /// Read only the header of a CPG file.
    pub fn read_header<R: Read>(reader: &mut R) -> Result<CpgHeader, CpgError> {
        let mut header_bytes = [0u8; HEADER_SIZE as usize];
        reader.read_exact(&mut header_bytes)?;
        parse_header(&header_bytes)
    }

    /// Serialize the graph payload. The caller prepends the header.
    fn serialize_payload(&self) -> Vec<u8> {
        let mut buf = Vec::with_capacity(
            4 + self.nodes.len() * NODE_WIRE_SIZE
                + 4
                + self.edges.len() * EDGE_WIRE_SIZE
                + 4
                + self.strings.iter().map(|s| 4 + s.len()).sum::<usize>()
                + 4
                + self.files.iter().map(|s| 4 + s.len()).sum::<usize>(),
        );

        write_u32(&mut buf, self.nodes.len() as u32);
        for node in &self.nodes {
            write_node(&mut buf, node);
        }

        write_u32(&mut buf, self.edges.len() as u32);
        for edge in &self.edges {
            write_edge(&mut buf, edge);
        }

        write_u32(&mut buf, self.strings.len() as u32);
        for string in &self.strings {
            write_length_prefixed_string(&mut buf, string);
        }

        write_u32(&mut buf, self.files.len() as u32);
        for file in &self.files {
            write_length_prefixed_string(&mut buf, file);
        }

        buf
    }
}

/// Build a global symbol table across every file with node ids shifted
/// to CPG ids.
fn build_global_symbols(arenas: &[AstArena], roots: &[u32], offsets: &[u32]) -> SymbolTable {
    let mut table = SymbolTable::new();
    for (file_index, arena) in arenas.iter().enumerate() {
        let file_id = file_index as u32;
        let offset = offsets[file_index];
        let symbols = extract_symbols(arena, roots[file_index], file_id);
        for mut symbol in symbols {
            symbol.node_id = symbol.node_id.saturating_add(offset);
            table.insert(symbol);
        }
    }
    table
}

/// Push one call edge into the edge vector, interning the callee name
/// as the edge's label.
///
/// A call whose callee could not be resolved is emitted with
/// `dst = NO_NODE`. This preserves the fact that a call happened even
/// when the graph cannot point at a specific target. Blast uses
/// `edges_to(NO_NODE)` plus a label match to report unresolved call
/// sites per target.
fn push_call_edge(
    call: &CallEdge,
    offset: u32,
    strings: &mut Vec<String>,
    string_ids: &mut HashMap<String, u32>,
    edges: &mut Vec<CpgEdge>,
) {
    if call.caller_node_id == NO_NODE {
        return;
    }
    let label = intern_string_into(&call.callee_name, strings, string_ids);
    edges.push(CpgEdge {
        src: call.caller_node_id.saturating_add(offset),
        dst: call.callee_node_id,
        kind: EdgeKind::Call,
        label,
    });
}

/// Map a CFG edge kind to a CPG edge kind.
pub(crate) fn cfg_edge_kind_to_edge_kind(kind: crate::cfg::CfgEdgeKind) -> EdgeKind {
    use crate::cfg::CfgEdgeKind;
    match kind {
        CfgEdgeKind::Normal => EdgeKind::CfgNormal,
        CfgEdgeKind::True => EdgeKind::CfgTrue,
        CfgEdgeKind::False => EdgeKind::CfgFalse,
        CfgEdgeKind::LoopBack => EdgeKind::CfgLoopBack,
        CfgEdgeKind::Break => EdgeKind::CfgBreak,
        CfgEdgeKind::Continue => EdgeKind::CfgContinue,
        CfgEdgeKind::Exception => EdgeKind::CfgException,
    }
}

/// Map a PDG edge kind to a CPG edge kind.
pub(crate) fn pdg_edge_kind_to_edge_kind(kind: crate::pdg::PdgEdgeKind) -> EdgeKind {
    match kind {
        crate::pdg::PdgEdgeKind::ControlDependence => EdgeKind::ControlDependence,
        crate::pdg::PdgEdgeKind::DataDependence => EdgeKind::DataDependence,
    }
}

/// Remove duplicate edges by `(src, dst, kind_code, label)`.
fn dedupe_edges(edges: &mut Vec<CpgEdge>) {
    let mut seen: HashSet<(u32, u32, u8, u32)> = HashSet::with_capacity(edges.len());
    edges.retain(|edge| seen.insert((edge.src, edge.dst, edge.kind.wire_code(), edge.label)));
}

/// Intern `text` into `strings`, returning its id. The `string_ids` map
/// is the reverse lookup and is updated in lockstep.
pub(crate) fn intern_string_into(
    text: &str,
    strings: &mut Vec<String>,
    string_ids: &mut HashMap<String, u32>,
) -> u32 {
    if let Some(&id) = string_ids.get(text) {
        return id;
    }
    let id = strings.len() as u32;
    let owned = text.to_owned();
    strings.push(owned.clone());
    string_ids.insert(owned, id);
    id
}

/// Return the arena indices of every function or method node in a file.
pub(crate) fn collect_functions(arena: &AstArena, root: u32) -> Vec<u32> {
    let mut functions = Vec::new();
    for id in arena.walk_preorder(root) {
        let kind = match arena.get(id) {
            Some(node) => node.kind,
            None => continue,
        };
        if matches!(kind, NodeKind::FunctionDef | NodeKind::MethodDef) {
            functions.push(id);
        }
    }
    functions
}

/// Build the 64-byte header.
fn build_header_bytes(
    node_count: u64,
    edge_count: u64,
    payload_length: u64,
    payload_checksum: u64,
) -> [u8; HEADER_SIZE as usize] {
    let mut header = [0u8; HEADER_SIZE as usize];
    header[0..4].copy_from_slice(&MAGIC);
    header[4..6].copy_from_slice(&FORMAT_VERSION.to_le_bytes());
    header[6..8].copy_from_slice(&HEADER_SIZE.to_le_bytes());
    header[8..12].copy_from_slice(&0u32.to_le_bytes());
    header[12..20].copy_from_slice(&node_count.to_le_bytes());
    header[20..28].copy_from_slice(&edge_count.to_le_bytes());
    header[28..36].copy_from_slice(&(HEADER_SIZE as u64).to_le_bytes());
    header[36..44].copy_from_slice(&payload_length.to_le_bytes());
    header[44..52].copy_from_slice(&payload_checksum.to_le_bytes());
    header
}

/// Parse and validate the 64-byte header.
fn parse_header(bytes: &[u8; HEADER_SIZE as usize]) -> Result<CpgHeader, CpgError> {
    let magic: [u8; 4] = [bytes[0], bytes[1], bytes[2], bytes[3]];
    if magic != MAGIC {
        return Err(CpgError::InvalidMagic(magic));
    }
    let format_version = u16::from_le_bytes([bytes[4], bytes[5]]);
    let header_size = u16::from_le_bytes([bytes[6], bytes[7]]);
    if format_version != FORMAT_VERSION {
        return Err(CpgError::UnsupportedVersion(format_version));
    }
    if header_size != HEADER_SIZE {
        return Err(CpgError::InvalidHeaderSize(header_size));
    }
    let flags = u32::from_le_bytes([bytes[8], bytes[9], bytes[10], bytes[11]]);
    let node_count = read_u64(&bytes[12..20]);
    let edge_count = read_u64(&bytes[20..28]);
    let payload_offset = read_u64(&bytes[28..36]);
    let payload_length = read_u64(&bytes[36..44]);
    let payload_checksum = read_u64(&bytes[44..52]);
    Ok(CpgHeader {
        format_version,
        flags,
        node_count,
        edge_count,
        payload_offset,
        payload_length,
        payload_checksum,
    })
}

/// Read a little-endian u64 from an 8-byte slice.
fn read_u64(bytes: &[u8]) -> u64 {
    let mut buf = [0u8; 8];
    buf.copy_from_slice(&bytes[..8]);
    u64::from_le_bytes(buf)
}

/// Append a little-endian u32 to `buf`.
fn write_u32(buf: &mut Vec<u8>, value: u32) {
    buf.extend_from_slice(&value.to_le_bytes());
}

/// Append a length-prefixed UTF-8 string to `buf`.
fn write_length_prefixed_string(buf: &mut Vec<u8>, value: &str) {
    let bytes = value.as_bytes();
    write_u32(buf, bytes.len() as u32);
    buf.extend_from_slice(bytes);
}

/// Append one node in wire format to `buf`.
fn write_node(buf: &mut Vec<u8>, node: &CpgNode) {
    let (code, payload) = node_kind_wire_parts(node.kind);
    buf.extend_from_slice(&code.to_le_bytes());
    buf.extend_from_slice(&payload.to_le_bytes());
    buf.extend_from_slice(&node.flags.to_le_bytes());
    buf.extend_from_slice(&0u16.to_le_bytes());
    buf.extend_from_slice(&node.file_id.to_le_bytes());
    buf.extend_from_slice(&node.start_byte.to_le_bytes());
    buf.extend_from_slice(&node.end_byte.to_le_bytes());
    buf.extend_from_slice(&node.start_row.to_le_bytes());
    buf.extend_from_slice(&node.start_col.to_le_bytes());
    buf.extend_from_slice(&node.name.to_le_bytes());
}

/// Append one edge in wire format to `buf`.
fn write_edge(buf: &mut Vec<u8>, edge: &CpgEdge) {
    buf.extend_from_slice(&edge.src.to_le_bytes());
    buf.extend_from_slice(&edge.dst.to_le_bytes());
    buf.push(edge.kind.wire_code());
    buf.extend_from_slice(&[0u8, 0, 0]);
    buf.extend_from_slice(&edge.label.to_le_bytes());
}

/// FNV-1a 64-bit checksum.
fn fnv1a_64(bytes: &[u8]) -> u64 {
    const OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut hash = OFFSET_BASIS;
    for &byte in bytes {
        hash ^= byte as u64;
        hash = hash.wrapping_mul(PRIME);
    }
    hash
}

/// A cursor over the payload byte slice.
struct PayloadReader<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> PayloadReader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { data, pos: 0 }
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8], CpgError> {
        if self.pos + n > self.data.len() {
            return Err(CpgError::Truncated {
                expected: self.pos + n,
                actual: self.data.len(),
            });
        }
        let slice = &self.data[self.pos..self.pos + n];
        self.pos += n;
        Ok(slice)
    }

    fn u8(&mut self) -> Result<u8, CpgError> {
        let bytes = self.take(1)?;
        Ok(bytes[0])
    }

    fn u16(&mut self) -> Result<u16, CpgError> {
        let bytes = self.take(2)?;
        Ok(u16::from_le_bytes([bytes[0], bytes[1]]))
    }

    fn u32(&mut self) -> Result<u32, CpgError> {
        let bytes = self.take(4)?;
        Ok(u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]))
    }

    fn node(&mut self) -> Result<CpgNode, CpgError> {
        let code = self.u16()?;
        let payload = self.u16()?;
        let flags = self.u16()?;
        let _pad = self.u16()?;
        let kind = node_kind_from_wire(code, payload).ok_or(CpgError::UnknownNodeKind(code))?;
        let file_id = self.u32()?;
        let start_byte = self.u32()?;
        let end_byte = self.u32()?;
        let start_row = self.u32()?;
        let start_col = self.u32()?;
        let name = self.u32()?;
        Ok(CpgNode {
            kind,
            flags,
            file_id,
            start_byte,
            end_byte,
            start_row,
            start_col,
            name,
        })
    }

    fn edge(&mut self) -> Result<CpgEdge, CpgError> {
        let src = self.u32()?;
        let dst = self.u32()?;
        let code = self.u8()?;
        let _pad = self.take(3)?;
        let kind = EdgeKind::from_wire_code(code).ok_or(CpgError::UnknownEdgeKind(code))?;
        let label = self.u32()?;
        Ok(CpgEdge {
            src,
            dst,
            kind,
            label,
        })
    }

    fn length_prefixed_string(&mut self) -> Result<String, CpgError> {
        let len = self.u32()? as usize;
        let bytes = self.take(len)?;
        String::from_utf8(bytes.to_vec()).map_err(|_| CpgError::Corrupt {
            reason: "string is not valid UTF-8".to_owned(),
        })
    }
}
