"""A capped coordinator runs on an unpriced model and refuses only a rate card it cannot quote."""

from pathlib import Path

import httpx
import pytest

from daedalus.config import ModelPresetConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.providers.openai_compat import ProviderEndpoint
from daedalus.providers.pricing import BUILTIN, ModelPricing
from daedalus.stores.database import Database
from daedalus.stores.projects import ProjectError
from tests.support.models import DEFAULT_PRESET
from tests.unit.test_orchestrator import rig
from tests.unit.test_staff_runtime import close_team

INVALID = ModelPricing(input=-1, output=2, input_limit=100, limit_source="provider docs")
"""A priced rate card that cannot be quoted: the one thing a capped coordinator still refuses."""


@pytest.mark.parametrize("price", [None, ModelPricing(input=1, output=2),
                                  ModelPricing(input=1, output=2, input_limit=100),
                                  ModelPricing(input=None, output=2, input_limit=100, limit_source="provider docs")])
async def test_capped_coordinator_enables_on_an_unpriced_subscription_model(settings: Settings, db: Database,
                                                                           tmp_path: Path, price: ModelPricing | None) -> None:
    # Subscription logins publish no per-token price. Refusing them under any cap kept every such
    # coordinator from being enabled; it runs unreserved and its spend shows as unknown.
    r = await rig(settings, db, tmp_path)
    try:
        r.manager.config.limits.usd_per_run = 5.0
        r.manager.settings.usd_per_day = 3.0
        r.manager.config.presets["subscription"] = ModelPresetConfig(provider="proxy", model="claude")
        r.manager.config.orchestrator.preset = "subscription"
        r.provider.endpoint = ProviderEndpoint(id="proxy", kind="openai_compat", base_url="http://127.0.0.1:1",
                                              pricing={} if price is None else {"scripted-model": price})
        project = await r.orch.enable(r.project.id)
        assert project.settings.orchestrator.enabled and project.settings.orchestrator.session_id
        assert r.provider.requests == []
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_enable_refuses_an_invalid_rate_card_before_creating_office(settings: Settings, db: Database,
                                                                          tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        r.manager.config.presets["broken"] = ModelPresetConfig(provider="proxy", model="claude")
        r.manager.config.orchestrator.preset = "broken"
        r.provider.endpoint = ProviderEndpoint(id="proxy", kind="openai", base_url="http://127.0.0.1:1",
                                              pricing={"scripted-model": INVALID})
        r.team.app.bus = r.manager.bus
        api = build_app(r.team.app, "tok")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            preview = await client.post(f"/api/projects/{r.project.id}/orchestrator/preflight",
                                        json={"model": "", "concurrency_cap": 10}, headers={"X-Daedalus-Token": "tok"})
            assert preview.status_code == 400
            assert "invalid price" in preview.json()["detail"]
        assert not (await r.refreshed()).settings.orchestrator.enabled
        with pytest.raises(ProjectError, match="coordinator model 'broken'.*spending limits") as refused:
            await r.orch.enable(r.project.id)
        assert "Correct its price entry" in str(refused.value)
        project = await r.refreshed()
        assert not project.settings.orchestrator.enabled
        assert project.settings.orchestrator.session_id == ""
        assert await r.manager.projects.sessions_of(project.id) == []
        assert await r.manager.projects.journal(project.id) == []
        assert r.provider.requests == []
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_deepseek_enable_and_refused_model_change_preserve_chat(settings: Settings, db: Database,
                                                                    tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        r.manager.config.orchestrator.preset = DEFAULT_PRESET
        r.manager.config.model.preset = "openrouter.deepseek-v4-flash"
        r.provider.endpoint = ProviderEndpoint(id="deepseek", kind="deepseek", base_url="http://127.0.0.1:1",
                                              pricing=BUILTIN["deepseek"])
        r.manager.providers.rungs_for = lambda config, preset=None: [(r.provider, "deepseek-flash")]
        enabled = await r.orch.enable(r.project.id)
        sid = enabled.settings.orchestrator.session_id
        before = await r.manager.live.load(sid)
        r.manager.config.presets["subscription"] = ModelPresetConfig(provider="proxy", model="claude")
        app = r.team.app
        app.bus = r.manager.bus
        api = build_app(app, "tok")
        headers = {"X-Daedalus-Token": "tok"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            team = await client.get(f"/api/projects/{r.project.id}/staff", headers=headers)
            assert team.json()["choices"]["coordinator_default_preset"] == DEFAULT_PRESET
            assert team.json()["choices"]["default_preset"] != DEFAULT_PRESET
            rejected_controls = await client.post(f"/api/sessions/{sid}/model",
                                                   json={"preset": DEFAULT_PRESET, "provider": "missing"}, headers=headers)
            assert rejected_controls.status_code == 400
            assert (await r.refreshed()).settings.orchestrator.model == enabled.settings.orchestrator.model
            assert await r.manager.live.load(sid) == before
            r.provider.endpoint = ProviderEndpoint(id="proxy", kind="openai", base_url="http://127.0.0.1:1",
                                                  pricing={"deepseek-flash": INVALID})
            patched = await client.patch(f"/api/projects/{r.project.id}/orchestrator",
                                         json={"model": "subscription"}, headers=headers)
            assert patched.status_code == 400
            assert "invalid price" in patched.json()["detail"]
            chip = await client.post(f"/api/sessions/{sid}/model", json={"preset": "subscription"}, headers=headers)
            assert chip.status_code == 400
        await r.orch.update(r.project.id, concurrency_cap=8)
        project = await r.refreshed()
        assert project.settings.orchestrator.concurrency_cap == 8
        assert project.settings.orchestrator.session_id == sid
        assert project.settings.orchestrator.model == enabled.settings.orchestrator.model
        assert await r.manager.live.load(sid) == before
        assert r.provider.requests == []
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_enable_without_presets_names_the_missing_selection(settings: Settings, db: Database,
                                                                  tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        r.manager.config.presets = {}
        with pytest.raises(ProjectError, match="choose a coordinator model"):
            await r.orch.enable(r.project.id)
        assert not (await r.refreshed()).settings.orchestrator.enabled
        assert r.provider.requests == []
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_uncapped_coordinator_keeps_unpriced_model_behavior(settings: Settings, db: Database,
                                                                 tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        r.manager.settings.usd_per_day = 0
        r.manager.config.limits.usd_per_run = 0
        r.manager.config.limits.usd_total = 0
        r.manager.config.limits.usd_total_per_provider = {}
        r.provider.endpoint = ProviderEndpoint(id="proxy", kind="openai", base_url="http://127.0.0.1:1")
        project = await r.orch.enable(r.project.id)
        assert project.settings.orchestrator.enabled
        assert project.settings.orchestrator.session_id
        assert r.provider.requests == []
    finally:
        await close_team(r.manager)
        await r.manager.close()
