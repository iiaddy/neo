"""neo LSP server catalog: builtin entries, binary resolution, detection.

Detection is by file extension. Config may override per-server command /
enabled flags, and may define entirely custom servers::

    "lsp": {
        "enabled": true,
        "servers": {
            "pyright": {"enabled": false},
            "myserver": {
                "command": ["my-ls", "--stdio"],
                "extensions": [".xyz"],
                "install_hint": "npm i -g my-ls",
            },
        },
    }
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from pathlib import Path
from typing import Any


@dataclasses.dataclass
class ServerEntry:
    name: str
    command: list[str]                       # primary argv; first hit on PATH wins
    extensions: tuple[str, ...] = ()
    language_id: str = ""                    # sent in textDocument/didOpen
    install_hint: str = ""
    alternatives: tuple[list[str], ...] = ()  # fallback argv candidates


SERVERS: dict[str, ServerEntry] = {
    "pyright": ServerEntry(
        name="pyright",
        command=["basedpyright-langserver", "--stdio"],
        alternatives=(["pyright-langserver", "--stdio"],),
        extensions=(".py", ".pyi"),
        language_id="python",
        install_hint="pip install basedpyright  (or: npm i -g pyright)",
    ),
    "gopls": ServerEntry(
        name="gopls",
        command=["gopls"],
        extensions=(".go",),
        language_id="go",
        install_hint="go install golang.org/x/tools/gopls@latest",
    ),
    "rust-analyzer": ServerEntry(
        name="rust-analyzer",
        command=["rust-analyzer"],
        extensions=(".rs",),
        language_id="rust",
        install_hint="rustup component add rust-analyzer",
    ),
    "eslint": ServerEntry(
        name="eslint",
        command=["vscode-eslint-language-server", "--stdio"],
        extensions=(".js", ".jsx", ".ts", ".tsx"),
        language_id="javascript",
        install_hint="npm i -g vscode-eslint-language-server",
    ),
    "typescript-language-server": ServerEntry(
        name="typescript-language-server",
        command=["typescript-language-server", "--stdio"],
        extensions=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
        language_id="typescript",
        install_hint="npm i -g typescript-language-server typescript",
    ),
}


def _lsp_config(config: Any) -> dict:
    if config is None:
        return {}
    if isinstance(config, dict):
        section = config.get("lsp")
    else:
        section = getattr(config, "lsp", None)
    if isinstance(section, dict):
        return section
    return {}


def _server_overrides(config: Any) -> dict:
    return _lsp_config(config).get("servers", {}) or {}


def lsp_enabled(config: Any) -> bool:
    """Global kill switch; defaults to on."""
    return bool(_lsp_config(config).get("enabled", True))


def server_enabled(name: str, config: Any) -> bool:
    if not lsp_enabled(config):
        return False
    override = _server_overrides(config).get(name)
    if isinstance(override, dict):
        return bool(override.get("enabled", True))
    return True


def _custom_entry(name: str, spec: dict) -> ServerEntry | None:
    """Build a ServerEntry from a config-defined custom server."""
    command = spec.get("command")
    if not command:
        return None
    extensions = tuple(spec.get("extensions", ()))
    return ServerEntry(
        name=name,
        command=list(command),
        extensions=extensions,
        language_id=spec.get("language_id", ""),
        install_hint=spec.get("install_hint", ""),
    )


def all_servers(config: Any = None) -> dict[str, ServerEntry]:
    """Builtin catalog merged with config-defined custom servers.

    Custom entries may add new servers or shadow builtins by name.
    Disabled servers are excluded.
    """
    merged = dict(SERVERS)
    for name, spec in _server_overrides(config).items():
        if isinstance(spec, dict) and spec.get("command") and name not in SERVERS:
            entry = _custom_entry(name, spec)
            if entry is not None:
                merged[name] = entry
    return {n: e for n, e in merged.items() if server_enabled(n, config)}


def detect_server(path: str | Path, config: Any = None) -> ServerEntry | None:
    """Return the enabled server entry for a file's extension, or None.

    First match in catalog order wins.
    """
    ext = Path(path).suffix.lower()
    if not ext:
        return None
    for entry in all_servers(config).values():
        if ext in entry.extensions:
            return entry
    return None


def _binary_available(argv0: str) -> bool:
    if os.path.isabs(argv0):
        return os.path.isfile(argv0) and os.access(argv0, os.X_OK)
    return shutil.which(argv0) is not None


def resolve_command(entry: ServerEntry, config: Any = None) -> list[str] | None:
    """Resolve an entry to a runnable argv, or None when no binary is found.

    A config override ``servers.<name>.command`` wins outright; otherwise the
    first candidate (primary, then alternatives) present on PATH is used.
    """
    override = _server_overrides(config).get(entry.name)
    if isinstance(override, dict) and override.get("command"):
        return list(override["command"])
    for argv in (entry.command, *entry.alternatives):
        if argv and _binary_available(argv[0]):
            return list(argv)
    return None
