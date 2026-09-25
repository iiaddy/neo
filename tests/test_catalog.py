"""The provider catalog must include every opencode-catalog provider except the
one literally named "opencode"."""
from neo.providers.catalog import PROVIDERS
from neo.providers.catalog_gen import GENERATED_ROWS

CURATED_EXTRAS = {"together", "fireworks", "kimi-code", "sambanova", "ollama"}


def test_generated_count():
    # 223 source providers minus the one literally named "opencode".
    assert len(GENERATED_ROWS) == 222


def test_opencode_literally_excluded():
    ids = [r[0] for r in GENERATED_ROWS]
    assert "opencode" not in ids
    assert all(p.id != "opencode" for p in PROVIDERS)


def test_opencode_go_included():
    # Only the literal "opencode" is excluded; similarly-named providers stay.
    ids = [r[0] for r in GENERATED_ROWS]
    assert "opencode-go" in ids
    assert any(p.id == "opencode-go" for p in PROVIDERS)


def test_merged_catalog_composition():
    gen_ids = {r[0] for r in GENERATED_ROWS}
    merged_ids = [p.id for p in PROVIDERS]
    assert len(merged_ids) == len(set(merged_ids)), "duplicate provider ids"
    extras = set(merged_ids) - gen_ids
    assert extras == CURATED_EXTRAS
    assert len(PROVIDERS) == 222 + len(CURATED_EXTRAS)


def test_provider_specs_valid():
    for p in PROVIDERS:
        assert p.id, "blank provider id"
        assert isinstance(p.base_url, str), p.id
        assert p.protocol in ("openai", "anthropic", "google"), p.id
        # env_vars may be empty for local keyless providers (e.g. ollama)
        assert isinstance(p.env_vars, tuple), p.id


# These providers need per-deployment configuration (custom endpoint, region,
# or gateway URL) and therefore carry no generic base URL. They must not be
# presented as working out of the box.
NEEDS_DEPLOYMENT_CONFIG = {
    "azure", "amazon-bedrock", "google-vertex", "azure-cognitive-services",
    "cloudflare-ai-gateway", "gitlab", "google-vertex-anthropic", "qvac",
    "salad-cloud", "sap-ai-core", "watsonx",
}


def test_blank_base_urls_are_known():
    blank = {p.id for p in PROVIDERS if not p.base_url}
    assert blank == NEEDS_DEPLOYMENT_CONFIG, blank ^ NEEDS_DEPLOYMENT_CONFIG
