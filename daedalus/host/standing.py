"""The standing instructions and memory an imported session brings from the program it came from.

Claude Code puts its CLAUDE.md files and its project's auto-memory into every prompt, and again after
every compaction; Codex its AGENTS.md files and its memory summary. A session imported without them
lost, at once, everything the operator had taught that program to keep — "it is all written in your
memory" was the operator's own diagnosis. The import reads those files where the program keeps them
(``sessions.instructions`` of the host daemon) and keeps them with the session; the session's
system prompt carries them on every run.

They live in the session, beside its brief, rather than in the history: no compaction reads them
and none can cut them, and the system prompt they ride in is the part of the request whose bytes
stay the same from run to run. The text is kept as a blob, like the import's copy of the original
file; the session's metadata holds only the reference and the list of files, which the app shows.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from daedalus.terminals.bridge import HostDaemonOutdated

if TYPE_CHECKING:
    from daedalus.host.session_runner import SessionManager, SessionState

logger = logging.getLogger(__name__)

TENANT = "daedalus"
STANDING_WINDOW_SHARE = 0.08
"""The share of the model's window the carried files may take in the system prompt: 80k tokens on a
million, 32k on 400k. Every file of the session the investigation followed fitted in less than half
of that on the window it ran on. What does not fit is named, not cut in the middle."""
CHARS_PER_TOKEN = 4
OUTDATED = ("the host terminal daemon is older than this version and does not hand over the program's "
            "instructions and memory; update it (bash deploy/host-terminal.sh install, which ends the open "
            "host terminals), then pull in what is new")
WORKSPACE_NAMES = ("AGENTS.md", "CLAUDE.md")
"""Files of the session's own folder that the workspace notes already read live at every run."""


def _norm(path: str) -> str:
    return path.replace("\\", "/").rstrip("/")


async def fetch(manager: SessionManager, harness: str, ext_id: str) -> dict[str, Any]:
    """What the program keeps for the session, read through the host daemon, masked with the operator's
    own secrets as well: ``{files: [...], problem}``. A daemon that cannot is a problem, not a failure."""
    bridge = manager.host_bridge
    if bridge is None:
        return {"files": [], "problem": "this installation has no host terminal bridge"}
    try:
        reply = await bridge.sessions_instructions(harness, ext_id)
    except HostDaemonOutdated:
        return {"files": [], "problem": OUTDATED}
    except (OSError, ConnectionError) as exc:
        return {"files": [], "problem": str(exc)[:300]}
    files = []
    for item in reply.get("files") or []:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        files.append({
            "path": str(item["path"]), "kind": str(item.get("kind") or ""), "content": manager.redactor.redact(str(item.get("content") or "")),
            "bytes": int(item.get("bytes") or 0), "truncated": bool(item.get("truncated")), "omitted": bool(item.get("omitted")),
        })
    return {"files": files, "problem": ""}


async def store(manager: SessionManager, harness: str, read: dict[str, Any]) -> dict[str, Any]:
    """Keep what :func:`fetch` read as a blob; the record the session's metadata holds about it."""
    files = read.get("files") or []
    record: dict[str, Any] = {
        "source": harness, "read_at": datetime.now(UTC).isoformat(), "problem": read.get("problem") or "",
        "files": [{k: f[k] for k in ("path", "kind", "bytes", "truncated", "omitted")} | {"chars": len(f["content"])} for f in files],
    }
    if files:
        meta = await manager.blobs.put(TENANT, json.dumps(files, ensure_ascii=False).encode("utf-8"), content_type="application/json")
        record["ref"] = meta.ref
    return record


def record_of(metadata: dict[str, Any]) -> dict[str, Any] | None:
    imported = metadata.get("imported") if isinstance(metadata, dict) else None
    record = imported.get("instructions") if isinstance(imported, dict) else None
    return record if isinstance(record, dict) else None


async def load(manager: SessionManager, state: SessionState) -> list[dict[str, Any]]:
    """The carried files of a session, read once per blob and kept on its state."""
    record = record_of(state.session.metadata)
    ref = record.get("ref") if record else None
    if not isinstance(ref, str):
        state.standing = ("", [])
        return []
    if state.standing[0] == ref:
        return state.standing[1]
    try:
        files = json.loads((await manager.blobs.get(TENANT, ref)).decode("utf-8"))
    except Exception:  # noqa: BLE001 — a missing blob must not stop the session from running
        logger.warning("session %s: the carried instructions blob %s could not be read", state.session.id, ref, exc_info=True)
        files = []
    state.standing = (ref, files if isinstance(files, list) else [])
    return state.standing[1]


def render(files: list[dict[str, Any]], *, source: str, window: int, workspace: str = "") -> str:
    """The system prompt's section of carried files: whole files in order while they fit the window's
    share, then the names of the rest. The session folder's own AGENTS.md and CLAUDE.md are left to the
    workspace notes, which read them as they are at every run."""
    root = _norm(workspace)
    room = int(window * CHARS_PER_TOKEN * STANDING_WINDOW_SHARE) if window > 0 else 0
    shown: list[str] = []
    left_out: list[str] = []
    for f in files:
        path = str(f.get("path") or "")
        head, _, name = _norm(path).rpartition("/")
        if root and head == root and name in WORKSPACE_NAMES:
            continue
        content = str(f.get("content") or "")
        if f.get("omitted") or not content or len(content) > room:
            left_out.append(f"  - {path} ({f.get('kind') or 'file'}, {int(f.get('bytes') or 0) // 1024 + 1} KB)")
            continue
        room -= len(content)
        cut = ' truncated="true"' if f.get("truncated") else ""  # the daemon reads at most 128 KiB of one file
        shown.append(f'<file path="{path}" kind="{f.get("kind") or ""}"{cut}>\n{content.strip()}\n</file>')
    if not shown and not left_out:
        return ""
    lines = [
        f"- The standing instructions and memory {source} kept for this work, carried over when the session was imported "
        "from it. They are the operator's own rules and the knowledge they already gave; follow them as you would your "
        "brief, and never ask the operator again for what they say. They stay in this prompt, untouched by compaction. "
        "The files themselves may have changed since the import: Read one for its current text."
    ]
    lines += shown
    if left_out:
        lines.append("- Not carried in this prompt for room; Read them when the work touches what they cover:")
        lines += left_out
    return "\n".join(lines)


__all__ = ["OUTDATED", "fetch", "load", "record_of", "render", "store"]
