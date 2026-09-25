"""neo LSP — public surface of the language-server subsystem."""

from .client import (
    LSPClient,
    LSPError,
    LSPNotInstalled,
    get_diagnostics,
    reset_state,
    shutdown_all,
)
from .postedit import after_edit, dedupe, snapshot_diagnostics
from .protocol import ProtocolError, encode_message, read_message, write_message
from .servers import (
    SERVERS,
    ServerEntry,
    all_servers,
    detect_server,
    resolve_command,
)

__all__ = [
    "SERVERS",
    "LSPClient",
    "LSPError",
    "LSPNotInstalled",
    "ProtocolError",
    "ServerEntry",
    "after_edit",
    "all_servers",
    "dedupe",
    "detect_server",
    "encode_message",
    "get_diagnostics",
    "read_message",
    "reset_state",
    "resolve_command",
    "shutdown_all",
    "snapshot_diagnostics",
    "write_message",
]
