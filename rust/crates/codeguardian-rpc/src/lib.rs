//! JSON-RPC 2.0 wire types and transport for the CodeGuardian bridge.
//!
//! ``protocol`` defines the envelope. ``server`` dispatches requests
//! to registered handlers. ``client`` sends requests and reads
//! responses. Both server and client are generic over their byte
//! streams, so tests can exercise them with in-memory buffers.

pub mod client;
pub mod protocol;
pub mod server;

pub use client::RpcClient;
pub use protocol::{RpcError, RpcNotification, RpcRequest, RpcResponse, JSONRPC_VERSION};
pub use server::{
    Handler, RpcServer, INTERNAL_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR,
};
