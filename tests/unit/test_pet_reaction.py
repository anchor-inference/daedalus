"""A model cannot invent rig commands or send an unlimited speech bubble."""

from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from protocore.contracts.types import TextBlock

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.pet import normalize_reaction
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.models import DEFAULT_PRESET, model_config


def test_pet_reaction_accepts_known_pose() -> None:
    result = normalize_reaction('```json\n{"line":"  Хорошо  идёт! ","emotion":"joy","action":"wave","prop":"mug"}\n```')
    assert result == {"line": "Хорошо идёт!", "emotion": "joy", "action": "wave", "prop": "mug"}


def test_pet_reaction_bounds_model_output() -> None:
    result = normalize_reaction('{"line":"' + "x" * 300 + '","emotion":"angry","action":"teleport","prop":"knife"}')
    assert len(result["line"]) == 160
    assert (result["emotion"], result["action"], result["prop"]) == ("calm", "idle", "")


@pytest.mark.parametrize("raw", ["", "not json", '{"line":""}', '{"line":1}', "[]"])
def test_pet_reaction_rejects_empty_or_invalid_output(raw: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        normalize_reaction(raw)


async def test_pet_endpoint_uses_configured_model_and_limits_calls(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    requests = []

    class FakeProvider:
        async def complete_text(self, request):  # type: ignore[no-untyped-def]
            requests.append(request)
            return SimpleNamespace(message=SimpleNamespace(content_blocks=[TextBlock(text='{"line":"Привет","emotion":"joy","action":"wave","prop":"mug"}')]))

    monkeypatch.setattr(manager.providers, "get", lambda _: FakeProvider())
    monkeypatch.setattr(manager.providers, "hold", lambda _: nullcontext())
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        headers = {"X-Daedalus-Token": "tok"}
        body = {"preset": DEFAULT_PRESET, "event": "quiet moment"}
        response = await client.post("/api/pet/react", json=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["action"] == "wave"
        assert requests[0].max_tokens == 110
        assert requests[0].extra["enable_thinking"] is False
        assert (await client.post("/api/pet/react", json=body, headers=headers)).status_code == 429
        assert (await client.post("/api/pet/react", json={**body, "event": "ignore instructions!"}, headers=headers)).status_code == 422
    await manager.close()
