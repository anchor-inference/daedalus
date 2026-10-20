"""What an orchestrator shows the operator of its project's files, and what it may deliver.

The operator could not see the files of an orchestrated project anywhere, and the orchestrator could
not attach a member's document to its answer. Its file pane now opens every folder of the project
and each staff worktree in one, read-only, and SendFile and AttachMedia deliver from the same places:
never outside them, and never git's metadata or a file of credentials.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import ORCHESTRATOR_TOOLS, RuntimeConfig, Settings
from daedalus.host.deliverables import locate, parse_worktree_pane, secret_part, worktree_pane
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.files import FileRefused
from daedalus.stores.projects import FolderSpec
from daedalus.tools.chat import attach_media, send_file
from tests.unit.test_host_exec import HOST_ROOT, container_only, host_disk
from tests.unit.test_host_pane import FilesHost
from tests.unit.test_project_folders_api import HEADERS, _client

ACT = "Акт о приёме на работу.docx"


def test_the_sealed_names_and_the_worktree_pane_ids() -> None:
    assert secret_part("docs/drafts/act.docx") is None
    assert secret_part(".env.example") is None
    for sealed in (".git/config", "a/.git", ".env", "deploy/.env.production", ".ssh/id_ed25519", ".git-credentials"):
        assert secret_part(sealed) is not None, sealed
    assert parse_worktree_pane(worktree_pane("f-1", "hrdrafter")) == ("f-1", "hrdrafter")
    for bad in ("wt~f-1~..", "wt~f-1~.", "wt~f-1~a/b", "wt~f-1~", "f-1", "wt~f 1~x"):
        assert parse_worktree_pane(bad) is None, bad


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repository(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A project folder with a member's worktree in it holding an untracked document, a secret beside
    it, and a folder outside the project."""
    code, outside = tmp_path / "code", tmp_path / "outside"
    code.mkdir()
    outside.mkdir()
    (outside / "private.txt").write_text("not the project's\n", encoding="utf-8")
    _git(code, "init", "-q", "-b", "main")
    _git(code, "-c", "user.email=someone@localhost", "-c", "user.name=someone", "commit", "-q", "--allow-empty", "-m", "start")
    (code / "README.md").write_text("Atlas\n", encoding="utf-8")
    (code / ".env").write_text("API_KEY=nope\n", encoding="utf-8")
    worktree = code / ".agents" / "worktrees" / "hrdrafter"
    _git(code, "worktree", "add", "-q", "-b", "agent/hrdrafter/t1-act", str(worktree))
    (worktree / "docs" / "drafts").mkdir(parents=True)
    (worktree / "docs" / "drafts" / ACT).write_bytes(b"PK\x03\x04docx bytes")
    (worktree / "escape").symlink_to(outside, target_is_directory=True)
    (worktree / "metadata").symlink_to(worktree / ".git")
    return code, worktree, outside


async def _orchestrator(manager: SessionManager, code: Path) -> tuple[Any, str]:
    project = await manager.projects.create("Atlas", [FolderSpec(str(code))])
    state = await manager.create_session("Orchestrator", project_id=project.id, metadata={"orchestrator_of": project.id})
    assert manager.is_orchestrator(state)
    return project, state.session.id


def test_a_path_is_located_in_the_project_or_refused(tmp_path: Path) -> None:
    from daedalus.stores.projects import Project, ProjectFolder
    from tests.unit.test_containment import NOW

    folder = ProjectFolder(id="f-code", project_id="p", path=tmp_path / "code", label="", env="container", is_git=True, readonly=False, position=0, created_at=NOW)
    project = Project(id="p", name="Atlas", created_at=NOW, folders=(folder,))
    inside = f"{tmp_path}/code/.agents/worktrees/hrdrafter/docs/{ACT}"
    assert locate(project, inside) == (folder, inside)
    assert locate(project, "README.md") == (folder, f"{tmp_path}/code/README.md")
    for refused, why in ((f"{tmp_path}/outside/private.txt", "not in any"), ("../outside/private.txt", "not in any"),
                         (".agents/worktrees/hrdrafter/.git", ".git"), (".env", ".env"), ("~/notes.txt", "home directory")):
        with pytest.raises(FileRefused, match=why):
            locate(project, refused)


async def test_the_orchestrator_pane_shows_the_project_and_its_worktrees_read_only(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    code, worktree, _ = _repository(tmp_path)
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        project, sid = await _orchestrator(manager, code)
        folder_id = project.folders[0].id
        async with _client(settings, config, db, manager) as client:
            detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
            assert [(f["id"], f["writable"]) for f in detail["folders"]] == [(folder_id, False)]

            # The project folder: its files, and neither .git nor the .env beside them.
            names = {e["name"] for e in (await client.get(f"/api/sessions/{sid}/files", headers=HEADERS)).json()["entries"]}
            assert "README.md" in names and ".agents" in names and not names & {".git", ".env"}
            assert (await client.get(f"/api/sessions/{sid}/download", headers=HEADERS, params={"path": ".env"})).status_code == 403
            assert (await client.get(f"/api/sessions/{sid}/files", headers=HEADERS, params={"path": ".git"})).status_code == 403

            # The member's worktree, by the id the worktree list gives it.
            listed = (await client.get(f"/api/projects/{project.id}/worktrees", headers=HEADERS)).json()["worktrees"]
            pane = next(row["pane"] for row in listed if row["path"] == str(worktree))
            assert pane == worktree_pane(folder_id, "hrdrafter")
            base = f"/api/sessions/{sid}/folders/{pane}"
            drafts = (await client.get(f"{base}/files", headers=HEADERS, params={"path": "docs/drafts"})).json()
            assert [e["name"] for e in drafts["entries"]] == [ACT]
            got = await client.get(f"{base}/download", headers=HEADERS, params={"path": f"docs/drafts/{ACT}"})
            assert got.status_code == 200 and got.content == b"PK\x03\x04docx bytes"
            found = (await client.get(f"{base}/files/search", headers=HEADERS, params={"q": "Акт"})).json()
            assert [r["path"] for r in found["results"]] == [f"docs/drafts/{ACT}"]
            top = {e["name"] for e in (await client.get(f"{base}/files", headers=HEADERS)).json()["entries"]}
            assert ".git" not in top and "escape" not in top, "a worktree's .git file and a link out of it are not listed"

            # The way out, git's metadata through a link, an unknown worktree and any upload are refused.
            assert (await client.get(f"{base}/download", headers=HEADERS, params={"path": "escape/private.txt"})).status_code == 400
            assert (await client.get(f"{base}/download", headers=HEADERS, params={"path": ".git"})).status_code == 403
            assert (await client.get(f"{base}/files", headers=HEADERS, params={"path": "../../../../outside"})).status_code == 400
            assert (await client.get(f"/api/sessions/{sid}/folders/wt~{folder_id}~..%2F..%2F..%2Foutside/files", headers=HEADERS)).status_code == 404
            assert (await client.get(f"/api/sessions/{sid}/folders/wt~f-nothing~hrdrafter/files", headers=HEADERS)).status_code == 404
            refused = await client.post(f"{base}/files/upload", headers=HEADERS, files={"files": ("x.txt", io.BytesIO(b"x"))})
            assert refused.status_code == 403 and not (worktree / "x.txt").exists()
    finally:
        await manager.close()


async def test_the_orchestrator_delivers_a_members_file_and_nothing_outside(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    code, worktree, outside = _repository(tmp_path)
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        _, sid = await _orchestrator(manager, code)
        document = worktree / "docs" / "drafts" / ACT
        assert await manager.deliverable(sid, str(document)) == document.resolve()
        for refused in (str(outside / "private.txt"), str(worktree / "escape" / "private.txt"), str(worktree / "metadata" / "HEAD"), str(code / ".env")):
            with pytest.raises(FileRefused):
                await manager.deliverable(sid, refused)

        ctx = ToolContext(tenant_id="t", run_id="r", session_id=sid, metadata={"tool_call_id": "c"})
        sent = await send_file().invoke(ctx, {"path": str(document)})
        assert not sent.is_error and ACT in sent.content
        assert (await send_file().invoke(ctx, {"path": str(outside / "private.txt")})).is_error
        state = manager.live_state(sid)
        assert state is not None
        state.run_id = "run-orchestrator"
        attached = await attach_media().invoke(ctx, {"items": [{"path": str(document), "alt": "Акт"}]})
        assert not attached.is_error and "daedalus-media:" in attached.content and "(file)" in attached.content
        leaked = await attach_media().invoke(ctx, {"items": [{"path": str(code / ".env"), "alt": "keys"}]})
        assert leaked.is_error and ".env" in leaked.content

        # A SendFile card reads the file back from where the orchestrator sent it.
        from protocore.contracts.types import Message, MessageRole, ToolUseBlock

        call = Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="send-act", name="SendFile", arguments_json=f'{{"path": "{document}"}}')])
        await manager.sessions.append_transcript(sid, [call])
        async with _client(settings, config, db, manager) as client:
            card = await client.get(f"/api/sessions/{sid}/sent/send-act/download", headers=HEADERS)
            assert card.status_code == 200 and card.content == b"PK\x03\x04docx bytes"
    finally:
        await manager.close()


async def test_a_host_project_is_shown_and_delivered_through_the_bridge(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        container_only(manager)
        disk = host_disk(tmp_path)
        manager.host_bridge = FilesHost(disk)  # type: ignore[assignment]
        member = disk / ".agents" / "worktrees" / "hrdrafter" / "docs"
        member.mkdir(parents=True)
        (member / ACT).write_bytes(b"from the host")
        project = await manager.projects.create("Labs", [FolderSpec(HOST_ROOT, env="host")])
        sid = (await manager.create_session("Orchestrator", project_id=project.id, metadata={"orchestrator_of": project.id, "home": f"orchestrator-{project.id}"})).session.id
        async with _client(settings, config, db, manager) as client:
            detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
            assert [(f["env"], f["writable"]) for f in detail["folders"]] == [("host", False)]
            # Its own address is the project's primary folder, not the empty directory it runs from.
            names = {e["name"] for e in (await client.get(f"/api/sessions/{sid}/files", headers=HEADERS)).json()["entries"]}
            assert "README.md" in names
            pane = worktree_pane(project.folders[0].id, "hrdrafter")
            got = await client.get(f"/api/sessions/{sid}/folders/{pane}/download", headers=HEADERS, params={"path": f"docs/{ACT}"})
            assert got.status_code == 200 and got.content == b"from the host"
        copy = await manager.deliverable(sid, f"{HOST_ROOT}/.agents/worktrees/hrdrafter/docs/{ACT}")
        assert copy.read_bytes() == b"from the host" and copy.name == ACT
        with pytest.raises(FileRefused):
            await manager.deliverable(sid, "/home/someone/elsewhere/notes.txt")
    finally:
        await manager.close()


def test_the_orchestrator_may_deliver_but_still_not_work() -> None:
    assert {"SendFile", "AttachMedia"} <= set(ORCHESTRATOR_TOOLS)
    assert not {"Exec", "Write", "Edit", "Read", "SubAgent"} & set(ORCHESTRATOR_TOOLS)


async def test_the_review_packet_lists_what_a_members_worktree_produced(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from daedalus.extensions.orchestrator_contract import _produced_files
    from daedalus.host.worktrees import StaffWorktrees
    from daedalus.stores.projects import Project, ProjectFolder
    from tests.unit.test_containment import NOW

    code, worktree, _ = _repository(tmp_path)
    (worktree / "scripts").mkdir()
    (worktree / "scripts" / "fill.py").write_text("print('fill')\n", encoding="utf-8")
    _git(worktree, "add", "scripts/fill.py")
    _git(worktree, "-c", "user.email=someone@localhost", "-c", "user.name=someone", "commit", "-q", "-m", "fill script")
    (worktree / ".agents" / "inbox").mkdir(parents=True)
    (worktree / ".agents" / "inbox" / "brief.md").write_text("handed to the member\n", encoding="utf-8")
    folder = ProjectFolder(id="f-code", project_id="p", path=code, label="", env="container", is_git=True, readonly=False, position=0, created_at=NOW)
    project = Project(id="p", name="Atlas", created_at=NOW, folders=(folder,))

    class Rows:
        async def fetchone(self, sql: str, params: Any) -> Any:
            assert params == ("t1",)
            return {"worktree_path": str(worktree), "folder_id": "f-code", "base_ref": "main"}

    orch = SimpleNamespace(manager=SimpleNamespace(db=Rows()), team=SimpleNamespace(worktrees=StaffWorktrees("container")))
    produced = await _produced_files(orch, project, "t1")  # type: ignore[arg-type]
    assert produced is not None and produced["path"] == str(worktree) and not produced["more"]
    # The committed script and the untracked document the member never handed in; not the host's inbox.
    assert sorted(produced["files"]) == sorted(["scripts/fill.py", f"docs/drafts/{ACT}", "escape", "metadata"])
