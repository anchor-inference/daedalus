"""The browser service against an in-process daemon: the mirror, the owners, reconcile, control and
its one wake, the cap queue, the audit and the load."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest

from daedalus.browser.gateway import check_frame
from daedalus.browser.model import BrowserGone, EnvUnavailable, NotFound, OverCap, Owner, group_id, profile_id
from daedalus.browser.service import Browsers
from daedalus.config import BrowserConfig
from daedalus.gateway import RelayResult
from daedalus.stores.database import Database
from tests.support.fake_browserd import FakeBrowserd


async def wait_until(check: Any, expected: Any, *, timeout: float = 30.0) -> None:
    last = None
    try:
        async with asyncio.timeout(timeout):
            while (last := await check()) != expected:
                await asyncio.sleep(0.01)
    except TimeoutError:
        raise AssertionError(f"waited {timeout:.0f}s for {expected!r}; last saw {last!r}") from None


class FakeOwners:
    def __init__(self) -> None:
        self.existing: dict[tuple[str, str], str] = {}

    def add(self, owner: Owner, label: str = "") -> Owner:
        self.existing[(owner.kind, owner.id)] = label or f"{owner.kind} {owner.id}"
        return owner

    async def exists(self, owner: Owner) -> bool:
        return (owner.kind, owner.id) in self.existing

    async def label(self, owner: Owner) -> str:
        return self.existing.get((owner.kind, owner.id), owner.id)


class FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def publish(self, event_type: str, payload: dict[str, Any], **ids: Any) -> None:
        self.published.append((event_type, dict(payload), ids))

    def of(self, event_type: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return [(p, i) for t, p, i in self.published if t == event_type]


@pytest.fixture
def run_dir() -> Iterable[Path]:
    # A unix socket path is limited to about a hundred bytes; pytest's temporary directories can pass it.
    path = Path(tempfile.mkdtemp(prefix="bd-"))
    yield path / "run"
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def daemon(run_dir: Path) -> AsyncIterator[FakeBrowserd]:
    fake = await FakeBrowserd(run_dir).start()
    yield fake
    await fake.stop()


@pytest.fixture
def owners() -> FakeOwners:
    return FakeOwners()


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def cfg() -> BrowserConfig:
    return BrowserConfig(agent_wait_seconds=5, control_wait_seconds=0.2)


@pytest.fixture
def woken() -> list[tuple[Owner, str]]:
    return []


@pytest.fixture
async def service(db: Database, run_dir: Path, owners: FakeOwners, bus: FakeBus, cfg: BrowserConfig, daemon: FakeBrowserd, woken: list[tuple[Owner, str]]) -> AsyncIterator[Browsers]:
    async def wake(owner: Owner, text: str) -> None:
        woken.append((owner, text))

    made = Browsers(db, run_dirs={"container": run_dir, "host": None}, config=lambda: cfg, owners=owners, bus=bus, wake=wake)  # type: ignore[arg-type]
    await made.start()
    assert await made.wait_available("container")
    yield made
    await made.close()


SESSION = Owner("session", "sess1", project_id="proj1", session_id="sess1")


async def test_opening_mirrors_the_group_and_announces_it_once(service: Browsers, owners: FakeOwners, bus: FakeBus, daemon: FakeBrowserd) -> None:
    owners.add(SESSION, "Research")
    opened = await service.open(SESSION, url="https://example.test/", actor="agent:sess1")
    group = opened["group"]
    assert opened["created"] and group["id"] == "s-sess1" and group["profile"] == "project-proj1" and group["owner"]["label"] == "Research"
    assert daemon.groups["s-sess1"].labels == {"owner_kind": "session", "owner_id": "sess1", "project_id": "proj1", "session_id": "sess1"}
    # The same owner opening again gets the same group, and nobody is told twice.
    again = await service.open(SESSION, actor="agent:sess1")
    assert not again["created"] and again["group"]["id"] == group["id"]
    announced = bus.of("browser.opened")
    assert len(announced) == 1 and announced[0][1] == {"project_id": "proj1", "session_id": "sess1", "staff_id": None}
    assert (await service.profiles())[0]["id"] == "project-proj1"
    # A throwaway context is a second group with the reserved profile.
    fresh = await service.open(SESSION, fresh=True, actor="agent:sess1")
    assert fresh["group"]["id"] == "s-sess1-x" and fresh["group"]["profile"] == "ephemeral" and fresh["group"]["fresh"]
    assert group_id(SESSION) == "s-sess1" and profile_id(Owner("staff", "m1")) == "staff-m1"


async def test_an_unknown_owner_and_a_missing_environment_are_refused(service: Browsers, owners: FakeOwners, cfg: BrowserConfig) -> None:
    with pytest.raises(NotFound):
        await service.open(Owner("session", "ghost"), actor="agent:ghost")
    owners.add(SESSION)
    cfg.env = "host"
    with pytest.raises(EnvUnavailable):
        await service.open(SESSION, actor="agent:sess1")


async def test_the_cap_makes_an_agent_wait_in_line_and_refuses_the_operator(service: Browsers, owners: FakeOwners, daemon: FakeBrowserd, cfg: BrowserConfig) -> None:
    daemon.max_browsers = 1
    first, second = owners.add(Owner("session", "a1")), owners.add(Owner("session", "b2"))
    await service.open(first, actor="agent:a1")
    with pytest.raises(OverCap):
        await service.open(second, actor="operator")
    waiting = asyncio.create_task(service.open(second, actor="agent:b2"))
    await wait_until(lambda: _async(len(service.queue())), 1)
    await service.close_group("s-a1", actor="agent:a1")
    opened = await asyncio.wait_for(waiting, 10)
    assert opened["created"] and service.queue() == []
    # Nobody closes a browser: the next one waits its whole wait and is told what to do.
    cfg.agent_wait_seconds = 0.3
    with pytest.raises(OverCap) as refused:
        await service.open(first, actor="agent:a1")
    assert "BrowserClose" in refused.value.message


async def _async(value: Any) -> Any:
    return value


async def test_a_give_back_wakes_the_owner_exactly_once_with_the_note(service: Browsers, owners: FakeOwners, bus: FakeBus, daemon: FakeBrowserd, woken: list[tuple[Owner, str]]) -> None:
    owners.add(SESSION, "Research")
    await service.open(SESSION, url="https://shop.test/", actor="agent:sess1")
    await service.control("s-sess1", "human", client_id="v1")
    await wait_until(lambda: _control(service), "human")
    await service.control("s-sess1", "agent", note="I signed in; go on.")
    await wait_until(lambda: _async(len(bus.of("browser.returned"))), 1)
    await asyncio.sleep(0.1)
    assert len(woken) == 1 and woken[0][0].session_id == "sess1"
    assert "gave the browser back" in woken[0][1] and "I signed in; go on." in woken[0][1] and "shop.test" in woken[0][1]
    assert bus.of("browser.returned")[0][0]["note"] == "I signed in; go on."
    # The hold running out gives it back as well, and that too is told once.
    daemon.set_control("s-sess1", "human", "v1")
    await wait_until(lambda: _control(service), "human")
    daemon.set_control("s-sess1", "agent")
    await wait_until(lambda: _async(len(woken)), 2)
    await asyncio.sleep(0.1)
    assert len(woken) == 2 and "Their note" not in woken[1][1]
    # Giving back what the agent already holds wakes nobody.
    await service.control("s-sess1", "agent")
    await asyncio.sleep(0.1)
    assert len(woken) == 2
    audit = [e["action"] for e in await service.audit_log("s-sess1")]
    assert audit.count("take") == 1 and audit.count("give") == 2


async def _control(service: Browsers) -> str:
    return str((await service.get("s-sess1"))["control"]["owner"])


async def test_a_handoff_pauses_and_tells_the_operator(service: Browsers, owners: FakeOwners, bus: FakeBus, daemon: FakeBrowserd) -> None:
    owners.add(SESSION, "Research")
    await service.open(SESSION, url="https://github.test/login", actor="agent:sess1")
    await service.handoff("s-sess1", "login", "sign in to github.test", actor="agent:sess1")
    assert daemon.groups["s-sess1"].control["owner"] == "paused"
    needs = bus.of("browser.needs_you")
    assert needs and needs[0][0] == {"group_id": "s-sess1", "reason": "login", "what": "sign in to github.test", "url": "https://github.test/login", "title": "Research", "by": "agent"}
    # The daemon raises it on its own for a CAPTCHA, and the host retells it with the owner's ids.
    daemon.needs_you("s-sess1", "captcha", "solve the CAPTCHA")
    await wait_until(lambda: _async(len(bus.of("browser.needs_you"))), 2)
    assert bus.of("browser.needs_you")[1][1]["session_id"] == "sess1"


async def test_a_crash_closes_the_groups_and_the_next_call_says_how_to_go_on(service: Browsers, owners: FakeOwners, bus: FakeBus, daemon: FakeBrowserd) -> None:
    owners.add(SESSION)
    opened = await service.open(SESSION, actor="agent:sess1")
    daemon.crash(opened["group"]["browser_id"])
    await wait_until(lambda: _async(len(bus.of("browser.closed"))), 1)
    assert bus.of("browser.closed")[0][0]["reason"] == "crashed"
    with pytest.raises(BrowserGone) as gone:
        await service.call("s-sess1", "tab.list", {"group_id": "s-sess1"}, what="listing")
    assert "crashed" in gone.value.message and "BrowserOpen" in gone.value.message
    reopened = await service.open(SESSION, actor="agent:sess1")
    assert reopened["created"] and (await service.get("s-sess1"))["status"] == "running"


async def test_reconcile_after_a_daemon_restart_and_an_adoption_by_labels(db: Database, service: Browsers, owners: FakeOwners, bus: FakeBus, daemon: FakeBrowserd) -> None:
    owners.add(SESSION)
    staff = owners.add(Owner("staff", "m7", project_id="proj1", staff_id="m7"))
    await service.open(SESSION, actor="agent:sess1")
    await daemon.restart()
    await wait_until(lambda: _status(db, "s-sess1"), "lost")
    assert any(p["reason"] == "lost" for p, _ in bus.of("browser.closed"))
    # A group this host has no row for is adopted from its labels; one whose owner is gone is closed.
    daemon._open({"group_id": "m-m7", "profile": "project-proj1", "labels": staff.labels()})
    daemon._open({"group_id": "s-gone", "profile": "session-gone", "labels": Owner("session", "gone").labels()})
    link = service.links["container"]
    await service._reconcile(link)
    assert await _status(db, "m-m7") == "open"
    assert "s-gone" not in daemon.groups


async def _status(db: Database, group: str) -> str | None:
    row = await db.fetchone("SELECT status FROM browser_groups WHERE id = ?", (group,))
    return str(row["status"]) if row is not None else None


async def test_the_audit_never_holds_what_anyone_typed_and_closing_the_owner_closes_its_browser(service: Browsers, owners: FakeOwners) -> None:
    owners.add(SESSION)
    await service.open(SESSION, actor="agent:sess1")
    await service.audit("s-sess1", "container", "agent:sess1", "act", {"text_len": 16, "text_sha256": "ab" * 32})
    assert "hunter2" not in json.dumps(await service.audit_log("s-sess1"))
    assert await service.close_owned("session", "sess1") == 1
    assert (await service.get("s-sess1"))["close_reason"] == "owner_gone"


async def test_what_the_agent_read_out_of_a_page_and_noted_about_a_site_is_in_the_action_log(service: Browsers, owners: FakeOwners) -> None:
    owners.add(SESSION)
    await service.open(SESSION, actor="agent:sess1")
    await service.audit("s-sess1", "container", "agent:sess1", "extract", {"tab": "t1", "url": "https://example.com/", "items": 3})
    await service.audit("s-sess1", "container", "agent:sess1", "note", {"url": "https://example.com/"})
    await service.audit("s-sess1", "container", "agent:sess1", "sensitive", {"kinds": ["purchase"]})
    kinds = [r["kind"] for r in await service.actions("s-sess1")]
    assert "extract" in kinds and "note" in kinds and "sensitive" not in kinds


async def test_the_load_counts_browsers_under_their_own_profile(service: Browsers, owners: FakeOwners, daemon: FakeBrowserd) -> None:
    owners.add(SESSION)
    await service.open(SESSION, actor="agent:sess1")
    load = await service.load(cap=2)
    assert load["running"] == 1 and load["used"]["rss_bytes"] == (250 << 20) + (20 << 20)
    assert load["projection"]["cap"] == 2 and load["likely"]["basis"] in ("default", "running", "measured")
    daemon.emit("browser.stats", {"supported": True, "browsers": [{"id": "b1", "rss_bytes": 300 << 20, "cpu_percent": 4.0}]})
    await wait_until(lambda: _async(bool(service.costs.profiles())), True)
    assert "browser" in service.costs.profiles()


async def test_a_group_the_daemon_forgot_reads_as_closed_idle_and_a_missing_thing_in_it_does_not(service: Browsers, owners: FakeOwners, daemon: FakeBrowserd) -> None:
    owners.add(SESSION)
    opened = await service.open(SESSION, actor="agent:sess1")
    tab = opened["tab"]["id"]
    # Nothing is open to answer: the group stays open.
    with pytest.raises(NotFound):
        await service.call("s-sess1", "dialog.answer", {"tab_id": tab, "accept": True}, what="answering the dialog")
    assert (await service.get("s-sess1"))["status"] == "running"
    # The daemon closed it without a word reaching this host (its idle close while the host was away).
    del daemon.groups["s-sess1"]
    with pytest.raises(BrowserGone) as gone:
        await service.call("s-sess1", "tab.list", {"group_id": "s-sess1"}, what="listing")
    assert "ten minutes" in gone.value.message
    assert (await service.get("s-sess1"))["close_reason"] == "idle"


async def test_the_wall_gets_its_rules_on_every_connection_and_egress_is_logged(db: Database, run_dir: Path, owners: FakeOwners, daemon: FakeBrowserd, cfg: BrowserConfig) -> None:
    rules = {"sealed_ports": [8765], "services_ports": [[8100, 8119]], "loopback_rewrite": "host.docker.internal", "local_sites": "ask", "lan_allow": []}
    made = Browsers(db, run_dirs={"container": run_dir, "host": None}, config=lambda: cfg, owners=owners, wall=lambda env: dict(rules))  # type: ignore[arg-type]
    await made.start()
    try:
        assert await made.wait_available("container")
        assert daemon.wall == rules
        owners.add(SESSION)
        await made.open(SESSION, actor="agent:sess1")
        daemon.walled["intranet.test"] = ("deny", "private")
        with pytest.raises(Exception) as refused:
            await made.call("s-sess1", "page.navigate", {"tab_id": daemon.groups["s-sess1"].active, "url": "http://intranet.test/"}, what="opening")
        assert refused.value.details["reason"] == "private"  # type: ignore[attr-defined]
        await wait_until(lambda: _egress(db), [("sess1", "Browser", "intranet.test", "deny")])
        # A restarted daemon starts at its strictest: the rules go again.
        await daemon.restart()
        daemon.wall = None
        await wait_until(lambda: _async(daemon.wall), rules)
    finally:
        await made.close()


async def _egress(db: Database) -> list[tuple[str, str, str, str]]:
    return [(r["session_id"], r["tool"], r["host"], r["action"]) for r in await db.fetchall("SELECT * FROM egress_log WHERE action != 'allow' ORDER BY seq")]


async def test_a_request_and_the_last_action_are_on_the_listing_until_answered(service: Browsers, owners: FakeOwners, daemon: FakeBrowserd) -> None:
    owners.add(SESSION, "Research")
    await service.open(SESSION, url="https://github.test/login", actor="agent:sess1")
    daemon.needs_you("s-sess1", "captcha", "solve the CAPTCHA")

    async def need() -> Any:
        return (await service.get("s-sess1"))["needs_you"]

    await wait_until(lambda: _reason(service), "captcha")
    assert (await need())["by"] == "daemon"
    daemon.emit("action", {"group_id": "s-sess1", "tab_id": "t1", "kind": "click", "element": "the Next button", "name": "Next"})
    await wait_until(lambda: _acting(service), True)
    group = await service.get("s-sess1")
    assert group["last_action"]["kind"] == "click" and group["last_action"]["element"] == "the Next button"
    # The operator takes the browser: what was asked of them is being done.
    daemon.set_control("s-sess1", "human", "v1")
    await wait_until(need, None)


async def _reason(service: Browsers) -> str | None:
    need = (await service.get("s-sess1"))["needs_you"]
    return need["reason"] if need else None


async def _acting(service: Browsers) -> bool:
    return bool((await service.get("s-sess1"))["acting"])


async def test_the_daemon_gets_the_operators_limits_and_again_when_they_change(service: Browsers, daemon: FakeBrowserd, cfg: BrowserConfig) -> None:
    await wait_until(lambda: asyncio.sleep(0, daemon.limits), {"max_browsers": 2, "idle_close_ms": 600_000, "record_max_bytes": 500 << 20, "record_retention_ms": 7 * 86_400_000})
    cfg.running_cap = 3
    cfg.idle_close_minutes = 0
    await wait_until(lambda: asyncio.sleep(0, (daemon.limits.get("max_browsers"), daemon.limits.get("idle_close_ms"))), (3, 0), timeout=10)
    sent = len([c for c in daemon.calls if c[0] == "limits.set"])
    await asyncio.sleep(2.5)
    assert len([c for c in daemon.calls if c[0] == "limits.set"]) == sent, "an unchanged setting is not sent again"


async def test_recording_is_the_operators_switch_and_its_frames_are_read_back(service: Browsers, owners: FakeOwners, daemon: FakeBrowserd, cfg: BrowserConfig) -> None:
    owners.add(SESSION)
    cfg.record_frames = True
    opened = await service.open(SESSION, actor="agent:sess1")
    gid = opened["group"]["id"]
    # The Settings default switched it on for the new browser: its first frame is taken.
    listed = await service.recording(gid)
    assert listed["recording"]["frames"] is True and [f["kind"] for f in listed["frames"]] == ["start"]
    await service.set_recording(gid, frames=False)
    assert (await service.recording(gid))["recording"]["frames"] is False
    meta, data = await service.frame(gid, 1)
    assert meta["no"] == 1 and data == daemon.screenshot
    envs = await service.recordings()
    assert envs[0]["env"] == "container" and envs[0]["groups"][0]["group_id"] == gid and envs[0]["max_bytes"] == 500 << 20
    # A closed browser's recording is still there to replay.
    await service.close_group(gid, actor="operator")
    assert len((await service.recording(gid))["frames"]) == 1
    await service.delete_recording(gid)
    assert (await service.recording(gid))["frames"] == []


async def test_the_running_browsers_and_the_load_name_their_figures(service: Browsers, owners: FakeOwners, daemon: FakeBrowserd) -> None:
    owners.add(SESSION, "Research")
    await service.open(SESSION, actor="agent:sess1")
    running = await service.running_browsers()
    assert len(running) == 1 and running[0]["rss_bytes"] == 250 << 20 and running[0]["memory_basis"] == "cgroup"
    assert running[0]["groups"][0]["owner"]["label"] == "Research"
    load = await service.load()
    assert load["memory_basis"] == "cgroup" and load["used"]["daemon_rss_bytes"] == 20 << 20 and load["used"]["machine_cpu_percent"] == 10.0
    await service.close_browser("container", running[0]["id"])
    assert not daemon.browsers


def test_the_gateway_hears_whether_a_view_is_in_view() -> None:
    seen: list[dict[str, Any]] = []
    check = check_frame(False, seen.append)
    result = RelayResult(code=1000, reason="", ended_by="app")
    assert check(b"\x30" + json.dumps({"tier": "live", "max_w": 640, "max_h": 400}).encode(), result) is not None
    assert check(b"\x32" + json.dumps({"hidden": True}).encode(), result) is not None
    assert seen == [{"tier": "live", "max_w": 640, "max_h": 400}, {"hidden": True}]


def test_a_copy_is_input_the_audit_counts_and_a_viewer_never_sends() -> None:
    frame = b"\x33" + json.dumps({"t": "copy", "id": "k1"}).encode()
    result = RelayResult(code=1000, reason="", ended_by="app")
    assert check_frame(False)(frame, result) == frame
    assert result.counts == {"copy": 1}
    watcher = RelayResult(code=1000, reason="", ended_by="app")
    assert check_frame(True)(frame, watcher) is None
    assert watcher.input_dropped == 1


async def test_watchers_count_only_views_in_view(service: Browsers) -> None:
    a = service.watch("g1")
    b = service.watch("g1")
    assert service.watched("g1")
    service.set_watch("g1", a, False)
    assert service.watched("g1")
    service.set_watch("g1", b, False)
    assert not service.watched("g1")
    service.set_watch("g1", b, True)
    service.unwatch("g1", b)
    assert not service.watched("g1")
    service.unwatch("g1", a)
    assert not service.watched("other")


def test_terminals_and_browsers_are_projected_on_one_machine() -> None:
    """The figures the app's workloads bar must repeat (machineload.test.ts, the same machine)."""
    from daedalus import load

    gib, mib = 1 << 30, 1 << 20
    machine = {"mem_total_bytes": 64 * gib, "mem_available_bytes": 40 * gib, "cpus": 16, "cpu_percent": 12.0}
    out = load.project_workloads(machine=machine, kinds={
        "terminals": {"cap": 20, "running": 3, "used_rss": 3 * gib + 40 * mib, "cost": load.Cost(700 * mib, 4.0, 10)},
        "browsers": {"cap": 2, "running": 1, "used_rss": 300 * mib, "cost": load.Cost(260 * mib, 15.0, 10)},
    })
    assert out["kinds"]["terminals"] == {"cap": 20, "extra": 17, "rss_bytes": 3 * gib + 40 * mib, "at_cap_rss_bytes": 3 * gib + 40 * mib + 17 * 700 * mib}
    assert out["kinds"]["browsers"]["at_cap_rss_bytes"] == 560 * mib
    assert out["machine_used_bytes"] == 38520487936 and out["mem_percent"] == 56.1 and out["cpu_percent"] == 17.2 and out["level"] == "ok"
    # A browser cap the machine cannot carry beside the terminals: judged together, it is "bad".
    heavy = load.project_workloads(machine=machine, kinds={
        "terminals": {"cap": 45, "running": 3, "used_rss": 3 * gib, "cost": load.Cost(700 * mib, 4.0, 10)},
        "browsers": {"cap": 32, "running": 1, "used_rss": 300 * mib, "cost": load.Cost(260 * mib, 15.0, 10)},
    })
    assert heavy["level"] == "bad"
