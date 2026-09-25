"""Sandbox configuration: parsing, defaults, and validation."""

from __future__ import annotations

import dataclasses


class SandboxError(Exception):
    """Raised when the sandbox cannot run (strict mode) or config is invalid."""


# Domains the agent typically needs: git hosts, package registries, docs.
DEFAULT_ALLOWED_DOMAINS = [
    "github.com",
    "*.github.com",
    "raw.githubusercontent.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "pypi.org",
    "*.pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "*.npmjs.org",
    "registry.yarnpkg.com",
]

# Environment passed into the sandbox by default. Deliberately small:
# secrets must be opted in via ``allowSecrets``.
DEFAULT_PASS_ENV = [
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "NO_COLOR",
    "CLICOLOR",
]

# Even when whitelisted, these never enter the sandbox unless the user
# explicitly lists them in ``allowSecrets``.
SECRET_ENV_PATTERNS = (
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PRIVATE_KEY",
    "AWS_ACCESS",
    "AWS_SECRET",
    "GITHUB_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
)

# Ports the in-sandbox HTTP/SOCKS listeners use (fixed, like srt/codex).
SANDBOX_HTTP_PROXY_PORT = 3128
SANDBOX_SOCKS_PROXY_PORT = 1080


@dataclasses.dataclass
class SandboxConfig:
    """Filesystem / network / environment policy for sandboxed commands."""

    mode: str = "auto"  # off | auto | strict
    network: str = "filtered"  # none | filtered | full
    allowed_domains: list = dataclasses.field(default_factory=lambda: list(DEFAULT_ALLOWED_DOMAINS))
    denied_domains: list = dataclasses.field(default_factory=list)
    allow_write: list = dataclasses.field(default_factory=lambda: ["."])
    deny_read: list = dataclasses.field(
        default_factory=lambda: ["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh"]
    )
    deny_write: list = dataclasses.field(default_factory=lambda: [".env"])
    pass_env: list = dataclasses.field(default_factory=lambda: list(DEFAULT_PASS_ENV))
    allow_secrets: list = dataclasses.field(default_factory=list)
    private_tmp: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "SandboxConfig":
        """Build from a ``neo.json`` ``sandbox`` section.

        Accepts both snake_case and camelCase keys (``allowedDomains`` and
        ``allowed_domains`` both work).
        """
        if not isinstance(data, dict):
            raise SandboxError("sandbox config must be an object")
        norm: dict = {}
        for key, value in data.items():
            norm[_to_snake(key)] = value
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(k for k in norm if k not in known)
        if unknown:
            raise SandboxError(f"unknown sandbox keys: {', '.join(unknown)}")
        cfg = cls()
        for key, value in norm.items():
            if key in (
                "allowed_domains",
                "denied_domains",
                "allow_write",
                "deny_read",
                "deny_write",
                "pass_env",
                "allow_secrets",
            ):
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise SandboxError(f"sandbox.{key} must be a list of strings")
                setattr(cfg, key, list(value))
            elif key in ("mode", "network"):
                if not isinstance(value, str):
                    raise SandboxError(f"sandbox.{key} must be a string")
                setattr(cfg, key, value.strip().lower())
            elif key == "private_tmp":
                setattr(cfg, key, bool(value))
            else:  # pragma: no cover - guarded by `known`
                setattr(cfg, key, value)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.mode not in ("off", "auto", "strict"):
            raise SandboxError(f"unknown sandbox mode: {self.mode!r} (off|auto|strict)")
        if self.network not in ("none", "filtered", "full"):
            raise SandboxError(f"unknown sandbox network: {self.network!r} (none|filtered|full)")


def _to_snake(key: str) -> str:
    out = []
    for i, ch in enumerate(key):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def default_sandbox_config() -> dict:
    """Default ``sandbox`` section for a fresh ``neo.json`` (as plain dict)."""
    return {
        "mode": "auto",
        "network": "filtered",
        "allowed_domains": list(DEFAULT_ALLOWED_DOMAINS),
        "denied_domains": [],
        "allow_write": ["."],
        "deny_read": ["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh"],
        "deny_write": [".env"],
        "pass_env": list(DEFAULT_PASS_ENV),
        "allow_secrets": [],
        "private_tmp": True,
    }
