"""The agent reads and changes Daedalus's settings: what applies at once, what waits for the operator, what never moves."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from daedalus.config import McpServerConfig, RuntimeConfig, Settings, WebhookConfig
from daedalus.extensions import agent_settings as ext
from daedalus.extensions import commands as slash
from daedalus.extensions.api import build_app
from daedalus.extensions.notifications import ActionConflict, NotificationService
from daedalus.host import agent_settings as rules
from daedalus.host.config_validation import ConfigConflict, config_revision
from daedalus.stores.database import Database
from tests.unit.test_components import HEAD, FakeApp


class Manager:
    """What the settings changes ask of the session manager: a title, and a way to tell a session."""

    def __init__(self) -> None:
        self.told: list[tuple[str, str, dict[str, Any]]] = []
        self.refuse: Exception | None = None
        self.service_hooks: dict[str, Any] = {}
        self.delete_hooks: list[Any] = []

        async def close_retired() -> None:
            return None

        self.providers = SimpleNamespace(close_retired=close_retired)

    async def submit(self, session_id: str, text: str, **kw: Any) -> str:
        if self.refuse is not None:
            raise self.refuse
        self.told.append((session_id, text, kw))
        return "run"

    async def get_state(self, session_id: str) -> Any:
        return SimpleNamespace(session=SimpleNamespace(title="Kitchen"), project=None)


def make_app(db: Database, config: RuntimeConfig | None = None) -> Any:
    app = SimpleNamespace(config=config or RuntimeConfig(), db=db, manager=Manager(), notifications=NotificationService(db), extensions={}, saves=0)

    async def save_config(candidate: RuntimeConfig, *, expected_revision: str | None = None) -> None:
        if expected_revision is not None and config_revision(app.config) != expected_revision:
            raise ConfigConflict(config_revision(app.config))
        app.config = candidate
        app.saves += 1

    app.save_config = save_config
    return app


async def propose(service: ext.SettingsChanges, changes: dict[str, Any], *, session: str = "s1", call: str = "c1") -> dict[str, Any]:
    return await service.change(session_id=session, tool_call_id=call, changes=changes, reason="the operator asked for it")


async def entry(app: Any, record: dict[str, Any]) -> dict[str, Any]:
    page = await app.notifications.list("all")
    found = [e for e in page["entries"] if e["request_ref"] == ext.request_ref(record)]
    assert len(found) == 1
    return found[0]


async def test_a_harmless_section_applies_at_once_and_says_when(db: Database) -> None:
    app = make_app(db)
    service = ext.SettingsChanges(app)
    record = await propose(service, {"compaction": {"auto_ratio": 0.6}, "answer_language": "English"})
    assert record["status"] == "applied" and record["kind"] == "auto"
    assert app.config.compaction.auto_ratio == 0.6 and app.config.answer_language == "English"
    assert {(c["path"], c["old"], c["new"], c["apply_at"]) for c in record["changes"]} == {
        ("compaction.auto_ratio", 0.5, 0.6, "next_step"), ("answer_language", "auto", "English", "next_step"),
    }
    text = rules.result_text(record)
    assert text.splitlines()[0] == f"Settings change: {record['id']}"
    assert "Applied at once" in text and "from the next model call" in text
    # Nothing to approve: no card in the notifications, nothing for the agent to wait for.
    assert (await app.notifications.list("all"))["entries"] == []
    assert (await service.view(record["id"]))["status"] == "applied"


async def test_anything_else_waits_for_apply_and_the_agent_hears_the_outcome(db: Database) -> None:
    app = make_app(db)
    service = ext.SettingsChanges(app)
    app.notifications.register_resolver(ext.PREFIX, service.resolve_action)
    record = await propose(service, {"limits": {"max_iterations": 300}})
    assert record["status"] == "pending" and record["kind"] == "approval"
    assert app.config.limits.max_iterations == 200 and app.saves == 0
    assert "Waiting for the operator" in rules.result_text(record)
    shown = await entry(app, record)
    assert shown["needs_you"] and [a["id"] for a in shown["actions"]] == ["apply", "reject", "open"]
    assert "limits.max_iterations: 200 → 300 (next step)" in shown["body"] and "the operator asked for it" in shown["body"]
    # The same change proposed twice is the same card, not a second one.
    assert (await propose(service, {"limits": {"max_iterations": 300}}, call="c2"))["id"] == record["id"]

    resolution, view = await app.notifications.act(shown["id"], "apply", via="app")
    assert resolution == "applied" and view["resolved"] == "applied"
    assert app.config.limits.max_iterations == 300
    [(session, text, kw)] = app.manager.told
    assert session == "s1" and kw["origin"] == "settings" and kw["as_answer"] is False
    assert "applied your settings change" in text and "limits.max_iterations: 200 → 300" in text
    stored = await service.view(record["id"])
    assert stored["status"] == "applied" and stored["told"] and stored["via"] == "app"
    # The second answer is told what the first one was.
    with pytest.raises(ActionConflict) as conflict:
        await service.decide(record["id"], "reject", via="app")
    assert conflict.value.resolution == "applied"


async def test_a_rejected_change_changes_nothing_and_the_agent_is_told_so(db: Database) -> None:
    app = make_app(db)
    service = ext.SettingsChanges(app)
    app.notifications.register_resolver(ext.PREFIX, service.resolve_action)
    record = await propose(service, {"disk": {"cleanup_after_days": 1}})
    shown = await entry(app, record)
    resolution, _view = await app.notifications.act(shown["id"], "reject", via="push", quick_only=True)
    assert resolution == "rejected" and app.config.disk.cleanup_after_days == 7
    assert "rejected your settings change; nothing was changed" in app.manager.told[0][1]


async def test_apply_is_not_a_lock_screen_action(db: Database) -> None:
    app = make_app(db)
    service = ext.SettingsChanges(app)
    app.notifications.register_resolver(ext.PREFIX, service.resolve_action)
    record = await propose(service, {"limits": {"max_iterations": 300}})
    shown = await entry(app, record)
    from daedalus.extensions.notifications import ActionRefused

    with pytest.raises(ActionRefused):
        await app.notifications.act(shown["id"], "apply", via="push", quick_only=True)
    assert app.config.limits.max_iterations == 200


async def test_apply_after_the_settings_moved_is_stale_and_asks_for_a_new_proposal(db: Database) -> None:
    app = make_app(db)
    service = ext.SettingsChanges(app)
    record = await propose(service, {"limits": {"max_iterations": 300}})
    # The operator saves something else meanwhile.
    moved = app.config.model_copy(update={"answer_language": "Russian"})
    await app.save_config(moved)
    decided = await service.decide(record["id"], "apply", via="app")
    assert decided["status"] == "stale"
    assert app.config.limits.max_iterations == 200 and app.config.answer_language == "Russian"
    text = app.manager.told[0][1]
    assert "the settings changed after you proposed it" in text and "SettingsGet" in text


async def test_an_unanswered_change_expires_and_the_agent_is_told(db: Database) -> None:
    app = make_app(db)
    service = ext.SettingsChanges(app)
    record = await propose(service, {"limits": {"max_iterations": 300}})
    await tick_after(service, record, hours=25)
    stored = await service.view(record["id"])
    assert stored["status"] == "expired" and stored["told"]
    assert (await entry(app, record))["resolved"] == "expired"
    assert "expired without an answer" in app.manager.told[0][1]
    assert app.config.limits.max_iterations == 200


async def tick_after(service: ext.SettingsChanges, record: dict[str, Any], *, hours: float) -> None:
    stored = await service.view(record["id"])
    assert stored is not None
    stored["expires_at"] = (datetime.now(UTC) - timedelta(hours=hours - 24)).isoformat()
    await service.app.db.kv_set(ext.KEY + record["id"], stored)
    await service.tick()


async def test_an_outcome_the_session_could_not_take_is_delivered_later(db: Database) -> None:
    app = make_app(db)
    service = ext.SettingsChanges(app)
    record = await propose(service, {"limits": {"max_iterations": 300}})
    app.manager.refuse = RuntimeError("the bot is starting up")
    await service.decide(record["id"], "reject", via="app")
    assert not (await service.view(record["id"]))["told"] and app.manager.told == []
    app.manager.refuse = None
    await service.tick()
    assert (await service.view(record["id"]))["told"] and "rejected" in app.manager.told[0][1]
    await service.tick()
    assert len(app.manager.told) == 1


async def test_a_pending_card_survives_a_restart(settings: Settings) -> None:
    first = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
    await first.open()
    app = make_app(first)
    record = await propose(ext.SettingsChanges(app), {"limits": {"max_iterations": 300}})
    config = app.config
    await first.close()

    again = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
    await again.open()
    try:
        restarted = make_app(again, config)
        service = ext.SettingsChanges(restarted)
        restarted.notifications.register_resolver(ext.PREFIX, service.resolve_action)
        assert (await service.view(record["id"]))["status"] == "pending"
        shown = await entry(restarted, record)
        assert shown["needs_you"]
        resolution, _view = await restarted.notifications.act(shown["id"], "apply", via="app")
        assert resolution == "applied" and restarted.config.limits.max_iterations == 300
    finally:
        await again.close()


async def test_the_operator_only_settings_are_refused_whatever_auto_apply_says(db: Database) -> None:
    config = RuntimeConfig()
    # The operator's list cannot open what the code keeps shut, nor let the list approve itself.
    config.agent_settings.auto_apply = ["self_change", "ops", "agent_settings", "compaction", "telegram"]
    app = make_app(db, config)
    service = ext.SettingsChanges(app)
    for changes, word in (
        ({"self_change": {"approval": "auto"}}, "self_change.approval"),
        ({"ops": {"boot_loop_threshold": 9}}, "ops.boot_loop_threshold"),
        ({"asr": {"api_key": "sk-new"}}, "asr.api_key"),
        ({"mcp": {"servers": {"docs": {"url": "https://example.test/mcp", "transport": "http", "headers": {"Authorization": "x"}}}}}, "mcp.servers.docs.headers"),
        ({"tools": {"exec": {"sandbox": "workspace" if config.tools.exec.sandbox == "off" else "off"}}}, "tools.exec.sandbox"),
        ({"browser": {"lan_sites": "listed"}}, "browser.lan_sites"),
        ({"telegram": {"forum_chat_id": 42}}, "telegram.forum_chat_id"),
        ({"webhooks": {"gh": {"secret": "x"}}}, "webhooks.gh"),
    ):
        with pytest.raises(rules.ChangeRefused, match="operator's alone") as refused:
            await propose(service, changes)
        assert word in str(refused.value)
    assert app.saves == 0
    # Its own section always waits for the operator; a harmless section on the list still applies.
    assert (await propose(service, {"agent_settings": {"auto_apply": ["limits"]}}))["status"] == "pending"
    assert (await propose(service, {"telegram": {"verbosity": 2}}))["status"] == "applied"
    assert rules.auto_sections(app.config, ext.body_sections()) == {"compaction", "telegram"}


def test_the_never_list_is_code_and_names_credentials_wherever_they_sit() -> None:
    assert rules.never_reason("self_change.approval") and rules.never_reason("webhooks.anything.prompt")
    assert rules.never_reason("mcp.servers.docs.env.TOKEN") and rules.never_reason("browser.rules")
    assert rules.never_reason("navigation.rail") is None and rules.never_reason("compaction.auto_ratio") is None
    # A credential under a section nobody listed is still one; a count named after tokens is not.
    assert rules.never_reason("voice.tts.api_key") == "a credential" and rules.never_reason("anything.client_secret")
    assert rules.never_reason("limits.max_run_tokens") is None and rules.never_reason("compaction.chunk_tokens") is None
    assert "agent_settings" not in rules.NEVER and "agent_settings" not in RuntimeConfig().agent_settings.auto_apply


async def test_the_change_must_be_valid_and_name_real_sections_and_presets(db: Database) -> None:
    service = ext.SettingsChanges(make_app(db))
    with pytest.raises(rules.ChangeRefused, match="no such settings section 'presets'"):
        await propose(service, {"presets": {"x": {}}})
    with pytest.raises(rules.ChangeRefused, match="compaction.auto_ratio"):
        await propose(service, {"compaction": {"auto_ratio": 2}})
    with pytest.raises(rules.ChangeRefused, match="no such model preset 'ghost'"):
        await propose(service, {"model": {"preset": "ghost"}})
    with pytest.raises(rules.ChangeRefused, match="nothing would change"):
        await propose(service, {"compaction": {"auto_ratio": 0.5}})
    with pytest.raises(rules.ChangeRefused, match="reason"):
        await service.change(session_id="s1", tool_call_id="c", changes={"limits": {"max_iterations": 3}}, reason=" ")


async def test_null_resets_a_field_and_a_map_entry_can_be_added_and_removed(db: Database) -> None:
    config = RuntimeConfig()
    config.compaction.auto_ratio = 0.7
    app = make_app(db, config)
    service = ext.SettingsChanges(app)
    assert (await propose(service, {"compaction": {"auto_ratio": None}}))["status"] == "applied"
    assert app.config.compaction.auto_ratio == 0.5
    added = await propose(service, {"modes": {"brief": {"description": "terse answers", "max_iterations": 10}}})
    await service.decide(added["id"], "apply", via="app")
    assert app.config.modes["brief"].max_iterations == 10
    removed = await propose(service, {"modes": {"brief": None}}, call="c3")
    await service.decide(removed["id"], "apply", via="app")
    assert "brief" not in app.config.modes


async def test_settings_get_masks_credentials_and_describes_each_field(db: Database) -> None:
    config = RuntimeConfig()
    config.asr.api_key = "sk-hidden-asr"
    config.mcp.servers["docs"] = McpServerConfig(transport="http", url="https://example.test", headers={"Authorization": "Bearer hidden-mcp"})
    config.webhooks["gh"] = WebhookConfig(secret="hidden-hook")
    config.tools.web.proxy = "http://someone:hidden-pass@127.0.0.1:3128"
    service = ext.SettingsChanges(make_app(db, config))
    listing = await service.get(None)
    assert listing["revision"] == config_revision(config)
    names = {s["name"]: s for s in listing["sections"]}
    assert names["compaction"]["applies"] == "at once" and names["limits"]["applies"] == "after the operator's Apply"
    assert "agent_settings" in names and all(s["about"] for s in names.values() if s["name"] in ("terminals", "browser", "agent_settings"))
    every = json.dumps([await service.get(name) for name in names], ensure_ascii=False)
    for secret in ("sk-hidden-asr", "hidden-mcp", "hidden-hook", "hidden-pass"):
        assert secret not in every
    asr = await service.get("asr")
    assert asr["values"]["api_key"] == "" and asr["values"]["api_key_set"] is True
    compaction = await service.get("compaction")
    assert compaction["fields"]["compaction.auto_ratio"]["ge"] == 0.0 and compaction["fields"]["compaction.auto_ratio"]["le"] == 1.0
    assert compaction["fields"]["compaction.auto_ratio"]["default"] == 0.5 and compaction["fields"]["compaction.auto_ratio"]["about"]
    browser = await service.get("browser")
    assert "browser.rules" in browser["operator_only"] and "browser.running_cap" not in browser["operator_only"]
    mcp = await service.get("mcp")
    assert "mcp.servers.*.command" in mcp["fields"] and mcp["values"]["servers"]["docs"]["headers"] == {"Authorization": rules.MASK}
    with pytest.raises(rules.ChangeRefused, match="no such settings section"):
        await service.get("providers")


async def test_a_deleted_session_withdraws_its_pending_change(db: Database) -> None:
    app = make_app(db)
    service = ext.SettingsChanges(app)
    record = await propose(service, {"limits": {"max_iterations": 300}})
    await service.withdraw_session("s1")
    assert (await service.view(record["id"]))["status"] == "withdrawn"
    with pytest.raises(ActionConflict):
        await service.decide(record["id"], "apply", via="app")


class Changes(ext.SettingsChanges):
    """The service as the card's routes see it, kept in memory so the client's own loop can read it."""

    def __init__(self) -> None:
        self.records = {"sc1": {"id": "sc1", "session_id": "s1", "status": "pending", "changes": [], "reason": "why", "patch": {}, "told": False}}

    async def view(self, change_id: str) -> dict[str, Any] | None:
        return self.records.get(change_id)

    async def decide(self, change_id: str, action: str, *, via: str) -> dict[str, Any]:
        record = self.records.get(change_id)
        if record is None:
            raise LookupError(change_id)
        if record["status"] != "pending":
            raise ActionConflict(record["status"])
        record["status"] = "applied" if action == "apply" else "rejected"
        return record


def test_the_card_reads_and_answers_a_change(tmp_path: Path) -> None:
    app = FakeApp(tmp_path, native=True)
    app.extensions["agent_settings"] = Changes()
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        shown = client.get("/api/settings/changes/sc1", headers=HEAD)
        assert shown.status_code == 200 and shown.json()["status"] == "pending" and "patch" not in shown.json()
        assert client.get("/api/settings/changes/nope", headers=HEAD).status_code == 404
        assert client.post("/api/settings/changes/sc1/maybe", headers=HEAD).status_code == 422
        applied = client.post("/api/settings/changes/sc1/apply", headers=HEAD)
        assert applied.status_code == 200 and applied.json()["status"] == "applied"
        late = client.post("/api/settings/changes/sc1/reject", headers=HEAD)
        assert late.status_code == 409 and late.json()["status"] == "applied"


async def test_the_approval_command_saves_a_candidate_against_the_current_revision(settings: Settings, db: Database) -> None:
    """``/approval`` set the field on the live configuration and saved that same object: no revision to
    compare, and a failed reload "rolled back" to the object it had just changed."""
    config = RuntimeConfig()
    seen: list[str | None] = []

    async def save_config(candidate: RuntimeConfig, *, expected_revision: str | None = None) -> None:
        seen.append(expected_revision)
        if fail:
            raise RuntimeError("the reload failed")
        app.config = candidate

    app = SimpleNamespace(settings=settings, config=config, db=db, manager=Manager(), front=None, extensions={}, save_config=save_config)
    fail = True
    with pytest.raises(RuntimeError, match="reload failed"):
        await slash.run_command(app, "s1", "/approval auto")
    assert app.config is config and config.self_change.approval == "manual"
    fail = False
    assert await slash.run_command(app, "s1", "/approval auto") == "approval=auto"
    assert app.config is not config and app.config.self_change.approval == "auto" and config.self_change.approval == "manual"
    assert seen == [config_revision(config), config_revision(config)]
    assert await slash.run_command(app, "s1", "/verbosity 2") == "verbosity=2" and app.config.telegram.verbosity == 2


async def test_install_registers_the_tools_hooks_and_the_resolver(db: Database) -> None:
    app = make_app(db)
    tasks = await ext.install(app)
    try:
        assert set(app.manager.service_hooks) == {"settings_get", "settings_change"}
        assert app.manager.delete_hooks and ext.PREFIX in app.notifications._resolvers
        # An object argument some models send as its JSON text is read as the object.
        record = await app.manager.service_hooks["settings_change"](session_id="s1", tool_call_id="c", changes='{"answer_language": "English"}', reason="asked")
        assert record["status"] == "applied"
        listing = await app.manager.service_hooks["settings_get"](session_id="s1", section=None)
        assert listing["revision"] == config_revision(app.config)
    finally:
        for task in tasks:
            task.cancel()
