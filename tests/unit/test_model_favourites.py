"""A model can be starred, and the star is the operator's: it is written through the preset's own route
and read back from the model list, without touching anything else about the preset."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from daedalus.config import ModelPresetConfig, RuntimeConfig, Settings
from daedalus.extensions.api import build_app

HEADERS = {"X-Daedalus-Token": "tok"}


class _Providers:
    async def close_retired(self) -> None:
        return None

    def price_view(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {}

    def available(self) -> list[str]:
        return []


class _Manager:
    providers = _Providers()

    async def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return []


class _App:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config = RuntimeConfig()
        self.manager = _Manager()
        self.front: Any = None

    async def save_config(self, config: RuntimeConfig) -> None:
        self.config = config


def _client(settings: Settings) -> tuple[TestClient, _App]:
    app = _App(settings)
    return TestClient(build_app(app, "tok")), app  # type: ignore[arg-type]


def _two_models(client: TestClient) -> None:
    client.put("/api/providers/me", json={"kind": "vllm", "base_url": "http://127.0.0.1:9/v1"}, headers=HEADERS)
    assert client.put("/api/presets/me.a", json={"provider": "me", "model": "a", "label": "Alpha"}, headers=HEADERS).status_code == 200
    assert client.put("/api/presets/me.b", json={"provider": "me", "model": "b", "label": "Beta"}, headers=HEADERS).status_code == 200


def _listed(client: TestClient) -> dict[str, bool]:
    body = client.get("/api/models", headers=HEADERS).json()
    return {entry["id"]: entry["favourite"] for entry in body["presets"]}


def test_a_model_is_not_a_favourite_until_it_is_starred(settings: Settings) -> None:
    assert ModelPresetConfig().favourite is False
    client, app = _client(settings)
    _two_models(client)
    assert _listed(client) == {"me.a": False, "me.b": False}
    assert app.config.presets["me.a"].favourite is False


def test_the_star_is_written_through_the_preset_route_and_read_from_the_model_list(settings: Settings) -> None:
    client, app = _client(settings)
    _two_models(client)
    reply = client.put("/api/presets/me.b", json={"favourite": True}, headers=HEADERS)
    assert reply.status_code == 200
    assert reply.json()["presets"]["me.b"]["favourite"] is True
    assert _listed(client) == {"me.a": False, "me.b": True}
    # A star changes nothing else about the model.
    assert app.config.presets["me.b"].label == "Beta" and app.config.presets["me.b"].model == "b"
    client.put("/api/presets/me.b", json={"favourite": False}, headers=HEADERS)
    assert _listed(client) == {"me.a": False, "me.b": False}


def test_editing_a_model_leaves_its_star_alone(settings: Settings) -> None:
    client, app = _client(settings)
    _two_models(client)
    client.put("/api/presets/me.a", json={"favourite": True}, headers=HEADERS)
    client.put("/api/presets/me.a", json={"label": "Alpha two", "thinking": False}, headers=HEADERS)
    assert app.config.presets["me.a"].favourite is True and app.config.presets["me.a"].label == "Alpha two"


def test_a_star_that_is_not_a_boolean_is_refused(settings: Settings) -> None:
    client, _ = _client(settings)
    _two_models(client)
    assert client.put("/api/presets/me.a", json={"favourite": "yes please"}, headers=HEADERS).status_code == 422
