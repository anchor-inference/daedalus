"""Staff: the team of a project, one live session per member, and requests answered once.

The rules that matter are the ones two callers race over — a second session for one member, two
answers to one request, two hires of one name — so those are exercised with real concurrent calls
against a real database, not with a check that a method was called.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from daedalus.config import StaffConfig
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec, ProjectSettings, ProjectStore
from daedalus.stores.staff import (
    PALETTE,
    AsksStore,
    StaffBusy,
    StaffError,
    StaffStore,
    cap_notes,
    colour_for,
    normalise_short_id,
)


async def _project(db: Database, tmp_path: Path, *, git: bool = True, name: str = "Bakery", **settings: object) -> tuple[ProjectStore, str]:
    projects = ProjectStore(db, local_env="container")
    root = tmp_path / name.lower()
    root.mkdir()
    if git:
        (root / ".git").mkdir()
    project = await projects.create(name, [str(root)], settings=ProjectSettings(**settings))  # type: ignore[arg-type]
    return projects, project.id


def _store(db: Database, **kw: object) -> StaffStore:
    options: dict[str, object] = {"local_env": "container", "presets": lambda: ["fast", "strong"], "personas": lambda: ["reviewer", "tester"]}
    options.update(kw)
    return StaffStore(db, **options)  # type: ignore[arg-type]


async def test_a_hire_round_trips_with_a_colour_and_a_journal_entry(db: Database, tmp_path: Path) -> None:
    projects, pid = await _project(db, tmp_path)
    store = _store(db)
    ada = await store.hire(pid, name="  Ada ", role="Writes the menu page", model="strong", agent="Reviewer", effort="high")
    assert (ada.name, ada.harness, ada.isolation, ada.agent, ada.created_by) == ("Ada", "daedalus", "worktree", "reviewer", "operator")
    assert ada.color == colour_for("Ada") and ada.color in PALETTE
    assert await store.get(ada.id) == ada
    assert await store.by_name(pid, "ADA") == ada
    assert await store.find(pid, ada.id) == ada and await store.find(pid, "ada") == ada
    assert [m.id for m in await store.list(pid)] == [ada.id]
    entry = (await projects.journal(pid))[0]
    assert (entry.author, entry.kind, entry.refs) == ("system", "hire", {"staff_id": ada.id})
    assert "Ada" in entry.text and "Daedalus" in entry.text

    given = await store.hire(pid, name="Bo", color="rose", isolation="shared")
    assert given.color == "rose"


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"name": ""}, "needs a name"),
        ({"name": "x" * 33}, "at most 32"),
        ({"name": "two\nlines"}, "one line"),
        ({"harness": "unknown"}, "runs on"),
        ({"isolation": "bubble"}, "isolation is"),
        ({"model": "gpt-9"}, "not a model preset"),
        ({"agent": "poet"}, "no persona"),
        ({"effort": "extreme"}, "effort is one of"),
        ({"permission_mode": "acceptEdits"}, "command-line agent"),
        ({"env": "host"}, "works where its folder is"),
        ({"env": "moon"}, "container or on the host"),
        ({"color": "#ff0000"}, "colour is one of"),
        ({"model": "has space"}, "not a name this store keeps"),
        ({"folder_id": "f-elsewhere"}, "not one of this project"),
        ({"created_by": "someone"}, "hired by the operator"),
    ],
)
async def test_what_a_hire_refuses(db: Database, tmp_path: Path, fields: dict[str, object], reason: str) -> None:
    _, pid = await _project(db, tmp_path)
    store = _store(db)
    request: dict[str, object] = {"name": "Ada"}
    request.update(fields)
    with pytest.raises(StaffError, match=reason):
        await store.hire(pid, **request)  # type: ignore[arg-type]


async def test_a_command_line_member_keeps_its_own_names_and_permission_mode(db: Database, tmp_path: Path) -> None:
    _, pid = await _project(db, tmp_path)
    store = _store(db)
    cc = await store.hire(pid, name="Cleo", harness="claude", agent="code-reviewer", model="opus", effort="max", permission_mode="acceptEdits")
    assert (cc.harness, cc.agent, cc.model, cc.effort, cc.permission_mode) == ("claude", "code-reviewer", "opus", "max", "acceptEdits")


async def test_folders_decide_what_isolation_and_environment_are_possible(db: Database, tmp_path: Path) -> None:
    projects = ProjectStore(db, local_env="container")
    repo, plain, host = tmp_path / "repo", tmp_path / "plain", tmp_path / "hostside"
    for path in (repo, plain, host):
        path.mkdir()
    (repo / ".git").mkdir()
    project = await projects.create("Mixed", [FolderSpec(str(repo)), FolderSpec(str(plain), readonly=True), FolderSpec(str(host), env="host")])
    repo_id, plain_id, host_id = (f.id for f in project.folders)
    store = _store(db)
    with pytest.raises(StaffError, match="read-only, so no worktree"):
        await store.hire(project.id, name="A", folder_id=plain_id, isolation="worktree")
    assert (await store.hire(project.id, name="B", folder_id=plain_id, isolation="readonly")).isolation == "readonly"
    # A Daedalus member works in a host folder through the host terminal daemon; natively nothing
    # of this process reaches into the container, so a container folder is refused there.
    assert (await store.hire(project.id, name="C", folder_id=host_id, isolation="shared")).default_folder_id == host_id
    with pytest.raises(StaffError, match="is in the container"):
        await _store(db, local_env="host").hire(project.id, name="G", folder_id=repo_id, isolation="shared")
    # A command-line member can run on the host, where that folder is; its git state is not known here.
    on_host = await store.hire(project.id, name="D", harness="codex", env="host", folder_id=host_id, isolation="worktree")
    assert on_host.env == "host"
    with pytest.raises(StaffError, match="is in the container"):
        await store.hire(project.id, name="E", harness="codex", env="host", folder_id=repo_id)
    await projects.update_folder(project.id, plain_id, readonly=False)
    with pytest.raises(StaffError, match="not a git repository"):
        await store.hire(project.id, name="F", folder_id=plain_id, isolation="worktree")


async def test_the_installations_own_and_a_chats_own_project_have_no_team(db: Database, tmp_path: Path) -> None:
    _, chat = await _project(db, tmp_path, name="Chat", ephemeral=True)
    _, voice = await _project(db, tmp_path, name="Voice", system="voice")
    store = _store(db)
    with pytest.raises(StaffError, match="chat's own project"):
        await store.hire(chat, name="Ada")
    with pytest.raises(StaffError, match="installation's own"):
        await store.hire(voice, name="Ada")
    with pytest.raises(KeyError):
        await store.hire("nope", name="Ada")


async def test_names_are_unique_among_the_active_and_dismissal_frees_one(db: Database, tmp_path: Path) -> None:
    projects, pid = await _project(db, tmp_path)
    store = _store(db)
    first = await store.hire(pid, name="Ada")
    with pytest.raises(StaffError, match="already has someone called"):
        await store.hire(pid, name="ada")
    # Two hires of one name at the same moment: the index lets exactly one through.
    results = await asyncio.gather(store.hire(pid, name="Bo"), store.hire(pid, name="BO"), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["Staff", "StaffError"]

    archived = await store.archive(first.id)
    assert archived.archived_at is not None and not archived.active
    assert await store.archive(first.id) == archived, "dismissing twice is not an error"
    again = await store.hire(pid, name="Ada")
    assert again.id != first.id
    assert [m.id for m in await store.list(pid)] == [m.id for m in await store.list(pid) if m.active]
    assert first.id in {m.id for m in await store.list(pid, archived=True)}
    assert [e.kind for e in await projects.journal(pid)][:1] == ["hire"] and "dismiss" in [e.kind for e in await projects.journal(pid)]
    # A name is unique within a project, not across the installation.
    _, other = await _project(db, tmp_path, name="Other")
    assert (await store.hire(other, name="Ada")).project_id == other


async def test_editing_changes_what_may_change_and_nothing_else(db: Database, tmp_path: Path) -> None:
    _, pid = await _project(db, tmp_path)
    store = _store(db)
    ada = await store.hire(pid, name="Ada")
    edited = await store.update(ada.id, role="Tests", model="fast", isolation="shared", instructions="Line one\nLine two", color="teal")
    assert (edited.role, edited.model, edited.isolation, edited.instructions, edited.color, edited.name) == ("Tests", "fast", "shared", "Line one\nLine two", "teal", "Ada")
    assert (await store.update(ada.id, model="")).model == ""
    with pytest.raises(StaffError, match="cannot be changed after hiring"):
        await store.update(ada.id, name="Eve")
    with pytest.raises(StaffError, match="cannot be changed after hiring"):
        await store.update(ada.id, harness="claude")
    with pytest.raises(StaffError, match="not a model preset"):
        await store.update(ada.id, model="gpt-9")
    await store.archive(ada.id)
    with pytest.raises(StaffError, match="dismissed"):
        await store.update(ada.id, role="x")


async def test_one_live_session_per_member_is_the_databases_rule(db: Database, tmp_path: Path) -> None:
    _, pid = await _project(db, tmp_path)
    store = _store(db)
    ada = await store.hire(pid, name="Ada")
    # Six launches racing for one member: exactly one session is opened, the others are told why.
    results = await asyncio.gather(*(store.claim_session(ada.id, kind="daedalus") for _ in range(6)), return_exceptions=True)
    won = [r for r in results if not isinstance(r, BaseException)]
    assert len(won) == 1 and all(isinstance(r, StaffBusy) for r in results if isinstance(r, BaseException))
    live = won[0]
    assert (await store.live(ada.id)) == live and live.status == "starting"
    # The index itself, bypassing the store: a second live row cannot be written at all.
    with pytest.raises(Exception, match="UNIQUE"):
        await db.execute("INSERT INTO staff_sessions(id, staff_id, kind, status_at, started_at) VALUES ('ss-x', ?, 'cli', '', '')", (ada.id,))

    with pytest.raises(StaffBusy, match="is working"):
        await store.archive(ada.id)
    ended = await store.end_session(live.id, "task done")
    assert ended is not None and ended.status == "exited" and ended.end_reason == "task done" and not ended.live
    assert await store.end_session(live.id, "again") is None
    nxt = await store.claim_session(ada.id, kind="daedalus", predecessor_id=live.id, task_id=None)
    assert [s.id for s in await store.chain(nxt.id)] == [nxt.id, live.id]
    assert [s.id for s in await store.sessions(ada.id)] == [nxt.id, live.id]
    assert (await store.session_counts(pid)) == {ada.id: 2}
    assert set(await store.live_sessions(pid)) == {ada.id}
    await store.end_session(nxt.id, "released")
    await store.archive(ada.id)
    with pytest.raises(StaffError, match="dismissed"):
        await store.claim_session(ada.id, kind="daedalus")


async def test_a_session_chain_with_a_loop_in_it_ends(db: Database, tmp_path: Path) -> None:
    _, pid = await _project(db, tmp_path)
    store = _store(db)
    ada = await store.hire(pid, name="Ada")
    one = await store.claim_session(ada.id, kind="cli")
    await store.end_session(one.id, "crashed")
    two = await store.claim_session(ada.id, kind="cli", predecessor_id=one.id)
    await db.execute("UPDATE staff_sessions SET predecessor_id = ? WHERE id = ?", (two.id, one.id))
    assert [s.id for s in await store.chain(two.id)] == [two.id, one.id]


async def test_status_waiting_for_and_the_throttled_signal(db: Database, tmp_path: Path) -> None:
    _, pid = await _project(db, tmp_path)
    now = [100.0]
    store = _store(db, clock=lambda: now[0], config=lambda: StaffConfig(signal_write_seconds=5))
    ada = await store.hire(pid, name="Ada")
    live = await store.claim_session(ada.id, kind="daedalus")
    previous, asked = (await store.set_status(live.id, "question", "which colour?"))  # type: ignore[misc]
    assert previous == "starting" and asked.status == "question" and asked.waiting_for == "which colour?"
    previous, working = (await store.set_status(live.id, "working", "stale"))  # type: ignore[misc]
    assert previous == "question" and working.waiting_for == "", "only a waiting status keeps what it waits for"
    with pytest.raises(StaffError, match="status is one of"):
        await store.set_status(live.id, "sleeping")
    with pytest.raises(StaffError, match="end_session"):
        await store.set_status(live.id, "exited")

    assert await store.touch(live.id) is False, "a status write was a signal a moment ago"
    now[0] += 6
    assert await store.touch(live.id) is True
    assert await store.touch(live.id) is False
    now[0] += 5
    assert await store.touch(live.id) is True
    started = await store.started(live.id, session_id=None, terminal_id="t-1", cli_session_id="c-1")
    assert started is not None and (started.terminal_id, started.cli_session_id) == ("t-1", "c-1")
    await store.end_session(live.id, "done")
    assert await store.set_status(live.id, "working") is None, "an ended session has no status to set"


def test_notes_drop_their_oldest_lines_past_the_cap() -> None:
    assert cap_notes("a\nb\nc", 100) == "a\nb\nc"
    assert cap_notes("first line\nsecond\nthird", 12) == "second\nthird"
    assert cap_notes("x" * 50, 10) == "x" * 10


async def test_appending_notes_is_capped(db: Database, tmp_path: Path) -> None:
    _, pid = await _project(db, tmp_path)
    store = _store(db, config=lambda: StaffConfig(notes_max_chars=500))
    ada = await store.hire(pid, name="Ada")
    for i in range(100):
        notes = await store.append_notes(ada.id, f"note {i:03d} " + "." * 20)
    assert len(notes) <= 500 and notes.endswith("note 099 " + "." * 20) and "note 000" not in notes
    assert (await store.get(ada.id)).notes == notes  # type: ignore[union-attr]
    assert await store.append_notes(ada.id, "   ") == notes
    with pytest.raises(KeyError):
        await store.append_notes("st-missing", "x")


async def test_message_receipts_only_move_forward(db: Database, tmp_path: Path) -> None:
    _, pid = await _project(db, tmp_path)
    store = _store(db)
    ada = await store.hire(pid, name="Ada")
    message = await store.add_message(ada.id, "Use the blue palette", origin="orchestrator", mode="now")
    assert (message.state, message.attempts) == ("queued", 0)
    assert (await store.set_message_state(message.id, "written")).state == "written"  # type: ignore[union-attr]
    assert (await store.set_message_state(message.id, "acknowledged")).state == "acknowledged"  # type: ignore[union-attr]
    late = await store.set_message_state(message.id, "submitted")
    assert late is not None and late.state == "acknowledged", "a late receipt must not undo a later one"
    assert (await store.set_message_state(message.id, "failed", "boom")).state == "acknowledged"  # type: ignore[union-attr]

    other = await store.add_message(ada.id, "Again", origin="operator", mode="after_turn")
    failed = await store.set_message_state(other.id, "failed", "the terminal was gone")
    assert failed is not None and (failed.state, failed.error, failed.attempts) == ("failed", "the terminal was gone", 0)
    assert (await store.set_message_state(other.id, "written")).state == "failed"  # type: ignore[union-attr]
    retried = await store.set_message_state(other.id, "queued")
    assert retried is not None and retried.state == "queued" and retried.error == ""
    assert (await store.set_message_state(other.id, "submitted")).attempts == 1  # type: ignore[union-attr]
    assert [m.id for m in await store.messages(ada.id)] == [other.id, message.id]
    assert await store.set_message_state("sm-missing", "written") is None
    with pytest.raises(StaffError):
        await store.add_message(ada.id, "x", origin="staff", mode="now")
    with pytest.raises(StaffError):
        await store.add_message(ada.id, "x", origin="operator", mode="shout")


async def test_the_first_answer_wins_and_the_others_learn_who_it_was(db: Database, tmp_path: Path) -> None:
    _, pid = await _project(db, tmp_path)
    store = _store(db)
    asks = AsksStore(db)
    ada = await store.hire(pid, name="Ada")
    ask = await asks.open(pid, origin="staff", kind="permission", text="Run npm install?", routed_to="orchestrator", staff_id=ada.id, request_ref="call-7", detail={"tool": "Exec"})
    assert ask.open and ask.short_id.startswith("q") and len(ask.short_id) == 6 and ask.detail == {"tool": "Exec"}
    assert [a.id for a in await asks.open_for(pid)] == [ask.id]
    assert await asks.open_for(pid, routed_to="operator") == []

    assert await asks.route(ask.id, "operator", suggestion="allow: the brief says installs are fine") is True
    routed = await asks.get(ask.id)
    assert routed is not None and routed.routed_to == "operator" and routed.suggestion.startswith("allow")
    assert [a.id for a in await asks.open_for(pid, routed_to="operator")] == [ask.id]

    results = await asyncio.gather(asks.resolve(ask.id, "operator", {"allow": True}), asks.resolve(ask.id, "orchestrator", {"allow": False}))
    assert sorted(results) == [False, True]
    done = await asks.get(ask.id)
    assert done is not None and not done.open
    assert done.resolved_by == ("operator" if results[0] else "orchestrator")
    assert done.resolution == ({"allow": True} if results[0] else {"allow": False})
    assert await asks.route(ask.id, "orchestrator") is False, "an answered request goes nowhere"
    assert await asks.open_for(pid) == []
    with pytest.raises(StaffError):
        await asks.resolve(ask.id, "somebody")


async def test_a_short_id_collision_is_drawn_again_and_freed_on_answer(db: Database, tmp_path: Path) -> None:
    _, pid = await _project(db, tmp_path)
    draws = iter(["qaaaaa", "qaaaaa", "qaaaaa", "qbbbbb", "qaaaaa"])
    asks = AsksStore(db, short_id=lambda: next(draws))
    first = await asks.open(pid, origin="orchestrator", kind="question", text="Ship it?", routed_to="operator")
    second = await asks.open(pid, origin="orchestrator", kind="question", text="And this?", routed_to="operator")
    assert (first.short_id, second.short_id) == ("qaaaaa", "qbbbbb")
    assert (await asks.get("QAAAAA")).id == first.id  # type: ignore[union-attr]
    await asks.resolve(first.id, "operator", {"text": "yes"})
    third = await asks.open(pid, origin="orchestrator", kind="question", text="Once more?", routed_to="operator")
    assert third.short_id == "qaaaaa", "an answered request's short id is free again"
    assert (await asks.get("qaaaaa")).id == third.id  # type: ignore[union-attr]

    stuck = AsksStore(db, short_id=lambda: "qaaaaa")
    with pytest.raises(StaffError, match="no free short id"):
        await stuck.open(pid, origin="orchestrator", kind="question", text="Stuck?", routed_to="operator")


async def test_what_a_request_refuses(db: Database, tmp_path: Path) -> None:
    _, pid = await _project(db, tmp_path)
    asks = AsksStore(db)
    with pytest.raises(StaffError):
        await asks.open(pid, origin="operator", kind="question", text="x", routed_to="operator")
    with pytest.raises(StaffError):
        await asks.open(pid, origin="staff", kind="wish", text="x", routed_to="operator")
    with pytest.raises(StaffError):
        await asks.open(pid, origin="staff", kind="question", text="x", routed_to="everyone")
    with pytest.raises(StaffError):
        await asks.open(pid, origin="staff", kind="question", text="  ", routed_to="operator")
    with pytest.raises(StaffError, match="detail"):
        await asks.open(pid, origin="staff", kind="question", text="x", routed_to="operator", detail={"blob": "x" * 20000})
    assert json.loads(json.dumps((await asks.open(pid, origin="staff", kind="folder", text="Add /srv/data?", routed_to="operator")).view()))["kind"] == "folder"


def test_a_typed_short_id_is_read_the_way_it_was_meant() -> None:
    assert normalise_short_id(" QO1L2 ") == "q0112"


async def test_two_names_that_share_a_worktree_slug_are_refused(db: Database, tmp_path: Path) -> None:
    _, pid = await _project(db, tmp_path)
    store = _store(db)
    anna = await store.hire(pid, name="Anna")
    with pytest.raises(StaffError, match="share the worktree and branch name 'anna' with Anna"):
        await store.hire(pid, name="Анна")
    with pytest.raises(StaffError, match="with Anna"):
        await store.hire(pid, name="anna!")
    # Racing hires of two names with one slug: exactly one lands.
    results = await asyncio.gather(store.hire(pid, name="Ivan"), store.hire(pid, name="Иван"), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["Staff", "StaffError"]
    # Dismissal frees the slug as it frees the name.
    await store.archive(anna.id)
    assert (await store.hire(pid, name="Анна")).name == "Анна"


async def test_who_is_working_on_a_cli_across_projects_and_where(db: Database, tmp_path: Path) -> None:
    _, bakery = await _project(db, tmp_path)
    _, forge = await _project(db, tmp_path, name="Forge")
    store = _store(db)
    ada = await store.hire(bakery, name="Ada", harness="claude")
    bo = await store.hire(forge, name="Bo", harness="claude")
    cy = await store.hire(bakery, name="Cy", harness="codex")
    await store.hire(forge, name="Di", harness="claude")
    live = await store.claim_session(ada.id, kind="cli")
    ended = await store.claim_session(bo.id, kind="cli")
    await store.end_session(ended.id, "done")
    await store.claim_session(cy.id, kind="cli")
    # Only a live session counts, only of that CLI; the environment is the project's default when the
    # member names none.
    assert await store.live_by_harness("claude") == [
        {"staff_id": ada.id, "name": "Ada", "project_id": bakery, "project": "Bakery", "env": "container", "staff_session_id": live.id, "status": "starting"},
    ]
    assert [s["name"] for s in await store.live_by_harness("codex")] == ["Cy"]
    assert await store.live_by_harness("grok") == []
