"""The session file pane of a session that works on the host, seen from the container: every route
reads the folder on the host through the bridge, answers in the local shapes, and stays inside the
session's folder whatever a path or a symlink names."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from protocore.contracts.types import Message, MessageRole, ToolUseBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api_host_pane import parse_range
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.terminals.model import FileChunk
from tests.unit.test_host_exec import HOST_ROOT, ShellHost, container_only, host_disk
from tests.unit.test_project_folders_api import HEADERS, _client


@dataclass
class FilesHost(ShellHost):
    """:class:`ShellHost` with the daemon's file side channels over the same directory: ``stat``
    follows a symlink, ``list_dir`` reports one as a symlink without following it, and a missing path
    is a ``FileNotFoundError``, as :class:`HostBridge` turns the daemon's answers into."""

    def _local(self, path: str) -> Path:
        assert path.startswith("/"), "the daemon is asked by absolute path"
        return Path(self.here(path))

    async def stat(self, path: str) -> dict[str, Any]:
        if not self.up:
            raise ConnectionError("the host terminal bridge is not available")
        local = self._local(path)
        if not local.exists():
            return {"exists": False}
        st = local.stat()
        return {"exists": True, "type": "dir" if local.is_dir() else "file", "size": st.st_size, "mtime": _go_time(st.st_mtime)}

    async def list_dir(self, path: str, *, limit: int) -> dict[str, Any]:
        local = self._local(path)
        if not local.is_dir():
            raise FileNotFoundError(path)
        entries = []
        for child in sorted(local.iterdir()):
            st = child.lstat()
            kind = "symlink" if child.is_symlink() else "dir" if child.is_dir() else "file"
            entries.append({"name": child.name, "type": kind, "size": st.st_size, "mtime": _go_time(st.st_mtime)})
        return {"entries": entries[:limit], "truncated": len(entries) > limit}

    async def read(self, path: str, *, offset: int, max_bytes: int) -> FileChunk:
        local = self._local(path)
        if not local.is_file():
            raise FileNotFoundError(path)
        with local.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(max_bytes)
        size = local.stat().st_size
        return FileChunk(data=data, offset=offset, next_offset=offset + len(data), size=size, eof=offset + len(data) >= size)


def _go_time(epoch: float) -> str:
    """A modification time as the daemon's Go side writes it: RFC 3339 with nanoseconds."""
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z"


@pytest.fixture
async def served(settings: Settings, config: RuntimeConfig, db: Database) -> Any:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        async with _client(settings, config, db, manager) as client:
            yield manager, client
    finally:
        await manager.close()


async def _host_chat(manager: SessionManager, client: Any, tmp_path: Path) -> tuple[str, Path, FilesHost]:
    container_only(manager)
    disk = host_disk(tmp_path)
    host = FilesHost(disk)
    manager.host_bridge = host  # type: ignore[assignment]
    started = await client.post("/api/sessions", headers=HEADERS, json={"title": "Errand", "env": "host"})
    assert started.status_code == 200, started.text
    sid = started.json()["id"]
    folder = disk / ".local/state/daedalus/chats" / sid
    (folder / "video1").mkdir(parents=True)
    (folder / "tts.py").write_text("import wave\nprint('Speak')\n")
    (folder / "video1" / "clip.mp4").write_bytes(bytes(range(256)) * 4096)
    (folder / "voice.wav").write_bytes(b"RIFF\x00\xff\xfe" * 10)
    (folder / "inside").symlink_to(folder / "tts.py")
    (folder / "outside").symlink_to(disk / "README.md")
    (folder / "away").symlink_to(disk / "src", target_is_directory=True)
    return sid, folder, host


async def test_the_pane_lists_and_reads_what_the_agent_made_on_the_host(served: Any, tmp_path: Path) -> None:
    manager, client = served
    sid, folder, _ = await _host_chat(manager, client, tmp_path)
    listing = await client.get(f"/api/sessions/{sid}/files", headers=HEADERS)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["kind"] == "dir" and body["path"] == ""
    names = [e["name"] for e in body["entries"]]
    assert names[0] == "video1", "folders first, as the local listing orders them"
    assert {"tts.py", "voice.wav", "inside"} <= set(names), "the host folder, not the container's inbox"
    assert "outside" not in names and "away" not in names, "a symlink out of the folder is not listed"
    entry = next(e for e in body["entries"] if e["name"] == "tts.py")
    assert set(entry) == {"name", "dir", "size", "mtime"} and entry["size"] == len("import wave\nprint('Speak')\n")
    assert abs(entry["mtime"] - (folder / "tts.py").stat().st_mtime) < 1, "the daemon's RFC 3339 time read as epoch seconds"
    assert next(e for e in body["entries"] if e["name"] == "inside")["dir"] is False

    text = (await client.get(f"/api/sessions/{sid}/files", headers=HEADERS, params={"path": "tts.py"})).json()
    assert text == {"path": "tts.py", "kind": "file", "content": "import wave\nprint('Speak')\n"}
    binary = (await client.get(f"/api/sessions/{sid}/files", headers=HEADERS, params={"path": "voice.wav"})).json()
    assert binary == {"path": "voice.wav", "kind": "binary", "size": 70}
    sub = (await client.get(f"/api/sessions/{sid}/files", headers=HEADERS, params={"path": "video1"})).json()
    assert [e["name"] for e in sub["entries"]] == ["clip.mp4"]
    missing = await client.get(f"/api/sessions/{sid}/files", headers=HEADERS, params={"path": "nope.txt"})
    assert missing.status_code == 404

    detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
    assert detail["workspace"] == f"{HOST_ROOT}/.local/state/daedalus/chats/{sid}", "the app places a turn's absolute host paths by the folder on the host"
    [own] = detail["project"]["folders"]
    assert own["env"] == "host"
    via_folder = await client.get(f"/api/sessions/{sid}/folders/{own['id']}/files", headers=HEADERS, params={"path": "video1"})
    assert via_folder.status_code == 200 and [e["name"] for e in via_folder.json()["entries"]] == ["clip.mp4"], "a turn's file card reaches the host folder by its folder id"


async def test_a_download_streams_from_the_host_and_honours_a_range(served: Any, tmp_path: Path) -> None:
    manager, client = served
    sid, folder, _ = await _host_chat(manager, client, tmp_path)
    whole = await client.get(f"/api/sessions/{sid}/download", headers=HEADERS, params={"path": "video1/clip.mp4"})
    assert whole.status_code == 200
    assert whole.content == (folder / "video1" / "clip.mp4").read_bytes(), "past one read of the bridge, byte for byte"
    assert whole.headers["content-type"] == "video/mp4" and whole.headers["content-disposition"] == 'attachment; filename="clip.mp4"'
    assert whole.headers["content-length"] == str(256 * 4096) and whole.headers["accept-ranges"] == "bytes"
    part = await client.get(f"/api/sessions/{sid}/download", headers={**HEADERS, "Range": "bytes=10-19"}, params={"path": "video1/clip.mp4"})
    assert part.status_code == 206 and part.content == bytes(range(10, 20)) and part.headers["content-range"] == f"bytes 10-19/{256 * 4096}"
    script = await client.get(f"/api/sessions/{sid}/download", headers=HEADERS, params={"path": "tts.py"})
    assert script.status_code == 200 and script.text.startswith("import wave")
    linked = await client.get(f"/api/sessions/{sid}/download", headers=HEADERS, params={"path": "inside"})
    assert linked.status_code == 200 and linked.text.startswith("import wave"), "a symlink inside the folder is followed"
    gone = await client.get(f"/api/sessions/{sid}/download", headers=HEADERS, params={"path": "video1"})
    assert gone.status_code == 404


def test_a_range_header_is_read_as_one_range_or_none() -> None:
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=50-500", 100) == (50, 99)
    assert parse_range("", 100) is None and parse_range("bytes=0-1,5-6", 100) is None
    with pytest.raises(Exception, match="outside the file"):
        parse_range("bytes=100-", 100)


async def test_no_path_leaves_the_session_folder(served: Any, tmp_path: Path) -> None:
    manager, client = served
    sid, folder, host = await _host_chat(manager, client, tmp_path)
    for route in ("files", "download"):
        for path in ("../../../README.md", "/etc/passwd", f"{HOST_ROOT}/README.md", "video1/../../x", "outside", "away/anything", "away"):
            answer = await client.get(f"/api/sessions/{sid}/{route}", headers=HEADERS, params={"path": path})
            assert answer.status_code == 400, (route, path, answer.status_code, answer.text)
    sent = await client.post(f"/api/sessions/{sid}/files/upload", headers=HEADERS, data={"path": "away"}, files={"files": ("x.txt", b"x")})
    assert sent.status_code == 400 and not (tmp_path / "operator-machine/src/x.txt").exists(), "an upload does not follow a symlink out either"
    assert (folder.parent.parent.parent.parent.parent / "README.md").read_text() == "Labs: experiments\n"

    host.up = False
    down = await client.get(f"/api/sessions/{sid}/files", headers=HEADERS)
    assert down.status_code == 503 and "host terminal bridge" in down.json()["detail"]


@pytest.mark.parametrize("tools", ["rg", "find and grep"])
async def test_search_by_name_and_by_text_run_on_the_host(served: Any, tmp_path: Path, tools: str) -> None:
    manager, client = served
    sid, folder, _ = await _host_chat(manager, client, tmp_path)
    (folder / "node_modules" / "tts").mkdir(parents=True)
    (folder / "node_modules" / "tts" / "index.js").write_text("import wave\n")
    if tools != "rg":
        if shutil.which("grep") is None or shutil.which("find") is None:
            pytest.skip("no grep or find here to stand in for the host's")
        tools_dir = tmp_path / "bin"
        tools_dir.mkdir()
        for program in ("grep", "find", "head", "realpath", "mkdir", "cat"):
            found = shutil.which(program)
            if found:
                (tools_dir / program).symlink_to(found)
        (tmp_path / "operator-machine" / ".profile").write_text(f"PATH={tools_dir}\n")
    elif shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed here")

    named = (await client.get(f"/api/sessions/{sid}/files/search", headers=HEADERS, params={"q": "TTS"})).json()
    assert named["engine"] == ("rg" if tools == "rg" else "walk")
    assert [r["path"] for r in named["results"]] == ["tts.py"], "case folded, and node_modules skipped as locally"
    [hit] = named["results"]
    assert set(hit) == {"path", "kind", "size", "mtime"} and hit["kind"] == "file" and hit["size"] > 0
    folders = (await client.get(f"/api/sessions/{sid}/files/search", headers=HEADERS, params={"q": "video"})).json()
    assert {(r["path"], r["kind"]) for r in folders["results"]} == {("video1", "dir")}
    globbed = (await client.get(f"/api/sessions/{sid}/files/search", headers=HEADERS, params={"q": "*.mp4"})).json()
    assert [r["path"] for r in globbed["results"]] == ["video1/clip.mp4"]

    grep = await client.get(f"/api/sessions/{sid}/files/grep", headers=HEADERS, params={"q": "speak"})
    assert grep.status_code == 200, grep.text
    assert grep.json() == {"query": "speak", "hits": [{"path": "tts.py", "line": 2, "text": "print('Speak')"}], "truncated": False}
    exact = (await client.get(f"/api/sessions/{sid}/files/grep", headers=HEADERS, params={"q": "Speak"})).json()
    assert len(exact["hits"]) == 1
    none = (await client.get(f"/api/sessions/{sid}/files/grep", headers=HEADERS, params={"q": "SPEAK"})).json()
    assert none["hits"] == [], "a capital makes the search case-sensitive, as ripgrep's smart case"


async def test_content_search_says_so_when_the_host_has_neither_program(served: Any, tmp_path: Path) -> None:
    manager, client = served
    sid, _, _ = await _host_chat(manager, client, tmp_path)
    (tmp_path / "operator-machine" / ".profile").write_text(f"PATH={tmp_path / 'empty'}\n")
    answer = await client.get(f"/api/sessions/{sid}/files/grep", headers=HEADERS, params={"q": "wave"})
    assert answer.status_code == 501 and "neither is installed" in answer.json()["detail"]


async def test_an_upload_lands_in_the_host_folder_without_clobbering(served: Any, tmp_path: Path) -> None:
    manager, client = served
    sid, folder, host = await _host_chat(manager, client, tmp_path)
    data = os.urandom(600 * 1024)  # past one piece of stdin
    first = await client.post(f"/api/sessions/{sid}/files/upload", headers=HEADERS, files={"files": ("tts.py", data)})
    assert first.status_code == 200 and first.json() == {"files": ["tts-1.py"]}, "an existing name is not overwritten"
    assert (folder / "tts-1.py").read_bytes() == data and (folder / "tts.py").read_text().startswith("import wave")
    nested = await client.post(f"/api/sessions/{sid}/files/upload", headers=HEADERS, data={"path": "notes/today"}, files={"files": ("plan.md", b"# plan\n")})
    assert nested.json() == {"files": ["plan.md"]} and (folder / "notes/today/plan.md").read_text() == "# plan\n"
    assert all(stdin is None or len(stdin) <= 256 * 1024 for _, _, stdin in host.calls), "each piece under the daemon's cap on stdin"


async def test_a_file_the_session_sent_is_fetched_from_the_host(served: Any, tmp_path: Path) -> None:
    manager, client = served
    sid, folder, _ = await _host_chat(manager, client, tmp_path)
    calls = [
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="s1", name="SendFile", arguments_json='{"path": "voice.wav"}')]),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="s2", name="SendFile", arguments_json=f'{{"path": "{HOST_ROOT}/.local/state/daedalus/chats/{sid}/tts.py"}}')]),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="s3", name="SendFile", arguments_json=f'{{"path": "{HOST_ROOT}/README.md"}}')]),
    ]
    await manager.sessions.append_transcript(sid, calls)
    relative = await client.get(f"/api/sessions/{sid}/sent/s1/download", headers=HEADERS)
    assert relative.status_code == 200 and relative.content == (folder / "voice.wav").read_bytes() and relative.headers["content-type"].startswith("audio/")
    absolute = await client.get(f"/api/sessions/{sid}/sent/s2/download", headers=HEADERS)
    assert absolute.status_code == 200 and absolute.text.startswith("import wave")
    outside = await client.get(f"/api/sessions/{sid}/sent/s3/download", headers=HEADERS)
    assert outside.status_code == 403, "a transcript cannot name a host file outside the session's folders"
