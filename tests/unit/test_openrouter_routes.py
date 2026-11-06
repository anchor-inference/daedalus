"""OpenRouter's providers of a model: the listing, the price of a choice, and every way a call can leave."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.llm import LLMProviderError, LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock
from pydantic import BaseModel

from daedalus.config import ModelPresetConfig, ProviderConfig, RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.context_windows import ContextWindows
from daedalus.extensions.guest_door import DoorRefused, DoorUnavailable, GuestBridge, model_routes
from daedalus.extensions.openrouter_routes import OpenRouterRoutes
from daedalus.host.session_runner import LLMObservabilityContext, SessionManager
from daedalus.providers import openrouter_routes as routes
from daedalus.providers.free_catalog import probe_agent_cycle
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.providers.pricing import pricing_table
from daedalus.providers.registry import ProviderRegistry
from daedalus.stores.database import Database
from tests.support.models import model_config

H = {"X-Daedalus-Token": "tok"}
BASE = "http://keyproxy:3200/openrouter"
MODEL = "deepseek/deepseek-v4.1-flash-1017"


def per_token(rate: float) -> str:
    return f"{rate / 1_000_000:.12f}"


def endpoint(tag: str, name: str, price_in: float, price_out: float, cached: float | None = None, **extra: Any) -> dict[str, Any]:
    """An endpoint in the shape the live listing has, its fields as read off a real answer."""
    pricing: dict[str, Any] = {"prompt": per_token(price_in), "completion": per_token(price_out), "discount": 0}
    if cached is not None:
        pricing["input_cache_read"] = per_token(cached)
    pricing.update(extra.pop("pricing", {}))
    return {
        "name": f"{name} | {MODEL}", "model_id": MODEL, "model_name": "DeepSeek", "context_length": 1_048_576, "pricing": pricing, "provider_name": name, "tag": tag,
        "service_tier": None, "quantization": "fp8", "max_completion_tokens": 943_718, "max_prompt_tokens": None,
        "supported_parameters": ["reasoning", "tools", "tool_choice", "max_tokens"], "supports_tool_choice": {"none": True, "auto": True, "required": True, "function": True},
        "status": 0, "uptime_last_30m": 100, "uptime_last_5m": 100, "uptime_last_1d": 99.86, "supports_implicit_caching": cached is not None, "native_tools": {}, **extra,
    }


PEAK = {"overrides": [{"utc_start": 0, "utc_end": 1400, "prompt": per_token(0.15), "completion": per_token(0.6), "input_cache_read": per_token(0.015)}, {"utc_start": 1400, "utc_end": 0, "prompt": per_token(0.075), "completion": per_token(0.3), "input_cache_read": per_token(0.0075)}]}
ENDPOINTS = [
    endpoint("wafer/us", "Wafer", 0.038, 0.5, 0.01),
    endpoint("alibaba", "Alibaba", 0.075, 0.3, 0.0075, pricing=PEAK, context_length=1_000_000, max_completion_tokens=393_216),
    endpoint("mancer/fp8", "Mancer 2", 0.15, 0.5),
    endpoint("deepinfra/fp8", "DeepInfra", 0.098, 0.294, 0.0029),
    endpoint("deepinfra/fp8/priority", "DeepInfra", 0.147, 0.441, 0.0044, service_tier="priority"),
    endpoint("fireworks", "Fireworks", 0.3, 1.2, 0.006),
    endpoint("fireworks/us", "Fireworks", 0.45, 1.8, 0.009),
    endpoint("relace/fp4", "Relace", 0.006, 0.5, 0.005, supports_tool_choice={"none": False, "auto": True, "required": False, "function": False}),
    endpoint("plain", "Plain", 0.2, 0.8, 0.02, supported_parameters=["max_tokens"]),
]
LISTING = {"data": {"id": MODEL, "architecture": {"input_modalities": ["text", "image"]}, "endpoints": ENDPOINTS}}
ZDR_LIST = {"data": [{"model_id": MODEL, "tag": "wafer/us"}, {"model_id": "other/model", "tag": "alibaba"}]}
REFUSAL = {"error": {
    "message": "No allowed providers are available for the selected model. Providers serving deepseek/deepseek-v4.1-flash-20261017: relace, wafer, but your request's provider.only preference permits only: vanished.",
    "code": 404,
    "metadata": {"available_providers": ["relace", "wafer"], "requested_providers": ["vanished"], "failed_routing_step": "Filter by Allowed Providers"},
}}


@pytest.fixture(autouse=True)
def fresh_zdr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "ZDR", routes.ZdrList())


def listing_client(listing: Any = LISTING, *, status: int = 200, zdr: Any = ZDR_LIST, seen: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path.endswith("/endpoints/zdr"):
            return httpx.Response(200, json=zdr) if zdr is not None else httpx.Response(500)
        return httpx.Response(status, json=listing)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def rows_of(**options: Any) -> list[dict[str, Any]]:
    listing = await routes.fetch_listing(BASE, "", MODEL, client=listing_client(), **options)
    assert listing is not None
    return listing["rows"]


# -- the listing ---------------------------------------------------------------------------------


async def test_the_listing_is_asked_on_the_models_own_path_with_the_key() -> None:
    seen: list[httpx.Request] = []
    await routes.fetch_listing(BASE + "/", "secret", "recursive/flint-v1.0:free", client=listing_client(seen=seen))
    assert seen[0].url.path == "/openrouter/models/recursive/flint-v1.0:free/endpoints"
    assert seen[0].headers["authorization"] == "Bearer secret"


async def test_a_row_carries_what_the_step_shows() -> None:
    listing = await routes.fetch_listing(BASE, "", MODEL, client=listing_client(), zero_retention=True)
    assert listing is not None and listing["images"] is True
    rows = {row["slug"]: row for row in listing["rows"]}
    wafer = rows["wafer/us"]
    assert (wafer["name"], wafer["input"], wafer["output"], wafer["cache_hit"]) == ("Wafer", 0.038, 0.5, 0.01)
    assert (wafer["context_length"], wafer["max_output_tokens"], wafer["uptime"], wafer["quantization"]) == (1_048_576, 943_718, 99.86, "fp8")
    assert (wafer["tools"], wafer["tool_choice"], wafer["reasoning"], wafer["caching"], wafer["zdr"]) == (True, "full", True, True, True)
    assert rows["alibaba"]["zdr"] is False  # listed, but for another model
    assert rows["relace/fp4"]["tool_choice"] == "partial"
    assert (rows["plain"]["tools"], rows["plain"]["tool_choice"], rows["plain"]["reasoning"]) == (False, "none", False)
    assert rows["deepinfra/fp8/priority"]["tier"] == "priority"


async def test_rows_come_cheapest_first_with_output_weighed_as_the_door_weighs_it() -> None:
    slugs = [row["slug"] for row in await rows_of()]
    assert slugs[0] == "deepinfra/fp8" and slugs[-1] == "fireworks/us"


@pytest.mark.parametrize("zdr", [None, {"unexpected": True}, {"data": "nothing"}])
async def test_without_the_retention_list_the_mark_is_unknown_and_the_listing_stands(zdr: Any) -> None:
    listing = await routes.fetch_listing(BASE, "", MODEL, client=listing_client(zdr=zdr), zero_retention=True)
    assert listing is not None and {row["zdr"] for row in listing["rows"]} == {None}


async def test_a_model_the_catalogue_does_not_know_is_an_empty_listing_not_a_failure() -> None:
    listing = await routes.fetch_listing(BASE, "", "nobody/nothing", client=listing_client({"error": {"message": "Not Found", "code": 404}}, status=404))
    assert listing == {"model": "nobody/nothing", "images": False, "rows": []}


@pytest.mark.parametrize(("body", "status"), [({"error": "boom"}, 500), ({"data": {}}, 200), ({"data": {"endpoints": "x"}}, 200), ("[]", 200)])
async def test_a_listing_that_cannot_be_read_is_none(body: Any, status: int) -> None:
    assert await routes.fetch_listing(BASE, "", MODEL, client=listing_client(body, status=status)) is None


async def test_a_network_failure_is_none() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    assert await routes.fetch_listing(BASE, "", MODEL, client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))) is None


@pytest.mark.parametrize("model", ["deepseek-flash", "a/../../credits", "", "a/b c"])
async def test_an_id_off_the_catalogues_path_is_never_asked_for(model: str) -> None:
    seen: list[httpx.Request] = []
    assert await routes.fetch_listing(BASE, "", model, client=listing_client(seen=seen)) is None
    assert seen == []


# -- the price of a choice -----------------------------------------------------------------------


async def test_one_provider_is_its_own_price() -> None:
    assert routes.bill_for(await rows_of(), ["wafer/us"]) == {"input": 0.038, "output": 0.5, "cache_hit": 0.01}


async def test_several_take_the_highest_of_each_rate() -> None:
    assert routes.bill_for(await rows_of(), ["wafer/us", "deepinfra/fp8"]) == {"input": 0.098, "output": 0.5, "cache_hit": 0.01}


async def test_a_provider_with_dear_hours_is_priced_at_them() -> None:
    """The listed $0.075 holds for ten hours of the day; the other fourteen bill twice that."""
    rows = {row["slug"]: row for row in await rows_of()}
    assert (rows["alibaba"]["input"], rows["alibaba"]["output"], rows["alibaba"]["cache_hit"], rows["alibaba"]["varies"]) == (0.15, 0.6, 0.015, True)
    assert rows["wafer/us"]["varies"] is False


async def test_a_slug_is_priced_for_the_variants_it_reaches_but_not_the_tiers() -> None:
    rows = await rows_of()
    # Naming Fireworks can land on Fireworks US, listed at half as much again.
    assert routes.bill_for(rows, ["fireworks"]) == {"input": 0.45, "output": 1.8, "cache_hit": 0.009}
    assert {row["slug"]: row["bill"]["input"] for row in rows}["fireworks"] == 0.45
    # A tier is opted into by its own slug: the plain endpoint does not reach it.
    assert routes.bill_for(rows, ["deepinfra/fp8"])["input"] == 0.098
    assert routes.bill_for(rows, ["deepinfra/fp8/priority"])["input"] == 0.147


async def test_a_provider_with_no_cached_price_leaves_none_and_a_cached_token_costs_the_full_rate() -> None:
    bill = routes.bill_for(await rows_of(), ["wafer/us", "mancer/fp8"])
    assert bill == {"input": 0.15, "output": 0.5, "cache_hit": None}
    price = pricing_table("openrouter", {MODEL: routes.priced_entry({}, bill)})[MODEL]
    assert price.cost({"input_tokens": 1_000_000, "cache_read_tokens": 1_000_000, "output_tokens": 0}) == pytest.approx(0.15)


def test_the_entry_keeps_the_operators_ceiling_and_loses_an_old_cache_rate() -> None:
    prior = {"input": 9.0, "output": 9.0, "cache_hit": 1.0, "input_limit": 1000, "limit_source": "by hand"}
    assert routes.priced_entry(prior, {"input": 0.1, "output": 0.2, "cache_hit": None}) == {"input": 0.1, "output": 0.2, "input_limit": 1000, "limit_source": "by hand"}


async def test_the_limits_are_the_smallest_of_the_chosen() -> None:
    rows = await rows_of()
    assert routes.lowest_window(rows, ["wafer/us", "alibaba"]) == 1_000_000
    assert routes.lowest_output(rows, ["wafer/us", "alibaba"]) == 393_216
    assert routes.lowest_window(rows, ["vanished"]) is None


async def test_gone_is_what_no_listed_endpoint_answers_to() -> None:
    rows = await rows_of()
    assert routes.gone(rows, ["wafer/us", "vanished"]) == ["vanished"]
    # The plain row can leave while the slug still reaches a variant.
    assert routes.gone([row for row in rows if row["slug"] != "fireworks"], ["fireworks"]) == []


# -- every way a call leaves ---------------------------------------------------------------------


def routed_config(**preset: Any) -> RuntimeConfig:
    fields = {"provider": "openrouter", "model": MODEL, "openrouter_providers": ["wafer/us", "alibaba"], **preset}
    return RuntimeConfig(providers={"openrouter": ProviderConfig(kind="openrouter", base_url=BASE, pricing={MODEL: {"input": 0.15, "output": 0.6}})},
                         presets={"chosen": ModelPresetConfig(**fields), "other": ModelPresetConfig(provider="openrouter", model="free/roaming")})


def registry_with(config: RuntimeConfig, sent: list[dict[str, Any]], answer: Any = None) -> tuple[ProviderRegistry, OpenAICompatibleProvider]:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if answer is not None:
            return answer
        if body.get("stream"):
            lines = [{"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}, {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.5}}]
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(f"data: {json.dumps(line)}\n\n" for line in lines) + "data: [DONE]\n\n")
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.5}})

    registry = ProviderRegistry(Settings(_env_file=None), config)  # type: ignore[call-arg]
    adapter = registry.get("openrouter")
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return registry, adapter


def request_for(model: str = MODEL) -> LLMRequest:
    return LLMRequest(model=model, messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])], max_tokens=16)


CHOSEN = {"only": ["wafer/us", "alibaba"], "allow_fallbacks": False}


async def test_a_streamed_run_sends_the_choice() -> None:
    sent: list[dict[str, Any]] = []
    _, adapter = registry_with(routed_config(), sent)
    async for _delta in adapter.stream_with_tools(request_for()):
        pass
    assert sent[0]["provider"] == CHOSEN


async def test_a_side_call_in_words_sends_the_choice() -> None:
    sent: list[dict[str, Any]] = []
    _, adapter = registry_with(routed_config(), sent)
    await adapter.complete_text(request_for())
    assert sent[0]["provider"] == CHOSEN


async def test_a_structured_side_call_sends_the_choice() -> None:
    class Answer(BaseModel):
        pass

    sent: list[dict[str, Any]] = []
    _, adapter = registry_with(routed_config(), sent)
    await adapter.complete_structured(request_for(), Answer)
    assert sent[0]["provider"] == CHOSEN


async def test_the_chain_and_an_ad_hoc_pair_reach_the_same_adapter() -> None:
    sent: list[dict[str, Any]] = []
    config = routed_config()
    registry, adapter = registry_with(config, sent)
    config.model.preset = "chosen"
    for found, model in (registry.rungs_for(config, "chosen")[0], registry.rungs_for_pair(config, "openrouter", MODEL)[0]):
        assert found is adapter
        await found.complete_text(request_for(model))
    assert [body["provider"] for body in sent] == [CHOSEN, CHOSEN]


async def test_a_compaction_sends_the_choice(settings: Settings, db: Database) -> None:
    sent: list[dict[str, Any]] = []
    manager = SessionManager(settings, routed_config(), db=db)
    await manager.start()
    try:
        _, adapter = registry_with(routed_config(), sent)
        history = [Message(role=MessageRole.user, content_blocks=[TextBlock(text="a long story")])]
        await manager._summarise_history(adapter, MODEL, history, language="en", instructions="", observability=LLMObservabilityContext(tenant_id="t", session_id="s", run_id="r", call_purpose="compaction", call_category="compaction"))
    finally:
        await manager.close()
    assert sent and all(body["provider"] == CHOSEN for body in sent)


async def test_a_change_reaches_a_run_already_under_way() -> None:
    """A run holds its adapter while settings replace the configuration: the choice is read per request."""
    sent: list[dict[str, Any]] = []
    registry, adapter = registry_with(routed_config(), sent)
    registry.reload(routed_config(openrouter_providers=["relace/fp4"]))
    assert registry.get("openrouter") is adapter
    await adapter.complete_text(request_for())
    assert sent[0]["provider"] == {"only": ["relace/fp4"], "allow_fallbacks": False}


async def test_nothing_chosen_sends_nothing_as_before() -> None:
    sent: list[dict[str, Any]] = []
    _, adapter = registry_with(routed_config(openrouter_providers=[]), sent)
    await adapter.complete_text(request_for())
    await adapter.complete_text(request_for("free/roaming"))
    assert all("provider" not in body for body in sent)


async def test_another_kind_of_endpoint_never_hears_of_it() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    adapter = OpenAICompatibleProvider(ProviderEndpoint(id="box", kind="openai_compat", base_url="http://box/v1"), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), routing_for=lambda _model: ("wafer/us",))
    await adapter.complete_text(request_for())
    assert "provider" not in sent[0]


async def test_the_free_catalogues_probe_sends_the_choice() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        call = {"id": "c", "function": {"name": "add_numbers", "arguments": '{"a": 2, "b": 3}'}}
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [call], "content": "5"}}]})

    assert await probe_agent_cycle(BASE, "a/b:free", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), route=("wafer/us",))
    assert [body["provider"] for body in sent] == [{"only": ["wafer/us"], "allow_fallbacks": False}] * 2


def test_no_call_of_the_model_leaves_by_a_way_this_file_does_not_cover() -> None:
    """Who writes a chat completion by hand has to send the choice too. A new one fails here until it is listed."""
    root = Path(__file__).resolve().parents[2]
    found = {str(path.relative_to(root)) for folder in ("daedalus", "deploy/guestd") for path in (root / folder).rglob("*.py") if re.search(r"/chat/completions\"", path.read_text())}
    assert found == {"daedalus/providers/openai_compat.py", "daedalus/providers/free_catalog.py", "deploy/guestd/standin.py"}
    source = (root / "daedalus/providers/openai_compat.py").read_text()
    assert source.count("await self._build_body(") == 3


# -- a chosen provider that is no longer there ------------------------------------------------------


async def test_a_refusal_of_every_chosen_provider_says_so_and_is_not_rerouted() -> None:
    sent: list[dict[str, Any]] = []
    heard: list[tuple[str, str]] = []
    registry, adapter = registry_with(routed_config(), sent, httpx.Response(404, json=REFUSAL))
    registry.on_route_refusal = lambda *pair: heard.append(pair)
    adapter._route_refusal = registry._route_refusal
    with pytest.raises(LLMProviderError, match=r"none of the providers chosen for this model can serve it now \(vanished\).*model's settings"):
        await adapter.complete_text(request_for())
    assert len(sent) == 1 and sent[0]["provider"]["allow_fallbacks"] is False
    assert heard == [("openrouter", MODEL)]


async def test_a_refusal_inside_a_stream_is_heard_the_same_way() -> None:
    sent: list[dict[str, Any]] = []
    _, adapter = registry_with(routed_config(), sent, httpx.Response(404, json=REFUSAL))
    with pytest.raises(LLMProviderError, match="none of the providers chosen"):
        async for _delta in adapter.stream_with_tools(request_for()):
            pass


async def test_another_not_found_keeps_its_own_words() -> None:
    sent: list[dict[str, Any]] = []
    _, adapter = registry_with(routed_config(), sent, httpx.Response(404, json={"error": {"message": "No such model", "code": 404}}))
    with pytest.raises(LLMProviderError, match="No such model"):
        await adapter.complete_text(request_for())


@pytest.mark.parametrize("text", ["not json", "[]", '{"error": "plain"}', '{"error": {"message": "Not Found"}}'])
def test_a_body_that_is_no_routing_refusal_is_not_read_as_one(text: str) -> None:
    assert routes.refused_route(text) is None


# -- the daily check -----------------------------------------------------------------------------


class _Endpoints:
    def get(self, _provider_id: str) -> Any:
        return SimpleNamespace(endpoint=SimpleNamespace(base_url=BASE, api_key=""))


class _App:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.manager = SimpleNamespace(providers=_Endpoints())
        self.extensions: dict[str, Any] = {}
        self.saves = 0
        self.conflicts = 0

    async def save_config(self, config: RuntimeConfig, *, expected_revision: str | None = None) -> None:
        from daedalus.host.config_validation import ConfigConflict

        if self.conflicts:
            self.conflicts -= 1
            self.config = self.config.model_copy(update={"seeded": [*self.config.seeded, "someone-else"]})
            raise ConfigConflict("moved")
        self.config = config
        self.saves += 1


def serve_listing(monkeypatch: pytest.MonkeyPatch, listing: Any) -> list[str]:
    asked: list[str] = []

    async def fetch(_base: str, _key: str, model: str, **_: Any) -> Any:
        asked.append(model)
        return listing

    monkeypatch.setattr("daedalus.extensions.openrouter_routes.fetch_listing", fetch)
    return asked


async def live_listing() -> dict[str, Any]:
    listing = await routes.fetch_listing(BASE, "", MODEL, client=listing_client())
    assert listing is not None
    return listing


async def test_the_check_brings_the_price_up_to_the_listing_and_stays_quiet_after(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(routed_config())
    asked = serve_listing(monkeypatch, await live_listing())
    service = OpenRouterRoutes(app)  # type: ignore[arg-type]
    assert await service.check(service.limited()) > 0
    assert asked == [MODEL]
    price = app.config.providers["openrouter"].pricing[MODEL]
    assert (price["input"], price["output"], price["cache_hit"]) == (0.15, 0.6, 0.015)
    saves = app.saves
    assert await service.check(service.limited()) == 0
    assert app.saves == saves


async def test_a_departed_provider_is_written_beside_the_choice_and_the_choice_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(routed_config(openrouter_providers=["wafer/us", "vanished"]))
    serve_listing(monkeypatch, await live_listing())
    service = OpenRouterRoutes(app)  # type: ignore[arg-type]
    await service.check(service.limited())
    preset = app.config.presets["chosen"]
    assert preset.openrouter_providers == ["wafer/us", "vanished"] and preset.openrouter_providers_gone == ["vanished"]
    assert app.config.providers["openrouter"].pricing[MODEL]["input"] == 0.038  # priced from what is still there


async def test_a_provider_that_comes_back_clears_the_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(routed_config(openrouter_providers=["wafer/us"], openrouter_providers_gone=["wafer/us"]))
    serve_listing(monkeypatch, await live_listing())
    service = OpenRouterRoutes(app)  # type: ignore[arg-type]
    await service.check(service.limited())
    assert app.config.presets["chosen"].openrouter_providers_gone == []


async def test_a_model_with_every_provider_gone_keeps_its_last_price(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(routed_config())
    serve_listing(monkeypatch, {"model": MODEL, "images": False, "rows": []})
    service = OpenRouterRoutes(app)  # type: ignore[arg-type]
    await service.check(service.limited())
    assert app.config.presets["chosen"].openrouter_providers_gone == ["wafer/us", "alibaba"]
    assert app.config.providers["openrouter"].pricing[MODEL]["input"] == 0.15


async def test_a_listing_that_cannot_be_read_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(routed_config())
    serve_listing(monkeypatch, None)
    service = OpenRouterRoutes(app)  # type: ignore[arg-type]
    assert await service.check(service.limited()) == 0
    assert app.saves == 0 and app.config.presets["chosen"].openrouter_providers_gone == []


async def test_an_operators_save_in_between_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(routed_config())
    app.conflicts = 1
    serve_listing(monkeypatch, await live_listing())
    service = OpenRouterRoutes(app)  # type: ignore[arg-type]
    await service.check(service.limited())
    assert "someone-else" in app.config.seeded and app.config.providers["openrouter"].pricing[MODEL]["input"] == 0.15


async def test_a_refusal_checks_that_model_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    app = _App(routed_config(openrouter_providers=["vanished"]))
    asked = serve_listing(monkeypatch, await live_listing())
    service = OpenRouterRoutes(app)  # type: ignore[arg-type]
    service.record_refusal("openrouter", MODEL)
    await asyncio.gather(*service._background)
    assert asked == [MODEL] and app.config.presets["chosen"].openrouter_providers_gone == ["vanished"]


def test_existing_presets_load_with_nothing_chosen() -> None:
    preset = ModelPresetConfig.model_validate({"provider": "openrouter", "model": MODEL})
    assert preset.openrouter_providers == [] and preset.openrouter_providers_gone == []


# -- the context window --------------------------------------------------------------------------


async def test_the_window_of_a_limited_model_is_the_smallest_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = await live_listing()

    async def fetch(_base: str, _key: str, _model: str) -> Any:
        return listing

    monkeypatch.setattr(routes, "fetch_listing", fetch)

    class _Db:
        async def kv_set(self, _key: str, _value: Any) -> None:
            pass

    app = _App(routed_config())
    app.db = _Db()  # type: ignore[attr-defined]
    found = await ContextWindows(app).provider_found("openrouter", MODEL, refresh=True)  # type: ignore[arg-type]
    assert found is not None and found.tokens == 1_000_000


# -- the API ---------------------------------------------------------------------------------------


class Host:
    """The app's view of the API with the listing and the catalogue faked."""

    def __init__(self, settings: Settings, db: Database, config: RuntimeConfig) -> None:
        self.settings, self.db, self.config = settings, db, config
        self.manager = SessionManager(settings, config, db=db)
        self.front = None
        self.extensions: dict[str, Any] = {}
        self.guard = None
        self.create_session = self.manager.create_session

    async def save_config(self, config: RuntimeConfig, *, expected_revision: str | None = None) -> None:
        self.manager.reload_config(config)
        self.config = config


@pytest.fixture
async def host(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> Any:
    config = model_config()
    config.providers["openrouter"] = ProviderConfig(kind="openrouter", base_url=BASE)
    made = Host(settings, db, config)
    await made.manager.start()

    async def no_door(_app: Any) -> None:
        pass

    monkeypatch.setattr("daedalus.extensions.api.guest_door.routes_changed", no_door)
    yield made
    await made.manager.close()


async def fake_listing(monkeypatch: pytest.MonkeyPatch, listing: Any) -> list[tuple[str, Any]]:
    asked: list[tuple[str, Any]] = []

    async def fetch(_base: str, _key: str, model: str, **options: Any) -> Any:
        asked.append((model, options.get("zero_retention")))
        return listing

    monkeypatch.setattr(routes, "fetch_listing", fetch)
    return asked


def client_of(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test")  # type: ignore[arg-type]


async def test_the_app_is_given_the_table_with_the_retention_marks(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    listing = await live_listing()
    asked = await fake_listing(monkeypatch, listing)
    async with client_of(host) as client:
        answer = await client.get("/api/providers/openrouter/model-providers", params={"model": MODEL}, headers=H)
    assert answer.status_code == 200 and answer.json()["unavailable"] is False and len(answer.json()["rows"]) == len(ENDPOINTS)
    assert asked == [(MODEL, True)]


@pytest.mark.parametrize("provider", ["nobody", "deepseek"])
async def test_only_an_openrouter_client_has_providers_and_nobody_is_told_off_for_asking(host: Host, provider: str) -> None:
    host.config.providers["deepseek"] = ProviderConfig(kind="deepseek", base_url="http://keyproxy:3200/deepseek")
    async with client_of(host) as client:
        answer = await client.get(f"/api/providers/{provider}/model-providers", params={"model": MODEL}, headers=H)
    assert answer.status_code == 200 and answer.json() == {"model": MODEL, "images": False, "rows": [], "unavailable": True}


async def test_a_listing_out_of_reach_is_an_answer_the_flow_can_go_on_from(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    await fake_listing(monkeypatch, None)
    async with client_of(host) as client:
        answer = await client.get("/api/providers/openrouter/model-providers", params={"model": MODEL}, headers=H)
    assert answer.status_code == 200 and answer.json()["rows"] == [] and answer.json()["unavailable"] is True


async def test_the_listing_needs_the_token(host: Host) -> None:
    async with client_of(host) as client:
        answer = await client.get("/api/providers/openrouter/model-providers", params={"model": MODEL})
    assert answer.status_code in (401, 403)


async def test_saving_a_choice_stores_it_and_the_price_it_makes_for_every_preset_of_the_pair(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    await fake_listing(monkeypatch, await live_listing())
    async with client_of(host) as client:
        first = await client.put("/api/presets/or.twin", json={"provider": "openrouter", "model": MODEL, "max_output_tokens": 500_000}, headers=H)
        assert first.status_code == 200
        answer = await client.put("/api/presets/or.pick", json={"provider": "openrouter", "model": MODEL, "max_output_tokens": 500_000, "openrouter_providers": ["wafer/us", "alibaba"]}, headers=H)
    assert answer.status_code == 200
    presets = host.config.presets
    assert presets["or.pick"].openrouter_providers == ["wafer/us", "alibaba"] == presets["or.twin"].openrouter_providers
    assert presets["or.pick"].max_output_tokens == 393_216  # the lower provider's limit, so a reply is not cut unannounced
    assert answer.json()["presets"]["or.pick"]["openrouter_providers"] == ["wafer/us", "alibaba"]
    price = host.config.providers["openrouter"].pricing[MODEL]
    assert (price["input"], price["output"], price["cache_hit"]) == (0.15, 0.6, 0.015)
    assert host.manager.providers.get("openrouter").endpoint.pricing_for(MODEL).input == 0.15


async def test_the_price_reaches_the_adapter_and_a_call_goes_out_limited(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    await fake_listing(monkeypatch, await live_listing())
    async with client_of(host) as client:
        await client.put("/api/presets/or.pick", json={"provider": "openrouter", "model": MODEL, "openrouter_providers": ["wafer/us"]}, headers=H)
    assert host.config.openrouter_route("openrouter", MODEL) == ("wafer/us",)
    assert host.manager.providers._route_of("openrouter", MODEL) == ("wafer/us",)


async def test_a_slug_the_listing_does_not_have_is_refused_and_nothing_moves(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    await fake_listing(monkeypatch, await live_listing())
    async with client_of(host) as client:
        answer = await client.put("/api/presets/or.pick", json={"provider": "openrouter", "model": MODEL, "openrouter_providers": ["invented"]}, headers=H)
    assert answer.status_code == 400 and "invented" in answer.json()["detail"]
    assert "or.pick" not in host.config.presets


async def test_a_choice_cannot_be_saved_without_the_listing_to_price_it(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    await fake_listing(monkeypatch, None)
    async with client_of(host) as client:
        answer = await client.put("/api/presets/or.pick", json={"provider": "openrouter", "model": MODEL, "openrouter_providers": ["wafer/us"]}, headers=H)
    assert answer.status_code == 502 and "or.pick" not in host.config.presets


async def test_nothing_ticked_on_a_new_model_never_asks_for_the_listing(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    asked = await fake_listing(monkeypatch, await live_listing())
    async with client_of(host) as client:
        answer = await client.put("/api/presets/or.plain", json={"provider": "openrouter", "model": MODEL, "openrouter_providers": []}, headers=H)
    assert answer.status_code == 200 and asked == [] and host.config.presets["or.plain"].openrouter_providers == []


async def test_clearing_hands_routing_and_the_price_back(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    await fake_listing(monkeypatch, await live_listing())

    async def catalogue(_base: str, _key: str | None) -> dict[str, Any]:
        return {"entries": [{"id": MODEL, "pricing": {"input": 0.051, "output": 0.567}}]}

    monkeypatch.setattr("daedalus.extensions.api.lookup_openai_models", catalogue)
    async with client_of(host) as client:
        await client.put("/api/presets/or.pick", json={"provider": "openrouter", "model": MODEL, "openrouter_providers": ["alibaba"]}, headers=H)
        answer = await client.put("/api/presets/or.pick", json={"openrouter_providers": []}, headers=H)
    assert answer.status_code == 200 and host.config.presets["or.pick"].openrouter_providers == []
    price = host.config.providers["openrouter"].pricing[MODEL]
    assert (price["input"], price["output"]) == (0.051, 0.567) and "cache_hit" not in price


async def test_clearing_with_the_catalogue_out_of_reach_still_clears(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    await fake_listing(monkeypatch, await live_listing())

    async def down(_base: str, _key: str | None) -> dict[str, Any]:
        raise ValueError("no answer")

    monkeypatch.setattr("daedalus.extensions.api.lookup_openai_models", down)
    async with client_of(host) as client:
        await client.put("/api/presets/or.pick", json={"provider": "openrouter", "model": MODEL, "openrouter_providers": ["alibaba"]}, headers=H)
        answer = await client.put("/api/presets/or.pick", json={"openrouter_providers": []}, headers=H)
    assert answer.status_code == 200 and host.config.presets["or.pick"].openrouter_providers == []


async def test_a_preset_moved_to_another_model_leaves_the_choice_behind(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    await fake_listing(monkeypatch, await live_listing())
    async with client_of(host) as client:
        await client.put("/api/presets/or.pick", json={"provider": "openrouter", "model": MODEL, "openrouter_providers": ["alibaba"]}, headers=H)
        await client.put("/api/presets/or.pick", json={"model": "other/model"}, headers=H)
    assert host.config.presets["or.pick"].openrouter_providers == []


async def test_a_new_preset_of_a_limited_pair_joins_its_choice(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    await fake_listing(monkeypatch, await live_listing())
    async with client_of(host) as client:
        await client.put("/api/presets/or.pick", json={"provider": "openrouter", "model": MODEL, "openrouter_providers": ["alibaba"]}, headers=H)
        await client.put("/api/presets/or.more", json={"provider": "openrouter", "model": MODEL}, headers=H)
    assert host.config.presets["or.more"].openrouter_providers == ["alibaba"]


async def test_another_kind_of_client_cannot_take_a_choice(host: Host) -> None:
    host.config.providers["box"] = ProviderConfig(kind="openai_compat", base_url="http://box/v1")
    async with client_of(host) as client:
        answer = await client.put("/api/presets/box.m", json={"provider": "box", "model": "m", "openrouter_providers": ["wafer/us"]}, headers=H)
    assert answer.status_code == 400


# -- the guest door, host side -----------------------------------------------------------------------


def door_host(config: RuntimeConfig) -> Any:
    registry = ProviderRegistry(Settings(_env_file=None), config)  # type: ignore[call-arg]
    return SimpleNamespace(config=config, manager=SimpleNamespace(providers=registry), settings=SimpleNamespace(guest_public_url=""), extensions={}, db=None, notifications=None)


def test_the_door_is_told_each_limited_model_with_the_price_it_is_billed_at() -> None:
    routes_table = model_routes(door_host(routed_config()))
    assert routes_table == [{"path": "openrouter", "model": MODEL, "only": ["wafer/us", "alibaba"], "price_in": 0.15, "price_out": 0.6}]


def test_a_model_with_no_price_stays_in_the_table_or_its_limit_would_lift() -> None:
    config = routed_config()
    config.providers["openrouter"].pricing = {}
    assert model_routes(door_host(config)) == [{"path": "openrouter", "model": MODEL, "only": ["wafer/us", "alibaba"], "price_in": None, "price_out": None}]


def test_a_model_with_nothing_chosen_is_not_in_the_table() -> None:
    assert model_routes(door_host(routed_config(openrouter_providers=[]))) == []


class FakeDoor:
    configured = True

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[Any] = []

    async def call(self, method: str, path: str, **options: Any) -> dict[str, Any]:
        self.calls.append((method, path, options.get("json")))
        if self.fail:
            raise DoorUnavailable("gone")
        return {}


async def test_a_change_is_told_to_the_door_at_once() -> None:
    door = FakeDoor()
    bridge = GuestBridge(door_host(routed_config()), door)  # type: ignore[arg-type]
    await bridge.publish_routes()
    assert door.calls[0][:2] == ("PUT", "/control/settings") and door.calls[0][2]["model_routes"][0]["only"] == ["wafer/us", "alibaba"]


async def test_with_no_agent_here_the_door_is_told_nothing_rather_than_an_empty_table() -> None:
    door = FakeDoor()
    app = door_host(routed_config())
    app.manager = None
    await GuestBridge(app, door).publish_routes()  # type: ignore[arg-type]
    assert door.calls == []


async def test_a_table_the_door_refuses_does_not_end_the_connection() -> None:
    class Refusing(FakeDoor):
        async def call(self, method: str, path: str, **options: Any) -> dict[str, Any]:
            raise DoorRefused(422, "no")

    bridge = GuestBridge(door_host(routed_config()), Refusing())  # type: ignore[arg-type]
    bridge.connected = True
    await bridge.publish_routes()
    assert bridge.connected is True


async def test_a_door_out_of_reach_hears_at_its_next_connection() -> None:
    door = FakeDoor(fail=True)
    bridge = GuestBridge(door_host(routed_config()), door)  # type: ignore[arg-type]
    bridge.connected = True
    await bridge.publish_routes()
    assert bridge.connected is False
