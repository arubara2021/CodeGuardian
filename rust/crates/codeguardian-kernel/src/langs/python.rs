//! Python language module.
//!
//! Implements `LanguageModule` for Python by supplying the language
//! identity, the tree-sitter grammar, and the mapping from Python's
//! tree-sitter kind strings onto `NodeKind`. Symbol extraction and
//! callee extraction use the shared defaults — Python's declarations
//! and call syntax match the language-agnostic implementation exactly.

use tree_sitter::Language as TsLanguage;

use crate::ast::NodeKind;
use crate::langs::{classify_shared, Language, LanguageModule};

/// The Python language module.
pub struct PythonModule;

impl LanguageModule for PythonModule {
    fn language(&self) -> Language {
        Language::Python
    }

    fn grammar(&self) -> TsLanguage {
        tree_sitter_python::LANGUAGE.into()
    }

    fn classify(&self, kind: &str, raw_id: u16) -> NodeKind {
        if let Some(shared) = classify_shared(kind) {
            return shared;
        }
        match kind {
            "module" => NodeKind::Module,
            "block" => NodeKind::Block,
            "function_definition" => NodeKind::FunctionDef,
            "class_definition" => NodeKind::ClassDef,
            "lambda" => NodeKind::Lambda,
            "decorator" => NodeKind::Decorator,
            "parameters" | "lambda_parameters" => NodeKind::Parameter,
            "argument_list" => NodeKind::Argument,
            "attribute" => NodeKind::Attribute,
            "type" => NodeKind::TypeAnnotation,
            "assignment" => NodeKind::Assignment,
            "augmented_assignment" => NodeKind::AugmentedAssignment,
            "return_statement" => NodeKind::Return,
            "yield" => NodeKind::Yield,
            "if_statement" => NodeKind::If,
            "elif_clause" => NodeKind::Elif,
            "else_clause" => NodeKind::Else,
            "for_statement" => NodeKind::For,
            "while_statement" => NodeKind::While,
            "try_statement" => NodeKind::Try,
            "except_clause" => NodeKind::Except,
            "finally_clause" => NodeKind::Finally,
            "with_statement" => NodeKind::With,
            "raise_statement" => NodeKind::Raise,
            "pass_statement" => NodeKind::Pass,
            "break_statement" => NodeKind::Break,
            "continue_statement" => NodeKind::Continue,
            "global_statement" => NodeKind::Global,
            "nonlocal_statement" => NodeKind::Nonlocal,
            "assert_statement" => NodeKind::Assert,
            "delete_statement" => NodeKind::Del,
            "import_statement" => NodeKind::Import,
            "import_from_statement" => NodeKind::ImportFrom,
            "match_statement" => NodeKind::Match,
            "case_clause" => NodeKind::Case,
            "call" => NodeKind::Call,
            "binary_operator" => NodeKind::BinaryOp,
            "unary_operator" => NodeKind::UnaryOp,
            "comparison_operator" => NodeKind::ComparisonOp,
            "boolean_operator" => NodeKind::BoolOp,
            "conditional_expression" => NodeKind::ConditionalExpression,
            "integer" | "float" | "string" | "true" | "false" | "none" => NodeKind::Literal,
            "list" => NodeKind::List,
            "dictionary" => NodeKind::Dict,
            "set" => NodeKind::Set,
            "tuple" => NodeKind::Tuple,
            "subscript" => NodeKind::Subscript,
            "slice" => NodeKind::Slice,
            "await" => NodeKind::AwaitExpression,
            _ => NodeKind::Other(raw_id),
        }
    }
}
