"""Public provider API: list_providers() and resolve_provider()."""

from __future__ import annotations

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

_VALID_PROTOCOLS = (PROTOCOL_OPENAI, PROTOCOL_ANTHROPIC, PROTOCOL_GOOGLE)


def _needs_base_url_msg(pid: str, protocol: str) -> str:
    if protocol == PROTOCOL_ANTHROPIC:
        hint = "an Anthropic-compatible endpoint (e.g. a proxy speaking the Anthropic Messages API)"
    elif protocol == PROTOCOL_GOOGLE:
        hint = "a Gemini-compatible endpoint (e.g. a proxy speaking the Gemini API)"
    else:
        hint = "an OpenAI-compatible proxy"
    return (
        f"set providers.{pid}.base_url in neo.json pointing at {hint}"
    )

_BY_ID: dict[str, ProviderSpec] = {spec.id: spec for spec in PROVIDERS}


def list_providers() -> list[ProviderSpec]:
    """Return the full provider catalog."""
    return list(PROVIDERS)


def resolve_provider(provider_id: str, cfg: Any) -> Provider:
    """Resolve a provider id to a concrete Provider.

    cfg.providers[pid] may carry {"api_key", "base_url", "protocol"} overrides.
    Unknown provider ids synthesize a ProviderSpec from cfg overrides
    (base_url is required there); the protocol override defaults to "openai"
    and must be one of "openai", "anthropic", "google".
    """
    overrides = (getattr(cfg, "providers", None) or {}).get(provider_id) or {}

    spec = _BY_ID.get(provider_id)
    if spec is None:
        base_url = overrides.get("base_url") or ""
        protocol = overrides.get("protocol") or PROTOCOL_OPENAI
        if protocol not in _VALID_PROTOCOLS:
            raise ProviderConfigError(
                f"unknown provider '{provider_id}': invalid protocol {protocol!r} "
                "(expected one of 'openai', 'anthropic', 'google')"
            )
        if not base_url:
            raise ProviderConfigError(
                f"unknown provider '{provider_id}': "
                f"{_needs_base_url_msg(provider_id, protocol)} "
                "(plus providers.<id>.api_key or an env var)"
            )
        spec = ProviderSpec(
            id=provider_id,
            title=provider_id,
            protocol=protocol,
            base_url=base_url,
            env_vars=(),
            default_model=overrides.get("default_model", ""),
        )

    from ..auth import resolve_api_key
    # The spec's env_vars (the catalog's for known providers) are checked
    # before the auth store and config, so a real environment variable always
    # beats a stale configured api_key.
    api_key = resolve_api_key(provider_id, cfg, env_vars=spec.env_vars or None)
    base_url = overrides.get("base_url") or spec.base_url
    if not base_url:
        raise ProviderConfigError(
            f"provider '{provider_id}' has no default base_url: "
            f"{_needs_base_url_msg(provider_id, spec.protocol)}"
        )

    cls = {
        PROTOCOL_OPENAI: OpenAICompatProvider,
        PROTOCOL_ANTHROPIC: AnthropicProvider,
        PROTOCOL_GOOGLE: GoogleProvider,
    }[spec.protocol]
    return cls(spec, api_key, base_url=base_url)
