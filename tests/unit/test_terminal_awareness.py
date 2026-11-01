"""The status line's facts about a terminal: the branch of its folder (read in the background, at most
once per ten seconds, never holding a listing), the agent working in it, and the staff member's spend."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import TerminalsConfig
from daedalus.stores.database import Database
from daedalus.terminals.awareness import BranchCache, detect_agent, parse_status, staff_agent, usage_view
from daedalus.terminals.model import EnvUnavailable, ExecResult, Owner, TerminalSpec
from daedalus.terminals.owners import ManagerOwners
from daedalus.terminals.service import Terminals
from tests.support.fake_ptyd import FakePtyd
from tests.unit.test_terminals_service import FakeOwners

CLEAN = "# branch.oid 1a2b3c4d5e6f\n# branch.head main\n# branch.upstream origin/main\n# branch.ab +0 -0\n"
DIRTY = CLEAN + "1 .M N... 100644 100644 100644 aa bb src/app.py\n? notes.txt\n"


def result(stdout: str = "", exit_code: int = 0, timed_out: bool = False) -> ExecResult:
    return ExecResult(exit_code=exit_code, signal="", stdout=stdout, stderr="", truncated=False, timed_out=timed_out, duration_ms=1)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Git:
    """A scripted ``exec.run``: what each read answers, and every read it was asked for."""

    def __init__(self, answer: ExecResult | Exception | None = None, *, hold: asyncio.Event | None = None) -> None:
        self.answer = answer if answer is not None else result(CLEAN)
        self.hold = hold
        self.calls: list[tuple[str, list[str], str, float]] = []

    async def __call__(self, env: str, argv: list[str], cwd: str, timeout: float) -> ExecResult:
        self.calls.append((env, argv, cwd, timeout))
        if self.hold is not None:
            await self.hold.wait()
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


# -- reading git status --------------------------------------------------------------------------


def test_the_branch_and_the_dirty_flag_come_from_one_porcelain_status() -> None:
    assert parse_status(CLEAN) == {"branch": "main", "detached": False, "dirty": False}
    assert parse_status(DIRTY) == {"branch": "main", "detached": False, "dirty": True}
    assert parse_status("# branch.oid 1a2b3c4d5e6f\n# branch.head (detached)\n") == {"branch": "1a2b3c4", "detached": True, "dirty": False}
    # A repository with no commit yet is on the branch it will commit to.
    assert parse_status("# branch.oid (initial)\n# branch.head trunk\n") == {"branch": "trunk", "detached": False, "dirty": False}
    assert parse_status("") is None


async def test_the_first_look_answers_at_once_and_the_branch_arrives_in_the_background() -> None:
    git = Git(result(DIRTY))
    cache = BranchCache(git, clock=Clock())
    assert cache.peek("container", "/work/site") is None
    await cache.settle()
    assert cache.peek("container", "/work/site") == {"branch": "main", "detached": False, "dirty": True}
    env, argv, cwd, timeout = git.calls[0]
    assert (env, cwd, timeout) == ("container", "/work/site", 2.0)
    # Beside the operator's own git: no optional index lock, no hook or fsmonitor of the repository.
    assert argv[0] == "git" and "--no-optional-locks" in argv and argv.index("--no-optional-locks") < argv.index("status")
    assert "core.fsmonitor=false" in argv and "--porcelain=v2" in argv and "--branch" in argv


async def test_a_folder_is_read_at_most_once_per_ten_seconds() -> None:
    clock = Clock()
    git = Git()
    cache = BranchCache(git, clock=clock)
    cache.peek("container", "/work/site")
    await cache.settle()
    for _ in range(20):
        clock.now += 0.4
        cache.peek("container", "/work/site")
        await cache.settle()
    assert len(git.calls) == 1
    clock.now += 2.5
    git.answer = result("# branch.oid ff\n# branch.head feature\n")
    # The old answer is served while the new read runs, then the new one.
    assert cache.peek("container", "/work/site") == {"branch": "main", "detached": False, "dirty": False}
    await cache.settle()
    assert cache.peek("container", "/work/site") == {"branch": "feature", "detached": False, "dirty": False}
    assert len(git.calls) == 2
    # Two environments may share a path and not a repository.
    cache.peek("host", "/work/site")
    await cache.settle()
    assert [c[0] for c in git.calls] == ["container", "container", "host"]


async def test_a_slow_read_is_never_doubled_and_its_time_counts_from_the_start() -> None:
    clock = Clock()
    hold = asyncio.Event()
    git = Git(hold=hold)
    cache = BranchCache(git, clock=clock)
    cache.peek("container", "/big")
    await asyncio.sleep(0)
    clock.now += 30.0
    assert cache.peek("container", "/big") is None
    assert len(git.calls) == 1
    hold.set()
    await cache.settle()
    assert cache.peek("container", "/big") == {"branch": "main", "detached": False, "dirty": False}


async def test_a_read_that_hangs_is_given_up_and_leaves_the_branch_unknown() -> None:
    clock = Clock()
    git = Git(hold=asyncio.Event())  # never set: a daemon that stopped answering
    cache = BranchCache(git, clock=clock, timeout=0.01, grace=0.05)
    cache.peek("container", "/stuck")
    # The cache's own bound is the daemon's timeout and a grace past it.
    await asyncio.wait_for(cache.settle(), 5.0)
    assert cache.peek("container", "/stuck") is None
    clock.now += 10.0
    cache.peek("container", "/stuck")
    await asyncio.sleep(0)
    assert len(git.calls) == 2
    await cache.close()


@pytest.mark.parametrize("answer", [
    result("", exit_code=128),  # not a repository
    result(CLEAN, timed_out=True),  # a status the daemon cut off is not believed
    EnvUnavailable("the container terminal service is not available"),
    RuntimeError("anything else"),
])
async def test_a_failed_read_leaves_the_branch_absent(answer: Any) -> None:
    cache = BranchCache(Git(answer), clock=Clock())
    cache.peek("container", "/work/site")
    await cache.settle()
    assert cache.peek("container", "/work/site") is None


async def test_a_folder_that_is_no_absolute_path_is_never_read() -> None:
    git = Git()
    cache = BranchCache(git, clock=Clock())
    for folder in ("", "~", "work/site"):
        assert cache.peek("container", folder) is None
    assert cache.peek("host", "C:\\work\\site") is None
    await cache.settle()
    assert [c[2] for c in git.calls] == ["C:\\work\\site"]


async def test_the_cache_forgets_the_oldest_folder_past_its_limit() -> None:
    clock = Clock()
    git = Git()
    cache = BranchCache(git, clock=clock, limit=2)
    for folder in ("/a", "/b", "/c"):
        cache.peek("container", folder)
        await cache.settle()
    clock.now += 1.0
    cache.peek("container", "/a")  # forgotten, so read again
    cache.peek("container", "/c")  # remembered and fresh
    await cache.settle()
    assert [c[2] for c in git.calls] == ["/a", "/b", "/c", "/a"]


# -- agents --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("command", "kind", "model", "effort"), [
    ("claude", "claude", None, None),
    ("claude --model opus --dangerously-skip-permissions", "claude", "opus", None),
    ("ANTHROPIC_LOG=debug claude --model=sonnet", "claude", "sonnet", None),
    ("codex -m gpt-5.1-codex -c model_reasoning_effort=high", "codex", "gpt-5.1-codex", "high"),
    ("/usr/local/bin/opencode", "opencode", None, None),
    ("npx cursor-agent", "cursor", None, None),
    ("env FOO=1 grok --effort low", "grok", None, "low"),
])
def test_an_agent_typed_at_a_prompt_is_named_from_its_command_line(command: str, kind: str, model: str | None, effort: str | None) -> None:
    agent = detect_agent(command)
    assert agent is not None
    assert (agent["kind"], agent["model"], agent["effort"], agent["source"]) == (kind, model, effort, "detected")
    # Nothing reads its transcript: no state and no spend, rather than a guess.
    assert agent["state"] is None and agent["usage"] is None


@pytest.mark.parametrize("command", ["", "make test", "vim claude.md", "git commit -m 'claude'", "echo 'unterminated"])
def test_other_commands_are_no_agent(command: str) -> None:
    assert detect_agent(command) is None


def test_a_staff_members_agent_maps_its_status_and_spend() -> None:
    usage = {"input_tokens": 40000, "output_tokens": 8200, "cache_read_tokens": 30800, "cost_usd": 1.25, "window_used_pct": None, "source": "subscription",
             "context_tokens": 11000, "context_window": 249000, "windows": [{"minutes": 300, "used_pct": 42.0}], "replies": 3, "at": "2026-11-01T10:00:00Z"}
    agent = staff_agent(harness="claude", model="opus", effort="high", status="permission", waiting_for="bash: npm install", usage=usage)
    assert agent == {
        "kind": "claude", "source": "staff", "model": "opus", "effort": "high", "state": "waiting", "status": "permission", "waiting_for": "bash: npm install",
        "usage": {"input_tokens": 40000, "output_tokens": 8200, "cache_read_tokens": 30800, "context_tokens": 11000, "context_window": 249000,
                  "cost_usd": 1.25, "source": "subscription", "windows": [{"minutes": 300, "used_pct": 42.0}], "at": "2026-11-01T10:00:00Z"},
    }
    states = {s: staff_agent(harness="codex", model="", effort="", status=s, waiting_for="", usage=None)["state"]
              for s in ("starting", "working", "question", "turn_done_unseen", "idle", "no_signal", "suspended", "error", "something new")}
    assert states == {"starting": "working", "working": "working", "question": "waiting", "turn_done_unseen": "done", "idle": "idle",
                      "no_signal": "idle", "suspended": "idle", "error": "error", "something new": "idle"}
    # The CLI's own default model is no model name.
    assert staff_agent(harness="codex", model="", effort="", status="idle", waiting_for="", usage=None)["model"] is None


def test_a_spend_without_counts_is_no_spend_and_a_zero_is_unknown() -> None:
    assert usage_view(None) is None and usage_view({}) is None
    assert usage_view({"input_tokens": 0, "output_tokens": 0, "cost_usd": None, "source": "subscription"}) is None
    partial = usage_view({"context_tokens": 9000, "context_window": 0, "cache_read_tokens": "x"})
    assert partial is not None
    assert partial["context_tokens"] == 9000 and partial["context_window"] is None and partial["cache_read_tokens"] is None


async def test_the_owners_give_a_staff_terminal_its_agent_from_what_the_team_keeps(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('p1','Bakery','2026-11-01')")
    await db.execute("INSERT INTO staff(id,project_id,name,harness,model,effort,created_by,created_at) VALUES ('st-ira','p1','Ira','claude','opus','high','operator','2026-11-01')")
    usage = {"input_tokens": 1000, "output_tokens": 200, "cache_read_tokens": 600, "context_tokens": 900, "context_window": 200000, "cost_usd": 0.5, "source": "subscription"}
    await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,terminal_id,status,waiting_for,status_at,started_at,usage_json) VALUES "
                     "('ss1','st-ira','cli','tm-ira','permission','bash: rm -rf build','2026-11-01','2026-11-01',?)", (json.dumps(usage),))
    owners = ManagerOwners(SimpleNamespace(db=db, projects=SimpleNamespace(local_env="container")))  # type: ignore[arg-type]
    staffed = await owners.staffed(["tm-ira", "tm-none"])
    assert set(staffed) == {"tm-ira"}
    assert staffed["tm-ira"]["activity"] == {"status": "permission", "label": "bash: rm -rf build", "level": "warn",
                                             "action": {"kind": "answer", "label": "Answer", "path": "/app/project/p1/staff/st-ira"}}
    agent = staffed["tm-ira"]["agent"]
    assert (agent["kind"], agent["model"], agent["effort"], agent["state"]) == ("claude", "opus", "high", "waiting")
    assert agent["usage"]["cache_read_tokens"] == 600 and agent["usage"]["context_window"] == 200000


# -- the listing ---------------------------------------------------------------------------------


@pytest.fixture
def run_dir() -> Iterable[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))
    yield path / "run"
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def daemon(run_dir: Path) -> AsyncIterator[FakePtyd]:
    fake = await FakePtyd(run_dir).start()
    yield fake
    await fake.stop()


class StaffedOwners(FakeOwners):
    def __init__(self) -> None:
        super().__init__()
        self.staff: dict[str, dict[str, Any]] = {}

    async def staffed(self, terminal_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        return {t: self.staff[t] for t in terminal_ids if t in self.staff}


@pytest.fixture
async def service(db: Database, run_dir: Path, daemon: FakePtyd) -> AsyncIterator[tuple[Terminals, StaffedOwners]]:
    owners = StaffedOwners()
    made = Terminals(db, run_dirs={"container": run_dir, "host": None}, config=lambda: TerminalsConfig(running_cap=20), owners=owners)  # type: ignore[arg-type]
    await made.start()
    assert await made.wait_available("container")
    yield made, owners
    await made.close()


async def test_the_listing_carries_the_branch_without_waiting_for_git_and_without_an_audit_row(service: tuple[Terminals, StaffedOwners], daemon: FakePtyd, db: Database) -> None:
    terminals, _ = service
    daemon.exec_results["git"] = {"stdout": DIRTY}
    view = await terminals.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    audited = (await db.fetchone("SELECT count(*) AS n FROM terminal_audit"))["n"]
    assert view["git"] is None  # the create's own view started the read and did not wait for it
    await terminals.branches.settle()
    listed = (await terminals.list())[0]
    assert listed["id"] == view["id"] and listed["git"] == {"branch": "main", "detached": False, "dirty": True}
    runs = [p for m, p in daemon.calls if m == "exec.run"]
    assert len(runs) == 1 and runs[0]["cwd"] == "/tmp" and runs[0]["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert (await db.fetchone("SELECT count(*) AS n FROM terminal_audit"))["n"] == audited
    # An ended terminal has no folder to be in.
    await terminals.kill(view["id"])
    assert (await terminals.list())[0]["git"] is None


async def test_the_listing_names_a_staff_agent_and_an_agent_typed_in_a_shell(service: tuple[Terminals, StaffedOwners], daemon: FakePtyd) -> None:
    terminals, owners = service
    shell = await terminals.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    member = await terminals.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    agent = staff_agent(harness="codex", model="gpt-5.1-codex", effort="high", status="working", waiting_for="", usage=None)
    owners.staff[member["id"]] = {"activity": {"status": "working", "label": "", "level": "ok"}, "agent": agent}
    # The staff lookup is asked only for a staff member's terminal.
    assert all(v["agent"] is None for v in await terminals.list())
    await terminals.db.execute("UPDATE terminals SET owner_kind = 'staff', owner_id = 'st-1' WHERE id = ?", (member["id"],))
    by_id = {v["id"]: v for v in await terminals.list()}
    assert by_id[member["id"]]["agent"] == agent and by_id[member["id"]]["activity"]["status"] == "working"
    assert by_id[shell["id"]]["agent"] is None

    fake = daemon.terminals[shell["id"]]
    fake.busy = True
    fake.commands = [{"n": 1, "command": "claude --model opus", "cwd": "/tmp", "exit_code": None, "finished_at": None}]
    named = {v["id"]: v for v in await terminals.list()}[shell["id"]]["agent"]
    assert named is not None and (named["kind"], named["model"], named["source"]) == ("claude", "opus", "detected")
    asked = len([m for m, _ in daemon.calls if m == "terminal.commands"])
    for _ in range(3):
        await terminals.list()
    # Asked once per command, not once per listing.
    assert len([m for m, _ in daemon.calls if m == "terminal.commands"]) == asked
    fake.busy = False
    assert {v["id"]: v for v in await terminals.list()}[shell["id"]]["agent"] is None
