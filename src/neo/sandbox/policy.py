"""Policy helpers: domain matching, path expansion, env sanitizing."""

from __future__ import annotations

import os
from pathlib import Path

from .config import SECRET_ENV_PATTERNS


def domain_allowed(host: str, allowed: list, denied: list) -> bool:
    """True when ``host`` may be reached under the network policy.

    - ``denied`` always wins.
    - an empty ``allowed`` list means "allow everything not denied".
    - entries may be exact (``github.com``) or wildcard (``*.github.com``,
      which matches subdomains but not the bare domain itself).
    """
    host = _normalize_host(host)
    for pattern in denied:
        if _match_domain(host, pattern):
            return False
    if not allowed:
        return True
    return any(_match_domain(host, pattern) for pattern in allowed)


def _normalize_host(host: str) -> str:
    host = host.strip().lower()
    if host.endswith("."):
        host = host[:-1]
    # strip a trailing :port if present (but keep IPv6 literals intact)
    if host.count(":") == 1 and ":" in host:
        host = host.rsplit(":", 1)[0]
    return host


def _match_domain(host: str, pattern: str) -> bool:
    pattern = pattern.strip().lower().rstrip(".")
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return host != suffix and host.endswith("." + suffix)
    return host == pattern


def expand_path(entry: str, workdir: Path) -> Path:
    """Resolve a config path entry: ``~``-expand, then relative → workdir."""
    expanded = os.path.expanduser(entry)
    path = Path(expanded)
    if not path.is_absolute():
        path = workdir / path
    return path


def resolved_paths(entry: str, workdir: Path) -> list[Path]:
    """Literal + real (symlink-resolved) path for a deny entry.

    A symlink planted at an allowed path must not cancel a deny of its
    target, so denies are applied to both spellings when they differ.
    """
    path = expand_path(entry, workdir)
    try:
        real = path.resolve()
    except OSError:
        return [path]
    return [path] if real == path else [path, real]


def sanitize_env(
    pass_env: list,
    allow_secrets: list,
    extra: dict | None = None,
) -> dict:
    """Build the sandbox environment: whitelist + secret scrubbing."""
    env: dict[str, str] = {}
    allowed_secrets = {name.upper() for name in allow_secrets}
    for name in pass_env:
        if name not in os.environ:
            continue
        upper = name.upper()
        if upper in allowed_secrets:
            env[name] = os.environ[name]
            continue
        if any(pat in upper for pat in SECRET_ENV_PATTERNS):
            continue  # secret-looking var, not explicitly opted in
        env[name] = os.environ[name]
    if extra:
        env.update(extra)
    return env
