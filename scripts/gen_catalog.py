"""Generate src/neo/providers/catalog_gen.py from the models.opencode.ai catalog.

Run:  .venv/bin/python scripts/gen_catalog.py
Input: /tmp/api-opencode.json (fetched from https://models.opencode.ai/api.json)
Only the provider literally named "opencode" is excluded.
"""
from __future__ import annotations

import json
from pathlib import Path

SRC = Path("/tmp/api-opencode.json")
OUT = Path(__file__).resolve().parent.parent / "src" / "neo" / "providers" / "catalog_gen.py"

# npm package -> neo protocol
_ANTHROPIC_NPM = {"@ai-sdk/anthropic", "@ai-sdk/google-vertex/anthropic"}
_GOOGLE_NPM = {"@ai-sdk/google", "@ai-sdk/google-vertex"}


def protocol_for(npm: str) -> str:
    if npm in _ANTHROPIC_NPM:
        return "anthropic"
    if npm in _GOOGLE_NPM:
        return "google"
    # opencode's rule: anything else speaks OpenAI-compatible
    return "openai"


# Known OpenAI-compatible base URLs for catalog entries with api=null.
KNOWN_BASE_URLS = {
    "aihubmix": "https://aihubmix.com/v1",
    "anthropic": "https://api.anthropic.com",
    "cerebras": "https://api.cerebras.ai/v1",
    "cohere": "https://api.cohere.com/compatibility/v1",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "openai": "https://api.openai.com/v1",
    "perplexity": "https://api.perplexity.ai",
    "togetherai": "https://api.together.xyz/v1",
    "v0": "https://api.v0.dev/v1",
    "venice": "https://api.venice.ai/api/v1",
    "xai": "https://api.x.ai/v1",
}


def default_model(models: dict) -> str:
    if not models:
        return ""
    tool_models = sorted(k for k, m in models.items()
                        if isinstance(m, dict) and m.get("tool_call"))
    if tool_models:
        return tool_models[0]
    return sorted(models.keys())[0]


def _excluded(pid: str) -> bool:
    # Only the provider literally named "opencode" is excluded.
    return pid == "opencode"


def main() -> None:
    api = json.loads(SRC.read_text(encoding="utf-8"))
    rows = []
    for pid in sorted(api):
        if _excluded(pid):
            continue
        e = api[pid]
        npm = e.get("npm") or ""
        base = e.get("api") or KNOWN_BASE_URLS.get(pid, "")
        env = tuple(e.get("env") or ())
        models = e.get("models") or {}
        rows.append((
            pid,
            e.get("name") or pid,
            protocol_for(npm),
            base,
            env,
            default_model(models),
            {},
        ))

    lines = [
        '"""Generated provider rows from the models.opencode.ai catalog.',
        "",
        "DO NOT EDIT BY HAND — regenerate with scripts/gen_catalog.py.",
        'The provider literally named "opencode" is excluded.',
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "# (id, title, protocol, base_url, env_vars, default_model, extra_headers)",
        "GENERATED_ROWS: tuple[tuple, ...] = (",
    ]
    for pid, title, proto, base, env, dmodel, headers in rows:
        lines.append("    (")
        lines.append(f"        {pid!r},")
        lines.append(f"        {title!r},")
        lines.append(f"        {proto!r},")
        lines.append(f"        {base!r},")
        lines.append(f"        {env!r},")
        lines.append(f"        {dmodel!r},")
        lines.append(f"        {headers!r},")
        lines.append("    ),")
    lines.append(")")
    lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} ({len(rows)} providers)")


if __name__ == "__main__":
    main()
