"""neo configuration: discovery, schema, merge."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .sandbox.config import default_sandbox_config

CONFIG_FILENAME = "neo.json"
GLOBAL_CONFIG_DIR = Path.home() / ".config" / "neo"


def default_permissions() -> dict:
    return {
        "read": {"*": "allow", "*.env": "ask", "*.env.*": "ask"},
        "edit": {"*": "ask"},
        "bash": {
            "*": "ask",
            "git status *": "allow",
            "git diff *": "allow",
            "git log *": "allow",
            "git branch *": "allow",
            "ls *": "allow",
        },
        "web": {"*": "allow"},
        "task": {"*": "ask"},
        "session": {"*": "allow"},
    }


@dataclass
class NeoConfig:
    model: str = "anthropic/claude-sonnet-4-6"  # "provider/model"
    small_model: str = "anthropic/claude-haiku-4-5"
    max_steps: int = 40
    context_window: int = 200_000
    theme: str = "neo-dark"
    thinking: str = "medium"  # off|low|medium|high
    permissions: dict = field(default_factory=default_permissions)
    providers: dict = field(default_factory=dict)  # pid -> {"api_key","base_url"}
    sandbox: dict = field(default_factory=default_sandbox_config)
    verify_commands: list = field(default_factory=list)
    keybindings: dict = field(default_factory=dict)
    disabled_tools: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "NeoConfig":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        cfg = cls()
        for k, v in clean.items():
            if k in ("permissions", "providers", "keybindings", "sandbox") and isinstance(v, dict):
                merged = getattr(cfg, k)
                _deep_merge(merged, v)
            else:
                setattr(cfg, k, v)
        return cfg


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def split_model(spec: str) -> tuple[str, str]:
    """'anthropic/claude-sonnet-4-6' -> ('anthropic', 'claude-sonnet-4-6').

    Bare model names map to their home provider; anything else falls back
    to the OpenAI-compatible protocol.
    """
    s = spec.strip()
    if "/" in s:
        pid, _, mid = s.partition("/")
        return pid.strip(), mid.strip()
    low = s.lower()
    if low.startswith(("gpt-", "gpt4", "o1", "o3", "o4")):
        return "openai", s
    if low.startswith("claude"):
        return "anthropic", s
    if low.startswith("gemini"):
        return "google", s
    return "openai", s


def _candidate_paths(workdir: Path) -> list[Path]:
    paths: list[Path] = []
    node = workdir.resolve()
    seen = set()
    while True:
        for name in (CONFIG_FILENAME, f".neo/{CONFIG_FILENAME}"):
            p = node / name
            if p not in seen:
                seen.add(p)
                paths.append(p)
        parent = node.parent
        if parent == node or node == Path.home().parent:
            break
        node = parent
        if len(paths) > 40:
            break
    return paths


def discover_config(workdir: str | Path = ".") -> tuple[NeoConfig, Path | None]:
    """Merge order: defaults < global ~/.config/neo/neo.json < walk-up project files."""
    workdir = Path(workdir).resolve()
    layers: list[Path] = []
    global_cfg = GLOBAL_CONFIG_DIR / CONFIG_FILENAME
    if global_cfg.is_file():
        layers.append(global_cfg)
    for p in reversed(_candidate_paths(workdir)):
        if p.is_file():
            layers.append(p)
    cfg = NeoConfig()
    found: Path | None = None
    for p in layers:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            cfg = NeoConfig.from_dict({**_as_dict(cfg), **data})
            found = p
    # env overrides
    if os.environ.get("NEO_MODEL"):
        cfg.model = os.environ["NEO_MODEL"]
    return cfg, found


def _as_dict(cfg: NeoConfig) -> dict:
    return {f: getattr(cfg, f) for f in cfg.__dataclass_fields__}


def config_dir() -> Path:
    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return GLOBAL_CONFIG_DIR
