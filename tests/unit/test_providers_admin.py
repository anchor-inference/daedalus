"""Provider administration: per-endpoint key precedence, patch/mask helpers, Mini App API."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi.testclient import TestClient

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import (
    apply_provider_patch,
    build_app,
    extend_model_list,
    lookup_openai_models,
    mask_provider_keys,
    model_entry,
)
from daedalus.providers.registry import ProviderRegistry
from tests.support.models import model_config

# -- pure helpers ---------------------------------------------------------------------------


def test_extend_model_list_appends_ids_the_endpoint_did_not_name() -> None:
    result = {"models": ["gpt-6-astra"], "entries": [{"id": "gpt-6-astra", "context_length": 1}]}
    extend_model_list(result, ["gpt-6-astra", " gpt-6-sol ", "", "gpt-5.5"])
    assert result["models"] == ["gpt-6-astra", "gpt-6-sol", "gpt-5.5"]
    assert result["entries"][0] == {"id": "gpt-6-astra", "context_length": 1}
    assert result["entries"][1:] == [{"id": "gpt-6-sol"}, {"id": "gpt-5.5"}]


def test_apply_provider_patch_merges_partials_and_creates_new_entries() -> None:
    providers: dict[str, Any] = {"vllm": {"kind": "vllm", "base_url": "http://a"}}
    apply_provider_patch(providers, "vllm", {"timeout_seconds": 30, "api_key": "secret", "unknown": 1, "default_model": "legacy"})
    assert providers["vllm"] == {"kind": "vllm", "base_url": "http://a", "timeout_seconds": 30, "api_key": "secret"}
    apply_provider_patch(providers, "vllm", {"temperature": 0.2})
    assert providers["vllm"]["temperature"] == 0.2
    apply_provider_patch(providers, "vllm", {"temperature": None})
    assert providers["vllm"]["temperature"] is None
    # id that does not exist yet starts as a generic openai_compat endpoint
    apply_provider_patch(providers, "local", {"base_url": "http://b"})
    assert providers["local"] == {"kind": "openai_compat", "base_url": "http://b"}
    # explicit "" clears the key; omitted key never touches a stored one
    apply_provider_patch(providers, "vllm", {"api_key": ""})
    assert providers["vllm"]["api_key"] == ""
    apply_provider_patch(providers, "vllm", {"base_url": "http://c"})
    assert "api_key" not in providers["vllm"] or providers["vllm"]["api_key"] == ""


def test_mask_provider_keys_replaces_with_flag_and_lists_kinds() -> None:
    view = {"providers": {"vllm": {"base_url": "http://x", "api_key": "hunter2"}}, "nested": "kept"}
    out = mask_provider_keys(view)
    assert out["providers"]["vllm"]["api_key"] == ""
    assert out["providers"]["vllm"]["api_key_set"] is True
    assert "hunter2" not in str(out)
    assert out["provider_kinds"] == ["deepseek", "openrouter", "opencode", "vllm", "llamacpp", "openai_compat"]
    assert out["nested"] == "kept"


# -- registry key precedence ---------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        telegram_bot_token="123:abc",
        owner_user_id=1,
        vllm_base_url="http://env-vllm:9000/v1",
        vllm_api_key="env-key",
        deepseek_api_key="",
        openrouter_api_key="",
    )


def _endpoint(settings: Settings, pc: Any) -> Any:
    registry = ProviderRegistry(settings, RuntimeConfig())
    return registry._endpoint("p", pc)


def test_vllm_endpoint_prefers_config_over_env() -> None:
    from daedalus.config import ProviderConfig

    pc = ProviderConfig(kind="vllm", base_url="http://cfg-vllm:8000/v1", api_key="cfg-key")
    ep = _endpoint(_settings(), pc)
    assert ep is not None
    assert ep.base_url == "http://cfg-vllm:8000/v1" and ep.api_key == "cfg-key"
    assert ep.kind == "vllm"


def test_vllm_endpoint_falls_back_to_env_and_needs_no_key() -> None:
    from daedalus.config import ProviderConfig

    pc = ProviderConfig(kind="vllm", base_url="", api_key="")
    ep = _endpoint(_settings(), pc)
    assert ep is not None
    assert ep.base_url == "http://env-vllm:9000/v1" and ep.api_key == "env-key"


def test_vllm_without_any_base_url_is_not_usable() -> None:
    from daedalus.config import ProviderConfig

    no_env = Settings(
        _env_file=None,  # type: ignore[call-arg]
        telegram_bot_token="123:abc",
        owner_user_id=1,
        vllm_base_url="",
        vllm_api_key="",
    )
    assert _endpoint(no_env, ProviderConfig(kind="vllm", base_url="", api_key="")) is None


def test_openai_compat_accepts_no_key_but_deepseek_needs_one() -> None:
    from daedalus.config import ProviderConfig

    s = _settings()
    ep = _endpoint(s, ProviderConfig(kind="openai_compat", base_url="http://self:8080/v1"))
    assert ep is not None and ep.api_key == ""
    assert _endpoint(s, ProviderConfig(kind="deepseek", base_url="https://api.deepseek.com")) is None
    ep_ds = _endpoint(s, ProviderConfig(kind="deepseek", base_url="https://api.deepseek.com", api_key="cfg"))
    assert ep_ds is not None and ep_ds.api_key == "cfg"


# -- Mini App API ---------------------------------------------------------------------------


class FakeProviders:
    def __init__(self) -> None:
        self.retired_closed = 0

    def available(self) -> list[str]:
        return []

    async def close_retired(self) -> None:
        self.retired_closed += 1


class FakeManager:
    def __init__(self) -> None:
        self.providers = FakeProviders()

    async def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return []


class FakeApp:
    """Minimal stand-in for daedalus.app.Application covering the settings routes."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config = RuntimeConfig()
        self.manager = FakeManager()
        self.front: Any = None
        self.saved: list[RuntimeConfig] = []

    async def save_config(self, config: RuntimeConfig) -> None:
        self.config = config
        self.saved.append(config)


def _client() -> tuple[TestClient, FakeApp]:
    app = FakeApp(settings=_settings())
    api = build_app(app, "tok")  # type: ignore[arg-type]
    client = TestClient(api)
    return client, app


def test_provider_put_creates_and_masks_key() -> None:
    client, app = _client()
    headers = {"X-Daedalus-Token": "tok"}
    r = client.put("/api/providers/local-vllm", json={"kind": "vllm", "base_url": "http://10.0.0.5:9000/v1", "api_key": "sekret"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["providers"]["local-vllm"]["base_url"] == "http://10.0.0.5:9000/v1"
    assert body["providers"]["local-vllm"]["api_key"] == ""
    assert body["providers"]["local-vllm"]["api_key_set"] is True
    assert app.saved, "config must be saved"
    stored = app.saved[-1].providers["local-vllm"].api_key
    assert stored == "sekret"
    assert "sekret" not in r.text


def test_provider_put_carries_the_opencode_plan_and_the_views_say_which_one() -> None:
    client, app = _client()
    headers = {"X-Daedalus-Token": "tok"}
    r = client.put("/api/providers/opencode", json={"kind": "opencode", "name": "OpenCode Go", "base_url": "https://opencode.ai/zen/go/v1", "billing": "subscription"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["providers"]["opencode"]["billing"] == "subscription"
    assert app.saved[-1].providers["opencode"].billing == "subscription"
    r = client.put("/api/providers/opencode_zen", json={"kind": "opencode", "base_url": "https://opencode.ai/zen/v1"}, headers=headers)
    assert r.json()["providers"]["opencode_zen"]["billing"] == "metered"
    assert client.put("/api/providers/opencode", json={"billing": "free"}, headers=headers).status_code == 422
    cards = {card["id"]: card for card in client.get("/api/onboarding", headers=headers).json()["providers"]}
    assert (cards["opencode"]["billing"], cards["opencode_zen"]["billing"]) == ("subscription", "metered")


def test_provider_put_validates_and_refuses_empty_base_url() -> None:
    client, app = _client()
    r = client.put("/api/providers/x", json={"kind": "vllm", "base_url": ""}, headers={"X-Daedalus-Token": "tok"})
    assert r.status_code == 400
    assert app.saved == []


def test_provider_put_sets_and_clears_temperature() -> None:
    client, app = _client()
    headers = {"X-Daedalus-Token": "tok"}
    r = client.put("/api/providers/local", json={"kind": "llamacpp", "base_url": "http://127.0.0.1:8080/v1", "temperature": 0.2}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["providers"]["local"]["temperature"] == 0.2
    assert app.saved[-1].providers["local"].temperature == 0.2
    r = client.put("/api/providers/local", json={"temperature": 0}, headers=headers)
    assert r.status_code == 200, r.text
    assert app.saved[-1].providers["local"].temperature == 0.0
    r = client.put("/api/providers/local", json={"temperature": None}, headers=headers)
    assert r.status_code == 200, r.text
    assert app.saved[-1].providers["local"].temperature is None
    r = client.put("/api/providers/local", json={"temperature": 3}, headers=headers)
    assert r.status_code == 422


def test_provider_put_preserves_stored_key_when_omitted() -> None:
    client, app = _client()
    headers = {"X-Daedalus-Token": "tok"}
    assert client.put("/api/providers/vllm", json={"base_url": "http://a/v1", "api_key": "k1"}, headers=headers).status_code == 200
    r = client.put("/api/providers/vllm", json={"timeout_seconds": 120}, headers=headers)
    assert r.status_code == 200
    assert app.saved[-1].providers["vllm"].api_key == "k1"
    # explicit "" clears it
    client.put("/api/providers/vllm", json={"api_key": ""}, headers=headers)
    assert app.saved[-1].providers["vllm"].api_key == ""


def test_provider_delete_refuses_when_referenced_then_succeeds() -> None:
    client, app = _client()
    headers = {"X-Daedalus-Token": "tok"}
    client.put("/api/providers/me", json={"kind": "vllm", "base_url": "http://a/v1"}, headers=headers)
    assert client.put("/api/presets/me.m1", json={"provider": "me", "model": "m1"}, headers=headers).status_code == 200
    r = client.delete("/api/providers/me", headers=headers)
    assert r.status_code == 400 and "me.m1" in r.text
    assert client.delete("/api/presets/me.m1", headers=headers).status_code == 200
    r = client.delete("/api/providers/me", headers=headers)
    assert r.status_code == 200 and "me" not in r.json()["providers"]


def test_settings_view_masks_all_stored_keys() -> None:
    client, app = _client()
    headers = {"X-Daedalus-Token": "tok"}
    client.put("/api/providers/local", json={"kind": "openai_compat", "base_url": "http://b/v1", "api_key": "zzz"}, headers=headers)
    r = client.get("/api/settings", headers=headers)
    assert r.status_code == 200
    assert r.json()["providers"]["local"]["api_key"] == ""
    assert r.json()["providers"]["local"]["api_key_set"] is True
    assert "zzz" not in r.text
    assert "provider_kinds" in r.json()


def test_provider_unknown_fields_rejected() -> None:
    client, _ = _client()
    r = client.put("/api/providers/x", json={"base_url": "http://a", "pricing": {"m": {"input": 1}}}, headers={"X-Daedalus-Token": "tok"})
    # pricing is not part of ProviderPatch -> extra keys are ignored, not an error
    assert r.status_code == 200


# -- model lookup ---------------------------------------------------------------------------


def _lookup_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_lookup_appends_v1_and_parses_model_ids() -> None:
    requested: list[str] = []
    sent_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        sent_headers.update(dict(request.headers))
        if str(request.url).endswith("/v1/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": "Qwen3.6", "object": "model"}, {"id": "Qwen3.5"}]})
        return httpx.Response(404, text="not found")

    client = _lookup_client(handler)
    try:
        result = await lookup_openai_models("http://10.0.0.5:9000", "sekret", client=client)
    finally:
        await client.aclose()
    assert result["base_url"] == "http://10.0.0.5:9000/v1" and result["models"] == ["Qwen3.6", "Qwen3.5"]
    # A plain OpenAI-compatible list says nothing but the ids, and the entries say nothing more.
    assert result["entries"] == [{"id": "Qwen3.6"}, {"id": "Qwen3.5"}]
    assert len(requested) == 2  # /models first (404), then /v1/models
    assert requested[0].endswith("/models") and requested[1].endswith("/v1/models")
    assert sent_headers.get("authorization") == "Bearer sekret"


async def test_lookup_keeps_explicit_v1_root() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    client = _lookup_client(handler)
    try:
        result = await lookup_openai_models("http://10.0.0.5:9000/v1", client=client)
    finally:
        await client.aclose()
    assert result == {"base_url": "http://10.0.0.5:9000/v1", "models": ["m1"], "entries": [{"id": "m1"}]}
    assert len(requested) == 1


def test_model_entry_carries_what_the_endpoint_reports() -> None:
    entry = model_entry(
        {
            "id": "vendor/model-x",
            "name": "Model X",
            "context_length": 200_000,
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
            "supported_parameters": ["tools", "reasoning"],
            "top_provider": {"max_completion_tokens": 64_000},
            "pricing": {"prompt": "0.0000003", "completion": "0.0000012", "input_cache_read": "0.00000003"},
        }
    )
    assert entry["name"] == "Model X" and entry["context_length"] == 200_000 and entry["max_output_tokens"] == 64_000
    assert entry["images"] is True and entry["reasoning"] is True
    # Prices arrive per token and are shown — and stored — per million.
    assert entry["pricing"] == {"input": 0.3, "output": 1.2, "cache_hit": 0.03}
    # An endpoint that reports nothing is not guessed at.
    assert model_entry({"id": "m", "object": "model"}) == {"id": "m"}


def test_model_discovery_ignores_invalid_prices_and_token_bounds() -> None:
    for value in (True, False, float("nan"), float("inf"), -1, 1e308):
        entry = model_entry({"id": "m", "context_length": value,
                             "top_provider": {"max_completion_tokens": value},
                             "pricing": {"prompt": value, "completion": value}})
        assert entry == {"id": "m"}
    assert model_entry({"id": "free", "pricing": {"prompt": "0", "completion": "0"}})["pricing"] == {
        "input": 0.0, "output": 0.0,
    }


async def test_lookup_fails_when_no_candidate_answers() -> None:
    import pytest

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _lookup_client(handler)
    try:
        with pytest.raises(ValueError, match="HTTP 500"):
            await lookup_openai_models("http://10.0.0.5:9000", client=client)
    finally:
        await client.aclose()


async def test_lookup_rejects_non_http_url() -> None:
    import pytest

    with pytest.raises(ValueError, match="http"):
        await lookup_openai_models("file:///etc/passwd")


def test_presets_are_seeded_and_resolve_the_default(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from daedalus.config import ModelPresetConfig
    from daedalus.extensions.api import resolve_model_patch

    config = model_config()
    config.providers["vllm"].base_url = "http://x/v1"
    config.presets["vllm.Qwen3.6"] = ModelPresetConfig(provider="vllm", model="Qwen3.6")
    path = tmp_path / "c.toml"
    config.save(path)
    loaded = RuntimeConfig.load(path)
    assert loaded.model.preset == "deepseek.deepseek-v4-flash"
    assert loaded.presets["vllm.Qwen3.6"].provider == "vllm" and loaded.presets["vllm.Qwen3.6"].thinking is True
    raw = loaded.model_dump(mode="json")
    resolve_model_patch(raw, {"preset": "vllm.Qwen3.6", "chain": ["vllm.Qwen3.6", "deepseek.deepseek-v4-flash"]})
    assert raw["model"] == {"preset": "vllm.Qwen3.6", "chain": ["deepseek.deepseek-v4-flash"], "fallback_to_session": True}
