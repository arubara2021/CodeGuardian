//! PyO3 bindings for the CodeGuardian native extension.
//!
//! Imported from Python as `codeguardian._native`. The module exposes
//! the kernel, blast, and policy functions and the shared
//! `KernelError` exception class.
//!
//! Kernel functions release the GIL while they work. Blast and policy
//! bindings that read the CPG do the same. Policy bindings that only
//! validate integers do not need to release the GIL because their work
//! is trivial.

use pyo3::prelude::*;

mod blast;
mod kernel;
mod policy;

use crate::blast::compute_blast;
use crate::kernel::{build_graph, graph_summary, kernel_version};
use crate::policy::{check_plugin_permissions, check_sandbox_spec, detect_host_limits};

pyo3::create_exception!(_native, KernelError, pyo3::exceptions::PyException);

/// Return the input string unchanged.
///
/// Proves the FFI boundary end to end. Retained from Phase 5 because
/// the Python bridge and its integration tests reference it.
#[pyfunction]
fn echo(value: &str) -> String {
    value.to_owned()
}

/// The module initializer Python calls on `import codeguardian._native`.
#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(echo, module)?)?;
    module.add_function(wrap_pyfunction!(kernel_version, module)?)?;
    module.add_function(wrap_pyfunction!(build_graph, module)?)?;
    module.add_function(wrap_pyfunction!(graph_summary, module)?)?;
    module.add_function(wrap_pyfunction!(compute_blast, module)?)?;
    module.add_function(wrap_pyfunction!(check_sandbox_spec, module)?)?;
    module.add_function(wrap_pyfunction!(check_plugin_permissions, module)?)?;
    module.add_function(wrap_pyfunction!(detect_host_limits, module)?)?;
    module.add("KernelError", module.py().get_type::<KernelError>())?;
    Ok(())
}
