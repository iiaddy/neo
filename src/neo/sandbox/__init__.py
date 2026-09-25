"""neo sandbox — OS-level isolation for shell commands.

On Linux this wraps commands in bubblewrap (bwrap), the same primitive
Claude Code's sandbox runtime and OpenAI Codex use:

- filesystem: whole tree read-only, workdir read-write, sensitive paths
  (``~/.ssh`` …) hidden, ``denyWrite`` paths re-mounted read-only
- network: ``none`` (no network), ``full`` (host network) or ``filtered``
  (isolated net namespace + a domain-allowlist HTTP proxy bridged in)
- environment: cleared, only an explicit ``passEnv`` whitelist survives

When bwrap is unavailable the sandbox degrades according to ``mode``:
``auto`` warns once and runs directly, ``strict`` fails loudly, ``off``
disables sandboxing entirely.
"""

from .config import SandboxConfig, SandboxError, default_sandbox_config
from .detect import SandboxStatus, detect_sandbox
from .linux import build_bwrap_argv
from .policy import domain_allowed, expand_path, sanitize_env
from .proxy import FilteringProxy
from .runner import SandboxPlan, SandboxSession, plan_sandbox

__all__ = [
    "SandboxConfig",
    "SandboxError",
    "SandboxPlan",
    "SandboxSession",
    "SandboxStatus",
    "default_sandbox_config",
    "detect_sandbox",
    "build_bwrap_argv",
    "domain_allowed",
    "expand_path",
    "sanitize_env",
    "FilteringProxy",
    "plan_sandbox",
]
