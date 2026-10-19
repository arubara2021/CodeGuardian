//! Blast radius bindings for the CodeGuardian native extension.
//!
//! `compute_blast` reads a serialized Code Property Graph, resolves a
//! target, computes direct and transitive callers, detects contract
//! violations and coverage gaps, scores the change, and recommends a
//! proceed, review, or block decision.
//!
//! The returned dictionary carries the fields the blast crate
//! produces. It is not a complete `blast-radius.json`; the
//! schema-required `change_kind` and `generated_at` fields are the
//! caller's concern.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use codeguardian_blast::{
    compute_blast_score, compute_direct_callers, compute_transitive_callers,
    detect_contract_violations, detect_coverage_gaps, recommend, resolve_target, BlastError,
    BlastTarget, CallKind, Caller, ContractViolation, CoverageGap, Risk, MAX_TRANSITIVE_CALLERS,
};
use codeguardian_kernel::{CodePropertyGraph, CpgError};

use crate::kernel::{build_exception, map_cpg_error};

/// Compute the blast radius for a target.
///
/// `cpg_bytes` is a serialized Code Property Graph, as returned by
/// `build_graph`. `target_path` and `target_qualified_name` identify
/// the function or class to analyze.
///
/// Returns a dict with eight keys: `target_node_id`, `direct_callers`,
/// `transitive_callers`, `transitive_truncated`, `contract_violations`,
/// `coverage_gaps`, `blast_score`, and `recommendation`. The caller
/// lists carry one dict per element; the recommendation is one of
/// `"proceed"`, `"review"`, or `"block"`.
///
/// Raises `KernelError` with code `FILE_NOT_FOUND` when the path is
/// not in the graph, `BLAST_TARGET_NOT_FOUND` when the qualified name
/// matches no declaration, and `BLAST_TARGET_AMBIGUOUS` when it
/// matches more than one.
#[pyfunction]
pub fn compute_blast<'py>(
    py: Python<'py>,
    cpg_bytes: Vec<u8>,
    target_path: String,
    target_qualified_name: String,
) -> PyResult<Bound<'py, PyDict>> {
    let cpg = py
        .allow_threads(move || -> Result<CodePropertyGraph, CpgError> {
            let mut cursor = std::io::Cursor::new(cpg_bytes);
            CodePropertyGraph::read_from(&mut cursor)
        })
        .map_err(|err| map_cpg_error(py, &err))?;

    let target = BlastTarget {
        file_path: target_path,
        qualified_name: target_qualified_name,
    };

    let target_node_id = resolve_target(&cpg, &target).map_err(|err| map_blast_error(py, &err))?;

    let direct = compute_direct_callers(&cpg, target_node_id);
    let (transitive, transitive_truncated) =
        compute_transitive_callers(&cpg, target_node_id, MAX_TRANSITIVE_CALLERS);
    let violations = detect_contract_violations(&cpg, target_node_id, &direct);
    let gaps = detect_coverage_gaps(&cpg, &transitive);

    let score = compute_blast_score(
        direct.len() as u32,
        transitive.len() as u32,
        violations.len() as u32,
        gaps.len() as u32,
    );
    let critical_count = violations
        .iter()
        .filter(|v| v.risk == Risk::Critical)
        .count() as u32;
    let recommendation = recommend(score, critical_count);

    let result = PyDict::new(py);
    result.set_item("target_node_id", target_node_id)?;
    result.set_item("direct_callers", callers_to_list(py, &direct)?)?;
    result.set_item("transitive_callers", callers_to_list(py, &transitive)?)?;
    result.set_item("transitive_truncated", transitive_truncated)?;
    result.set_item("contract_violations", violations_to_list(py, &violations)?)?;
    result.set_item("coverage_gaps", gaps_to_list(py, &gaps)?)?;
    result.set_item("blast_score", score)?;
    result.set_item("recommendation", recommendation.as_str())?;
    Ok(result)
}

/// Build a Python list of caller dicts.
fn callers_to_list<'py>(py: Python<'py>, callers: &[Caller]) -> PyResult<Bound<'py, PyList>> {
    let list = PyList::empty(py);
    for caller in callers {
        let dict = PyDict::new(py);
        dict.set_item("node_id", caller.node_id)?;
        dict.set_item("qualified_name", &caller.qualified_name)?;
        dict.set_item("path", &caller.file_path)?;
        dict.set_item("line", caller.line)?;
        dict.set_item("depth", caller.depth)?;
        dict.set_item(
            "call_kind",
            match caller.call_kind {
                CallKind::Direct => "direct",
                CallKind::Transitive => "transitive",
            },
        )?;
        list.append(dict)?;
    }
    Ok(list)
}

/// Build a Python list of contract-violation dicts.
fn violations_to_list<'py>(
    py: Python<'py>,
    violations: &[ContractViolation],
) -> PyResult<Bound<'py, PyList>> {
    let list = PyList::empty(py);
    for violation in violations {
        let dict = PyDict::new(py);
        dict.set_item("caller_node_id", violation.caller_node_id)?;
        dict.set_item("caller_qualified_name", &violation.caller_qualified_name)?;
        dict.set_item("caller_file_path", &violation.caller_file_path)?;
        dict.set_item("caller_line", violation.caller_line)?;
        dict.set_item("assumption", &violation.assumption)?;
        dict.set_item(
            "risk",
            match violation.risk {
                Risk::Info => "info",
                Risk::Warning => "warning",
                Risk::Critical => "critical",
            },
        )?;
        list.append(dict)?;
    }
    Ok(list)
}

/// Build a Python list of coverage-gap dicts.
fn gaps_to_list<'py>(py: Python<'py>, gaps: &[CoverageGap]) -> PyResult<Bound<'py, PyList>> {
    let list = PyList::empty(py);
    for gap in gaps {
        let dict = PyDict::new(py);
        dict.set_item("caller_node_id", gap.caller_node_id)?;
        dict.set_item("caller_qualified_name", &gap.caller_qualified_name)?;
        dict.set_item("caller_file_path", &gap.caller_file_path)?;
        dict.set_item("caller_line", gap.caller_line)?;
        dict.set_item("reason", &gap.reason)?;
        list.append(dict)?;
    }
    Ok(list)
}

/// Convert a blast error into a `KernelError` exception.
fn map_blast_error(py: Python<'_>, err: &BlastError) -> PyErr {
    match err {
        BlastError::FileNotFound(path) => {
            let details = PyDict::new(py);
            let _ = details.set_item("path", path);
            build_exception(py, "FILE_NOT_FOUND", &err.to_string(), details.into_any())
        }
        BlastError::TargetNotFound {
            file_path,
            qualified_name,
        } => {
            let details = PyDict::new(py);
            let _ = details.set_item("file_path", file_path);
            let _ = details.set_item("qualified_name", qualified_name);
            build_exception(
                py,
                "BLAST_TARGET_NOT_FOUND",
                &err.to_string(),
                details.into_any(),
            )
        }
        BlastError::TargetAmbiguous {
            file_path,
            qualified_name,
            count,
        } => {
            let details = PyDict::new(py);
            let _ = details.set_item("file_path", file_path);
            let _ = details.set_item("qualified_name", qualified_name);
            let _ = details.set_item("count", *count);
            build_exception(
                py,
                "BLAST_TARGET_AMBIGUOUS",
                &err.to_string(),
                details.into_any(),
            )
        }
    }
}
