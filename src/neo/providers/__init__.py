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


def list_providers(cfg: Any = None) -> list[ProviderSpec]:
    """Return the full provider catalog.

    When ``cfg`` is given, ``cfg.providers[pid]`` overlays are applied:
    catalog entries pick up ``title``/``base_url``/``models``/
    ``model_names``/``default_model``/``extra_headers`` overrides, and
    unknown ids with a ``base_url`` are synthesized into full specs
    (``protocol`` defaults to ``"openai"``). The /login and /model pickers
    pass the live config so a freshly configured custom provider shows up
    immediately.
    """
    specs = list(PROVIDERS)
    overrides = (getattr(cfg, "providers", None) or {}) if cfg is not None else {}
    if not overrides:
        return specs
    out: list[ProviderSpec] = []
    seen: set[str] = set()
    for spec in specs:
        ov = overrides.get(spec.id)
        if isinstance(ov, dict) and ov:
            spec = _apply_overrides(spec, ov)
        out.append(spec)
        seen.add(spec.id)
    for pid, ov in overrides.items():
        if pid in seen or not isinstance(ov, dict):
            continue
        if not ov.get("base_url"):
            continue  # nothing usable to synthesize
        protocol = ov.get("protocol") or PROTOCOL_OPENAI
        if protocol not in _VALID_PROTOCOLS:
            continue
        out.append(ProviderSpec(
            id=pid,
            title=ov.get("title") or pid,
            protocol=protocol,
            base_url=ov.get("base_url"),
            env_vars=tuple(ov.get("env_vars") or ()),
            default_model=ov.get("default_model") or "",
            models=tuple(ov.get("models") or ()),
            model_names=dict(ov.get("model_names") or {}),
            extra_headers=dict(ov.get("extra_headers") or {}),
        ))
    return out


def _apply_overrides(spec: ProviderSpec, ov: dict[str, Any]) -> ProviderSpec:
    """Overlay config values onto a catalog spec (truthy values win)."""
    from dataclasses import replace
    kw: dict[str, Any] = {}
    if ov.get("title"):
        kw["title"] = ov["title"]
    if ov.get("base_url"):
        kw["base_url"] = ov["base_url"]
    if ov.get("default_model"):
        kw["default_model"] = ov["default_model"]
    if ov.get("models"):
        kw["models"] = tuple(ov["models"])
    if ov.get("model_names"):
        kw["model_names"] = dict(ov["model_names"])
    if ov.get("extra_headers"):
        kw["extra_headers"] = dict(ov["extra_headers"])
    return replace(spec, **kw) if kw else spec


def resolve_provider(provider_id: str, cfg: Any) -> Provider:
    """Resolve a provider id to a concrete Provider.

    cfg.providers[pid] may carry {"api_key", "api_key_env", "base_url",
    "protocol", "title", "models", "model_names", "default_model"} overrides.
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
            title=overrides.get("title") or provider_id,
            protocol=protocol,
            base_url=base_url,
            env_vars=(),
            default_model=overrides.get("default_model") or "",
            models=tuple(overrides.get("models") or ()),
            model_names=dict(overrides.get("model_names") or {}),
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
