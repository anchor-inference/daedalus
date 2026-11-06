"""What the key proxy may serve a stand-in, from the host that writes it to the door whose calls it judges.

The other side of ``test_keyproxy_guest.py``, which holds a hand-written table against the proxy alone. These run
the real table through the real door request and the real proxy check, so a stand-in that the two would disagree
about, on any route, fails here rather than as a model that stopped answering.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from guestd.standin import KeyProxyCaller, ModelFailed, request_for
from guestd.store import Store

from daedalus.app import Application
from daedalus.config import ModelPresetConfig, ProviderConfig, RuntimeConfig, Settings
from daedalus.extensions import guest_door
from daedalus.extensions.guest_door import GuestBridge, model_choices, model_routes, model_table, write_model_table
from daedalus.providers.registry import ProviderRegistry
from tests.unit.test_keyproxy_guest import load

BASE = "http://keyproxy:3200"
LIMITED = "moonshotai/kimi-k3"
ROUTE = ["wafer/us", "deepinfra/fp8/flex"]
KEYS = ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "OPENCODE_API_KEY", "ZAI_CODING_API_KEY")

ROUTES: dict[str, tuple[str, str]] = {
    "limited": ("openrouter", LIMITED),
    "roaming": ("openrouter", "deepseek/deepseek-chat"),
    "free": ("openrouter", "openai/gpt-oss:free"),
    "metered": ("deepseek", "deepseek-chat"),
    "claude": ("claude", "claude-opus-4-5"),
    "codex": ("codex", "gpt-5.1-codex"),
    "grok": ("grok", "grok-4"),
    "opencode": ("opencode", "kimi-k3"),
    "zen": ("opencode_zen", "grok-code-free"),
    "plan": ("zai_coding", "glm-5"),
}
"""One of each way a stand-in leaves: OpenRouter limited and not, a key, the three CLI logins, the prepaid plans."""


def config_with(**selection: list[str]) -> RuntimeConfig:
    config = RuntimeConfig()
    config.providers["openrouter"].pricing = {model: {"input": 1, "output": 2} for _, model in ROUTES.values()}
    config.providers["deepseek"].pricing = {"deepseek-chat": {"input": 1, "output": 2}}
    config.presets = {
        preset_id: ModelPresetConfig(provider=provider, model=model, openrouter_providers=selection.get(preset_id, ROUTE if preset_id == "limited" else []))
        for preset_id, (provider, model) in ROUTES.items()
    }
    return config


def host(settings: Settings, config: RuntimeConfig) -> Any:
    registry = ProviderRegistry(Settings(_env_file=None), config)  # type: ignore[call-arg]
    settings.guest_model_upstreams = "*"
    return SimpleNamespace(config=config, manager=SimpleNamespace(providers=registry), settings=settings, extensions={})


@pytest.fixture
def proxy(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The proxy as compose and the launcher start it, with its table where the host writes it."""
    monkeypatch.setenv("KEYPROXY_BUDGET_DB", str(settings.state_dir / "daedalus.sqlite"))
    monkeypatch.setenv("KEYPROXY_GUEST_UPSTREAMS", "*")
    monkeypatch.setenv("KEYPROXY_BASE_URL", BASE)
    for name in KEYS:
        monkeypatch.setenv(name, "the-key")
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    return load()


def stand_in_of(app: Any, proxy: Any, tmp_path: Path, preset_id: str) -> tuple[KeyProxyCaller, dict[str, Any], dict[str, Any]]:
    """A door holding the table the host pushes, and a link as the host makes it. The model call goes through the
    door's own client into the proxy's check, so what is judged is the bytes and the address it would really get."""
    store = Store(tmp_path / f"{preset_id}.sqlite")
    store.set_setting("model_routes", json.dumps(model_routes(app)))
    option = next(o for o in model_choices(app)[0] if o["id"] == preset_id)
    link = store.routed({"briefing": "a briefing", "model_path": option["path"], "model_name": option["model"]})

    def answer(sent: httpx.Request) -> httpx.Response:
        refused = proxy.guest_refusal(sent.url.path, sent.method, sent.content)
        return httpx.Response(403, json={"error": {"message": refused}}) if refused else httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    return KeyProxyCaller("guest.sock", httpx.AsyncClient(transport=httpx.MockTransport(answer))), link, request_for(link, [])


def test_the_table_is_every_model_behind_the_proxy_with_its_providers(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYPROXY_BASE_URL", BASE)
    config = config_with()
    config.providers["own"] = ProviderConfig(kind="openai_compat", base_url="https://own.example.test/v1")
    config.presets["outside"] = ModelPresetConfig(provider="own", model="anything")
    table = {(row["path"], row["model"]): row["only"] for row in model_table(config)}
    assert table[("openrouter", LIMITED)] == ROUTE and table[("openrouter", "deepseek/deepseek-chat")] == []
    assert table[("claude/v1", "claude-opus-4-5")] == [] and table[("opencode", "kimi-k3")] == []
    assert not any(path.startswith("own") for path, _ in table) and len(table) == len(ROUTES)


def test_a_model_is_limited_as_the_doors_table_has_it_whichever_preset_comes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two presets of one model, one of them limited: the door sends the limit, so the proxy must expect it."""
    monkeypatch.setenv("KEYPROXY_BASE_URL", BASE)
    for first, second in ((ROUTE, []), ([], ROUTE)):
        config = config_with()
        config.presets["one"] = ModelPresetConfig(provider="openrouter", model="shared/model", openrouter_providers=first)
        config.presets["two"] = ModelPresetConfig(provider="openrouter", model="shared/model", openrouter_providers=second)
        assert next(row["only"] for row in model_table(config) if row["model"] == "shared/model") == ROUTE


@pytest.mark.parametrize("preset_id", list(ROUTES))
async def test_a_stand_in_is_served_on_every_route_as_it_stands(settings: Settings, proxy: Any, tmp_path: Path, preset_id: str) -> None:
    app = host(settings, config_with())
    write_model_table(app)
    caller, link, request = stand_in_of(app, proxy, tmp_path, preset_id)
    assert ("provider" in request) is (preset_id == "limited")
    assert (await caller(link, request)).text == "hello"
    # The retry for a model that takes only the newer name of the bound, which is a second shape of the same call.
    retried = {**{k: v for k, v in request.items() if k != "max_tokens"}, "max_completion_tokens": request["max_tokens"]}
    assert (await caller(link, retried)).text == "hello"


async def test_a_door_that_changes_the_call_is_refused_on_the_same_route(settings: Settings, proxy: Any, tmp_path: Path) -> None:
    app = host(settings, config_with())
    write_model_table(app)
    caller, link, request = stand_in_of(app, proxy, tmp_path, "limited")
    bare = {k: v for k, v in request.items() if k != "provider"}
    for taken in (bare, {**request, "model": "anthropic/a-dear-one"}, {**request, "provider": {"only": ["deepinfra"], "allow_fallbacks": False}}):
        with pytest.raises(ModelFailed, match="403"):
            await caller(link, taken)


async def test_a_link_on_a_preset_since_removed_stops_being_served(settings: Settings, proxy: Any, tmp_path: Path) -> None:
    """The cost of binding the model. A door cannot be told from a door that kept an old link, and the turn says it failed."""
    config = config_with()
    app = host(settings, config)
    write_model_table(app)
    caller, link, request = stand_in_of(app, proxy, tmp_path, "metered")
    del config.presets["metered"]
    write_model_table(app)
    with pytest.raises(ModelFailed, match="403"):
        await caller(link, request)


def test_the_table_is_a_whole_file_the_proxy_can_read_and_it_is_not_rewritten_for_nothing(settings: Settings, proxy: Any) -> None:
    app = host(settings, config_with())
    assert write_model_table(app) is True
    path = settings.state_dir / guest_door.MODEL_TABLE_NAME
    assert path == proxy.GUEST_MODELS and stat.S_IMODE(path.stat().st_mode) == 0o644, "the agent's container is root and the proxy is not"
    before = path.stat().st_mtime_ns, path.stat().st_ino
    assert write_model_table(app) is False
    assert (path.stat().st_mtime_ns, path.stat().st_ino) == before
    assert list(settings.state_dir.glob(".guest-models*")) == [], "nothing of the write is left beside it"
    assert proxy.guest_models()[("openrouter", LIMITED)] == frozenset(ROUTE)


def test_a_file_that_cannot_be_written_is_said_and_stands_as_it_was(settings: Settings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    def full(_path: Path, _data: bytes) -> None:
        raise OSError("no space left")

    monkeypatch.setattr(guest_door, "write_atomically", full)
    assert write_model_table(host(settings, config_with())) is True, "the table moved, so the door still hears"
    assert "could not be written" in caplog.text


class Door:
    configured = True

    def __init__(self) -> None:
        self.told: list[Any] = []

    async def call(self, method: str, path: str, **options: Any) -> dict[str, Any]:
        self.told.append(options["json"]["model_routes"])
        return {}


@pytest.fixture
def application(settings: Settings, proxy: Any) -> tuple[Application, Door]:
    built = Application(settings)
    built.config = config_with()
    built.manager = SimpleNamespace(providers=ProviderRegistry(settings, built.config), reload_config=lambda _config: None)  # type: ignore[assignment]
    door = Door()
    built.extensions[guest_door.EXTENSION_KEY] = GuestBridge(built, door)  # type: ignore[arg-type]
    return built, door


async def test_a_save_that_moves_a_model_reaches_the_proxy_and_the_door_at_once(application: tuple[Application, Door], proxy: Any) -> None:
    app, door = application
    new = app.config.model_copy(deep=True)
    new.presets["added"] = ModelPresetConfig(provider="openrouter", model="new/model")
    await app.save_config(new)
    assert proxy.guest_models()[("openrouter", "new/model")] == frozenset()
    assert len(door.told) == 1

    # Limited after the link was made: the call as it stood is refused from the next one, in both directions.
    narrowed = new.model_copy(deep=True)
    narrowed.presets["added"].openrouter_providers = ["wafer/us"]
    await app.save_config(narrowed)
    assert any(route["only"] == ["wafer/us"] for route in door.told[-1])
    body = {"model": "new/model", "messages": [], "max_tokens": 10}
    assert "limited" in proxy.guest_refusal("/openrouter/chat/completions", "POST", json.dumps(body).encode())
    within = {**body, "provider": {"only": ["wafer/us"], "allow_fallbacks": False}}
    assert proxy.guest_refusal("/openrouter/chat/completions", "POST", json.dumps(within).encode()) == ""

    del narrowed.presets["added"]
    await app.save_config(narrowed.model_copy(deep=True))
    assert "not one the app lists" in proxy.guest_refusal("/openrouter/chat/completions", "POST", json.dumps(within).encode())


async def test_a_save_of_something_else_leaves_the_door_alone(application: tuple[Application, Door]) -> None:
    app, door = application
    await app.save_config(app.config.model_copy(deep=True))
    door.told.clear()
    await app.save_config(app.config.model_copy(deep=True))
    assert door.told == []


async def test_installing_writes_the_table_whether_or_not_there_is_a_door(settings: Settings, proxy: Any) -> None:
    app = SimpleNamespace(**vars(host(settings, config_with())), notifications=None)
    app.manager = None
    assert await guest_door.install(app) == []
    assert proxy.guest_models()[("claude/v1", "claude-opus-4-5")] == frozenset()
