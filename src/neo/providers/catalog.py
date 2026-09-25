"""Provider catalog for neo, loaded from ``providers.json``.

The JSON file is the single source of truth for the provider list
(227 providers — every models.opencode.ai / models.dev provider except the
one literally named ``opencode``, plus the curated extras together,
fireworks, kimi-code, sambanova and ollama — and a ``custom``
OpenAI-compatible entry the user configures via ``neo.json``).

Regenerate it with ``scripts/build_catalog.py``; never hand-edit the
generated rows.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from .base import ProviderSpec

_CATALOG_RESOURCE = "providers.json"


def _load_rows() -> list[dict[str, Any]]:
    data = resources.files(__package__).joinpath(_CATALOG_RESOURCE).read_text(
        encoding="utf-8"
    )
    rows = json.loads(data)
    if not isinstance(rows, list):
        raise ValueError(f"{_CATALOG_RESOURCE} must contain a JSON list")
    return rows


def _spec_from_row(row: dict[str, Any]) -> ProviderSpec:
    return ProviderSpec(
        id=row["id"],
        title=row.get("title") or row["id"],
        protocol=row.get("protocol") or "openai",
        base_url=row.get("base_url") or "",
        env_vars=tuple(row.get("env_vars") or ()),
        default_model=row.get("default_model") or "",
        extra_headers=dict(row.get("extra_headers") or {}),
    )


PROVIDERS: list[ProviderSpec] = [_spec_from_row(r) for r in _load_rows()]

# Fail fast on a corrupt catalog instead of serving a half-loaded list.
_ids = [p.id for p in PROVIDERS]
if len(set(_ids)) != len(_ids):
    dupes = sorted({i for i in _ids if _ids.count(i) > 1})
    raise ValueError(f"duplicate provider ids in {_CATALOG_RESOURCE}: {dupes}")
del _ids
