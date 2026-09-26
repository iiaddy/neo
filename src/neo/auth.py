"""neo auth store: per-provider API keys in ~/.config/neo/auth.json.

Resolution order for a key (see resolve_api_key):
    catalog env vars -> auth.json -> config.providers[pid]["api_key"]

The store file is written with mode 0o600 and re-chmodded on every write,
even when the file already existed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_DEFAULT_PATH = Path.home() / ".config" / "neo" / "auth.json"
_FILE_MODE = 0o600

# Explicit env-var mapping; anything else falls back to {PID}_API_KEY with
# the provider id sanitized to uppercase-underscore form.
_ENV_MAP: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}

_SANITIZE_RE = re.compile(r"[^A-Z0-9]+")


def _catalog_env_names(provider_id: str) -> tuple[str, ...]:
    """Env var names declared by the provider catalog, or () when unknown."""
    try:
        # Lazy: neo.providers imports this module for key resolution, so a
        # top-level import would be circular.
        from .providers.catalog import PROVIDERS
    except Exception:
        return ()
    pid = provider_id.lower()
    for spec in PROVIDERS:
        if spec.id == pid:
            return tuple(spec.env_vars)
    return ()


def _env_names(provider_id: str) -> tuple[str, ...]:
    # A catalog entry's env_vars are authoritative for that provider.
    catalog = _catalog_env_names(provider_id)
    if catalog:
        return catalog
    explicit = _ENV_MAP.get(provider_id.lower())
    if explicit:
        return explicit
    # Sanitized fallback: "kimi-code" -> KIMI_CODE_API_KEY. A raw
    # upper-cased id would give the invalid shell name KIMI-CODE_API_KEY.
    safe = _SANITIZE_RE.sub("_", provider_id.upper()).strip("_")
    return (f"{safe}_API_KEY",)


class AuthStore:
    """Small JSON-backed key store. Never logs or prints stored keys."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else _DEFAULT_PATH

    # -- persistence -------------------------------------------------------
    def _read_all(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_all(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(self.path, _FILE_MODE)

    # -- public API --------------------------------------------------------
    def get(self, provider_id: str) -> str | None:
        value = self._read_all().get(provider_id)
        return value if isinstance(value, str) and value else None

    def set(self, provider_id: str, key: str) -> None:
        data = self._read_all()
        data[provider_id] = key
        self._write_all(data)

    def delete(self, provider_id: str) -> None:
        data = self._read_all()
        if provider_id in data:
            del data[provider_id]
            self._write_all(data)

    def providers(self) -> dict:
        return dict(self._read_all())


def resolve_api_key(provider_id: str, config, store: AuthStore | None = None,
                    env_vars: tuple[str, ...] | None = None) -> str | None:
    """Resolve a provider's API key: env vars -> auth.json -> config.

    `config` may be a NeoConfig (config.providers[pid]["api_key"]) or any
    object exposing the same mapping. `env_vars`, when given, are the
    authoritative env names and are checked first; otherwise the catalog's
    env_vars win over the hardcoded map and the sanitized
    ``{PROVIDER_ID}_API_KEY`` fallback. An explicit
    ``providers.<pid>.api_key_env`` names one more environment variable and
    is checked right after the env names, before the auth store, so an
    explicit choice beats a stale stored key. Returns None when nothing is
    set. The key is never logged or printed by this function.
    """
    for name in (tuple(env_vars) if env_vars else _env_names(provider_id)):
        value = os.environ.get(name)
        if value:
            return value
    overrides = (getattr(config, "providers", None) or {}).get(provider_id) or {}
    api_key_env = overrides.get("api_key_env")
    if api_key_env:
        value = os.environ.get(api_key_env)
        if value:
            return value
    store = store if store is not None else AuthStore()
    stored = store.get(provider_id)
    if stored:
        return stored
    key = overrides.get("api_key")
    return key if key else None
