"""The harness tables against a real database: the catalog, one open launch per staff session, and
delivery facts that only ever accumulate.

The races — two starts for one session, two callers ending one launch — are run as real concurrent
calls, because the rule lives in the database and a check-then-write would pass a sequential test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import daedalus.harness.claude  # noqa: F401 — registers the adapters there are, whichever test ran first
import daedalus.harness.codex  # noqa: F401
import daedalus.harness.cursor  # noqa: F401
import daedalus.harness.grok  # noqa: F401
import daedalus.harness.opencode  # noqa: F401
import daedalus.harness.pi  # noqa: F401
from daedalus.harness.catalog import HarnessCatalog
from daedalus.harness.contract import (
    AgentEntry,
    Catalog,
    CheckResult,
    CheckStep,
    Delivery,
    InstallInfo,
    Launch,
    LoginState,
)
from daedalus.stores.database import Database
from daedalus.stores.harness import HarnessStore, HarnessStoreError
from daedalus.stores.projects import ProjectStore
from daedalus.stores.staff import StaffStore


async def _staff_session(db: Database, tmp_path: Path, name: str = "Ada") -> tuple[StaffStore, str, str]:
    projects = ProjectStore(db, local_env="container")
    root = tmp_path / "bakery"
    root.mkdir(exist_ok=True)
    project = await projects.create("Bakery", [str(root)])
    staff = StaffStore(db, local_env="container", presets=lambda: [], personas=lambda: [])
    member = await staff.hire(project.id, name=name, harness="claude", isolation="shared")
    session = await staff.claim_session(member.id, kind="cli")
    return staff, member.id, session.id


def _launch(staff_session_id: str, launch_id: str = "l-1") -> Launch:
    return Launch(
        launch_id=launch_id,
        staff_session_id=staff_session_id,
        harness="claude",
        env="container",
        terminal_id=None,
        companion_terminal_id=None,
        launch_dir="",
        session_ref="0b6f1c2e-0000-4000-8000-000000000001",
        harness_version="2.1.281",
        started_at="2026-01-01T00:00:00+00:00",
    )


async def test_a_check_fills_the_catalog_and_a_failed_read_keeps_the_last_good_list(db: Database) -> None:
    store = HarnessStore(db)
    catalog = Catalog(agents=(AgentEntry("reviewer", "project", "Reads diffs"),), models=("opus", "sonnet"), modes=("manual", "acceptEdits"), efforts=("low", "high"))
    row = await store.record_check("container", "claude", install=InstallInfo(True, "/opt/claude/bin/claude", "2.1.281", "native"), login=LoginState("yes", "someone's plan"), catalog=catalog)
    assert (row.installed, row.installed_version, row.install_method, row.logged_in) == (True, "2.1.281", "native", "yes")
    assert row.agents == [{"name": "reviewer", "source": "project", "description": "Reads diffs", "model": ""}]
    assert (row.models, row.modes, row.efforts, row.profiles) == (["opus", "sonnet"], ["manual", "acceptEdits"], ["low", "high"], [])

    again = await store.record_check("container", "claude", install=InstallInfo(True, "/opt/claude/bin/claude", "2.1.282", "native"), login=LoginState("unknown"), catalog=None, error="catalog timed out")
    assert (again.installed_version, again.logged_in, again.error) == ("2.1.282", "unknown", "catalog timed out")
    assert again.models == ["opus", "sonnet"] and again.agents == row.agents

    await store.record_latest("container", "claude", "2.1.290")
    await store.record_self_check("container", "claude", CheckResult(ok=False, steps=(CheckStep("launch", True), CheckStep("hook", False, "no SessionStart in 30 s")), version="2.1.282", duration_ms=31_000))
    row = await store.catalog_row("container", "claude")
    assert row is not None and row.latest_version == "2.1.290" and row.latest_checked_at
    assert row.self_check["ok"] is False and [s["name"] for s in row.self_check["steps"]] == ["launch", "hook"]
    assert row.view()["installed"] is True

    absent = await store.record_check("host", "grok", install=InstallInfo(False, detail="the grok on PATH is not Grok Build"), login=LoginState("unknown"))
    assert (absent.installed, absent.installed_version, absent.error) == (False, "", "the grok on PATH is not Grok Build")
    assert [r.harness for r in await store.catalog_rows("container")] == ["claude"]
    assert [(r.env, r.harness) for r in await store.catalog_rows()] == [("container", "claude"), ("host", "grok")]
    with pytest.raises(HarnessStoreError, match="container or host"):
        await store.record_check("moon", "claude", install=InstallInfo(False), login=LoginState("unknown"))


async def test_one_open_launch_per_staff_session_even_when_two_starts_race(db: Database, tmp_path: Path) -> None:
    _, _, ss = await _staff_session(db, tmp_path)
    store = HarnessStore(db)
    results = await asyncio.gather(*(store.open_launch(_launch(ss, f"l-{n}")) for n in range(6)), return_exceptions=True)
    opened = [r for r in results if isinstance(r, Launch)]
    refused = [r for r in results if isinstance(r, HarnessStoreError)]
    assert len(opened) == 1 and len(refused) == 5
    launch = opened[0]
    assert await store.open_launch_for(ss) == launch
    assert await store.live_launch(launch.launch_id) == launch
    assert [x.launch_id for x in await store.open_launches("container")] == [launch.launch_id]
    assert await store.open_launches("host") == []

    updated = await store.update_launch(launch.launch_id, terminal_id="t-main", companion_terminal_id="t-server", launch_dir="/run/ptyd/launch/l")
    assert (updated.terminal_id, updated.companion_terminal_id, updated.launch_dir, updated.session_ref) == ("t-main", "t-server", "/run/ptyd/launch/l", launch.session_ref)
    with pytest.raises(HarnessStoreError, match="no launch"):
        await store.update_launch("l-missing", session_ref="x")

    ended = await asyncio.gather(*(store.end_launch(launch.launch_id) for _ in range(4)))
    assert sorted(ended) == [False, False, False, True]
    # A hook post for an ended launch finds nothing, so a CLI that outlived its launch moves no status.
    assert await store.live_launch(launch.launch_id) is None
    assert (await store.launch(launch.launch_id)) is not None
    # The session may be launched again once the previous launch is over.
    await store.open_launch(_launch(ss, "l-next"))
    with pytest.raises(HarnessStoreError):
        await store.open_launch(_launch(ss, "l-next"))


async def test_delivery_facts_accumulate_and_each_state_keeps_its_first_time(db: Database, tmp_path: Path) -> None:
    staff, member, ss = await _staff_session(db, tmp_path)
    store = HarnessStore(db)
    await store.open_launch(_launch(ss))
    message = await staff.add_message(member, "Add a gluten-free page", origin="orchestrator", mode="now", staff_session_id=ss)

    first = await store.record_delivery("l-1", Delivery(message.id, "written", via="paste", degraded_to="after_turn"))
    assert (first.via, first.degraded_to, first.written_at is not None, first.submitted_at) == ("paste", "after_turn", True, None)
    await store.record_delivery("l-1", Delivery(message.id, "written", client_ref="c-7"))
    submitted = await store.record_delivery("l-1", Delivery(message.id, "submitted"))
    assert (submitted.via, submitted.degraded_to, submitted.client_ref) == ("paste", "after_turn", "c-7")
    assert submitted.written_at == first.written_at and submitted.submitted_at is not None
    assert await store.count_enter(message.id) == 1
    assert await store.count_enter(message.id) == 2
    acknowledged = await store.record_delivery("l-1", Delivery(message.id, "acknowledged"))
    assert acknowledged.acknowledged_at is not None and acknowledged.enters == 2

    found = await store.by_client_ref("l-1", "c-7")
    assert found is not None and found.message_id == message.id
    assert await store.by_client_ref("l-1", "") is None
    assert await store.by_client_ref("l-other", "c-7") is None
    assert set(await store.deliveries([message.id, "sm-missing"])) == {message.id}
    assert await store.deliveries([]) == {}
    with pytest.raises(HarnessStoreError, match="no delivery"):
        await store.count_enter("sm-missing")


async def test_the_harness_rows_go_with_the_staff_rows_they_key_to(db: Database, tmp_path: Path) -> None:
    staff, member, ss = await _staff_session(db, tmp_path)
    store = HarnessStore(db)
    await store.open_launch(_launch(ss))
    message = await staff.add_message(member, "Hello", origin="operator", mode="after_turn", staff_session_id=ss)
    await store.record_delivery("l-1", Delivery(message.id, "written", via="paste"))
    await db.execute("DELETE FROM staff_messages WHERE id = ?", (message.id,))
    assert await store.delivery(message.id) is None
    await db.execute("DELETE FROM staff_sessions WHERE id = ?", (ss,))
    assert await store.launch("l-1") is None


async def test_the_catalog_lists_every_harness_with_what_is_derived_from_the_code(db: Database) -> None:
    catalog = HarnessCatalog(HarnessStore(db))
    await catalog.store.record_check("container", "claude", install=InstallInfo(True, "/opt/claude/bin/claude", "2.1.281", "native"), login=LoginState("yes"))
    await catalog.store.record_check("container", "opencode", install=InstallInfo(True, "/opt/npm/bin/opencode", "2.0.1", "npm"), login=LoginState("no"))
    entries = {e["harness"]: e for e in await catalog.harnesses("container")}
    assert list(entries) == ["claude", "codex", "opencode", "pi", "grok", "cursor"]
    claude = entries["claude"]
    assert (claude["installed"], claude["tested"], claude["supported"], claude["label"], claude["steer"]) == (True, True, True, "Claude Code", "tui_queue")
    assert (entries["opencode"]["tested"], entries["opencode"]["supported"]) == (False, False)
    assert (entries["codex"]["installed"], entries["codex"]["tested"], entries["codex"]["logged_in"]) == (False, False, "unknown")
    # The listing says which can run rather than offering a harness nothing can run.
    assert [name for name, e in entries.items() if e["adapter"]] == ["claude", "codex", "opencode", "pi", "grok", "cursor"]
    assert all(not e["installed"] for e in await catalog.harnesses("host"))
    assert catalog.capabilities("grok").steer == "cancel_and_send"


async def test_the_models_offered_are_a_choice_per_environment_that_outlives_a_check(db: Database) -> None:
    catalog = HarnessCatalog(HarnessStore(db))
    install = InstallInfo(True, "/opt/claude/bin/claude", "2.1.281", "native")
    await catalog.store.record_check("container", "claude", install=install, login=LoginState("yes"), catalog=Catalog(models=("opus", "claude-opus-5-5", "claude-sonnet-5-5")))
    assert await catalog.store.offered_models("container", "claude") is None
    await catalog.store.set_offered_models("container", "claude", ["claude-sonnet-5-5", "claude-opus-5-5"])
    assert await catalog.store.offered_models("host", "claude") is None and await catalog.store.offered_models("container", "codex") is None
    # A check that could only read the aliases does not drop what the operator chose: it is still named.
    await catalog.store.record_check("container", "claude", install=install, login=LoginState("yes"), catalog=Catalog(models=("opus", "claude-sonnet-5-5")))
    claude = (await catalog.harnesses("container"))[0]
    assert (claude["models"], claude["all_models"], claude["models_chosen"]) == (["claude-sonnet-5-5", "claude-opus-5-5"], ["opus", "claude-sonnet-5-5"], True)
    await catalog.store.set_offered_models("container", "claude", None)
    assert (await catalog.harnesses("container"))[0]["models"] == ["opus", "claude-sonnet-5-5"]
    with pytest.raises(HarnessStoreError):
        await catalog.store.set_offered_models("moon", "claude", ["opus"])
