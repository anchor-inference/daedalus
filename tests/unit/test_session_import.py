"""Importing another program's session: the job that writes it, the project its folder joins, the
second import that is refused or made a separate chat, pulling in what is new, the long session that
starts from a summary, and the routes with the host's failures. The machine is a fake host terminal
daemon that serves synthetic sessions in the normalised shape; nothing touches the network."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from protocore.contracts.types import COMPACTION_SUMMARY_METADATA_KEY, MessageRole, ThinkingBlock, ToolUseBlock

from daedalus.config import ModelPresetConfig, RuntimeConfig, Settings
from daedalus.host.engine_factory import TENANT
from daedalus.host.session_import import SessionImporter, normalise_cwd
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec
from daedalus.terminals.bridge import HostDaemonOutdated
from daedalus.terminals.model import ExecResult
from tests.support.models import model_config
from tests.support.waiting import until_await
from tests.unit.test_foreign_sessions import call, claude_session, long_session, result, text, turn
from tests.unit.test_project_unification import HEADERS, _client
from tests.unit.test_session_runner import ScriptedProvider

HOME = "/home/operator"
DOOR = f"{HOME}/projects/door"
SECRET = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"


def header(ext_id: str, cwd: str, *, title: str = "Fix the flaky door sensor test", model: str = "claude-opus-4-1-20250805", messages: int = 13, live: bool = False) -> dict[str, Any]:
    return {"v": 1, "harness": "claude", "id": ext_id, "cwd": cwd, "title": title, "started_at": "2026-10-01T10:00:00Z",
            "updated_at": "2026-10-01T11:00:00Z", "messages": messages, "bytes": 4096, "branch": "main", "model": model,
            "flags": {"compacted": 0, "sidechains": 0, "live": live}, "source": {"path": f"~/.claude/projects/x/{ext_id}.jsonl"}}


class FakeHost:
    """``sessions.*`` and ``fs.browse`` of a machine with a few synthetic sessions; pages hold three
    turns, so every read crosses pages as a long one does."""

    PAGE = 3

    def __init__(self) -> None:
        self.up = True
        self.outdated = False
        self.folders = {HOME: ["projects"], f"{HOME}/projects": ["door", "lab"], DOOR: ["firmware"], f"{DOOR}/firmware": [], f"{HOME}/projects/lab": []}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.reads: list[tuple[str, Any, bool]] = []

    def add(self, ext_id: str, cwd: str, turns: list[dict[str, Any]], **kwargs: Any) -> None:
        self.sessions[ext_id] = {"header": header(ext_id, cwd, messages=len(turns), **kwargs), "turns": turns,
                                 "raw": "\n".join(json.dumps({"type": "user", "uuid": t["ext_id"]}) for t in turns) + f"\n{{\"token\": \"{SECRET}\"}}\n"}

    def _check(self) -> None:
        if not self.up:
            raise ConnectionError("the host terminal bridge is not available")
        if self.outdated:
            raise HostDaemonOutdated("the host terminal daemon is older than this version and cannot read other programs' sessions")

    def available(self) -> bool:
        return self.up

    async def exec_run(self, env: str, argv: list[str], **kwargs: Any) -> ExecResult:
        return ExecResult(exit_code=0, signal="", stdout="", stderr="", truncated=False, timed_out=False, duration_ms=1)

    async def sessions_harnesses(self) -> dict[str, Any]:
        self._check()
        return {"harnesses": [{"id": "gemini", "name": "Gemini CLI", "found": False, "sessions": 0},
                              {"id": "codex", "name": "Codex", "found": True, "sessions": 2},
                              {"id": "claude", "found": True, "sessions": len(self.sessions)}]}

    async def sessions_scan(self, harness: str, path: str = "", *, query: str = "", deep: bool = False, limit: int = 200, cursor: str = "") -> dict[str, Any]:
        self._check()
        heads = [s["header"] for s in self.sessions.values()]
        if not path:
            return {"path": "", "here": [], "children": [], "folders": [{"path": h["cwd"], "sessions": 1, "latest": h["updated_at"]} for h in heads], "truncated": False, "cursor": ""}
        here = [dict(h) for h in heads if h["cwd"] == path and query.lower() in h["title"].lower()]
        children = [{"name": h["cwd"][len(path) + 1:].split("/")[0], "path": f"{path}/{h['cwd'][len(path) + 1:].split('/')[0]}", "sessions": 1, "latest": h["updated_at"]}
                    for h in heads if h["cwd"].startswith(path + "/")]
        return {"path": path, "here": here, "children": children, "folders": [], "truncated": False, "cursor": ""}

    async def sessions_read(self, harness: str, session: str, *, start: Any = 0, max_bytes: int = 512 << 10, sidechains: bool = True, raw: bool = False) -> dict[str, Any]:
        self._check()
        self.reads.append((session, start, raw))
        if session not in self.sessions:
            raise FileNotFoundError(f"no {harness} session {session}")
        found = self.sessions[session]
        if raw:
            data = found["raw"].encode()
            page = data[int(start):int(start) + 40]
            following = int(start) + len(page)
            return {"header": found["header"], "data_b64": base64.b64encode(page).decode(), "next": following, "done": following >= len(data), "masked": 0}
        turns = found["turns"][int(start):int(start) + self.PAGE]
        following = int(start) + len(turns)
        return {"header": found["header"], "turns": turns, "next": following, "done": following >= len(found["turns"]),
                "live": found["header"]["flags"]["live"], "masked": 1 if start == 0 else 0}

    async def browse(self, path: str, *, hidden: bool = False, limit: int = 500) -> dict[str, Any]:
        self._check()
        if path not in self.folders:
            raise FileNotFoundError(f"not found: {path}")
        entries = [{"name": n, "path": f"{path}/{n}", "mtime": "2026-10-08T10:00:00Z", "writable": True, "readable": True, "is_git": False, "link": False} for n in self.folders[path]]
        return {"path": path, "parent": path.rsplit("/", 1)[0] or "/", "home": HOME, "writable": True, "is_git": False, "entries": entries[:limit], "truncated": False, "places": []}


def import_config() -> RuntimeConfig:
    config = model_config()
    config.presets["opus"] = ModelPresetConfig(provider="openrouter", model="anthropic/claude-opus-4.1", context_window=200_000)
    return config


@pytest.fixture
async def manager(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, import_config(), db=db)
    await manager.start()
    provider = ScriptedProvider([])
    manager.providers.rungs_for = lambda config, preset_id=None: [(provider, "scripted-model")]  # type: ignore[method-assign]
    if manager.projects.local_env != "container":
        await manager.close()
        pytest.skip("a host folder is reached through the daemon only from a container installation")
    host = FakeHost()
    manager.host_bridge = host  # type: ignore[assignment]
    try:
        yield manager
    finally:
        await manager.close()


def host_of(manager: SessionManager) -> FakeHost:
    return manager.host_bridge  # type: ignore[return-value]


async def finished(client: Any, job_id: str) -> dict[str, Any]:
    state: dict[str, Any] = {}

    async def done() -> bool:
        state.update((await client.get(f"/api/imports/{job_id}", headers=HEADERS)).json())
        return state["state"] != "running"

    await until_await(done, f"the import {job_id} finishing")
    return state


async def test_an_import_writes_the_whole_transcript_and_a_history_without_thinking(settings: Settings, db: Database, manager: SessionManager) -> None:
    host = host_of(manager)
    session = claude_session()
    session[0] = turn(0, "user", text(f"fix the flaky door sensor test, the token is {SECRET}"))
    host.add("c-door", DOOR, session)
    async with await _client(settings, import_config(), db, manager) as client:
        started = await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-door"})
        assert started.status_code == 200, started.text
        job = await finished(client, started.json()["job_id"])
        assert job["state"] == "done" and job["stage"] == "open", job
        sid = job["session_id"]
        state = await manager.get_state(sid)
        assert state is not None and state.project is not None
        assert state.project.settings.ephemeral, "an import into a folder nobody has starts as a chat"
        assert state.project.primary.env == "host" and str(state.project.primary.path) == DOOR
        assert manager.env_of(sid, state.metadata, state.project) == "host"
        assert state.session.title == "Fix the flaky door sensor test"

        transcript = await manager.sessions.list_transcript(sid)
        assert len(transcript) == 10, "every message of the session, as the converter made them"
        assert any(isinstance(b, ThinkingBlock) for m in transcript for b in m.content_blocks)
        history = await manager.sessions.list_messages(sid, TENANT, limit=1000)
        assert not any(isinstance(b, ThinkingBlock) for m in history for b in m.content_blocks)
        assert [b.name for m in history for b in m.content_blocks if isinstance(b, ToolUseBlock)] == ["Exec", "Read", "Edit"]
        assert SECRET not in "".join(m.model_dump_json() for m in transcript), "masked before it was written"

        hits = await manager.sessions.search_transcript("wait_for", session_id=sid)
        assert hits, "the imported transcript is searchable at once"

        overrides = await manager.live.load(sid)
        assert overrides.get("preset") == "opus", "the same model when there is a preset for it"

        imported = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()["imported"]
        assert imported["harness"] == "claude" and imported["id"] == "c-door" and imported["mode"] == "full"
        assert imported["masked"] >= 2 and imported["original"]["stored"] is True and "ref" not in imported["original"]
        row = await db.fetchone("SELECT * FROM session_imports WHERE harness = 'claude' AND ext_id = 'c-door'")
        assert row is not None and row["session_id"] == sid and json.loads(row["cursor"])["next"] == len(session)

        original = await client.get(f"/api/sessions/{sid}/import/original", headers=HEADERS)
        assert original.status_code == 200 and original.headers["content-type"].startswith("application/x-ndjson")
        assert b'"uuid": "rec-0"' in original.content and SECRET.encode() not in original.content

        view = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()["messages"]
        renamed = next(m for m in view if m["tool_calls"])
        assert renamed["imported"]["tools"] == {"toolu_1": "Bash"} and renamed["tool_calls"][0]["name"] == "Exec"

        listed = (await client.get("/api/sessions", headers=HEADERS)).json()["sessions"]
        assert next(r for r in listed if r["id"] == sid)["imported_from"] == "claude"


async def test_a_second_import_is_refused_unless_asked_for_as_a_new_chat(settings: Settings, db: Database, manager: SessionManager) -> None:
    host = host_of(manager)
    host.add("c-door", DOOR, claude_session())
    async with await _client(settings, import_config(), db, manager) as client:
        first = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-door"})).json()["job_id"])
        again = await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-door"})
        assert again.status_code == 409
        assert again.json()["detail"] == {"code": "already_imported", "message": again.json()["detail"]["message"], "session_id": first["session_id"]}
        preview = (await client.get("/api/imports/preview", headers=HEADERS, params={"harness": "claude", "id": "c-door"})).json()
        assert preview["imported_as"]["session_id"] == first["session_id"]

        copy = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-door", "again": True})).json()["job_id"])
        assert copy["state"] == "done" and copy["session_id"] != first["session_id"]
        row = await db.fetchone("SELECT session_id FROM session_imports WHERE ext_id = 'c-door'")
        assert row["session_id"] == copy["session_id"], "the newest copy is the one that pulls in what is new"
        stale = await client.post(f"/api/sessions/{first['session_id']}/import/refresh", headers=HEADERS)
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "superseded"
        state = await manager.get_state(copy["session_id"])
        assert state is not None and not state.project.settings.ephemeral, "two chats in one folder are a project"

        await manager.delete_session(copy["session_id"])
        assert await db.fetchone("SELECT 1 FROM session_imports WHERE ext_id = 'c-door'") is None, "deleting the chat forgets the link"


async def test_pulling_in_what_is_new_appends_to_both_layers(settings: Settings, db: Database, manager: SessionManager) -> None:
    host = host_of(manager)
    session = claude_session()
    host.add("c-live", DOOR, session, live=True)
    async with await _client(settings, import_config(), db, manager) as client:
        job = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-live"})).json()["job_id"])
        sid = job["session_id"]
        before_transcript = len(await manager.sessions.list_transcript(sid))
        before_history = len(await manager.sessions.list_messages(sid, TENANT, limit=1000))

        nothing = (await client.post(f"/api/sessions/{sid}/import/refresh", headers=HEADERS)).json()
        assert nothing["added"] == 0 and nothing["live"] is True

        session.extend([turn(13, "user", text("now the window sensor")), turn(14, "assistant", call("toolu_9", "Bash", {"command": "pytest -q tests/test_window.py"})),
                        turn(15, "user", result("toolu_9", "3 passed")), turn(16, "assistant", text("The window sensor passes."))])
        pulled = (await client.post(f"/api/sessions/{sid}/import/refresh", headers=HEADERS)).json()
        assert pulled["added"] == 4 and pulled["turns"] == 4
        transcript = await manager.sessions.list_transcript(sid)
        history = await manager.sessions.list_messages(sid, TENANT, limit=1000)
        assert len(transcript) == before_transcript + 4 and len(history) == before_history + 4
        assert history[-1].text == "The window sensor passes."
        assert transcript[-1].created_at > transcript[-5].created_at
        assert (await client.post(f"/api/sessions/{sid}/import/refresh", headers=HEADERS)).json()["added"] == 0
        assert host.reads[-1][1] == len(session), "a refresh reads on from where the last one stopped"


async def test_the_folder_decides_the_project(settings: Settings, db: Database, manager: SessionManager) -> None:
    host = host_of(manager)
    importer = SessionImporter(manager)
    door = await manager.projects.create("Door", [FolderSpec(DOOR, env="host")])
    exact = await importer.destination(DOOR)
    assert exact.kind == "project" and exact.project is not None and exact.project.id == door.id and not exact.worktree_cwd
    inside = await importer.destination(f"{DOOR}/firmware/")
    assert inside.kind == "project" and inside.worktree_cwd == f"{DOOR}/firmware", "a folder inside a project joins it and works from there"
    holder = await importer.destination(f"{HOME}/projects")
    assert holder.kind == "refused" and holder.code == "contains_project" and holder.other is not None and holder.other.id == door.id
    gone = await importer.destination(f"{HOME}/projects/gone")
    assert gone.kind == "refused" and gone.code == "missing"
    assert (await importer.destination(f"{HOME}/projects/lab")).kind == "new_chat"
    assert (await importer.destination(f"{HOME}/projects/lab", make_project=True)).kind == "new_project"
    assert normalise_cwd("c:\\Users\\someone\\proj\\") == "C:/Users/someone/proj"

    host.add("c-fw", f"{DOOR}/firmware", claude_session())
    host.add("c-lab", f"{HOME}/projects/lab", claude_session())
    host.add("c-lab2", f"{HOME}/projects/lab", claude_session())
    async with await _client(settings, import_config(), db, manager) as client:
        fw = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-fw", "project_id": door.id})).json()["job_id"])
        state = await manager.get_state(fw["session_id"])
        assert state is not None and state.project.id == door.id and state.metadata["worktree_cwd"] == f"{DOOR}/firmware"
        assert manager.work_dir(state) == Path(f"{DOOR}/firmware")

        lab = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-lab", "make_project": True})).json()["job_id"])
        lab_state = await manager.get_state(lab["session_id"])
        assert lab_state is not None and lab_state.project.settings.ephemeral is False, "made a project at once when asked"
        lab2 = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-lab2"})).json()["job_id"])
        assert (await manager.get_state(lab2["session_id"])).project.id == lab_state.project.id

        mismatch = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-lab", "again": True, "project_id": door.id})).json()["job_id"])
        assert mismatch["state"] == "failed" and mismatch["error"]["code"] == "project_mismatch"

        host.add("c-holder", f"{HOME}/projects", claude_session())
        refused = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-holder"})).json()["job_id"])
        assert refused["state"] == "failed" and refused["error"]["code"] == "contains_project" and refused["session_id"] is None
        moved = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-holder", "cwd": f"{HOME}/projects/lab"})).json()["job_id"])
        assert moved["state"] == "done", "a session may be continued in a folder chosen by hand"


async def test_an_ephemeral_chat_becomes_a_project_with_a_second_import(settings: Settings, db: Database, manager: SessionManager) -> None:
    host = host_of(manager)
    host.add("c-1", DOOR, claude_session())
    host.add("c-2", DOOR, claude_session())
    async with await _client(settings, import_config(), db, manager) as client:
        one = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-1"})).json()["job_id"])
        project = (await manager.get_state(one["session_id"])).project
        assert project.settings.ephemeral
        preview = (await client.get("/api/imports/preview", headers=HEADERS, params={"harness": "claude", "id": "c-2"})).json()
        assert preview["destination"]["kind"] == "chat" and preview["destination"]["becomes_project"] is True
        two = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-2"})).json()["job_id"])
        joined = (await manager.get_state(two["session_id"])).project
        assert joined.id == project.id and joined.settings.ephemeral is False


async def test_a_long_session_starts_from_a_summary_and_its_tail(settings: Settings, db: Database, manager: SessionManager) -> None:
    host = host_of(manager)
    host.PAGE = 400
    host.add("c-long", DOOR, long_session(600, output=2_000))
    async with await _client(settings, import_config(), db, manager) as client:
        preview = (await client.get("/api/imports/preview", headers=HEADERS, params={"harness": "claude", "id": "c-long"})).json()
        assert preview["suggested_mode"] == "tail" and preview["model"]["preset"] == "opus" and preview["model"]["same"] is True
        job = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-long"})).json()["job_id"])
        assert job["state"] == "done", job
        sid = job["session_id"]
        history = await manager.sessions.list_messages(sid, TENANT, limit=10_000)
        summary = history[0]
        assert summary.metadata[COMPACTION_SUMMARY_METADATA_KEY] and summary.metadata["daedalus.compaction"]["reason"] == "import"
        assert "Earlier: the firmware was ported." in summary.text, "the program's own last summary is the base"
        assert "summary" in summary.text, "the turns after it were summarised by the compaction ladder"
        archived = summary.metadata["daedalus.archived"]
        assert archived["seqs"] and archived["from_seq"] < archived["to_seq"]
        assert f"HistoryExpand({archived['from_seq']}, {archived['to_seq']})" in summary.text
        expanded = await manager.sessions.expand_transcript(sid, archived["from_seq"], archived["from_seq"] + 3)
        assert expanded and "Earlier: the firmware was ported." in expanded[0][1].text
        assert history[1].role is MessageRole.user and history[1].metadata["daedalus.origin"] == "operator"
        assert len(history) < 400, "the working history is the summary and a tail"
        assert len(await manager.sessions.list_transcript(sid)) == 2402, "the transcript keeps every message and the summary"
        assert (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()["imported"]["mode"] == "tail"


async def test_the_routes_list_and_say_what_the_host_cannot(settings: Settings, db: Database, manager: SessionManager) -> None:
    host = host_of(manager)
    door = await manager.projects.create("Door", [FolderSpec(DOOR, env="host")])
    host.add("c-door", DOOR, claude_session())
    host.add("c-fw", f"{DOOR}/firmware", claude_session(), title="Port the firmware")
    async with await _client(settings, import_config(), db, manager) as client:
        harnesses = (await client.get("/api/imports/harnesses", headers=HEADERS)).json()["harnesses"]
        assert [h["id"] for h in harnesses] == ["claude", "codex", "gemini"] and harnesses[0]["name"] == "Claude Code"

        everywhere = (await client.get("/api/imports/scan", headers=HEADERS, params={"harness": "claude"})).json()
        assert {f["path"] for f in everywhere["folders"]} == {DOOR, f"{DOOR}/firmware"}
        assert all(f["project"] == {"id": door.id, "name": "Door", "kind": "project"} for f in everywhere["folders"])

        job = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-door"})).json()["job_id"])
        listing = (await client.get("/api/imports/scan", headers=HEADERS, params={"harness": "claude", "path": DOOR})).json()
        assert [h["id"] for h in listing["here"]] == ["c-door"]
        assert listing["here"][0]["imported_as"]["session_id"] == job["session_id"] and listing["here"][0]["flags"]["imported_as"] == job["session_id"]
        assert listing["children"][0]["path"] == f"{DOOR}/firmware" and listing["children"][0]["empty"] is False
        assert [c["name"] for c in listing["crumbs"]] == ["~", "projects", "door"]
        projects = (await client.get("/api/imports/scan", headers=HEADERS, params={"harness": "claude", "path": f"{HOME}/projects"})).json()
        assert {c["name"]: c["empty"] for c in projects["children"]} == {"door": False, "lab": True}, "a folder without sessions is listed to walk through"

        preview = (await client.get("/api/imports/preview", headers=HEADERS, params={"harness": "claude", "id": "c-fw"})).json()
        assert preview["destination"]["kind"] == "project" and preview["destination"]["worktree_cwd"] == f"{DOOR}/firmware"
        assert preview["complete"] is True and preview["first"][0]["text"] == "fix the flaky door sensor test"
        assert preview["last"][-1]["text"].endswith("→ appended\n\nThe test waits for the door now.")
        assert preview["counts"]["native_calls"] == 3 and preview["suggested_mode"] == "full" and preview["masked"] >= 1

        missing = await client.get("/api/imports/preview", headers=HEADERS, params={"harness": "claude", "id": "nope"})
        assert missing.status_code == 404
        assert (await client.get("/api/imports/unknown-job", headers=HEADERS)).status_code == 404
        assert (await client.get("/api/imports/scan", headers=HEADERS, params={"harness": "Claude Code"})).status_code == 400

        host.outdated = True
        outdated = await client.get("/api/imports/harnesses", headers=HEADERS)
        assert outdated.status_code == 501 and outdated.json()["detail"]["code"] == "host_outdated"
        host.outdated, host.up = False, False
        down = await client.get("/api/imports/scan", headers=HEADERS, params={"harness": "claude"})
        assert down.status_code == 503 and down.json()["detail"]["code"] == "host_down" and down.json()["detail"]["start"]


async def test_an_import_that_fails_halfway_leaves_no_chat_behind(settings: Settings, db: Database, manager: SessionManager, monkeypatch: pytest.MonkeyPatch) -> None:
    host = host_of(manager)
    host.add("c-door", DOOR, claude_session())
    sessions_before = {s.id for s in await manager.sessions.list_sessions(TENANT, limit=1000)}
    projects_before = {p.id for p in await manager.projects.list()}

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the disk is full")

    monkeypatch.setattr(manager.sessions, "replace_messages", broken)
    async with await _client(settings, import_config(), db, manager) as client:
        job = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": "c-door"})).json()["job_id"])
    assert job["state"] == "failed" and job["error"] == {"code": "failed", "message": "the disk is full"} and job["session_id"] is None
    assert {s.id for s in await manager.sessions.list_sessions(TENANT, limit=1000)} == sessions_before
    assert {p.id for p in await manager.projects.list()} == projects_before, "the chat's own project went with it"
    assert await db.fetchone("SELECT 1 FROM session_imports") is None
