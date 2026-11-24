"""A session can choose its own vision model, over the global ``[vision] preset``.

The vision model was one setting for every chat, so a conversation about screenshots of small print
could not have a stronger model look at them without changing it for every other chat too. These
tests hold the choice to winning wherever a session's pictures are looked at, to surviving a restart
and a cleared model, to falling back to the global one when its preset goes, and to the endpoint
refusing a preset that does not exist or does not take images.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import ModelPresetConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools import vision
from tests.support.models import DEFAULT_PRESET, FALLBACK_PRESET, VISION_PRESET, model_config

REAL_CLIENT = httpx.AsyncClient
SHARP = "openrouter.sharp-eyes"


def _config() -> Any:
    config = model_config()
    config.presets[SHARP] = ModelPresetConfig(provider="openrouter", model="sharp/eyes", label="Sharp eyes", images=True)
    return config


@pytest.fixture
async def stand(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, _config(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={"api_token": "tok"}, guard=None,
                          create_session=manager.create_session)
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test", headers={"X-Daedalus-Token": "tok"}) as http:
        yield SimpleNamespace(http=http, manager=manager, settings=settings, db=db)
    await manager.close()


async def test_the_session_choice_wins_over_the_global_one_for_every_way_a_picture_is_looked_at(stand: Any) -> None:
    manager = stand.manager
    chosen = await manager.create_session("small print")
    other = await manager.create_session("anything else")
    await manager.set_vision(chosen.session.id, SHARP)

    assert manager.vision_preset_for(chosen.session.id)[0] == SHARP
    assert manager.vision_preset_for(other.session.id)[0] == VISION_PRESET, "the choice is this session's alone"
    assert manager.vision_preset_for(None)[0] == VISION_PRESET, "a caller with no session looks with the global one"
    # The engine's tools (ImageView, BrowserLook, a page's look) read their route through the session's
    # services; a kept file read goes through ``vision_model`` with the session's id.
    assert chosen.services is not None and list(chosen.services.extra["vision"])[1] == "sharp/eyes"
    assert other.services is not None and list(other.services.extra["vision"])[1] == "qwen/qwen3.7-flash"
    assert manager.vision_model(chosen.session.id)[1] == "sharp/eyes"


async def test_a_kept_image_read_from_a_session_is_described_by_that_session_s_model(stand: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = stand.manager
    state = await manager.create_session("kept")
    await manager.set_vision(state.session.id, SHARP)
    asked: list[str] = []

    async def look(route: Any, _manager: Any, data: bytes, mime: str, task: str, **_: Any) -> tuple[str, str]:
        asked.append(route[1])
        return "a chart", route[1]

    monkeypatch.setattr(vision, "look", look)
    await vision.describe_kept(manager, b"\x89PNG\r\n\x1a\n", "image/png", "chart.png", session_id=state.session.id)
    await vision.describe_kept(manager, b"\x89PNG\r\n\x1a\n", "image/png", "chart.png")
    assert asked == ["sharp/eyes", "qwen/qwen3.7-flash"]


async def test_a_subagent_looks_with_its_leader_s_choice_unless_it_has_its_own(stand: Any) -> None:
    manager = stand.manager
    leader = await manager.create_session("leader")
    helper = await manager.create_session("helper", metadata={"subagent_of": leader.session.id})
    await manager.set_vision(leader.session.id, SHARP)
    assert manager.vision_preset_for(helper.session.id)[0] == SHARP
    await manager.set_vision(helper.session.id, FALLBACK_PRESET)
    assert manager.vision_preset_for(helper.session.id)[0] == FALLBACK_PRESET


async def test_the_choice_survives_a_restart_and_a_cleared_model(stand: Any) -> None:
    manager = stand.manager
    state = await manager.create_session("kept over a restart")
    session_id = state.session.id
    await manager.set_vision(session_id, SHARP)
    await manager.set_model(session_id, preset=FALLBACK_PRESET)
    await manager.set_model(session_id, clear=True)
    assert manager.vision_preset_for(session_id)[0] == SHARP, "clearing the session's model is not a choice of vision model"
    assert (await stand.db.fetchone("SELECT vision_preset FROM live_control WHERE session_id = ?", (session_id,)))["vision_preset"] == SHARP

    again = SessionManager(stand.settings, _config(), db=stand.db)
    await again.start()
    try:
        assert again.vision_preset_for(session_id)[0] == SHARP
    finally:
        await again.close()

    await manager.set_vision(session_id, "")
    assert manager.vision_preset_for(session_id)[0] == VISION_PRESET
    assert (await stand.db.fetchone("SELECT vision_preset FROM live_control WHERE session_id = ?", (session_id,)))["vision_preset"] is None


async def test_a_removed_or_blinded_preset_falls_back_to_the_global_one(stand: Any) -> None:
    manager = stand.manager
    state = await manager.create_session("gone")
    await manager.set_vision(state.session.id, SHARP)
    manager.config.presets[SHARP].images = False
    assert manager.vision_preset_for(state.session.id)[0] == VISION_PRESET
    del manager.config.presets[SHARP]
    assert manager.vision_preset_for(state.session.id)[0] == VISION_PRESET
    assert manager.vision_view(state.session.id)["vision_preset"] == "", "Details shows what the session really looks with"


async def test_the_endpoint_validates_the_preset_and_details_return_the_choice(stand: Any) -> None:
    http, manager = stand.http, stand.manager
    state = await manager.create_session("through the api")
    url = f"/api/sessions/{state.session.id}/vision"

    page = (await http.get(f"/api/sessions/{state.session.id}")).json()
    assert page["vision_preset"] == "" and page["vision_default"] == VISION_PRESET
    assert page["vision_default_label"] == "Qwen 3.7 Flash (vision)"

    missing = await http.post(url, json={"preset": "nowhere"})
    assert missing.status_code == 400 and "nowhere" in missing.text
    blind = await http.post(url, json={"preset": DEFAULT_PRESET})
    assert blind.status_code == 400 and "taking images" in blind.text
    assert (await http.post(url, json={"preset": 3})).status_code == 400
    assert (await http.post("/api/sessions/nobody/vision", json={"preset": SHARP})).status_code == 404
    assert manager.vision_preset_for(state.session.id)[0] == VISION_PRESET, "a refused choice writes nothing"

    chosen = await http.post(url, json={"preset": SHARP})
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["vision_preset"] == SHARP and chosen.json()["vision_label"] == "Sharp eyes"
    assert (await http.get(f"/api/sessions/{state.session.id}")).json()["vision_preset"] == SHARP
    assert (await http.post(url, json={"preset": ""})).json()["vision_preset"] == ""
