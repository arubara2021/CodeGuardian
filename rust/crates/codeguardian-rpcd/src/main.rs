//! The CodeGuardian JSON-RPC daemon.
//!
//! A single-purpose binary that speaks JSON-RPC 2.0 over stdio. It is
//! spawned by `codeguardian.core.bridges.jsonrpc_bridge.JSONRPCBridge`
//! and hosts the handlers for the sandbox and audit crates. Those
//! crates are deliberately not exposed via PyO3 because they sit on
//! the wrong side of the trust boundary: the audit writer owns the log
//! file, and the sandbox engine runs untrusted code.
//!
//! The protocol is line-delimited JSON. Each message is one JSON
//! object followed by a newline. stdout carries only the protocol;
//! every diagnostic goes to stderr.
//!
//! The daemon registers six handlers. `sandbox.health`,
//! `sandbox.create`, and `sandbox.destroy` cover the sandbox lifecycle
//! except for execution, which requires an installed backend and is
//! exercised elsewhere. `audit.write`, `audit.verify`, and
//! `audit.export` cover the audit log.

use std::io;
use std::process::ExitCode;
use std::sync::Arc;

use codeguardian_rpc::RpcServer;
use tokio::runtime::Runtime;

mod handlers;

/// The JSON-RPC method name for a sandbox health check.
const SANDBOX_HEALTH: &str = "sandbox.health";
/// The JSON-RPC method name for creating a sandbox handle.
const SANDBOX_CREATE: &str = "sandbox.create";
/// The JSON-RPC method name for destroying a sandbox handle.
const SANDBOX_DESTROY: &str = "sandbox.destroy";
/// The JSON-RPC method name for appending an audit entry.
const AUDIT_WRITE: &str = "audit.write";
/// The JSON-RPC method name for verifying an audit chain.
const AUDIT_VERIFY: &str = "audit.verify";
/// The JSON-RPC method name for exporting an audit log.
const AUDIT_EXPORT: &str = "audit.export";

fn main() -> ExitCode {
    let runtime = match build_runtime() {
        Ok(rt) => Arc::new(rt),
        Err(err) => {
            eprintln!("codeguardian-rpcd: failed to build runtime: {err}");
            return ExitCode::FAILURE;
        }
    };

    let mut server = RpcServer::new();
    register_handlers(&mut server, Arc::clone(&runtime));

    let stdin = io::stdin();
    let stdout = io::stdout();
    let reader = stdin.lock();
    let writer = stdout.lock();

    match server.serve(reader, writer) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("codeguardian-rpcd: serve failed: {err}");
            ExitCode::FAILURE
        }
    }
}

/// Build the tokio runtime the handlers run on.
///
/// Two worker threads is enough: the JSON-RPC loop is serial and each
/// handler awaits at most one operation. The runtime is not the
/// bottleneck; disk and subprocess latency are.
fn build_runtime() -> io::Result<Runtime> {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
}

/// Register every handler with the server.
///
/// Each handler closure captures a clone of the runtime and blocks the
/// calling thread while the async work runs on the runtime's workers.
/// The RPC server's dispatch loop is synchronous, so a handler that
/// returned a `Future` to it would not be awaited.
fn register_handlers(server: &mut RpcServer, runtime: Arc<Runtime>) {
    {
        let rt = Arc::clone(&runtime);
        server.register(SANDBOX_HEALTH, move |params| {
            rt.block_on(handlers::sandbox_health(params))
        });
    }
    {
        let rt = Arc::clone(&runtime);
        server.register(SANDBOX_CREATE, move |params| {
            rt.block_on(handlers::sandbox_create(params))
        });
    }
    {
        let rt = Arc::clone(&runtime);
        server.register(SANDBOX_DESTROY, move |params| {
            rt.block_on(handlers::sandbox_destroy(params))
        });
    }
    {
        let rt = Arc::clone(&runtime);
        server.register(AUDIT_WRITE, move |params| {
            rt.block_on(handlers::audit_write(params))
        });
    }
    {
        let rt = Arc::clone(&runtime);
        server.register(AUDIT_VERIFY, move |params| {
            rt.block_on(handlers::audit_verify(params))
        });
    }
    {
        let rt = Arc::clone(&runtime);
        server.register(AUDIT_EXPORT, move |params| {
            rt.block_on(handlers::audit_export(params))
        });
    }
}
