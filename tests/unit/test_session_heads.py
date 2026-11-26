"""The branch and the repository of a chat working in a host folder come from git on the host, through
the terminal daemon, and a poll of the session never waits for it: what is known is served from
memory, a missing or stale answer is asked again in the background, one question at a time."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import session_heads
from daedalus.extensions.api import build_app
from daedalus.extensions.session_heads import HostHeads
from daedalus.host.git_branch import UNKNOWN, Head
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

H = {"X-Daedalus-Token": "tok"}


class Daemon:
    """The host's terminal daemon as far as these reads go: it answers what it is told to, when let."""

    def __init__(self, *, exit_code: int = 0, stdout: str = "main\n", stderr: str = "", timed_out: bool = False,
                 configured: bool = True) -> None:
        self.answer = SimpleNamespace(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out)
        self.calls: list[dict[str, Any]] = []
        self.let = asyncio.Event()
        self.let.set()
        self.envs = {"host"} if configured else set()
        self.fails: Exception | None = None

    def configured(self, env: str) -> bool:
        return env in self.envs

    async def exec_run(self, env: str, argv: list[str], **options: Any) -> Any:
        self.calls.append({"env": env, "argv": argv, **options})
        await self.let.wait()
        if self.fails is not None:
            raise self.fails
        return self.answer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_a_poll_never_waits_on_the_daemon_and_is_answered_once_it_has_spoken() -> None:
    daemon, clock = Daemon(), Clock()
    heads = HostHeads(lambda: daemon, clock=clock)
    daemon.let.clear()
    assert heads.peek("s1", "host", "/srv/shop") == UNKNOWN  # nothing known yet, and the poll did not wait
    assert heads.peek("s1", "host", "/srv/shop") == UNKNOWN
    await asyncio.sleep(0)
    assert len(daemon.calls) == 1, "one question in flight per session, however often it is polled"
    daemon.let.set()
    await heads.settled()
    assert heads.peek("s1", "host", "/srv/shop") == Head(True, "main")
    [call] = daemon.calls
    assert call["env"] == "host" and call["argv"][0] == "git" and call["argv"][-6:] == ["-C", "/srv/shop", "symbolic-ref", "--quiet", "--short", "HEAD"]
    # Bounded by the daemon's own timeout, and not an audit row every few seconds per open chat.
    assert call["timeout"] == session_heads.TIMEOUT_S and call["audit"] is False


async def test_an_answer_is_served_while_fresh_and_asked_again_in_the_background_when_stale() -> None:
    daemon, clock = Daemon(), Clock()
    heads = HostHeads(lambda: daemon, clock=clock)
    heads.peek("s1", "host", "/srv/shop")
    await heads.settled()
    clock.now += session_heads.FRESH_S - 1
    assert heads.peek("s1", "host", "/srv/shop") == Head(True, "main")
    await heads.settled()
    assert len(daemon.calls) == 1
    clock.now += 2
    daemon.answer.stdout = "topic\n"
    daemon.let.clear()
    assert heads.peek("s1", "host", "/srv/shop") == Head(True, "main"), "the stale answer is served while the new one is asked"
    daemon.let.set()
    await heads.settled()
    assert heads.peek("s1", "host", "/srv/shop") == Head(True, "topic") and len(daemon.calls) == 2


async def test_a_chat_that_moved_to_another_folder_is_not_answered_with_the_old_one() -> None:
    daemon = Daemon()
    heads = HostHeads(lambda: daemon, clock=Clock())
    heads.peek("s1", "host", "/srv/shop")
    await heads.settled()
    assert heads.peek("s1", "host", "/srv/other") == UNKNOWN
    await heads.settled()
    assert daemon.calls[-1]["argv"][-5] == "/srv/other"


@pytest.mark.parametrize(("answer", "head"), [
    ({"exit_code": 1, "stdout": ""}, Head(True, None)),  # a detached HEAD: a repository on no branch
    ({"exit_code": 128, "stdout": "", "stderr": "fatal: not a git repository (or any of the parent directories): .git"}, Head(False, None)),
    ({"exit_code": 128, "stdout": "", "stderr": "fatal: cannot change to '/srv/shop': No such file or directory"}, UNKNOWN),
    ({"exit_code": 128, "stdout": "", "stderr": "fatal: detected dubious ownership in repository"}, UNKNOWN),
    ({"exit_code": -1, "stdout": "", "timed_out": True}, UNKNOWN),
    ({"exit_code": 0, "stdout": "a\x00b\n"}, Head(True, None)),
])
async def test_what_git_said_is_read_and_nothing_else_is_guessed(answer: dict[str, Any], head: Head) -> None:
    daemon = Daemon(**answer)
    heads = HostHeads(lambda: daemon, clock=Clock())
    heads.peek("s1", "host", "/srv/shop")
    await heads.settled()
    assert heads.peek("s1", "host", "/srv/shop") == head


async def test_no_daemon_or_a_failing_one_is_not_known_and_is_not_asked_on_every_poll() -> None:
    clock = Clock()
    missing = Daemon(configured=False)
    heads = HostHeads(lambda: missing, clock=clock)
    heads.peek("s1", "host", "/srv/shop")
    await heads.settled()
    assert heads.peek("s1", "host", "/srv/shop") == UNKNOWN and missing.calls == []
    assert HostHeads(lambda: None, clock=clock).peek("s1", "host", "/srv/shop") == UNKNOWN
    broken = Daemon()
    broken.fails = ConnectionError("the host terminal bridge is not available")
    heads = HostHeads(lambda: broken, clock=clock)
    heads.peek("s1", "host", "/srv/shop")
    await heads.settled()
    heads.peek("s1", "host", "/srv/shop")
    await heads.settled()
    assert heads.peek("s1", "host", "/srv/shop") == UNKNOWN and len(broken.calls) == 1


async def test_the_table_keeps_the_sessions_asked_last(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_heads, "KEPT", 2)
    daemon = Daemon()
    heads = HostHeads(lambda: daemon, clock=Clock())
    for session in ("a", "b"):
        heads.peek(session, "host", "/srv/shop")
        await heads.settled()
    heads.peek("a", "host", "/srv/shop")  # asked again: now the most recent
    heads.peek("c", "host", "/srv/shop")
    await heads.settled()
    assert heads.peek("a", "host", "/srv/shop") == Head(True, "main")
    assert heads.peek("b", "host", "/srv/shop") == UNKNOWN


@pytest.fixture
async def manager(settings: Settings, db: Database) -> Any:
    made = SessionManager(settings, RuntimeConfig(), db=db)
    await made.start()
    yield made
    await made.close()


async def test_the_session_detail_of_a_host_folder_chat_reads_through_the_daemon(settings: Settings, db: Database, manager: SessionManager,
                                                                                 monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    daemon = Daemon(stdout="order-form\n")
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={"terminals": daemon},
                          guard=None, create_session=manager.create_session)
    api = build_app(app, "tok")  # type: ignore[arg-type]
    state = await manager.create_session("on the host")
    monkeypatch.setattr(manager, "env_of", lambda *_: "host")
    monkeypatch.setattr(manager, "folder_workspace", lambda *_: Path("/srv/host/shop"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        first = (await client.get(f"/api/sessions/{state.session.id}", headers=H)).json()
        assert first["branch"] is None and first["repository"] is None  # not known until the host has answered
        await api.state.host_heads.settled()
        later = (await client.get(f"/api/sessions/{state.session.id}", headers=H)).json()
    assert later["branch"] == "order-form" and later["repository"] is True
    assert [call["argv"][-5] for call in daemon.calls] == ["/srv/host/shop"]
