"""A tunnel that was raised anyway is shown to the operator: on the doctor's list, and once in the Inbox."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from daedalus import doctor
from daedalus.extensions import tunnel_watch
from daedalus.host import tunnels
from daedalus.host.notify_text import render


def _running(pid: int = 4121, *, kind: str = "ngrok", command: str = "ngrok http 3000") -> tunnels.Running:
    return tunnels.Running(pid, kind, command, 180.0)


async def test_the_doctor_lists_what_runs_and_says_when_it_cannot_look(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = doctor.DoctorContext(settings=None, config=None)  # type: ignore[arg-type]
    monkeypatch.setattr(tunnels, "running", lambda split=None: [])
    [clear] = await doctor._tunnels(ctx)
    assert clear.ok and clear.name == "tunnels"
    monkeypatch.setattr(tunnels, "running", lambda split=None: [_running()])
    [found] = await doctor._tunnels(ctx)
    assert not found.ok and found.severity == "warn" and "ngrok · pid 4121 · running 3 min · ngrok http 3000" in found.message
    assert "settings" in found.fix_hint and "kill" in found.fix_hint
    monkeypatch.setattr(tunnels, "running", lambda split=None: [_running(n) for n in range(1, 9)])
    [many] = await doctor._tunnels(ctx)
    assert many.message.startswith("8 running") and "and 3 more" in many.message
    monkeypatch.setattr(tunnels, "running", lambda split=None: None)
    [unknown] = await doctor._tunnels(ctx)
    assert unknown.ok and unknown.severity == "info"


class Inbox:
    def __init__(self, *, fails: bool = False) -> None:
        self.drafts: list[object] = []
        self.fails = fails

    def language(self) -> str:
        return "en"

    async def post(self, draft: object) -> None:
        if self.fails:
            raise RuntimeError("the inbox is down")
        self.drafts.append(draft)


def _watch(monkeypatch: pytest.MonkeyPatch, inbox: Inbox | None) -> tuple[tunnel_watch.TunnelWatch, list]:
    seen: list = []
    monkeypatch.setattr(tunnels, "running", lambda split=None: list(seen))
    return tunnel_watch.TunnelWatch(SimpleNamespace(notifications=inbox)), seen  # type: ignore[arg-type]


async def test_a_new_tunnel_is_told_once_and_again_after_it_has_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = Inbox()
    watch, seen = _watch(monkeypatch, inbox)
    assert await watch.check() == [] and inbox.drafts == []
    seen.append(_running())
    await watch.check()
    await watch.check()  # still there: not told again
    assert len(inbox.drafts) == 1
    draft = inbox.drafts[0]
    assert draft.category == "system" and draft.tone == "warning" and draft.link == "/app/health"  # type: ignore[attr-defined]
    assert draft.title == "A tunnel is running on this machine"  # type: ignore[attr-defined]
    assert "pid 4121" in draft.body and "ngrok http 3000" in draft.body and "settings" in draft.body  # type: ignore[attr-defined]
    seen.append(_running(4122, kind="ssh -R", command="ssh -N -R 80:localhost:3000 serveo.net"))
    await watch.check()
    assert len(inbox.drafts) == 2 and "pid 4122" in inbox.drafts[1].body and "pid 4121" not in inbox.drafts[1].body  # type: ignore[attr-defined]
    seen.clear()
    await watch.check()
    seen.append(_running())
    await watch.check()
    assert len(inbox.drafts) == 3


async def test_two_new_tunnels_are_one_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = Inbox()
    watch, seen = _watch(monkeypatch, inbox)
    seen.extend([_running(1), _running(2)])
    await watch.check()
    assert len(inbox.drafts) == 1 and "pid 1" in inbox.drafts[0].body and "pid 2" in inbox.drafts[0].body  # type: ignore[attr-defined]


async def test_a_platform_that_cannot_be_looked_at_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = Inbox()
    watch = tunnel_watch.TunnelWatch(SimpleNamespace(notifications=inbox))  # type: ignore[arg-type]
    monkeypatch.setattr(tunnels, "running", lambda split=None: None)
    assert await watch.check() == [] and inbox.drafts == []


async def test_a_failed_notice_or_no_inbox_does_not_stop_the_watch(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    watch, seen = _watch(monkeypatch, Inbox(fails=True))
    seen.append(_running())
    assert len(await watch.check()) == 1  # the notice failed, the sighting is still returned
    assert "could not post its notice" in caplog.text
    bare, seen = _watch(monkeypatch, None)
    seen.append(_running())
    assert len(await bare.check()) == 1


async def test_the_watch_is_installed_as_a_task_and_ends_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    app = SimpleNamespace(notifications=None, extensions={})
    [task] = await tunnel_watch.install(app)  # type: ignore[arg-type]
    try:
        assert isinstance(app.extensions["tunnel_watch"], tunnel_watch.TunnelWatch) and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_the_notice_is_written_in_both_languages() -> None:
    for language, word in (("en", "settings"), ("ru", "настройках")):
        assert render("tunnel.running", language)
        assert word in render("tunnel.running.body", language, processes="- ngrok · pid 1")
