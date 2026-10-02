"""The harness manager against the fake CLIs in a live daemon double: what a check finds, updates
with their self-check, the refusals, installs and sign-in in a terminal, and the catalog of a folder.

Nothing touches the network: the release feeds are recorded bodies handed in as ``fetch``, the
installers run in a stand-in for the operator's terminal, and ``PATH`` holds only the fakes, so no
CLI or Node of the machine running the suite can answer in their place.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

import daedalus.harness.claude  # noqa: F401 — registers the adapters there are, whichever test ran first
import daedalus.harness.codex  # noqa: F401
import daedalus.harness.cursor  # noqa: F401
import daedalus.harness.opencode  # noqa: F401
from daedalus.config import HarnessConfig
from daedalus.harness.contract import CheckResult, CheckStep, EnvironmentPort
from daedalus.harness.manager import NODE, HarnessManager, HarnessRefused
from daedalus.harness.tools import NODE_VERSION, ClaudeTooling, npm_tags_url
from daedalus.host.events import EventBus
from daedalus.stores.database import Database
from daedalus.stores.harness import HarnessStore
from tests.support.harness_ports import Rig

CLAUDE_FEED = ClaudeTooling.RELEASES_URL

FAKE_NPM = '''
import json, os, sys
PACKAGES = {"@openai/codex": "codex", "opencode-ai": "opencode", "@earendil-works/pi-coding-agent": "pi"}
args = sys.argv[1:]
with open(os.path.join(os.environ["HOME"], "npm-calls.jsonl"), "a") as log:
    log.write(json.dumps(args) + "\\n")
if os.environ.get("FAKE_NPM_FAIL") == "1":
    print("npm ERR! code E500", file=sys.stderr)
    sys.exit(1)
spec = args[-1]
name, _, version = spec.rpartition("@")
state = os.path.join(os.environ["HOME"], ".fake-cli", PACKAGES[name])
os.makedirs(state, exist_ok=True)
with open(os.path.join(state, "version"), "w") as out:
    out.write(version + "\\n")
print("added 1 package")
'''


def script(path: Path, body: str) -> Path:
    """An executable at ``path``: ``body`` itself when it is a shell script, else Python code run by
    this interpreter from a file beside it."""
    if body.startswith("\n"):
        code = path.with_name(f".{path.name}.py")
        code.write_text(body)
        body = f"#!/bin/sh\nexec {sys.executable} {code} \"$@\"\n"
    path.write_text(body)
    path.chmod(0o755)
    return path


def feeds(bodies: Mapping[str, Any]) -> Callable[[str], Awaitable[str]]:
    async def fetch(url: str) -> str:
        if url not in bodies:
            raise ConnectionError(f"no recorded body for {url}")
        body = bodies[url]
        return body if isinstance(body, str) else json.dumps(body)

    return fetch


DEFAULT_FEEDS = {
    CLAUDE_FEED: "2.1.281",
    npm_tags_url("@openai/codex"): {"latest": "0.155.1"},
    npm_tags_url("opencode-ai"): {"latest": "1.18.23"},
    npm_tags_url("@earendil-works/pi-coding-agent"): {"latest": "0.84.2"},
}


class Bench:
    """A rig, a manager over it, and what the manager did: events, visible runs, npm calls."""

    def __init__(self, rig: Rig, db: Database, tools: Path) -> None:
        self.rig = rig
        self.db = db
        self.tools = tools
        self.live: list[dict[str, Any]] = []
        self.started: list[tuple[str, list[str], str]] = []
        self.on_start: Callable[[list[str]], None] | None = None
        self.exit_code: int | None = 0
        self.config = HarnessConfig()
        self.folders: dict[str, tuple[str, str]] = {}
        self.bus = EventBus(db)
        self.manager = self.make(DEFAULT_FEEDS)

    def make(self, bodies: Mapping[str, Any]) -> HarnessManager:
        bench = self

        class Runner:
            async def start(self, env: str, argv: list[str], *, title: str) -> str:
                bench.started.append((env, argv, title))
                if bench.on_start is not None:
                    bench.on_start(argv)
                return f"t{len(bench.started)}"

            async def wait(self, terminal_id: str, *, timeout: float) -> tuple[int | None, str]:
                return bench.exit_code, "the installer printed this\nlast line\n"

        async def live_staff(harness: str) -> list[dict[str, Any]]:
            return [s for s in self.live if s["harness"] == harness]

        async def folder(folder_id: str) -> tuple[str, str] | None:
            return self.folders.get(folder_id)

        def port(env: str) -> EnvironmentPort | None:
            return self.rig.env_port if env == "container" else None

        self.manager = HarnessManager(
            HarnessStore(self.db), ports=port, environments=lambda: ["container"], config=lambda: self.config, live_staff=live_staff,
            folder=folder, runner=Runner(), fetch=feeds(bodies), bus=self.bus,
        )
        return self.manager

    @property
    def home(self) -> Path:
        return self.rig.home

    async def settle(self) -> None:
        """Wait for everything the manager started in the background."""
        while self.manager._tasks:
            await asyncio.gather(*list(self.manager._tasks), return_exceptions=True)

    async def events(self, event_type: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT payload_json FROM app_events WHERE type = ? ORDER BY seq", (event_type,))
        return [json.loads(r["payload_json"]) for r in rows]

    def npm_calls(self) -> list[list[str]]:
        path = self.home / "npm-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def add_node(self, version: str = NODE_VERSION) -> None:
        script(self.tools / "node", f"\nprint('v{version}')\n")


@contextlib.asynccontextmanager
async def bench(db: Database, *, env: Mapping[str, str] | None = None, drop: tuple[str, ...] = (), npm: tuple[str, ...] = ()) -> AsyncIterator[Bench]:
    rig = Rig(extra_env=env)
    tools = rig.root / "tools"
    tools.mkdir()
    npm_bin = rig.home / ".npm-global" / "bin"
    npm_bin.mkdir(parents=True)
    for name in drop:
        (rig.bin / name).unlink()
    for name in npm:
        shutil.move(rig.bin / name, npm_bin / name)
    script(tools / "npm", FAKE_NPM)
    # Only the fakes' directories: /bin would bring the machine's own node, and /usr/bin its CLIs.
    rig.ptyd.base_env["PATH"] = f"{rig.bin}:{npm_bin}:{tools}"
    async with rig:
        yield Bench(rig, db, tools)


def by_harness(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["harness"]: r for r in rows}


async def test_a_check_finds_each_cli_with_its_version_sign_in_agents_and_models(db: Database) -> None:
    async with bench(db, env={"FAKE_CODEX_LOGGED_IN": "0"}) as b:
        agents = b.home / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "helper.md").write_text("---\nname: helper\ndescription: Does small things\nmodel: haiku\n---\nBody\n")
        (b.home / ".pi" / "agent").mkdir(parents=True)
        (b.home / ".pi" / "agent" / "settings.json").write_text(json.dumps({"defaultProvider": "anthropic"}))

        rows = by_harness(await b.manager.check("container"))
        assert list(rows) == ["claude", "codex", "opencode", "pi", "grok", "cursor"]
        claude = rows["claude"]
        assert (claude["installed"], claude["installed_version"], claude["latest_version"], claude["logged_in"], claude["login_detail"]) == (True, "2.1.281", "2.1.281", "yes", "claude.ai · max")
        assert claude["agents"] == [{"name": "helper", "source": "user", "description": "Does small things", "model": "haiku"}]
        assert claude["models"] == ["fable", "opus", "sonnet", "haiku"] and "acceptEdits" in claude["modes"]
        assert (claude["tested"], claude["supported"], claude["update_available"], claude["operation"]) == (True, True, False, None)
        assert claude["binary_path"] == str(b.rig.bin / "claude")
        # Claude Code has an adapter; installed and signed in, nothing holds it back.
        assert claude["unavailable"] == "", claude["unavailable"]

        assert (rows["codex"]["logged_in"], rows["codex"]["models"]) == ("no", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.5"])
        assert rows["codex"]["unavailable"] == "Codex is not signed in in the container environment"
        assert [a["name"] for a in rows["opencode"]["agents"]] == ["build", "plan", "general"]
        assert (rows["opencode"]["logged_in"], rows["opencode"]["login_detail"]) == ("yes", "Anthropic, OpenAI")
        assert (rows["pi"]["logged_in"], rows["pi"]["login_detail"], rows["pi"]["models"]) == ("yes", "anthropic · oauth", ["anthropic/claude-haiku-4", "openai/gpt-5-mini"])
        grok = rows["grok"]
        assert (grok["installed_version"], grok["install_method"], grok["latest_version"], grok["models"]) == ("1.0.40", "official", "1.0.40", ["grok-code-fast", "grok-4"])
        assert (grok["agents"][0]["name"], grok["agents"][0]["source"]) == ("general-purpose", "builtin")
        assert (grok["logged_in"], grok["login_detail"]) == ("yes", "grok.com")

        # The pinned Node is not there, so the npm CLIs could not be installed in the container.
        assert b.manager.node_state("container")["installed"] is False
        checks = await b.events("harness.check")
        assert sorted(e["harness"] for e in checks) == ["claude", "codex", "cursor", "grok", "opencode", "pi"]
        assert all(e["ok"] for e in checks if e["harness"] != "cursor")
        assert not next(e for e in checks if e["harness"] == "cursor")["installed"]

        form = await b.manager.catalog_view("container")
        assert (form["claude"]["logged_in"], form["codex"]["logged_in"], form["claude"]["version"]) == (True, False, "2.1.281")
        assert form["claude"]["agents"][0]["name"] == "helper"


async def test_a_missing_cli_and_a_release_feed_that_is_down(db: Database) -> None:
    async with bench(db, drop=("pi",)) as b:
        b.make({**DEFAULT_FEEDS, CLAUDE_FEED: "<html>down</html>"})
        rows = by_harness(await b.manager.check("container"))
        assert (rows["pi"]["installed"], rows["pi"]["error"], rows["pi"]["unavailable"]) == (False, "", "pi is not installed in the container environment")
        assert rows["pi"]["install_problem"] == "needs Node: install Node first"
        assert rows["claude"]["installed"] is True and "the latest version could not be learnt" in rows["claude"]["error"]
        # A feed that is down is a note on the row, not a failed check.
        assert rows["claude"]["installed_version"] == "2.1.281"
        assert await b.manager.hire_problem("container", "pi") == "pi is not installed in the container environment"
        assert await b.manager.hire_problem("container", "claude") == ""
        assert await b.manager.hire_problem("host", "pi") == ""


async def test_an_update_runs_the_updater_then_the_self_check_with_one_prompt_on_the_cheapest_model(db: Database) -> None:
    async with bench(db, env={"FAKE_CLAUDE_LATEST": "2.1.290"}) as b:
        b.make({**DEFAULT_FEEDS, CLAUDE_FEED: "2.1.290"})
        await b.manager.check("container", "claude")
        asked: list[tuple[str, bool]] = []

        async def session(env: EnvironmentPort, model: str, model_turn: bool) -> CheckResult:
            asked.append((model, model_turn))
            steps = (CheckStep("launch", True), CheckStep("ready", True), CheckStep("deliver", True), CheckStep("reply", True, "ready"), CheckStep("exit", True))
            return CheckResult(ok=True, steps=steps, version="2.1.290", duration_ms=5)

        b.manager.self_checks["claude"] = session
        row = by_harness(await b.manager.harnesses("container"))["claude"]
        assert (row["update_available"], row["latest_version"]) == (True, "2.1.290")

        outcome = await b.manager.update("container", "claude")
        assert (outcome["ok"], outcome["changed"], outcome["from"], outcome["to"], outcome["error"]) == (True, True, "2.1.281", "2.1.290", "")
        assert asked == [("haiku", True)]
        row = by_harness(await b.manager.harnesses("container"))["claude"]
        assert (row["installed_version"], row["update_available"], row["self_check_ok"]) == ("2.1.290", False, True)
        assert [s["name"] for s in row["self_check"]["steps"]] == ["version", "supported", "signin", "launch", "ready", "deliver", "reply", "exit"]
        assert "outside the tested versions" not in row["self_check"]["steps"][1]["detail"]
        updated = await b.events("harness.updated")
        assert updated == [{"env": "container", "harness": "claude", "kind": "update", "version": "2.1.290", "previous": "2.1.281", "ok": True, "changed": True, "check_ok": True}]
        assert b.manager.operations == {}

        # Nothing newer: nothing runs, and it is not reported as a change.
        again = await b.manager.update("container", "claude")
        assert (again["ok"], again["changed"], again["to"]) == (True, False, "2.1.290")
        assert asked == [("haiku", True)]

        # The operator may keep the prompt from being sent; the adapter is told so.
        b.config = HarnessConfig(self_check_model_turn=False)
        await b.manager.self_check("container", "claude")
        assert asked[-1] == ("haiku", False)


async def test_without_an_adapter_the_session_step_is_skipped_never_passed(db: Database) -> None:
    async with bench(db, env={"FAKE_PI_LATEST": "0.87.1"}) as b:
        b.make({**DEFAULT_FEEDS, npm_tags_url("@earendil-works/pi-coding-agent"): {"latest": "0.87.1"}})
        outcome = await b.manager.update("container", "pi")
        assert (outcome["ok"], outcome["to"]) == (True, "0.87.1")
        steps = {s["name"]: s for s in outcome["check"]["steps"]}
        assert steps["session"]["skipped"] is True and steps["session"]["ok"] is True
        assert "no adapter" in steps["session"]["detail"]
        stored = by_harness(await b.manager.harnesses("container"))["pi"]["self_check"]
        assert stored["steps"][-1] == {"name": "session", "ok": True, "skipped": True, "detail": "no adapter runs this CLI yet, so no session was started", "duration_ms": stored["steps"][-1]["duration_ms"]}


async def test_a_failing_self_check_blocks_launches_until_it_passes(db: Database) -> None:
    async with bench(db) as b:
        async def broken(env: EnvironmentPort, model: str, model_turn: bool) -> CheckResult:
            return CheckResult(ok=False, steps=(CheckStep("launch", True), CheckStep("hook", False, "no SessionStart within 30 s")), version="", duration_ms=1)

        b.manager.self_checks["claude"] = broken
        await b.manager.check("container", "claude")
        result = await b.manager.self_check("container", "claude")
        assert result.ok is False
        assert await b.manager.unavailable("container", "claude") == "Claude Code's last self-check failed at hook: no SessionStart within 30 s"


async def test_an_update_is_refused_while_staff_work_on_that_cli_in_that_environment(db: Database) -> None:
    async with bench(db, env={"FAKE_CLAUDE_LATEST": "2.1.290"}) as b:
        b.make({**DEFAULT_FEEDS, CLAUDE_FEED: "2.1.290"})
        b.live = [{"harness": "claude", "staff_id": "st-1", "name": "Ada", "project": "Bakery", "project_id": "p1", "env": "container", "staff_session_id": "ss-1", "status": "working"}]
        for _ in range(2):
            with pytest.raises(HarnessRefused) as refused:
                await b.manager.update("container", "claude")
            # Refused for the staff each time, never for an operation the first refusal left behind.
            assert refused.value.status == 409 and "Ada (Bakery)" in refused.value.message and refused.value.staff[0]["staff_id"] == "st-1"
        assert b.manager.operations == {}
        assert (await b.events("harness.updated")) == []
        # Staff on the same CLI in the other environment do not hold this one.
        b.live[0]["env"] = "host"
        assert (await b.manager.update("container", "claude"))["to"] == "2.1.290"

        # Update all goes on past the one it may not touch.
        b.live[0]["env"] = "container"
        b.make({**DEFAULT_FEEDS, CLAUDE_FEED: "2.1.299"})
        await b.manager.check("container", refresh_latest=True)
        results = await b.manager.update_all("container")
        assert [(r["harness"], r.get("refused")) for r in results] == [("claude", True)]


async def test_one_operation_at_a_time_per_cli(db: Database) -> None:
    async with bench(db, env={"FAKE_CLAUDE_LATEST": "2.1.290"}) as b:
        b.make({**DEFAULT_FEEDS, CLAUDE_FEED: "2.1.290"})
        first, second = await asyncio.gather(b.manager.update("container", "claude"), b.manager.update("container", "claude"), return_exceptions=True)
        assert isinstance(first, dict) and first["ok"] is True
        assert isinstance(second, HarnessRefused) and second.status == 409 and second.message == "Claude Code is already being updated in the container environment"


async def test_an_npm_cli_is_updated_through_npm_into_its_prefix_and_keeps_its_major(db: Database) -> None:
    async with bench(db, npm=("opencode",)) as b:
        b.make({**DEFAULT_FEEDS, npm_tags_url("opencode-ai"): {"latest": "2.0.3", "v1": "1.18.32", "next": "0.0.0-next-1"}})
        row = by_harness(await b.manager.check("container", "opencode"))["opencode"]
        assert (row["install_method"], row["latest_version"], row["update_available"]) == ("npm", "1.18.32", True)

        # npm needs Node, which the container does not have yet.
        refused = await b.manager.update("container", "opencode")
        assert (refused["ok"], refused["error"]) == (False, "npm needs Node, which is not installed in this environment: install Node first")
        assert b.npm_calls() == []

        b.add_node()
        outcome = await b.manager.update("container", "opencode")
        assert (outcome["ok"], outcome["to"]) == (True, "1.18.32"), outcome
        assert b.npm_calls() == [["install", "--global", "--no-fund", "--no-audit", "--prefix", str(b.home / ".npm-global"), "opencode-ai@1.18.32"]]


async def test_a_failed_updater_is_reported_with_its_words(db: Database) -> None:
    async with bench(db, npm=("codex",), env={"FAKE_NPM_FAIL": "1"}) as b:
        b.make({**DEFAULT_FEEDS, npm_tags_url("@openai/codex"): {"latest": "0.156.1"}})
        b.add_node()
        outcome = await b.manager.update("container", "codex")
        assert outcome["ok"] is False and outcome["error"].startswith("npm install --global failed: npm ERR! code E500")
        assert outcome["to"] == "0.155.1"
        assert (await b.events("harness.updated"))[-1]["error"] == outcome["error"]


async def test_an_untested_version_is_flagged_and_a_look_alike_grok_is_refused(db: Database) -> None:
    async with bench(db, env={"FAKE_CODEX_VERSION": "0.160.0"}) as b:
        script(b.rig.bin / "grok", "\nprint('grok-cli 0.3.1')\n")
        rows = by_harness(await b.manager.check("container"))
        codex = rows["codex"]
        assert (codex["installed"], codex["tested"], codex["supported"]) == (True, False, True)
        grok = rows["grok"]
        assert grok["installed"] is False and grok["error"] == "unsupported binary: 'grok-cli 0.3.1' is not Grok Build"
        b.config = HarnessConfig(allow_untested=False)
        assert await b.manager.unavailable("container", "codex") == "Codex 0.160.0 is outside the tested versions"


async def test_an_install_runs_in_a_terminal_and_is_checked_when_it_ends(db: Database) -> None:
    async with bench(db, drop=("pi",)) as b:
        claude = (b.rig.bin / "claude").read_text()
        (b.rig.bin / "claude").unlink()
        b.make({**DEFAULT_FEEDS, npm_tags_url("@earendil-works/pi-coding-agent"): {"latest": "0.87.1"}})

        # The installer puts the CLI on PATH, as the real one does.
        b.on_start = lambda argv: script(b.rig.bin / "claude", claude) and None
        started = await b.manager.install("container", "claude")
        assert started["operation"]["terminal_id"] == "t1" and started["operation"]["kind"] == "install"
        env, argv, title = b.started[0]
        assert (env, argv[:2], title) == ("container", ["sh", "-c"], "Installing Claude Code")
        assert "https://claude.ai/install.sh" in argv[2]
        with pytest.raises(HarnessRefused, match="already being installed"):
            await b.manager.install("container", "claude")
        await b.settle()
        row = by_harness(await b.manager.harnesses("container"))["claude"]
        assert (row["installed"], row["installed_version"], row["self_check_ok"]) == (True, "2.1.281", True)
        assert (await b.events("harness.updated"))[-1] == {"env": "container", "harness": "claude", "kind": "install", "version": "2.1.281", "previous": "", "ok": True, "terminal_id": "t1", "check_ok": True}
        with pytest.raises(HarnessRefused, match="already installed") as again:
            await b.manager.install("container", "claude")
        assert again.value.status == 409

        # An npm CLI needs the pinned Node first; then it goes into the container's own prefix.
        with pytest.raises(HarnessRefused, match="install Node first"):
            await b.manager.install("container", "pi")
        b.add_node()
        b.on_start = None
        b.exit_code = 1
        await b.manager.install("container", "pi")
        assert b.started[-1][1] == ["npm", "install", "--global", "--no-fund", "--no-audit", "--prefix", str(b.home / ".npm-global"), "@earendil-works/pi-coding-agent@0.87.1"]
        await b.settle()
        failed = (await b.events("harness.updated"))[-1]
        assert (failed["harness"], failed["ok"], failed["error"]) == ("pi", False, "the installer exited with 1: last line")
        assert b.manager.operations == {}


async def test_the_pinned_node_is_installed_into_the_container_only(db: Database) -> None:
    async with bench(db) as b:
        with pytest.raises(HarnessRefused, match="operator's own Node"):
            await b.manager.install_node("host")
        b.on_start = lambda argv: b.add_node()
        await b.manager.install_node("container")
        assert b.started[0][1][:2] == ["sh", "-c"] and "sha256sum -c -" in b.started[0][1][2]
        await b.settle()
        assert b.manager.node_state("container") | {"checked_at": None} == {"installed": True, "version": NODE_VERSION, "path": str(b.tools / "node"), "pinned": NODE_VERSION, "current": True, "checked_at": None}
        assert (await b.events("harness.updated"))[-1]["harness"] == NODE
        with pytest.raises(HarnessRefused, match="already installed"):
            await b.manager.install_node("container")


async def test_sign_in_opens_the_clis_own_sign_in_and_looks_again_afterwards(db: Database) -> None:
    async with bench(db, drop=("grok",)) as b:
        started = await b.manager.sign_in("container", "claude")
        assert started == {"terminal_id": "t1", "argv": ["claude", "auth", "login", "--claudeai"]}
        await b.settle()
        assert by_harness(await b.manager.harnesses("container"))["claude"]["logged_in"] == "yes"
        with pytest.raises(HarnessRefused, match="not installed"):
            await b.manager.sign_in("container", "grok")


async def test_a_folders_own_agents_come_first_in_its_catalog(db: Database) -> None:
    async with bench(db) as b:
        user = b.home / ".claude" / "agents"
        user.mkdir(parents=True)
        (user / "helper.md").write_text("---\nname: helper\ndescription: the user's helper\n---\n")
        (user / "planner.md").write_text("---\nname: planner\n---\n")
        project = b.rig.work / ".claude" / "agents"
        project.mkdir(parents=True)
        (project / "helper.md").write_text("---\nname: helper\ndescription: the project's helper\n---\n")
        (project / "reviewer.md").write_text("---\ndescription: no name, so the file's\n---\n")
        await b.manager.check("container", "claude")
        b.folders = {"f1": ("container", str(b.rig.work)), "f2": ("host", "/srv/elsewhere")}

        catalog = await b.manager.catalog("container", "claude", "f1")
        assert [(a.name, a.source, a.description) for a in catalog.agents] == [
            ("helper", "project", "the project's helper"), ("reviewer", "project", "no name, so the file's"), ("planner", "user", ""),
        ]
        assert "haiku" in catalog.models and catalog.modes[0] == "default"
        # A folder of the other environment, or none, is the user's list.
        assert [a.name for a in (await b.manager.catalog("container", "claude", "f2")).agents] == ["helper", "planner"]
        assert [a.name for a in (await b.manager.catalog("container", "claude")).agents] == ["helper", "planner"]
        form = await b.manager.catalog_view("container", "f1")
        assert [a["name"] for a in form["claude"]["agents"]] == ["helper", "reviewer", "planner"]


async def test_an_environment_that_cannot_be_reached_is_refused_before_anything_runs(db: Database) -> None:
    async with bench(db) as b:
        for call in (b.manager.check("host"), b.manager.update("host", "claude"), b.manager.install("host", "claude"), b.manager.sign_in("host", "claude")):
            with pytest.raises(HarnessRefused) as refused:
                await call
            assert refused.value.status == 503 and "host environment" in refused.value.message
        with pytest.raises(HarnessRefused, match="container or host"):
            await b.manager.check("moon")
        assert b.manager.operations == {}
        assert await b.manager.due("container") is True
        await b.manager.check("container")
        assert await b.manager.due("container") is False


async def test_every_tick_asks_for_each_environments_port_even_with_nothing_to_check(db: Database) -> None:
    """The port is what registers the transcript directories with the daemon; after a restart with a
    fresh catalog nothing is due, and the transcripts stayed unreadable until something else asked."""
    asked: list[str] = []

    def port(env: str) -> None:
        asked.append(env)

    manager = HarnessManager(HarnessStore(db), ports=port, environments=lambda: ["host", "container"], config=HarnessConfig, live_staff=lambda env, harness: [], folder=lambda folder_id: None)

    async def nothing_due(env: str) -> bool:
        return False

    manager.due = nothing_due  # type: ignore[method-assign]
    running = asyncio.create_task(manager.run())
    try:
        for _ in range(50):
            if asked:
                break
            await asyncio.sleep(0.01)
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running
    assert asked == ["host", "container"]
