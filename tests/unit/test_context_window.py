"""A preset's context window is found rather than asked for: provider listing, models.dev, the bundled
table, a refusal — in that order — and written into the configuration unless the operator typed it.

Every provider below is a fake answering in the shape the real one uses; nothing leaves the process.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import tomli_w
from fastapi.testclient import TestClient
from protocore.contracts.llm import LLMContextWindowExceeded, LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import ModelPresetConfig, ProviderConfig, RuntimeConfig, Settings
from daedalus.extensions.api import build_app, model_entry, with_known_windows
from daedalus.extensions.context_windows import ContextWindows
from daedalus.host.config_validation import ConfigConflict, config_revision
from daedalus.providers import context_window as cw
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.providers.registry import ProviderRegistry

# -- what each provider's listing says -------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "tokens"),
    [
        ({"id": "a/b", "context_length": 200_000, "top_provider": {"context_length": 100_000}}, 200_000),  # OpenRouter
        ({"id": "a/b", "top_provider": {"context_length": 64_000}}, 64_000),  # OpenRouter, top provider only
        ({"id": "Qwen/Qwen3-32B", "object": "model", "max_model_len": 40_960}, 40_960),  # vLLM
        ({"id": "llama-3.3-70b-versatile", "context_window": 131_072}, 131_072),  # Groq, the Codex login
        ({"id": "mistral-large-latest", "max_context_length": 131_072}, 131_072),  # Mistral
        ({"id": "qwen3-8b", "max_context_length": 32_768, "loaded_context_length": 8_192}, 8_192),  # LM Studio: what is loaded
        ({"id": "claude-opus-5-5", "type": "model", "max_input_tokens": 1_000_000, "max_tokens": 128_000}, 1_000_000),  # Anthropic
        ({"name": "models/gemini-2.5-pro", "inputTokenLimit": 1_048_576, "outputTokenLimit": 65_536}, 1_048_576),  # Google
        ({"id": "x", "limit": {"context": 262_144}}, 262_144),  # models.dev
        ({"id": "x", "context_length": "32768"}, 32_768),
        ({"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"}, None),  # ids only
        ({"id": "x", "context_length": True}, None),
        ({"id": "x", "context_length": 12}, None),  # not a plausible window
    ],
)
def test_every_vendor_field_is_read(entry: dict[str, Any], tokens: int | None) -> None:
    assert cw.window_from_entry(entry) == tokens


def test_listings_in_every_shape_and_ids_with_prefixes_are_found() -> None:
    assert [cw.entry_id(r) for r in cw.listing_rows([{"id": "a"}, {"id": "b"}])] == ["a", "b"]  # Together: a bare list
    assert cw.entry_id(cw.listing_rows({"models": [{"name": "models/gemini-2.5-pro"}]})[0]) == "gemini-2.5-pro"
    assert cw.entry_id(cw.listing_rows({"models": [{"slug": "gpt-6-sol"}]})[0]) == "gpt-6-sol"
    rows = [{"id": "anthropic/claude-opus-5"}, {"id": "llama3.1:8b"}]
    assert cw.find_entry(rows, "claude-opus-5") == rows[0]
    assert cw.find_entry(rows, "LLAMA3.1:8B") == rows[1]
    assert cw.find_entry(rows, "missing") is None


# -- the bundled table and models.dev ------------------------------------------------------------


def test_the_bundled_table_matches_the_most_specific_family() -> None:
    assert cw.table_window("claude-opus-4-6") == 1_000_000
    assert cw.table_window("anthropic/claude-opus-4-1") == 200_000  # the general Opus 4 line
    assert cw.table_window("claude-haiku-4-5") == 200_000
    assert cw.table_window("llama3.1:8b") == 131_072  # the Ollama spelling, tag dropped
    assert cw.table_window("Qwen/Qwen3-Coder-30B-A3B-Instruct") == 262_144
    assert cw.table_window("qwen3-8b") == 32_768
    assert cw.table_window("gpt-5") == 400_000 and cw.table_window("gpt-5-codex") == 400_000
    assert cw.table_window("kimi-k2-thinking") == 262_144 and cw.table_window("kimi-k2-instruct") == 131_072
    assert cw.table_window("some-model-nobody-knows") is None
    assert cw.table_window("") is None


def test_the_table_file_is_well_formed() -> None:
    import tomllib

    with cw.TABLE_PATH.open("rb") as fh:
        windows = tomllib.load(fh)["windows"]
    assert windows and all(isinstance(v, int) and 8_000 <= v <= 4_000_000 for v in windows.values())
    assert all(k == k.lower() and "/" not in k for k in windows)


def test_models_dev_windows_take_the_common_value_and_a_tie_the_smaller() -> None:
    catalog = {
        "anthropic": {"models": {"claude-opus-5": {"limit": {"context": 1_000_000}}}},
        "openrouter": {"models": {"anthropic/claude-opus-5": {"limit": {"context": 1_000_000}}, "z/glm-4.6": {"limit": {"context": 200_000}}}},
        "cheap-host": {"models": {"claude-opus-5": {"limit": {"context": 200_000}}, "glm-4.6": {"limit": {"context": 128_000}}}},
        "broken": {"models": {"m": {"limit": {}}}},
        "not-a-provider": 3,
    }
    windows = cw.windows_from_catalog(catalog)
    assert windows["claude-opus-5"] == 1_000_000
    assert windows["anthropic/claude-opus-5"] == 1_000_000
    assert windows["glm-4.6"] == 128_000  # one host each: the smaller
    assert "m" not in windows
    assert cw.catalog_window(windows, "Anthropic/Claude-Opus-5") == 1_000_000


# -- the order -----------------------------------------------------------------------------------


def test_the_provider_wins_then_the_catalogue_then_the_table_and_a_smaller_refusal_over_all() -> None:
    assert cw.decide(provider=cw.Found(64_000), catalog=128_000, table=200_000, refused=None) == cw.Resolved(64_000, "provider")
    assert cw.decide(provider=None, catalog=128_000, table=200_000, refused=None) == cw.Resolved(128_000, "catalog", "models.dev")
    assert cw.decide(provider=None, catalog=None, table=200_000, refused=None) == cw.Resolved(200_000, "table")
    assert cw.decide(provider=cw.Found(200_000), catalog=None, table=None, refused=131_072) == cw.Resolved(131_072, "error")
    # A refusal above the known window says nothing new: the window was never reached.
    assert cw.decide(provider=cw.Found(64_000), catalog=None, table=None, refused=131_072) == cw.Resolved(64_000, "provider")
    assert cw.decide(provider=None, catalog=None, table=None, refused=32_768) == cw.Resolved(32_768, "error")
    # Nothing known: no decision, so the preset keeps its number.
    assert cw.decide(provider=None, catalog=None, table=None, refused=None) is None


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("This model's maximum context length is 131072 tokens. However, your messages resulted in 140000 tokens.", 131_072),
        ('{"error":{"message":"prompt is too long: 210582 tokens > 200000 maximum"}}', 200_000),
        ("the request exceeds the available context size (8192 tokens), try increasing it", 8_192),
        ("The input token count (1200000) exceeds the maximum number of tokens allowed (1048576).", 1_048_576),
        ("Trying to keep the first 5000 tokens when context the overflows. However, the model is loaded with context length of only 4096 tokens", 4_096),
        ("rate limited, try later", None),
    ],
)
def test_a_refusal_states_the_window_in_each_providers_words(text: str, tokens: int | None) -> None:
    assert cw.stated_window(text) == tokens


# -- asking the provider -------------------------------------------------------------------------


def _client(routes: dict[tuple[str, str], Any], seen: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        body = routes.get((request.method, request.url.path))
        if body is None:
            return httpx.Response(404, json={"error": "not found"})
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_vllm_says_its_served_length_in_the_listing_and_the_key_goes_along() -> None:
    seen: list[httpx.Request] = []
    client = _client({("GET", "/v1/models"): {"object": "list", "data": [{"id": "m", "max_model_len": 65_536}]}}, seen)
    found = await cw.provider_window("http://gpu:8000/v1", "m", kind="vllm", api_key="k", client=client)
    assert found == cw.Found(65_536)
    assert seen[0].headers["authorization"] == "Bearer k"


@pytest.mark.asyncio
async def test_anthropic_answers_for_one_model_when_the_list_page_does_not_hold_it() -> None:
    client = _client({
        ("GET", "/anthropic/models"): {"data": [{"id": "claude-haiku-4-5", "max_input_tokens": 200_000}], "has_more": True},
        ("GET", "/anthropic/models/claude-opus-5-5"): {"id": "claude-opus-5-5", "type": "model", "max_input_tokens": 1_000_000},
    })
    assert await cw.provider_window("http://keyproxy:3200/anthropic", "claude-opus-5-5", client=client) == cw.Found(1_000_000)


@pytest.mark.asyncio
async def test_a_base_typed_without_v1_still_finds_the_listing() -> None:
    client = _client({("GET", "/v1/models"): {"data": [{"id": "m", "context_length": 32_000}]}})
    assert await cw.provider_window("http://box:9000", "m", client=client) == cw.Found(32_000)


@pytest.mark.asyncio
async def test_llama_cpp_reports_the_slot_context_at_props() -> None:
    client = _client({
        ("GET", "/props"): {"default_generation_settings": {"params": {"n_ctx": 16_384}}, "model_alias": "local"},
        ("GET", "/v1/models"): {"data": [{"id": "local", "meta": {"n_ctx_train": 131_072}}]},
    })
    assert await cw.provider_window("http://box:8080/v1", "local", kind="llamacpp", client=client) == cw.Found(16_384, "llama.cpp /props")


@pytest.mark.asyncio
async def test_lm_studio_reports_the_loaded_length() -> None:
    client = _client({
        ("GET", "/v1/models"): {"data": [{"id": "qwen3-8b", "object": "model"}]},
        ("GET", "/api/v0/models/qwen3-8b"): {"id": "qwen3-8b", "state": "loaded", "max_context_length": 32_768, "loaded_context_length": 8_192},
    })
    assert await cw.provider_window("http://box:1234/v1", "qwen3-8b", client=client) == cw.Found(8_192, "LM Studio")


@pytest.mark.asyncio
async def test_ollama_reports_num_ctx_and_otherwise_the_loaded_length_never_the_trained_one() -> None:
    listing = {"data": [{"id": "llama3.1:8b", "object": "model", "owned_by": "library"}]}
    with_param = _client({
        ("GET", "/v1/models"): listing,
        ("POST", "/api/show"): {"parameters": "stop \"<|eot_id|>\"\nnum_ctx 16384", "model_info": {"llama.context_length": 131_072}},
    })
    assert await cw.provider_window("http://box:11434/v1", "llama3.1:8b", client=with_param) == cw.Found(16_384, "Ollama num_ctx")
    loaded = _client({
        ("GET", "/v1/models"): listing,
        ("POST", "/api/show"): {"parameters": "", "model_info": {"llama.context_length": 131_072}},
        ("GET", "/api/ps"): {"models": [{"name": "llama3.1:8b", "model": "llama3.1:8b", "context_length": 4_096}]},
    })
    assert await cw.provider_window("http://box:11434/v1", "llama3.1:8b", client=loaded) == cw.Found(4_096, "Ollama, as loaded")
    trained_only = _client({("GET", "/v1/models"): listing, ("POST", "/api/show"): {"model_info": {"llama.context_length": 131_072}}})
    assert await cw.provider_window("http://box:11434/v1", "llama3.1:8b", client=trained_only) is None


@pytest.mark.asyncio
async def test_an_endpoint_that_names_ids_only_or_does_not_answer_knows_nothing() -> None:
    ids_only = _client({("GET", "/models"): {"data": [{"id": "deepseek-chat"}]}})
    assert await cw.provider_window("https://api.deepseek.com", "deepseek-chat", kind="deepseek", client=ids_only) is None

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    dead = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    assert await cw.provider_window("http://box:1/v1", "m", client=dead) is None
    assert await cw.provider_window("not a url", "m") is None


# -- a refusal is heard ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_context_refusal_reports_the_stated_window_to_the_listener() -> None:
    heard: list[tuple[str, str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "This model's maximum context length is 32768 tokens. However, you requested 40000 tokens (39000 in the messages, 1000 in the completion).", "type": "invalid_request_error"}})

    provider = OpenAICompatibleProvider(
        ProviderEndpoint(id="box", kind="openai_compat", base_url="http://box/v1"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        context_refusal=lambda pid, model, window: heard.append((pid, model, window)),
    )
    request = LLMRequest(model="m", messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])], max_tokens=1000)
    with pytest.raises(LLMContextWindowExceeded):
        await provider.complete_text(request)
    assert heard == [("box", "m", 32_768)]


def test_the_registry_passes_refusals_to_whoever_listens_now() -> None:
    config = RuntimeConfig(providers={"box": ProviderConfig(base_url="http://box/v1")})
    registry = ProviderRegistry(Settings(_env_file=None), config)  # type: ignore[call-arg]
    heard: list[tuple[str, str, int]] = []
    registry.get("box")._context_refusal("box", "m", 1)  # nobody listening yet: nothing happens
    registry.on_context_refusal = lambda *args: heard.append(args)
    registry.get("box")._context_refusal("box", "m", 8_192)
    assert heard == [("box", "m", 8_192)]


# -- the configuration ---------------------------------------------------------------------------


def test_existing_presets_are_told_apart_once_default_table_and_typed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    with path.open("wb") as fh:
        tomli_w.dump({
            "seeded": ["claude-subscription", "openai-anthropic-keys", "more-provider-endpoints", "opencode-go-subscription", "known-image-models"],
            "providers": {"p": {"base_url": "http://box/v1"}},
            "model": {"preset": "p.a"},
            "presets": {
                "p.a": {"provider": "p", "model": "unknown-model", "context_window": 128_000},
                "p.b": {"provider": "p", "model": "claude-opus-4-6", "context_window": 1_000_000},
                "p.c": {"provider": "p", "model": "gpt-6-sol", "context_window": 400_000},
                "p.d": {"provider": "p", "model": "x"},
            },
        }, fh)
    config = RuntimeConfig.load(path)
    assert {pid: p.context_window_source for pid, p in config.presets.items()} == {"p.a": "default", "p.b": "table", "p.c": "manual", "p.d": "default"}
    assert "context-window-source" in config.seeded
    # Once: a preset the resolver later marks is left as it is on the next start.
    config.presets["p.a"] = config.presets["p.a"].model_copy(update={"context_window": 64_000, "context_window_source": "provider"})
    config.save(path)
    assert RuntimeConfig.load(path).presets["p.a"].context_window_source == "provider"


# -- the service -----------------------------------------------------------------------------------


class _Db:
    def __init__(self) -> None:
        self.kv: dict[str, Any] = {}

    async def kv_get(self, key: str, default: Any = None) -> Any:
        return self.kv.get(key, default)

    async def kv_set(self, key: str, value: Any) -> None:
        self.kv[key] = json.loads(json.dumps(value))


class _Endpoint:
    def __init__(self, base_url: str, kind: str = "openai_compat") -> None:
        self.base_url, self.kind, self.api_key = base_url, kind, ""


class _Adapter:
    def __init__(self, endpoint: _Endpoint) -> None:
        self.endpoint = endpoint


class _Providers:
    def __init__(self, endpoints: dict[str, _Endpoint]) -> None:
        self.endpoints = endpoints
        self.catalog_windows: dict[str, int] = {}
        self.on_context_refusal: Any = None

    def get(self, provider_id: str) -> _Adapter:
        return _Adapter(self.endpoints[provider_id])

    def available(self) -> list[str]:
        return list(self.endpoints)

    async def close_retired(self) -> None:
        pass


class _Manager:
    def __init__(self, providers: _Providers) -> None:
        self.providers = providers

    async def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return []


class _App:
    def __init__(self, config: RuntimeConfig, providers: _Providers) -> None:
        self.config = config
        self.db = _Db()
        self.manager = _Manager(providers)
        self.settings = Settings(_env_file=None, telegram_bot_token="1:a", owner_user_id=1)  # type: ignore[call-arg]
        self.front: Any = None
        self.extensions: dict[str, Any] = {}
        self.saves = 0
        self.conflicts = 0

    async def save_config(self, config: RuntimeConfig, *, expected_revision: str | None = None) -> None:
        if self.conflicts:
            # Someone else saved in between: their change lands, and the caller is told.
            self.conflicts -= 1
            self.config = self.config.model_copy(update={"seeded": [*self.config.seeded, "someone-else"]})
            raise ConfigConflict(config_revision(self.config))
        if expected_revision is not None and expected_revision != config_revision(self.config):
            raise ConfigConflict(config_revision(self.config))
        self.config = config
        self.saves += 1


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        providers={"box": ProviderConfig(base_url="http://box/v1"), "hosted": ProviderConfig(base_url="https://hosted/v1")},
        presets={
            "box.m": ModelPresetConfig(provider="box", model="m"),
            "box.typed": ModelPresetConfig(provider="box", model="m", context_window=50_000, context_window_source="manual"),
            "hosted.claude": ModelPresetConfig(provider="hosted", model="claude-haiku-4-5"),
            "hosted.unknown": ModelPresetConfig(provider="hosted", model="mystery", context_window=32_000),
        },
    )


def _fake_listing(monkeypatch: pytest.MonkeyPatch, answers: dict[tuple[str, str], int | None]) -> list[tuple[str, str]]:
    asked: list[tuple[str, str]] = []

    async def fake(base_url: str, model: str, **_: Any) -> cw.Found | None:
        asked.append((base_url, model))
        tokens = answers.get((base_url, model))
        return cw.Found(tokens) if tokens else None

    monkeypatch.setattr("daedalus.extensions.context_windows.provider_window", fake)
    return asked


@pytest.mark.asyncio
async def test_a_sweep_writes_found_windows_and_leaves_typed_ones_and_unknown_ones_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _fake_listing(monkeypatch, {("http://box/v1", "m"): 65_536})
    app = _App(_config(), _Providers({"box": _Endpoint("http://box/v1"), "hosted": _Endpoint("https://hosted/v1")}))
    service = ContextWindows(app)  # type: ignore[arg-type]
    assert await service.sweep() == 2
    presets = app.config.presets
    assert (presets["box.m"].context_window, presets["box.m"].context_window_source) == (65_536, "provider")
    assert (presets["box.typed"].context_window, presets["box.typed"].context_window_source) == (50_000, "manual")
    assert (presets["hosted.claude"].context_window, presets["hosted.claude"].context_window_source) == (200_000, "table")
    assert (presets["hosted.unknown"].context_window, presets["hosted.unknown"].context_window_source) == (32_000, "default")
    # The answers are kept: a second sweep within the day asks nobody and changes nothing.
    count = len(asked)
    assert await service.sweep() == 0
    assert len(asked) == count
    assert app.db.kv["context_windows"]["listed"]["box"]["m"]["tokens"] == 65_536
    # Refresh asks again.
    await service.sweep(refresh=True)
    assert len(asked) > count


@pytest.mark.asyncio
async def test_the_catalogue_stands_between_the_listing_and_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_listing(monkeypatch, {})
    providers = _Providers({"box": _Endpoint("http://box/v1"), "hosted": _Endpoint("https://hosted/v1")})
    providers.catalog_windows = {"claude-haiku-4-5": 190_000}
    app = _App(_config(), providers)
    service = ContextWindows(app)  # type: ignore[arg-type]
    view = await service.resolve_preset("hosted.claude")
    assert view["window"] == 190_000 and view["source"] == "catalog"
    assert view["sources"] == {"provider": None, "catalog": 190_000, "table": 200_000, "error": None}


@pytest.mark.asyncio
async def test_a_refusal_shrinks_automatic_presets_is_remembered_and_spares_typed_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_listing(monkeypatch, {("http://box/v1", "m"): 65_536})
    app = _App(_config(), _Providers({"box": _Endpoint("http://box/v1"), "hosted": _Endpoint("https://hosted/v1")}))
    service = ContextWindows(app)  # type: ignore[arg-type]
    await service.sweep()
    service.record_refusal("box", "m", 32_768)
    await next(iter(service._background))
    presets = app.config.presets
    assert (presets["box.m"].context_window, presets["box.m"].context_window_source) == (32_768, "error")
    assert presets["box.typed"].context_window == 50_000
    # Remembered across a restart, and a later sweep keeps the smaller limit.
    again = ContextWindows(app)  # type: ignore[arg-type]
    await again.load()
    assert again.refused == {"box": {"m": 32_768}}
    assert await again.sweep() == 0
    assert app.config.presets["box.m"].context_window == 32_768


@pytest.mark.asyncio
async def test_a_save_in_between_is_kept_and_the_window_written_on_top(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_listing(monkeypatch, {("http://box/v1", "m"): 65_536})
    app = _App(_config(), _Providers({"box": _Endpoint("http://box/v1"), "hosted": _Endpoint("https://hosted/v1")}))
    app.conflicts = 1
    service = ContextWindows(app)  # type: ignore[arg-type]
    await service.sweep()
    assert "someone-else" in app.config.seeded
    assert app.config.presets["box.m"].context_window == 65_536


@pytest.mark.asyncio
async def test_a_listing_kept_from_before_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _fake_listing(monkeypatch, {("http://box/v1", "m"): 65_536})
    app = _App(_config(), _Providers({"box": _Endpoint("http://box/v1"), "hosted": _Endpoint("https://hosted/v1")}))
    app.db.kv["context_windows"] = {"listed": {"box": {"m": {"tokens": 16_000, "at": time.time() - 2 * 86_400}}}, "refused": {}}
    service = ContextWindows(app)  # type: ignore[arg-type]
    await service.load()
    assert await service.provider_found("box", "m") == cw.Found(65_536)
    assert ("http://box/v1", "m") in asked


# -- the API -------------------------------------------------------------------------------------

H = {"X-Daedalus-Token": "tok"}


def _api(monkeypatch: pytest.MonkeyPatch, answers: dict[tuple[str, str], int | None]) -> tuple[TestClient, _App]:
    _fake_listing(monkeypatch, answers)
    config = RuntimeConfig(providers={"box": ProviderConfig(base_url="http://box/v1")})
    app = _App(config, _Providers({"box": _Endpoint("http://box/v1")}))
    app.extensions["context_windows"] = ContextWindows(app)  # type: ignore[arg-type]
    return TestClient(build_app(app, "tok")), app  # type: ignore[arg-type]


def test_a_new_preset_gets_its_window_found_and_a_typed_one_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    client, app = _api(monkeypatch, {("http://box/v1", "m"): 65_536})
    response = client.put("/api/presets/box.m", json={"provider": "box", "model": "m"}, headers=H)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["presets"]["box.m"]["context_window"] == 65_536
    assert body["presets"]["box.m"]["context_window_source"] == "provider"
    assert body["context_window_sources"]["box.m"]["provider"] == 65_536
    # The operator types a number: it is theirs, and the resolver leaves it.
    client.put("/api/presets/box.m", json={"context_window": 40_000}, headers=H)
    assert (app.config.presets["box.m"].context_window, app.config.presets["box.m"].context_window_source) == (40_000, "manual")
    refreshed = client.post("/api/presets/box.m/context-window", headers=H).json()
    assert refreshed["window"] == 40_000 and refreshed["source"] == "manual" and refreshed["sources"]["provider"] == 65_536
    # And hands it back.
    client.put("/api/presets/box.m", json={"context_window_source": "auto"}, headers=H)
    assert (app.config.presets["box.m"].context_window, app.config.presets["box.m"].context_window_source) == (65_536, "provider")
    assert client.post("/api/presets/nope/context-window", headers=H).status_code == 404


def test_a_new_preset_nothing_knows_keeps_the_default_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    client, app = _api(monkeypatch, {})
    client.put("/api/presets/box.x", json={"provider": "box", "model": "mystery"}, headers=H)
    assert (app.config.presets["box.x"].context_window, app.config.presets["box.x"].context_window_source) == (128_000, "default")


def test_add_a_model_entries_carry_a_window_and_where_it_came_from() -> None:
    assert model_entry({"id": "q", "max_model_len": 40_960}) == {"id": "q", "context_length": 40_960, "context_source": "provider"}
    result = {"models": ["a", "claude-haiku-4-5", "mystery", "q"], "entries": [{"id": "a"}, {"id": "claude-haiku-4-5"}, {"id": "mystery"}, {"id": "q", "context_length": 40_960}]}
    with_known_windows(result, {"a": 64_000})
    assert result["entries"] == [
        {"id": "a", "context_length": 64_000, "context_source": "catalog"},
        {"id": "claude-haiku-4-5", "context_length": 200_000, "context_source": "table"},
        {"id": "mystery"},
        {"id": "q", "context_length": 40_960, "context_source": "provider"},
    ]
