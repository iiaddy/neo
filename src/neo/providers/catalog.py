"""Provider catalog for neo.

222 providers generated from the models.opencode.ai catalog plus 5 curated
extras (together, fireworks, kimi-code, sambanova, ollama). The only
exclusion is the provider literally named "opencode".
"""

from __future__ import annotations

from .base import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_GOOGLE,
    PROTOCOL_OPENAI,
    ProviderSpec,
)

# (id, title, protocol, base_url, env_vars, default_model, extra_headers)
_ROWS: tuple[tuple, ...] = (
    (
        "anthropic",
        "Anthropic",
        PROTOCOL_ANTHROPIC,
        "https://api.anthropic.com",
        ("ANTHROPIC_API_KEY",),
        "claude-sonnet-4-6",
        {},
    ),
    (
        "openai",
        "OpenAI",
        PROTOCOL_OPENAI,
        "https://api.openai.com/v1",
        ("OPENAI_API_KEY",),
        "gpt-5.4",
        {},
    ),
    (
        "google",
        "Google Gemini",
        PROTOCOL_GOOGLE,
        "https://generativelanguage.googleapis.com/v1beta",
        ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "gemini-2.5-flash",
        {},
    ),
    (
        "xai",
        "xAI",
        PROTOCOL_OPENAI,
        "https://api.x.ai/v1",
        ("XAI_API_KEY",),
        "grok-4",
        {},
    ),
    (
        "deepseek",
        "DeepSeek",
        PROTOCOL_OPENAI,
        "https://api.deepseek.com/v1",
        ("DEEPSEEK_API_KEY",),
        "deepseek-chat",
        {},
    ),
    (
        "openrouter",
        "OpenRouter",
        PROTOCOL_OPENAI,
        "https://openrouter.ai/api/v1",
        ("OPENROUTER_API_KEY",),
        "anthropic/claude-sonnet-4-6",
        {
            "HTTP-Referer": "https://github.com/neo-agent",
            "X-Title": "neo",
        },
    ),
    (
        "groq",
        "Groq",
        PROTOCOL_OPENAI,
        "https://api.groq.com/openai/v1",
        ("GROQ_API_KEY",),
        "llama-3.3-70b-versatile",
        {},
    ),
    (
        "mistral",
        "Mistral",
        PROTOCOL_OPENAI,
        "https://api.mistral.ai/v1",
        ("MISTRAL_API_KEY",),
        "mistral-large-latest",
        {},
    ),
    (
        "cohere",
        "Cohere",
        PROTOCOL_OPENAI,
        "https://api.cohere.com/compatibility/v1",
        ("COHERE_API_KEY",),
        "command-r-plus",
        {},
    ),
    (
        "cerebras",
        "Cerebras",
        PROTOCOL_OPENAI,
        "https://api.cerebras.ai/v1",
        ("CEREBRAS_API_KEY",),
        "llama-3.3-70b",
        {},
    ),
    (
        "together",
        "Together AI",
        PROTOCOL_OPENAI,
        "https://api.together.xyz/v1",
        ("TOGETHER_API_KEY",),
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        {},
    ),
    (
        "fireworks",
        "Fireworks AI",
        PROTOCOL_OPENAI,
        "https://api.fireworks.ai/inference/v1",
        ("FIREWORKS_API_KEY",),
        "accounts/fireworks/models/llama-v3p3-70b-instruct",
        {},
    ),
    (
        "nebius",
        "Nebius",
        PROTOCOL_OPENAI,
        "https://api.tokenfactory.nebius.com/v1",
        ("NEBIUS_API_KEY",),
        "meta-llama/Meta-Llama-3.1-70B-Instruct",
        {},
    ),
    (
        "moonshotai",
        "Moonshot AI",
        PROTOCOL_OPENAI,
        "https://api.moonshot.ai/v1",
        ("MOONSHOT_API_KEY",),
        "kimi-k2",
        {},
    ),
    (
        "moonshotai-cn",
        "Moonshot AI CN",
        PROTOCOL_OPENAI,
        "https://api.moonshot.cn/v1",
        ("MOONSHOT_API_KEY",),
        "kimi-k2",
        {},
    ),
    (
        "kimi-code",
        "Kimi Coding",
        PROTOCOL_OPENAI,
        "https://api.kimi.com/coding/v1",
        ("KIMI_CODE_API_KEY",),
        "kimi-for-coding",
        {},
    ),
    (
        "alibaba",
        "Alibaba Qwen",
        PROTOCOL_OPENAI,
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        ("DASHSCOPE_API_KEY",),
        "qwen-max",
        {},
    ),
    (
        "minimax",
        "MiniMax",
        PROTOCOL_ANTHROPIC,
        "https://api.minimax.io/anthropic",
        ("MINIMAX_API_KEY",),
        "MiniMax-M2",
        {},
    ),
    (
        "minimax-cn",
        "MiniMax CN",
        PROTOCOL_ANTHROPIC,
        "https://api.minimaxi.com/anthropic",
        ("MINIMAX_API_KEY",),
        "MiniMax-M2",
        {},
    ),
    (
        "deepinfra",
        "DeepInfra",
        PROTOCOL_OPENAI,
        "https://api.deepinfra.com/v1/openai",
        ("DEEPINFRA_API_KEY",),
        "meta-llama/Llama-3.3-70B-Instruct",
        {},
    ),
    (
        "perplexity",
        "Perplexity",
        PROTOCOL_OPENAI,
        "https://api.perplexity.ai",
        ("PERPLEXITY_API_KEY",),
        "sonar",
        {},
    ),
    (
        "venice",
        "Venice",
        PROTOCOL_OPENAI,
        "https://api.venice.ai/api/v1",
        ("VENICE_API_KEY",),
        "dolphin-3.0-mistral-24b",
        {},
    ),
    (
        "zai",
        "Zhipu Z.AI",
        PROTOCOL_OPENAI,
        "https://open.bigmodel.cn/api/paas/v4",
        ("ZHIPUAI_API_KEY",),
        "glm-4.5",
        {},
    ),
    (
        "xiaomi",
        "Xiaomi MiMo",
        PROTOCOL_OPENAI,
        "https://api.xiaomimimo.com/v1",
        ("XIAOMI_API_KEY",),
        "mimo-v2-flash",
        {},
    ),
    (
        "huggingface",
        "Hugging Face",
        PROTOCOL_OPENAI,
        "https://router.huggingface.co/v1",
        ("HF_TOKEN",),
        "moonshotai/Kimi-K2-Instruct",
        {},
    ),
    (
        "github-copilot",
        "GitHub Copilot",
        PROTOCOL_OPENAI,
        "https://api.githubcopilot.com",
        ("GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"),
        "gpt-4.1",
        {},
    ),
    (
        "nvidia",
        "NVIDIA",
        PROTOCOL_OPENAI,
        "https://integrate.api.nvidia.com/v1",
        ("NVIDIA_API_KEY",),
        "meta/llama-3.1-70b-instruct",
        {},
    ),
    (
        "sambanova",
        "SambaNova",
        PROTOCOL_OPENAI,
        "https://api.sambanova.ai/v1",
        ("SAMBANOVA_API_KEY",),
        "Meta-Llama-3.1-70B-Instruct",
        {},
    ),
    (
        "ai21",
        "AI21",
        PROTOCOL_OPENAI,
        "https://api.ai21.com/studio/v1",
        ("AI21_API_KEY",),
        "jamba-large",
        {},
    ),
    (
        "aihubmix",
        "AIHubMix",
        PROTOCOL_OPENAI,
        "https://aihubmix.com/v1",
        ("AIHUBMIX_API_KEY",),
        "gpt-4o",
        {},
    ),
    (
        "vercel",
        "Vercel AI Gateway",
        PROTOCOL_ANTHROPIC,
        "https://ai-gateway.vercel.sh/v1",
        ("AI_GATEWAY_API_KEY",),
        "anthropic/claude-sonnet-4-6",
        {},
    ),
    (
        "meta",
        "Meta",
        PROTOCOL_OPENAI,
        "https://api.meta.ai/v1",
        ("META_API_KEY",),
        "llama-3.3-70b",
        {},
    ),
    (
        "ollama",
        "Ollama",
        PROTOCOL_OPENAI,
        "http://127.0.0.1:11434/v1",
        (),
        "llama3.1",
        {},
    ),
    (
        "lmstudio",
        "LM Studio",
        PROTOCOL_OPENAI,
        "http://127.0.0.1:1234/v1",
        (),
        "local-model",
        {},
    ),
    (
        "azure",
        "Azure OpenAI",
        PROTOCOL_OPENAI,
        "",
        ("AZURE_API_KEY",),
        "gpt-4.1",
        {},
    ),
    (
        "amazon-bedrock",
        "AWS Bedrock",
        PROTOCOL_OPENAI,
        "",
        ("AWS_ACCESS_KEY_ID",),
        "anthropic.claude-sonnet-4-6",
        {},
    ),
    (
        "google-vertex",
        "Google Vertex",
        PROTOCOL_GOOGLE,
        "",
        ("GOOGLE_CLOUD_PROJECT",),
        "gemini-2.5-flash",
        {},
    ),
)

PROVIDERS: list[ProviderSpec] = [
    ProviderSpec(
        id=pid,
        title=title,
        protocol=protocol,
        base_url=base_url,
        env_vars=tuple(env_vars),
        default_model=default_model,
        extra_headers=dict(extra),
    )
    for pid, title, protocol, base_url, env_vars, default_model, extra in _ROWS
]

# Merge the generated full-catalog rows (222 providers from models.opencode.ai;
# only the provider literally named "opencode" is excluded). Five curated
# extras (together, fireworks, kimi-code, sambanova, ollama) are not in the
# generated set. Curated _ROWS win on id conflicts.
try:
    from .catalog_gen import GENERATED_ROWS

    _curated_ids = {p.id for p in PROVIDERS}
    for pid, title, protocol, base_url, env_vars, default_model, extra in GENERATED_ROWS:
        if pid in _curated_ids:
            continue
        PROVIDERS.append(
            ProviderSpec(
                id=pid,
                title=title,
                protocol=protocol,
                base_url=base_url,
                env_vars=tuple(env_vars),
                default_model=default_model,
                extra_headers=dict(extra),
            )
        )
    del _curated_ids
except ImportError:  # generated catalog not present; curated rows still work
    pass
