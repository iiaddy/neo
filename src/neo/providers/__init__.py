"""Public provider API: list_providers() and resolve_provider()."""

from __future__ import annotations

import os
from typing import Any

from .anthropic import AnthropicProvider
from .base import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_GOOGLE,
    PROTOCOL_OPENAI,
    Provider,
    ProviderAuthError,
    ProviderConfigError,
    ProviderSpec,
)
from .catalog import PROVIDERS
from .google import GoogleProvider
from .openai_compat import OpenAICompatProvider

__all__ = [
    "PROVIDERS",
    "Provider",
    "ProviderSpec",
    "ProviderAuthError",
    "ProviderConfigError",
    "AnthropicProvider",
    "GoogleProvider",
    "OpenAICompatProvider",
    "list_providers",
    "resolve_provider",
]

_NEEDS_BASE_URL_MSG = (
    "provider '{pid}' has no default base_url: set providers.{pid}.base_url "
    "in neo.json pointing at an OpenAI-compatible proxy"
)

_BY_ID: dict[str, ProviderSpec] = {spec.id: spec for spec in PROVIDERS}


def list_providers() -> list[ProviderSpec]:
    """Return the full provider catalog."""
    return list(PROVIDERS)


def _first_set_env(env_vars: tuple[str, ...]) -> str | None:
    for name in env_vars:
        value = os.environ.get(name)
        if value:
            return value
    return None


def resolve_provider(provider_id: str, cfg: Any) -> Provider:
    """Resolve a provider id to a concrete Provider.

    cfg.providers[pid] may carry {"api_key", "base_url"} overrides.
    Unknown provider ids synthesize an OpenAI-compatible ProviderSpec from
    cfg overrides (base_url is required there).
    """
    overrides = (getattr(cfg, "providers", None) or {}).get(provider_id) or {}

    spec = _BY_ID.get(provider_id)
    if spec is None:
        base_url = overrides.get("base_url") or ""
        if not base_url:
            raise ProviderConfigError(
                f"unknown provider '{provider_id}': set providers.{provider_id}.base_url "
                "in neo.json (plus providers.<id>.api_key or an env var)"
            )
        spec = ProviderSpec(
            id=provider_id,
            title=provider_id,
            protocol=PROTOCOL_OPENAI,
            base_url=base_url,
            env_vars=(),
            default_model=overrides.get("default_model", ""),
        )

    from ..auth import resolve_api_key
    api_key = (resolve_api_key(provider_id, cfg)
               or _first_set_env(spec.env_vars))
    base_url = overrides.get("base_url") or spec.base_url
    if not base_url:
        raise ProviderConfigError(_NEEDS_BASE_URL_MSG.format(pid=provider_id))

    cls = {
        PROTOCOL_OPENAI: OpenAICompatProvider,
        PROTOCOL_ANTHROPIC: AnthropicProvider,
        PROTOCOL_GOOGLE: GoogleProvider,
    }[spec.protocol]
    return cls(spec, api_key, base_url=base_url)
