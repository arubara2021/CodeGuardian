//! Rust language module.
//!
//! Implements `LanguageModule` for Rust by supplying the language
//! identity, the tree-sitter grammar, and the mapping from Rust's
//! tree-sitter kind strings onto `NodeKind`. Symbol extraction and
//! callee extraction use the shared defaults — Rust's `function_item`,
//! `struct_item`, `trait_item`, `impl_item`, and `enum_item` all carry
//! a `name` field or a name-bearing enclosing scope, and `call_expression`
//! carries its callee in the first child, which the shared callee
//! extractor already handles.
//!
//! Rust adds four declaration kinds the shared classifier does not
//! know: `trait_item`, `impl_item`, `macro_definition`, and
//! `macro_invocation`. All four are classified here.

use tree_sitter::Language as TsLanguage;

use crate::ast::NodeKind;
use crate::langs::{classify_shared, Language, LanguageModule};

/// The Rust language module.
pub struct RustModule;

impl LanguageModule for RustModule {
    fn language(&self) -> Language {
        Language::Rust
    }

    fn grammar(&self) -> TsLanguage {
        tree_sitter_rust::LANGUAGE.into()
    }

    fn classify(&self, kind: &str, raw_id: u16) -> NodeKind {
        if let Some(shared) = classify_shared(kind) {
            return shared;
        }
        match kind {
            "source_file" => NodeKind::Module,
            "function_item" | "function_signature_item" => NodeKind::FunctionDef,
            "struct_item" => NodeKind::Struct,
            "enum_item" => NodeKind::Enum,
            "trait_item" => NodeKind::Trait,
            "impl_item" => NodeKind::Impl,
            "mod_item" => NodeKind::Module,
            "use_declaration" => NodeKind::Import,
            "type_item" => NodeKind::TypeAlias,
            "type_parameters" => NodeKind::TypeParameter,
            "where_clause" => NodeKind::WhereClause,
            "lifetime" => NodeKind::Lifetime,
            "block" => NodeKind::Block,
            "if_expression" => NodeKind::If,
            "else_clause" => NodeKind::Else,
            "match_expression" => NodeKind::Match,
            "match_arm" => NodeKind::Case,
            "loop_expression" => NodeKind::Loop,
            "while_expression" => NodeKind::While,
            "for_expression" => NodeKind::For,
            "return_expression" => NodeKind::Return,
            "break_expression" => NodeKind::Break,
            "continue_expression" => NodeKind::Continue,
            "call_expression" => NodeKind::Call,
            "field_expression" => NodeKind::Attribute,
            "index_expression" => NodeKind::Subscript,
            "binary_expression" => NodeKind::BinaryOp,
            "unary_expression" => NodeKind::UnaryOp,
            "closure_expression" => NodeKind::Lambda,
            "await_expression" => NodeKind::AwaitExpression,
            "macro_invocation" => NodeKind::MacroCall,
            "macro_definition" => NodeKind::MacroDefinition,
            "parameters" => NodeKind::Parameter,
            "parameter" | "self_parameter" => NodeKind::Parameter,
            "arguments" => NodeKind::Argument,
            "type_cast_expression" => NodeKind::TypeCast,
            "integer_literal"
            | "float_literal"
            | "string_literal"
            | "raw_string_literal"
            | "char_literal"
            | "boolean_literal"
            | "unit_expression" => NodeKind::Literal,
            "array_expression" => NodeKind::List,
            "tuple_expression" => NodeKind::Tuple,
            _ => NodeKind::Other(raw_id),
        }
    }
}
