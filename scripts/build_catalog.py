"""Rebuild ``src/neo/providers/providers.json`` from the upstream model catalogs.

Sources (same schema, merged by provider id):
  - https://models.opencode.ai/api.json   (opencode's catalog)
  - https://models.dev/api.json           (MiMo-Code's catalog)

Then the 37 hand-tuned CURATED rows win on id conflicts, and a ``custom``
OpenAI-compatible entry is appended last (configured via ``neo.json``).

The provider literally named "opencode" is always excluded.

Usage:
  .venv/bin/python scripts/build_catalog.py
  .venv/bin/python scripts/build_catalog.py --opencode /tmp/api-opencode.json --models-dev /tmp/models-dev.json
  .venv/bin/python scripts/build_catalog.py --out /tmp/providers.json   # dry-run check

Local paths skip the network fetch for that source.
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DEFAULT = REPO / "src" / "neo" / "providers" / "providers.json"

OPENCODE_URL = "https://models.opencode.ai/api.json"
MODELS_DEV_URL = "https://models.dev/api.json"

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

CUSTOM = {
    "id": "custom",
    "title": "Custom (OpenAI-compatible)",
    "protocol": "openai",
    "base_url": "",
    "env_vars": ["CUSTOM_API_KEY"],
    "default_model": "",
    "extra_headers": {},
}

CURATED = [
    {'id': 'anthropic', 'title': 'Anthropic', 'protocol': 'anthropic', 'base_url': 'https://api.anthropic.com', 'env_vars': ['ANTHROPIC_API_KEY'], 'default_model': 'claude-sonnet-4-6', 'extra_headers': {}},
    {'id': 'openai', 'title': 'OpenAI', 'protocol': 'openai', 'base_url': 'https://api.openai.com/v1', 'env_vars': ['OPENAI_API_KEY'], 'default_model': 'gpt-5.4', 'extra_headers': {}},
    {'id': 'google', 'title': 'Google Gemini', 'protocol': 'google', 'base_url': 'https://generativelanguage.googleapis.com/v1beta', 'env_vars': ['GEMINI_API_KEY', 'GOOGLE_API_KEY'], 'default_model': 'gemini-2.5-flash', 'extra_headers': {}},
    {'id': 'xai', 'title': 'xAI', 'protocol': 'openai', 'base_url': 'https://api.x.ai/v1', 'env_vars': ['XAI_API_KEY'], 'default_model': 'grok-4', 'extra_headers': {}},
    {'id': 'deepseek', 'title': 'DeepSeek', 'protocol': 'openai', 'base_url': 'https://api.deepseek.com/v1', 'env_vars': ['DEEPSEEK_API_KEY'], 'default_model': 'deepseek-chat', 'extra_headers': {}},
    {'id': 'openrouter', 'title': 'OpenRouter', 'protocol': 'openai', 'base_url': 'https://openrouter.ai/api/v1', 'env_vars': ['OPENROUTER_API_KEY'], 'default_model': 'anthropic/claude-sonnet-4-6', 'extra_headers': {'HTTP-Referer': 'https://github.com/neo-agent', 'X-Title': 'neo'}},
    {'id': 'groq', 'title': 'Groq', 'protocol': 'openai', 'base_url': 'https://api.groq.com/openai/v1', 'env_vars': ['GROQ_API_KEY'], 'default_model': 'llama-3.3-70b-versatile', 'extra_headers': {}},
    {'id': 'mistral', 'title': 'Mistral', 'protocol': 'openai', 'base_url': 'https://api.mistral.ai/v1', 'env_vars': ['MISTRAL_API_KEY'], 'default_model': 'mistral-large-latest', 'extra_headers': {}},
    {'id': 'cohere', 'title': 'Cohere', 'protocol': 'openai', 'base_url': 'https://api.cohere.com/compatibility/v1', 'env_vars': ['COHERE_API_KEY'], 'default_model': 'command-r-plus', 'extra_headers': {}},
    {'id': 'cerebras', 'title': 'Cerebras', 'protocol': 'openai', 'base_url': 'https://api.cerebras.ai/v1', 'env_vars': ['CEREBRAS_API_KEY'], 'default_model': 'llama-3.3-70b', 'extra_headers': {}},
    {'id': 'together', 'title': 'Together AI', 'protocol': 'openai', 'base_url': 'https://api.together.xyz/v1', 'env_vars': ['TOGETHER_API_KEY'], 'default_model': 'meta-llama/Llama-3.3-70B-Instruct-Turbo', 'extra_headers': {}},
    {'id': 'fireworks', 'title': 'Fireworks AI', 'protocol': 'openai', 'base_url': 'https://api.fireworks.ai/inference/v1', 'env_vars': ['FIREWORKS_API_KEY'], 'default_model': 'accounts/fireworks/models/llama-v3p3-70b-instruct', 'extra_headers': {}},
    {'id': 'nebius', 'title': 'Nebius', 'protocol': 'openai', 'base_url': 'https://api.tokenfactory.nebius.com/v1', 'env_vars': ['NEBIUS_API_KEY'], 'default_model': 'meta-llama/Meta-Llama-3.1-70B-Instruct', 'extra_headers': {}},
    {'id': 'moonshotai', 'title': 'Moonshot AI', 'protocol': 'openai', 'base_url': 'https://api.moonshot.ai/v1', 'env_vars': ['MOONSHOT_API_KEY'], 'default_model': 'kimi-k2', 'extra_headers': {}},
    {'id': 'moonshotai-cn', 'title': 'Moonshot AI CN', 'protocol': 'openai', 'base_url': 'https://api.moonshot.cn/v1', 'env_vars': ['MOONSHOT_API_KEY'], 'default_model': 'kimi-k2', 'extra_headers': {}},
    {'id': 'kimi-code', 'title': 'Kimi Coding', 'protocol': 'openai', 'base_url': 'https://api.kimi.com/coding/v1', 'env_vars': ['KIMI_CODE_API_KEY'], 'default_model': 'kimi-for-coding', 'extra_headers': {}},
    {'id': 'alibaba', 'title': 'Alibaba Qwen', 'protocol': 'openai', 'base_url': 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1', 'env_vars': ['DASHSCOPE_API_KEY'], 'default_model': 'qwen-max', 'extra_headers': {}},
    {'id': 'minimax', 'title': 'MiniMax', 'protocol': 'anthropic', 'base_url': 'https://api.minimax.io/anthropic', 'env_vars': ['MINIMAX_API_KEY'], 'default_model': 'MiniMax-M2', 'extra_headers': {}},
    {'id': 'minimax-cn', 'title': 'MiniMax CN', 'protocol': 'anthropic', 'base_url': 'https://api.minimaxi.com/anthropic', 'env_vars': ['MINIMAX_API_KEY'], 'default_model': 'MiniMax-M2', 'extra_headers': {}},
    {'id': 'deepinfra', 'title': 'DeepInfra', 'protocol': 'openai', 'base_url': 'https://api.deepinfra.com/v1/openai', 'env_vars': ['DEEPINFRA_API_KEY'], 'default_model': 'meta-llama/Llama-3.3-70B-Instruct', 'extra_headers': {}},
    {'id': 'perplexity', 'title': 'Perplexity', 'protocol': 'openai', 'base_url': 'https://api.perplexity.ai', 'env_vars': ['PERPLEXITY_API_KEY'], 'default_model': 'sonar', 'extra_headers': {}},
    {'id': 'venice', 'title': 'Venice', 'protocol': 'openai', 'base_url': 'https://api.venice.ai/api/v1', 'env_vars': ['VENICE_API_KEY'], 'default_model': 'dolphin-3.0-mistral-24b', 'extra_headers': {}},
    {'id': 'zai', 'title': 'Zhipu Z.AI', 'protocol': 'openai', 'base_url': 'https://open.bigmodel.cn/api/paas/v4', 'env_vars': ['ZHIPUAI_API_KEY'], 'default_model': 'glm-4.5', 'extra_headers': {}},
    {'id': 'xiaomi', 'title': 'Xiaomi MiMo', 'protocol': 'openai', 'base_url': 'https://api.xiaomimimo.com/v1', 'env_vars': ['XIAOMI_API_KEY'], 'default_model': 'mimo-v2-flash', 'extra_headers': {}},
    {'id': 'huggingface', 'title': 'Hugging Face', 'protocol': 'openai', 'base_url': 'https://router.huggingface.co/v1', 'env_vars': ['HF_TOKEN'], 'default_model': 'moonshotai/Kimi-K2-Instruct', 'extra_headers': {}},
    {'id': 'github-copilot', 'title': 'GitHub Copilot', 'protocol': 'openai', 'base_url': 'https://api.githubcopilot.com', 'env_vars': ['GITHUB_TOKEN', 'COPILOT_GITHUB_TOKEN'], 'default_model': 'gpt-4.1', 'extra_headers': {}},
    {'id': 'nvidia', 'title': 'NVIDIA', 'protocol': 'openai', 'base_url': 'https://integrate.api.nvidia.com/v1', 'env_vars': ['NVIDIA_API_KEY'], 'default_model': 'meta/llama-3.1-70b-instruct', 'extra_headers': {}},
    {'id': 'sambanova', 'title': 'SambaNova', 'protocol': 'openai', 'base_url': 'https://api.sambanova.ai/v1', 'env_vars': ['SAMBANOVA_API_KEY'], 'default_model': 'Meta-Llama-3.1-70B-Instruct', 'extra_headers': {}},
    {'id': 'ai21', 'title': 'AI21', 'protocol': 'openai', 'base_url': 'https://api.ai21.com/studio/v1', 'env_vars': ['AI21_API_KEY'], 'default_model': 'jamba-large', 'extra_headers': {}},
    {'id': 'aihubmix', 'title': 'AIHubMix', 'protocol': 'openai', 'base_url': 'https://aihubmix.com/v1', 'env_vars': ['AIHUBMIX_API_KEY'], 'default_model': 'gpt-4o', 'extra_headers': {}},
    {'id': 'vercel', 'title': 'Vercel AI Gateway', 'protocol': 'anthropic', 'base_url': 'https://ai-gateway.vercel.sh/v1', 'env_vars': ['AI_GATEWAY_API_KEY'], 'default_model': 'anthropic/claude-sonnet-4-6', 'extra_headers': {}},
    {'id': 'meta', 'title': 'Meta', 'protocol': 'openai', 'base_url': 'https://api.meta.ai/v1', 'env_vars': ['META_API_KEY'], 'default_model': 'llama-3.3-70b', 'extra_headers': {}},
    {'id': 'ollama', 'title': 'Ollama', 'protocol': 'openai', 'base_url': 'http://127.0.0.1:11434/v1', 'env_vars': [], 'default_model': 'llama3.1', 'extra_headers': {}},
    {'id': 'lmstudio', 'title': 'LM Studio', 'protocol': 'openai', 'base_url': 'http://127.0.0.1:1234/v1', 'env_vars': [], 'default_model': 'local-model', 'extra_headers': {}},
    {'id': 'azure', 'title': 'Azure OpenAI', 'protocol': 'openai', 'base_url': '', 'env_vars': ['AZURE_API_KEY'], 'default_model': 'gpt-4.1', 'extra_headers': {}},
    {'id': 'amazon-bedrock', 'title': 'AWS Bedrock', 'protocol': 'openai', 'base_url': '', 'env_vars': ['AWS_ACCESS_KEY_ID'], 'default_model': 'anthropic.claude-sonnet-4-6', 'extra_headers': {}},
    {'id': 'google-vertex', 'title': 'Google Vertex', 'protocol': 'google', 'base_url': '', 'env_vars': ['GOOGLE_CLOUD_PROJECT'], 'default_model': 'gemini-2.5-flash', 'extra_headers': {}},
]


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


def load_source(ref: str) -> dict:
    if Path(ref).exists():
        return json.loads(Path(ref).read_text(encoding="utf-8"))
    req = urllib.request.Request(ref, headers={"User-Agent": "neo-agent/build_catalog"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def row_for(pid: str, e: dict) -> dict:
    npm = e.get("npm") or ""
    return {
        "id": pid,
        "title": e.get("name") or pid,
        "protocol": protocol_for(npm),
        "base_url": e.get("api") or KNOWN_BASE_URLS.get(pid, ""),
        "env_vars": list(e.get("env") or ()),
        "default_model": default_model(e.get("models") or {}),
        "models": sorted((e.get("models") or {}).keys()),
        "extra_headers": {},
    }


def build(opencode_ref: str, models_dev_ref: str) -> list[dict]:
    curated_ids = {c["id"] for c in CURATED}
    # Load sources once; curated rows absorb the source model lists too so
    # /model can show every model of a provider, not just its default.
    apis: list[dict] = []
    source_models: dict[str, set[str]] = {}
    for ref in (opencode_ref, models_dev_ref):
        api = load_source(ref)
        apis.append(api)
        for pid, e in api.items():
            if _excluded(pid):
                continue
            source_models.setdefault(pid, set()).update(
                (e.get("models") or {}).keys())

    rows = []
    for c in CURATED:
        models = set(c.get("models") or ())
        if c.get("default_model"):
            models.add(c["default_model"])
        models |= source_models.get(c["id"], set())
        rows.append({**c, "models": sorted(models)})

    generated: dict[str, dict] = {}
    for api in apis:
        for pid in sorted(api):
            if _excluded(pid) or pid in curated_ids or pid in generated:
                continue
            generated[pid] = row_for(pid, api[pid])
    rows.extend(generated[pid] for pid in sorted(generated))

    rows.append({**CUSTOM, "models": []})

    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate provider ids after merge"
    assert "opencode" not in ids
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--opencode", default=OPENCODE_URL)
    ap.add_argument("--models-dev", default=MODELS_DEV_URL)
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()

    rows = build(args.opencode, args.models_dev)
    out = Path(args.out)
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(rows)} providers)")


if __name__ == "__main__":
    main()
