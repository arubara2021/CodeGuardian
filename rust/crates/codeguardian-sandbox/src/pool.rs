//! Warm sandbox pool.
//!
//! Creating a sandbox is the slow part of executing a command. The
//! pool keeps a small number of idle handles alive so the first
//! execution of a batch does not pay the creation cost twice. `acquire`
//! returns an idle handle when one is available and creates a fresh
//! one otherwise. `release` returns a handle to the pool if the pool
//! has room and destroys it otherwise.
//!
//! The pool holds handles, not sandboxes. A backend like Bubblewrap
//! where `create` is a no-op has nothing to keep alive; the pool's
//! value is holding the handle itself and saving the round trip to
//! `create`. A backend like Docker where `create` allocates real
//! resources benefits from the pool holding those resources idle.
//!
//! The invariant this module protects is that a handle in the pool is
//! always destroyed when the pool is shut down. Dropping the pool does
//! not automatically shut it down; the caller must call `shutdown`
//! explicitly, which is why the caller is the pipeline and not a
//! short-lived function.

use std::sync::Arc;

use tokio::sync::Mutex;

use crate::backend::{SandboxBackend, SandboxHandle, SandboxSpec};
use crate::SandboxError;

/// A pool of warm sandbox handles for one backend and one spec.
pub struct WarmPool {
    backend: Arc<dyn SandboxBackend>,
    spec: SandboxSpec,
    max_idle: usize,
    idle: Mutex<Vec<SandboxHandle>>,
}

impl WarmPool {
    /// Construct a pool that keeps at most `max_idle` handles ready.
    ///
    /// A `max_idle` of zero makes the pool a passthrough: every
    /// `acquire` creates a new sandbox and every `release` destroys it.
    pub fn new(backend: Arc<dyn SandboxBackend>, spec: SandboxSpec, max_idle: usize) -> Self {
        Self {
            backend,
            spec,
            max_idle,
            idle: Mutex::new(Vec::new()),
        }
    }

    /// Return the number of handles currently idle in the pool.
    pub async fn idle_count(&self) -> usize {
        self.idle.lock().await.len()
    }

    /// Return the maximum number of handles the pool will keep idle.
    pub fn max_idle(&self) -> usize {
        self.max_idle
    }

    /// Acquire a handle. Reuses an idle one when available, creates a
    /// fresh one otherwise.
    pub async fn acquire(&self) -> Result<SandboxHandle, SandboxError> {
        {
            let mut idle = self.idle.lock().await;
            if let Some(handle) = idle.pop() {
                return Ok(handle);
            }
        }
        self.backend.create(&self.spec).await
    }

    /// Return a handle to the pool, or destroy it if the pool is full.
    pub async fn release(&self, handle: SandboxHandle) -> Result<(), SandboxError> {
        let should_push = {
            let idle = self.idle.lock().await;
            idle.len() < self.max_idle
        };
        if should_push {
            let mut idle = self.idle.lock().await;
            idle.push(handle);
            return Ok(());
        }
        self.backend.destroy(handle).await
    }

    /// Destroy every idle handle in the pool.
    ///
    /// Handles acquired from the pool but not yet released are the
    /// caller's responsibility. The pool does not track outstanding
    /// handles.
    pub async fn shutdown(&self) -> Result<(), SandboxError> {
        let handles: Vec<SandboxHandle> = {
            let mut idle = self.idle.lock().await;
            std::mem::take(&mut *idle)
        };
        let mut first_error: Option<SandboxError> = None;
        for handle in handles {
            if let Err(err) = self.backend.destroy(handle).await {
                if first_error.is_none() {
                    first_error = Some(err);
                }
            }
        }
        match first_error {
            Some(err) => Err(err),
            None => Ok(()),
        }
    }
}
