"""The provider catalog loads from providers.json: every opencode/models.dev
catalog provider except the one literally named "opencode", the curated
extras, and a user-configurable "custom" OpenAI-compatible entry."""
import json
from importlib import resources

import neo.providers
from neo.providers.catalog import PROVIDERS

CURATED_EXTRAS = {"together", "fireworks", "kimi-code", "sambanova", "ollama"}


def _json_rows():
    data = resources.files(neo.providers).joinpath("providers.json").read_text(
        encoding="utf-8"
    )
    return json.loads(data)


def test_json_is_source_of_truth():
    rows = _json_rows()
    assert isinstance(rows, list) and rows
    for r in rows:
        assert set(r) >= {"id", "title", "protocol", "base_url", "env_vars",
                          "default_model", "extra_headers"}, r.get("id")
    assert [r["id"] for r in rows] == [p.id for p in PROVIDERS]


def test_catalog_count():
    # 222 catalog providers + 5 curated extras + 1 custom entry.
    assert len(PROVIDERS) == 222 + len(CURATED_EXTRAS) + 1


def test_no_duplicate_ids():
    ids = [p.id for p in PROVIDERS]
    assert len(ids) == len(set(ids)), "duplicate provider ids"


def test_opencode_literally_excluded():
    assert all(p.id != "opencode" for p in PROVIDERS)


def test_opencode_go_included():
    # Only the literal "opencode" is excluded; similarly-named providers stay.
    assert any(p.id == "opencode-go" for p in PROVIDERS)


def test_curated_extras_present():
    ids = {p.id for p in PROVIDERS}
    assert CURATED_EXTRAS <= ids


def test_custom_openai_compatible_entry():
    custom = next(p for p in PROVIDERS if p.id == "custom")
    assert custom.title == "Custom (OpenAI-compatible)"
    assert custom.protocol == "openai"
    assert custom.base_url == "", "custom must be configured via neo.json"
    assert "CUSTOM_API_KEY" in custom.env_vars


def test_provider_specs_valid():
    for p in PROVIDERS:
        assert p.id, "blank provider id"
        assert isinstance(p.base_url, str), p.id
        assert p.protocol in ("openai", "anthropic", "google"), p.id
        # env_vars may be empty for local keyless providers (e.g. ollama)
        assert isinstance(p.env_vars, tuple), p.id


def test_xiaomi_gateway_providers_from_mimocode():
    # MiMo-Code's in-house gateway providers (models.dev) are included.
    ids = {p.id: p for p in PROVIDERS}
    for pid in ("xiaomi", "xiaomi-token-plan-cn", "xiaomi-token-plan-ams",
                "xiaomi-token-plan-sgp"):
        assert pid in ids, pid
        assert ids[pid].base_url.startswith("https://"), pid


# These providers need per-deployment configuration (custom endpoint, region,
# or gateway URL) and therefore carry no generic base URL. They must not be
# presented as working out of the box. "custom" joins them: the user supplies
# providers.custom.base_url in neo.json.
NEEDS_DEPLOYMENT_CONFIG = {
    "azure", "amazon-bedrock", "google-vertex", "azure-cognitive-services",
    "cloudflare-ai-gateway", "gitlab", "google-vertex-anthropic", "qvac",
    "salad-cloud", "sap-ai-core", "watsonx", "custom",
}


def test_blank_base_urls_are_known():
    blank = {p.id for p in PROVIDERS if not p.base_url}
    assert blank == NEEDS_DEPLOYMENT_CONFIG, blank ^ NEEDS_DEPLOYMENT_CONFIG
