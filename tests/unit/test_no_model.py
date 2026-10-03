"""An installation with no model answers every way in, and never with a traceback.

A fresh install has an empty preset table (``RuntimeConfig.presets``): a provider endpoint is an
address, not a choice of model. Everything that would run a model — the chat commands, the HTTP API,
Telegram, a schedule, a subagent, the voice concierge, ``daedalus check``, the doctor — has to say so
in a sentence that names the fix. The parametrised test below is the guard: it walks every call site
of ``config.preset()`` with the table empty and asserts on what came back.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from aiogram.filters import CommandObject
from aiogram.types import Message

from daedalus.config import NO_MODEL_MESSAGE, NoModelConfigured, RuntimeConfig, Settings
from daedalus.doctor import DoctorContext, run_checks
from daedalus.extensions import commands as slash
from daedalus.extensions.api import build_app
from daedalus.extensions.notifications import NotificationService
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.transport.telegram.front import TelegramFront
from tests.unit.test_front import RecordingBot, _message

H = {"X-Daedalus-Token": "tok"}


class Install:
    """A running installation with no model in it, and the four ways in wired to the same config."""

    def __init__(self, settings: Settings, db: Database, manager: SessionManager, front: TelegramFront, client: httpx.AsyncClient) -> None:
        self.settings, self.db, self.manager, self.front, self.client = settings, db, manager, front, client
        self.config = manager.config
        self.app = SimpleNamespace(
            settings=settings,
            config=manager.config,
            db=db,
            manager=manager,
            front=None,
            extensions={},
            guard=None,
            save_config=self._save,
            create_session=manager.create_session,
        )
        self.app.notifications = NotificationService(db, manager.bus)

    async def _save(self, config: RuntimeConfig) -> None:
        self.app.config = config

    async def session(self) -> str:
        state = await self.manager.create_session("s")
        return state.session.id

    async def command(self, line: str) -> str:
        return await slash.run_command(self.app, await self.session(), line)


@pytest.fixture
async def install(settings: Settings, db: Database) -> Any:
    config = RuntimeConfig()
    assert config.has_model is False, "the shipped defaults must carry no model"
    config.telegram.inbound_merge_window_seconds = 0.01
    manager = SessionManager(settings, config, db=db)
    await manager.start()

    async def save(cfg: RuntimeConfig) -> None:
        return None

    front = TelegramFront(settings, config, manager, save_config=save)
    front.bot = RecordingBot()  # type: ignore[assignment]
    saved: list[RuntimeConfig] = []

    async def save_config(cfg: RuntimeConfig) -> None:
        saved.append(cfg)
        api_app.config = cfg
        manager.reload_config(cfg)

    api_app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session, save_config=save_config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(api_app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        yield Install(settings, db, manager, front, client)
    await manager.close()


# -- the chat commands, the API, Telegram, the doctor: one entry point each ------------------


async def chat_model(i: Install) -> str:
    return await i.command("/model")


async def chat_thinking(i: Install) -> str:
    return await i.command("/thinking on")


async def chat_settings(i: Install) -> str:
    return await i.command("/settings")


async def api_send_message(i: Install) -> str:
    response = await i.client.post(f"/api/sessions/{await i.session()}/messages", json={"text": "hello"}, headers=H)
    assert response.status_code == 409, response.text
    return response.json()["detail"]


async def api_upload(i: Install) -> str:
    response = await i.client.post(f"/api/sessions/{await i.session()}/upload", data={"text": "hello"}, headers=H)
    assert response.status_code == 409, response.text
    return response.json()["detail"]


async def api_onboarding(i: Install) -> str:
    response = await i.client.get("/api/onboarding", headers=H)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_model"] is False and body["presets"] == 0 and "model" in body["needs"]
    assert body["providers"], "the endpoints are configured even when no model is"
    return body["message"]


async def api_session_list(i: Install) -> str:
    await i.session()
    response = await i.client.get("/api/sessions", headers=H)
    assert response.status_code == 200, response.text
    # A list has nowhere to put a sentence: it says "no model" where a model name would go.
    assert response.json()["sessions"][0]["model"] == "no model"
    return NO_MODEL_MESSAGE


def _replying(text: str) -> tuple[Message, list[str]]:
    """A message whose ``answer`` is recorded instead of sent: the reply needs no bot behind it."""
    message = _message(text)
    said: list[str] = []

    async def answer(body: str, **_: Any) -> None:
        said.append(body)

    object.__setattr__(message, "answer", answer)
    return message, said


async def telegram_model(i: Install) -> str:
    message, said = _replying("/model")
    await i.front.cmd_model(message, CommandObject(prefix="/", command="model", args=None))
    return "\n".join(said)


async def telegram_thinking(i: Install) -> str:
    message, said = _replying("/thinking on")
    await i.front.cmd_thinking(message, CommandObject(prefix="/", command="thinking", args="on"))
    return "\n".join(said)


async def telegram_settings(i: Install) -> str:
    message, said = _replying("/settings")
    await i.front.cmd_settings(message)
    return "\n".join(said)


async def telegram_message(i: Install) -> str:
    """A plain message in the chat: the run cannot start, and the chat is told why, not a stack trace."""
    await i.front.on_message(_message("do some work"))
    await asyncio.sleep(0.2)
    return "\n".join(str(m["text"]) for m in i.front.bot.sent)  # type: ignore[attr-defined]


async def doctor(i: Install) -> str:
    context = DoctorContext(settings=i.settings, config=i.config, db=i.db, manager=i.manager, front=None, extensions={}, guard=None)
    checks = [c for c in await run_checks(context) if c.name == "default model"]
    assert len(checks) == 1 and checks[0].ok is False and checks[0].severity == "fail"
    return f"{checks[0].message} {checks[0].fix_hint}"


async def voice_concierge(i: Install) -> str:
    from daedalus.extensions.voice import Voice

    extension = Voice(i.app)
    with pytest.raises(NoModelConfigured) as raised:
        await extension.say("what is on today")
    return str(raised.value)


async def _operator_schedule(i: Install, cron: str, command_id: str) -> dict[str, Any]:
    from daedalus.extensions.recurring import Recurring
    from daedalus.stores.control import Principal

    recurring = Recurring(i.app)
    i.app.extensions["recurring"] = recurring
    revision = await i.db.fetchone("SELECT revision FROM domain_collection_revisions"
                                   " WHERE scope_kind='global' AND scope_id='global'")
    return await recurring.create(
        Principal("operator:1", "operator"), name="daily", prompt="check the mail",
        cron=cron, run_at=None, kind="agent", target_session=None, project_id=None,
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        expected_collection_revision=revision["revision"], client_operation_id=command_id,
    )


async def scheduled_run(i: Install) -> str:
    from daedalus.extensions.scheduler import Scheduler

    scheduler = Scheduler(i.app)
    await _operator_schedule(i, "0 9 * * *", "scheduled-run")
    row = dict((await i.db.fetchall("SELECT * FROM schedules"))[0])
    with pytest.raises(NoModelConfigured) as raised:
        await scheduler.fire(row)
    return str(raised.value)


async def scheduled_tick_on_a_modelless_install(i: Install) -> str:
    """A schedule that comes due here is skipped, not counted against — and not switched off.

    ``fire`` raising is the right answer to being called; the tick around it decides what that
    means. Counted as a start failure, a fresh install would lose every schedule it ships before
    anyone had configured a model, and adding one later would not bring them back.
    """
    from daedalus.extensions.scheduler import Scheduler

    scheduler = Scheduler(i.app)
    await _operator_schedule(i, "* * * * *", "scheduled-tick")
    await i.db.execute("UPDATE schedules SET next_run_at = ?", ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),))
    for _ in range(i.config.scheduler.max_failures + 1):
        await i.db.execute("UPDATE schedules SET next_run_at = ?", ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),))
        await scheduler.tick()
    row = dict((await i.db.fetchall("SELECT * FROM schedules"))[0])
    assert int(row["enabled"]) == 1, "the schedule was switched off for an installation that had no model"
    assert int(row["failure_count"]) == 0, "a missing model is not the schedule's failure"
    posted = [dict(r) for r in await i.db.fetchall("SELECT * FROM notifications WHERE kind = 'schedule_no_model'")]
    assert posted, "nothing told the operator the schedule was skipped"
    return str(posted[0]["body"])


async def subagent(i: Install) -> str:
    from daedalus.extensions.subagents import Subagents

    extension = Subagents(i.app)
    with pytest.raises(NoModelConfigured) as raised:
        await extension.spawn(leader_id=await i.session(), name="helper", task="look something up")
    return str(raised.value)


async def manager_submit(i: Install) -> str:
    with pytest.raises(NoModelConfigured) as raised:
        await i.manager.submit(await i.session(), "anything at all")
    return str(raised.value)


ENTRY_POINTS: dict[str, Callable[[Install], Awaitable[str]]] = {
    "chat /model": chat_model,
    "chat /thinking": chat_thinking,
    "chat /settings": chat_settings,
    "api send message": api_send_message,
    "api upload": api_upload,
    "api onboarding": api_onboarding,
    "api session list": api_session_list,
    "telegram /model": telegram_model,
    "telegram /thinking": telegram_thinking,
    "telegram /settings": telegram_settings,
    "telegram message": telegram_message,
    "doctor": doctor,
    "voice concierge": voice_concierge,
    "schedule": scheduled_run,
    "schedule tick": scheduled_tick_on_a_modelless_install,
    "subagent": subagent,
    "manager.submit": manager_submit,
}


@pytest.mark.parametrize("entry", list(ENTRY_POINTS), ids=list(ENTRY_POINTS))
async def test_every_entry_point_says_a_model_is_missing_and_where_to_add_one(install: Install, entry: str) -> None:
    said = await ENTRY_POINTS[entry](install)
    assert "no model" in said.lower(), said
    assert "Models" in said, f"{entry} does not say where to add one: {said}"
    assert "Traceback" not in said


async def test_check_reports_no_model_without_falling_over(install: Install, capsys: pytest.CaptureFixture[str]) -> None:
    """``daedalus check`` prints the state of an installation; an empty table is one of its states."""
    from daedalus import __main__ as cli

    args = SimpleNamespace(state_dir=str(install.settings.state_dir), workspaces_dir=str(install.settings.workspaces_dir))
    assert await cli.cmd_check(args) == 0
    printed = capsys.readouterr().out
    assert "model: none" in printed and "presets: (none)" in printed


async def test_the_first_model_added_becomes_the_default_and_runs(install: Install) -> None:
    response = await install.client.put("/api/presets/vllm.m1", json={"provider": "vllm", "model": "m1", "images": True}, headers=H)
    assert response.status_code == 200, response.text
    settings_view = response.json()
    assert settings_view["model"]["preset"] == "vllm.m1"
    assert settings_view["vision"]["preset"] == "vllm.m1"  # an image-capable first model is the eyes too
    onboarding = (await install.client.get("/api/onboarding", headers=H)).json()
    assert onboarding["has_model"] is True and "model" not in onboarding["needs"]
    # And the last model can be taken out again: no model is a state, not a broken config.
    assert (await install.client.delete("/api/presets/vllm.m1", headers=H)).status_code == 200
    assert (await install.client.get("/api/onboarding", headers=H)).json()["has_model"] is False
