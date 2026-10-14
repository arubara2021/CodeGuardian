//! PyO3 bindings for the CodeGuardian native extension.
//!
//! Imported from Python as ``codeguardian._native``. Phase 5 exposes a
//! single ``echo`` function to prove the bridge. Kernel bindings land
//! in Phase 6.

use pyo3::prelude::*;

#[pyfunction]
fn echo(value: &str) -> String {
    value.to_owned()
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(echo, module)?)?;
    Ok(())
}
