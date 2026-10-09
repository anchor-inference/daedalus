"""Importing a session of another agent program, so the operator continues it here.

The session is read on the operator's machine by the host terminal daemon (``sessions.read``), turned
into Daedalus messages (:mod:`daedalus.host.foreign_sessions`) and written as one new session, on the
host, in the folder the program worked in. It is written the way a fork is (``SessionManager.fork_into``):
the transcript gets everything, word for word, and is searchable at once; the working history gets
either all of it or, for a session too long to start from, a summary and the last turns verbatim, the
summary marked ``daedalus.archived`` so HistoryExpand returns what it stands for.

The folder decides the project. A folder a project already has, or a folder inside one, joins that
project; any other folder gets a project of its own that starts as a chat (``ephemeral``), so one
imported session sits in the sidebar like any chat, and a second one in the same folder makes it a
project by the ordinary rule (``ProjectStore.settle``). A folder that holds another project's folder
cannot be one (``ProjectStore`` refuses nesting) and is refused here with that project's name.

The program's file is never written. A session that is still running there is imported as it stands;
"pull in what is new" later appends what the program wrote since, and nothing goes back the other way.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

from protocore.contracts.types import COMPACTION_SUMMARY_METADATA_KEY, Message, MessageRole, TextBlock

from daedalus.host.engine_factory import TENANT
from daedalus.host.foreign_sessions import (
    HARNESS_ORDER,
    IMPORTED_KEY,
    Conversion,
    Converter,
    Plan,
    harness_name,
    large,
    match_preset,
    parse_time,
    plan_history,
)
from daedalus.host.session_runner import _forget_persisted, annotate_summary, identifier_index, operator_quotes
from daedalus.stores.projects import FolderSpec, Project, ProjectError, ProjectFolder, ProjectSettings

if TYPE_CHECKING:
    from daedalus.host.session_runner import SessionManager, SessionState
    from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

ORIGINAL_MAX_BYTES = 64 * 1024 * 1024
"""The largest original an import keeps a copy of. Above it the session is imported all the same;
only the "original" download is missing."""
READ_PAGE_BYTES = 512 << 10
"""One page of ``sessions.read``, under the daemon's 640 KiB reply limit."""
READ_MAX_PAGES = 4_000
PREVIEW_PAGES = 6
PREVIEW_TURNS = 3
TRANSCRIPT_BATCH = 500
JOBS_KEPT = 50
STAGES = ("read", "parse", "mask", "write", "index", "summarise", "open")

IMPORT_SUMMARY_INSTRUCTIONS = (
    "These turns come from a session of another agent program that the operator is continuing here. "
    "Keep what was decided, what was changed in which files, and what was still open when it stopped."
)


class ImportRefused(Exception):
    """An import that cannot be done as asked, with a code the app tells apart and the words for it."""

    def __init__(self, code: str, message: str, *, status: int = 409, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail

    def view(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.detail}


# -- the registry ---------------------------------------------------------------------------------


class ImportStore:
    """``session_imports``: which program session each imported session continues, and where its
    last import stopped. One row per program session; importing it again as a new chat moves the row
    to the new chat, which is then the one that pulls in what is new."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        try:
            cursor = json.loads(row["cursor"] or "{}")
        except ValueError:
            cursor = {}
        return {"harness": row["harness"], "ext_id": row["ext_id"], "session_id": row["session_id"], "cwd": row["cwd"],
                "cursor": cursor, "imported_at": row["imported_at"], "refreshed_at": row["refreshed_at"]}

    async def get(self, harness: str, ext_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM session_imports WHERE harness = ? AND ext_id = ?", (harness, ext_id))
        return self._row(row) if row else None

    async def for_session(self, session_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM session_imports WHERE session_id = ?", (session_id,))
        return self._row(row) if row else None

    async def imported(self, harness: str, ext_ids: list[str]) -> dict[str, dict[str, str]]:
        """``{ext_id: {session_id, title}}`` for the ones of ``ext_ids`` already imported."""
        out: dict[str, dict[str, str]] = {}
        for start in range(0, len(ext_ids), 500):
            chunk = ext_ids[start:start + 500]
            if not chunk:
                continue
            marks = ",".join("?" for _ in chunk)
            rows = await self.db.fetchall(
                f"SELECT i.ext_id, i.session_id, s.title FROM session_imports i JOIN sessions s ON s.id = i.session_id WHERE i.harness = ? AND i.ext_id IN ({marks})",
                (harness, *chunk),
            )
            out.update({str(r["ext_id"]): {"session_id": str(r["session_id"]), "title": str(r["title"] or "")} for r in rows})
        return out

    async def record(self, harness: str, ext_id: str, session_id: str, cwd: str, cursor: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO session_imports(harness, ext_id, session_id, cwd, cursor, imported_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(harness, ext_id) DO UPDATE SET session_id = excluded.session_id, cwd = excluded.cwd,"
            " cursor = excluded.cursor, imported_at = excluded.imported_at, refreshed_at = NULL",
            (harness, ext_id, session_id, cwd, json.dumps(cursor), _now()),
        )

    async def advance(self, harness: str, ext_id: str, cursor: dict[str, Any]) -> None:
        await self.db.execute("UPDATE session_imports SET cursor = ?, refreshed_at = ? WHERE harness = ? AND ext_id = ?",
                              (json.dumps(cursor), _now(), harness, ext_id))


def _now() -> str:
    return datetime.now(UTC).isoformat()


# -- folders --------------------------------------------------------------------------------------


def _windows(path: str) -> bool:
    return len(path) >= 2 and path[1] == ":" and path[0].isalpha()


def normalise_cwd(path: str) -> str:
    """A folder as the programs record it, in one spelling for comparing: no trailing separator and,
    for a Windows path, forward slashes and an upper-case drive letter."""
    text = (path or "").strip()
    if _windows(text) or "\\" in text:
        text = text.replace("\\", "/")
        if _windows(text):
            text = text[0].upper() + text[1:]
    while len(text) > 1 and text.endswith("/") and not (len(text) == 3 and _windows(text)):
        text = text[:-1]
    return text


def _key(path: str) -> str:
    # Windows and macOS compare folder names without case; Linux does not.
    normal = normalise_cwd(path)
    return normal.lower() if _windows(normal) else normal


def _inside(inner: str, outer: str) -> bool:
    a, b = _key(inner), _key(outer)
    return a != b and a.startswith(b.rstrip("/") + "/")


def basename(path: str) -> str:
    flavour = PureWindowsPath if _windows(path) else PurePosixPath
    return flavour(path).name or path


@dataclass
class Destination:
    """Where an import of a session in ``cwd`` lands.

    ``kind`` is ``project`` (a project has the folder or one around it), ``chat`` (so does a chat's
    own project, which this import makes a project), ``new_chat`` or ``new_project`` (nobody has it;
    the import makes a project of it, a chat until it holds a second session unless asked otherwise),
    or ``refused`` with ``code``: ``missing`` (not on the machine), ``contains_project`` (the folder
    holds another project's, and folders do not nest), ``inside_project`` (it is inside a folder of a
    container project) or ``installation`` (one of the installation's own projects)."""

    kind: str
    cwd: str
    project: Project | None = None
    folder: ProjectFolder | None = None
    worktree_cwd: str = ""
    code: str = ""
    problem: str = ""
    other: Project | None = None
    """The project the refusal names, or the container project with the same folder."""
    exists: bool | None = None

    def view(self) -> dict[str, Any]:
        def named(project: Project | None) -> dict[str, Any] | None:
            return {"id": project.id, "name": project.name, "ephemeral": project.settings.ephemeral} if project is not None else None

        return {
            "kind": self.kind, "cwd": self.cwd, "project": named(self.project), "folder_id": self.folder.id if self.folder else None,
            "worktree_cwd": self.worktree_cwd or None, "code": self.code or None, "problem": self.problem or None,
            "other_project": named(self.other), "exists": self.exists,
            "becomes_project": self.kind == "chat",
        }


# -- jobs -----------------------------------------------------------------------------------------


@dataclass
class ImportJob:
    id: str
    harness: str
    ext_id: str
    state: str = "running"
    stage: str = "read"
    counts: dict[str, int] = field(default_factory=dict)
    session_id: str | None = None
    error: dict[str, Any] | None = None
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    task: asyncio.Task[None] | None = None

    def view(self) -> dict[str, Any]:
        return {"job_id": self.id, "harness": self.harness, "id": self.ext_id, "state": self.state, "stage": self.stage,
                "stages": list(STAGES), "counts": dict(self.counts), "session_id": self.session_id, "error": self.error,
                "started_at": self.started_at, "finished_at": self.finished_at}


@dataclass
class ImportRequest:
    harness: str
    ext_id: str
    mode: str = ""
    model: str = ""
    project_id: str = ""
    make_project: bool = False
    cwd: str = ""
    again: bool = False


@dataclass
class ReadResult:
    header: dict[str, Any]
    turns: list[dict[str, Any]]
    masked: int = 0
    live: bool = False
    next: Any = None
    done: bool = True


class SessionImporter:
    """Reads, converts and writes imported sessions; one per manager, held by the API extension."""

    def __init__(self, manager: SessionManager, *, publish: Callable[[dict[str, Any]], Awaitable[None]] | None = None) -> None:
        self.manager = manager
        self._store: ImportStore | None = None
        self.jobs: dict[str, ImportJob] = {}
        self._publish = publish

    @property
    def store(self) -> ImportStore:
        # Made on first use, so registering the routes asks nothing of the manager but that it exists.
        if self._store is None:
            self._store = ImportStore(self.manager.db)
        return self._store

    def bridge(self) -> Any:
        bridge = self.manager.host_bridge
        if bridge is None:
            raise ConnectionError("this installation has no host terminal bridge")
        return bridge

    # -- reading --------------------------------------------------------------------------------

    async def read(self, harness: str, ext_id: str, *, start: Any = 0, pages: int = READ_MAX_PAGES,
                   progress: Callable[[int, int], Awaitable[None]] | None = None) -> ReadResult:
        """The session's turns from ``start``, page by page, for at most ``pages`` pages; ``done``
        says whether the end was reached. A daemon whose cursor stops moving ends the read rather
        than asking for the same page for ever."""
        bridge = self.bridge()
        out = ReadResult(header={}, turns=[], done=False, next=start)
        cursor = start
        for _ in range(pages):
            page = await bridge.sessions_read(harness, ext_id, start=cursor, max_bytes=READ_PAGE_BYTES, sidechains=True)
            if isinstance(page.get("header"), dict) and page["header"]:
                out.header = page["header"]
            out.turns.extend(t for t in page.get("turns") or [] if isinstance(t, dict))
            out.masked += int(page.get("masked") or 0)
            out.live = bool(page.get("live")) or out.live
            following = page.get("next")
            if progress is not None:
                # ``total`` is the turns the read pages through; ``messages`` in the header counts only
                # the visible ones, which a session of many tool calls outnumbers several times over.
                await progress(len(out.turns), int(page.get("total") or out.header.get("messages") or 0))
            if page.get("done") or following is None or following == cursor:
                out.done = bool(page.get("done")) or following is None or following == cursor
                out.next = following if following is not None else cursor
                break
            cursor = following
            out.next = cursor
        return out

    async def read_original(self, harness: str, ext_id: str, size_hint: int = 0) -> tuple[bytes | None, str]:
        """The program's own bytes of the session, or ``None`` and why not. Read raw from the daemon,
        which masks what it knows; masked again here with the operator's own secret values."""
        if size_hint and size_hint > ORIGINAL_MAX_BYTES:
            return None, f"the original is {size_hint} bytes, over the {ORIGINAL_MAX_BYTES} kept"
        bridge = self.bridge()
        chunks: list[bytes] = []
        total = 0
        cursor: Any = 0
        try:
            for _ in range(READ_MAX_PAGES):
                page = await bridge.sessions_read(harness, ext_id, start=cursor, max_bytes=READ_PAGE_BYTES, raw=True)
                data = page.get("data_b64")
                if not isinstance(data, str):
                    return None, "the host terminal daemon did not send the original"
                chunk = base64.b64decode(data)
                total += len(chunk)
                if total > ORIGINAL_MAX_BYTES:
                    return None, f"the original is over the {ORIGINAL_MAX_BYTES} bytes kept"
                chunks.append(chunk)
                following = page.get("next")
                if page.get("done") or following is None or following == cursor:
                    break
                cursor = following
        except (OSError, ConnectionError) as exc:
            return None, str(exc)
        text = b"".join(chunks).decode("utf-8", errors="surrogateescape")
        masked = self.manager.redactor.redact(text)
        return masked.encode("utf-8", errors="surrogateescape"), ""

    # -- where it goes --------------------------------------------------------------------------

    async def destination(self, cwd: str, *, make_project: bool = False, check: bool = True, projects: list[Project] | None = None) -> Destination:
        """The project a session in ``cwd`` joins, or the one an import would make (see the module).
        ``check`` asks the machine whether the folder is there; a listing of many sessions passes the
        projects it read once and does not ask."""
        cwd = normalise_cwd(cwd)
        if not cwd:
            return Destination("refused", cwd, code="missing", problem="the session does not say which folder it worked in")
        exact: tuple[Project, ProjectFolder] | None = None
        around: list[tuple[int, Project, ProjectFolder]] = []
        inner: Project | None = None
        container_same: Project | None = None
        container_around: Project | None = None
        for project in projects if projects is not None else await self.manager.projects.list():
            for folder in project.folders:
                path = normalise_cwd(str(folder.path))
                if folder.env == "host":
                    if _key(path) == _key(cwd):
                        exact = exact or (project, folder)
                    elif _inside(cwd, path):
                        around.append((len(path), project, folder))
                    elif _inside(path, cwd):
                        inner = inner or project
                else:
                    if _key(path) == _key(cwd):
                        container_same = container_same or project
                    elif _inside(cwd, path):
                        container_around = container_around or project
                    elif _inside(path, cwd):
                        inner = inner or project
        exists: bool | None = None
        if check:
            exists = await self._exists(cwd)
        chosen = exact or (max(around, key=lambda row: row[0])[1:] if around else None)
        if chosen is not None:
            project, folder = chosen
            if project.settings.system or project.setup_by == "dispatcher":
                return Destination("refused", cwd, code="installation", problem=f"{folder.path} is a folder of {project.name}, which belongs to the installation", other=project, exists=exists)
            worktree = cwd if _key(cwd) != _key(str(folder.path)) else ""
            kind = "chat" if project.settings.ephemeral else "project"
            return Destination(kind, cwd, project=project, folder=folder, worktree_cwd=worktree, exists=exists, other=container_same)
        if exists is False:
            return Destination("refused", cwd, code="missing", problem=f"{cwd} is not on the machine any more; choose the folder to continue in", exists=False)
        if inner is not None:
            return Destination("refused", cwd, code="contains_project", problem=f"{cwd} holds a folder of the project {inner.name}, and project folders do not nest; continue in that project's folder or choose another", other=inner, exists=exists)
        if container_around is not None:
            return Destination("refused", cwd, code="inside_project", problem=f"{cwd} is inside a folder of the project {container_around.name}, which works in the container", other=container_around, exists=exists)
        return Destination("new_project" if make_project else "new_chat", cwd, other=container_same, exists=exists)

    async def _exists(self, cwd: str) -> bool | None:
        try:
            await self.bridge().browse(cwd, limit=1)
        except FileNotFoundError:
            return False
        except (OSError, ConnectionError):
            return None
        return True

    # -- the model ------------------------------------------------------------------------------

    def choose_model(self, source_model: str, asked: str = "") -> dict[str, Any]:
        """The preset an import runs on: the one asked for, else the one that runs the model the session
        ran on, else the default. ``same`` says whether it is the session's own model."""
        config = self.manager.config
        presets = config.presets
        if asked:
            if asked not in presets:
                raise ImportRefused("invalid", f"{asked!r} is not one of the models", status=400)
            preset_id = asked
        else:
            preset_id = match_preset(presets, source_model) or ""
        same = bool(preset_id) and preset_id == match_preset(presets, source_model)
        if not preset_id:
            found = config.default_preset()
            preset_id = found[0] if found else ""
        preset = presets.get(preset_id)
        return {"source": source_model or "", "preset": preset_id or None, "label": preset.display(preset_id) if preset else "",
                "same": same, "window": int(preset.context_window) if preset else 128_000}

    # -- preview --------------------------------------------------------------------------------

    async def preview(self, harness: str, ext_id: str) -> dict[str, Any]:
        """What the import window shows before anything is written: the first and last exchanges, the
        counts, the size the history would be on the model it would run on, the mode that size
        suggests, and where it would land. Reads at most a few pages; a longer session is estimated
        from what was read and the size of its files."""
        read = await self.read(harness, ext_id, pages=PREVIEW_PAGES)
        header = read.header
        conversion = Converter(harness, redactor=self.manager.redactor).convert(read.turns)
        model = self.choose_model(str(header.get("model") or ""))
        tokens = conversion.tokens()
        if not read.done and read.turns:
            # Scale by how far into the session the pages got: the turns read against the turns it has.
            whole = int(header.get("messages") or 0)
            seen = max(1, sum(1 for t in read.turns if t.get("role") in ("user", "assistant") and not t.get("sidechain")))
            if whole > seen:
                tokens = int(tokens * whole / seen)
        window = model["window"]
        messages = sum(1 for e in conversion.entries if e.history is not None)
        suggested = "tail" if (tokens > 0.6 * window or messages > 2000 or (not read.done and int(header.get("messages") or 0) > 2000)) else "full"
        destination = await self.destination(str(header.get("cwd") or ""))
        imported = await self.store.get(harness, ext_id)
        imported_as = None
        if imported is not None:
            row = await self.manager.db.fetchone("SELECT title FROM sessions WHERE id = ?", (imported["session_id"],))
            imported_as = {"session_id": imported["session_id"], "title": str(row["title"]) if row else ""}

        def brief(entries: list[Any]) -> list[dict[str, Any]]:
            return [{"role": e.transcript.role.value, "text": e.transcript.text[:600], "at": e.transcript.created_at.isoformat()} for e in entries]

        talk = [e for e in conversion.entries if not e.sidechain and e.transcript.role in (MessageRole.user, MessageRole.assistant) and e.transcript.text.strip()]
        return {
            "header": header, "harness_name": harness_name(harness),
            "first": brief(talk[:PREVIEW_TURNS]), "last": brief(talk[-PREVIEW_TURNS:]) if read.done else [],
            "complete": read.done, "counts": conversion.counts, "messages": messages,
            "tokens": tokens, "window": window, "suggested_mode": suggested,
            "model": model, "models": [{"id": pid, "label": p.display(pid), "window": p.context_window} for pid, p in self.manager.config.presets.items()],
            "destination": destination.view(), "imported_as": imported_as,
            "live": read.live or bool((header.get("flags") or {}).get("live")),
            "masked": read.masked + conversion.masked,
        }

    # -- the job --------------------------------------------------------------------------------

    def running(self, harness: str, ext_id: str) -> ImportJob | None:
        return next((j for j in self.jobs.values() if j.state == "running" and j.harness == harness and j.ext_id == ext_id), None)

    async def start(self, request: ImportRequest) -> ImportJob:
        """Check what can be checked at once, then import in the background; the job says how far it got."""
        if request.mode not in ("", "full", "tail"):
            raise ImportRefused("invalid", "the mode is full or tail", status=400)
        if request.model:
            self.choose_model("", request.model)
        busy = self.running(request.harness, request.ext_id)
        if busy is not None:
            raise ImportRefused("in_progress", "this session is being imported already", job_id=busy.id)
        existing = await self.store.get(request.harness, request.ext_id)
        if existing is not None and not request.again:
            raise ImportRefused("already_imported", "this session has been imported already; pull in what is new there, or import it again as a new chat",
                                session_id=existing["session_id"])
        job = ImportJob(id=uuid.uuid4().hex[:12], harness=request.harness, ext_id=request.ext_id)
        self.jobs[job.id] = job
        for old in list(self.jobs)[:-JOBS_KEPT]:
            if self.jobs[old].state != "running":
                del self.jobs[old]
        job.task = asyncio.create_task(self._run(job, request), name=f"import-{job.id}")
        await self._progress(job)
        return job

    async def _progress(self, job: ImportJob, stage: str | None = None, **counts: int) -> None:
        if stage is not None:
            job.stage = stage
        job.counts.update(counts)
        if self._publish is not None:
            try:
                await self._publish(job.view())
            except Exception:  # noqa: BLE001 — the import goes on whether or not anyone hears about it
                logger.exception("import progress could not be published")

    async def _run(self, job: ImportJob, request: ImportRequest) -> None:
        try:
            job.session_id = await self.run(job, request)
            job.state = "done"
        except ImportRefused as exc:
            job.state, job.error = "failed", exc.view()
        except (OSError, ConnectionError) as exc:
            job.state, job.error = "failed", {"code": "host", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 — a failed import is reported on the job, not lost in a task
            logger.exception("import of %s session %s failed", request.harness, request.ext_id)
            job.state, job.error = "failed", {"code": "failed", "message": str(exc)[:500]}
        job.finished_at = _now()
        await self._progress(job)

    async def run(self, job: ImportJob, request: ImportRequest) -> str:
        """Import one session and return the new session's id. A failure after the session was made
        removes it, with a project made for it, so a half-written chat is never left behind."""
        manager = self.manager

        async def read_progress(turns: int, total: int) -> None:
            await self._progress(job, turns_read=turns, turns_total=total)

        read = await self.read(request.harness, request.ext_id, progress=read_progress)
        header = read.header
        if not read.turns:
            raise ImportRefused("empty", "the session has nothing to import")
        await self._progress(job, "parse", turns_read=len(read.turns))
        await self._progress(job, "mask")
        conversion = Converter(request.harness, redactor=manager.redactor).convert(read.turns)
        masked = read.masked + conversion.masked
        await self._progress(job, masked=masked, messages=len(conversion.entries))
        cwd = normalise_cwd(request.cwd or str(header.get("cwd") or ""))
        destination = await self.destination(cwd, make_project=request.make_project)
        if destination.kind == "refused":
            raise ImportRefused(destination.code, destination.problem, destination=destination.view())
        if request.project_id and (destination.project is None or destination.project.id != request.project_id):
            raise ImportRefused("project_mismatch", f"{cwd} is not a folder of that project; add it to the project first", destination=destination.view())
        model = self.choose_model(str(header.get("model") or ""), request.model)
        mode = request.mode or ("tail" if large(conversion, model["window"]) else "full")

        await self._progress(job, "write")
        made_project: Project | None = None
        state: SessionState | None = None
        try:
            state, made_project = await self._open(destination, header, request, model, conversion, masked, mode, read)
            sid = state.session.id
            job.session_id = sid
            for data, mime in conversion.blobs.values():
                await manager.blobs.put(TENANT, data, content_type=mime)
            written = 0
            transcripts = [e.transcript for e in conversion.entries]
            for start in range(0, len(transcripts), TRANSCRIPT_BATCH):
                batch = transcripts[start:start + TRANSCRIPT_BATCH]
                await manager.sessions.append_transcript(sid, batch)
                written += len(batch)
                await self._progress(job, written=written)
            await self._progress(job, "index")
            seqs = await self._seqs(sid, [manager.sessions.transcript_key(m) for m in transcripts])
            plan = plan_history(conversion, mode=mode, window=model["window"])
            history = await self._history(job, state, request.harness, conversion, plan, seqs)
            await manager.sessions.replace_messages(sid, TENANT, history)
            state.history_keys = [manager.sessions.transcript_key(m) for m in history]
            _forget_persisted(state)
            original, why = await self.read_original(request.harness, request.ext_id, int(header.get("bytes") or 0))
            stored: dict[str, Any] = {"stored": False, "reason": why}
            if original is not None:
                meta = await manager.blobs.put(TENANT, gzip.compress(original), content_type="application/gzip")
                stored = {"stored": True, "ref": meta.ref, "bytes": len(original), "name": f"{request.harness}-{request.ext_id}.jsonl"}
            cursor = self._cursor(conversion, read)
            await self.store.record(request.harness, request.ext_id, sid, cwd, cursor)
            imported = dict(state.session.metadata.get("imported") or {})
            imported.update(cursor=cursor, mode=plan.mode, history_messages=len(history), original=stored)
            await self._set_imported(state, imported)
            await self._progress(job, "open", history=len(history))
            return sid
        except BaseException:
            if state is not None:
                try:
                    await manager.delete_session(state.session.id)
                except Exception:  # noqa: BLE001
                    logger.exception("could not remove the half-imported session %s", state.session.id)
            if made_project is not None and await manager.projects.get(made_project.id) is not None:
                try:
                    await manager.projects.delete(made_project.id)
                    await manager.reload_project(None, made_project.id)
                except Exception:  # noqa: BLE001
                    logger.exception("could not remove the project made for a failed import")
            job.session_id = None
            raise

    async def _open(self, destination: Destination, header: dict[str, Any], request: ImportRequest, model: dict[str, Any],
                    conversion: Conversion, masked: int, mode: str, read: ReadResult) -> tuple[SessionState, Project | None]:
        """Make the session where :meth:`destination` said, the project too when it is new, and set
        its model. Never through ``create_session(on_host=True)``, which makes a scratch folder."""
        manager = self.manager
        made: Project | None = None
        project, folder = destination.project, destination.folder
        if project is None:
            settings = ProjectSettings(ephemeral=not request.make_project, default_env="host", snapshots=False)
            try:
                project = await manager.projects.create(basename(destination.cwd) or "Imported", [FolderSpec(destination.cwd, env="host")], settings=settings)
            except ProjectError as exc:
                raise ImportRefused("refused", str(exc)) from exc
            made = project
            folder = project.primary
            await manager.bus.publish("project.changed", {"change": "created", "actor": "operator"}, project_id=project.id)
        assert folder is not None
        title = str(header.get("title") or "").strip()[:128] or f"{harness_name(request.harness)} session"
        imported = {
            "harness": request.harness, "ext_id": request.ext_id, "cwd": destination.cwd, "source": header.get("source"),
            "imported_at": _now(), "version": 1, "counts": conversion.counts, "masked": masked, "mode": mode,
            "source_model": header.get("model") or "", "branch": header.get("branch") or "", "live": read.live,
            "complete": read.done, "transcript_dropped": conversion.dropped,
        }
        metadata: dict[str, Any] = {"telegram_detached": True, "imported": imported}
        if destination.worktree_cwd:
            metadata["worktree_cwd"] = destination.worktree_cwd
        try:
            state = await manager.create_session(title, project_id=project.id, folder_id=folder.id, metadata=metadata)
        except Exception:
            if made is not None:
                await manager.projects.delete(made.id)
            raise
        if model["preset"]:
            await manager.set_model(state.session.id, preset=model["preset"])
        return state, made

    async def _seqs(self, session_id: str, keys: list[str]) -> dict[str, int]:
        out: dict[str, int] = {}
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            rows = await self.manager.db.fetchall(f"SELECT key, seq FROM transcript WHERE session_id = ? AND key IN ({marks})", (session_id, *chunk))
            out.update({str(r["key"]): int(r["seq"]) for r in rows})
        return out

    async def _history(self, job: ImportJob, state: SessionState, harness: str, conversion: Conversion, plan: Plan, seqs: dict[str, int]) -> list[Message]:
        manager = self.manager
        entries = conversion.entries
        sid = state.session.id
        archived = sorted({seqs[k] for k in (manager.sessions.transcript_key(entries[i].transcript) for i in plan.archived) if k in seqs})
        if plan.mode == "full":
            history: list[Message] = []
            for index in plan.kept:
                message = (entries[index].tail_history or entries[index].history) if plan.cut else entries[index].history
                assert message is not None
                if index == plan.summary and archived:
                    note = f"[archived turns seq {archived[0]}–{archived[-1]}: HistoryExpand({archived[0]}, {archived[-1]}) returns them verbatim]"
                    key = manager.sessions.transcript_key(message)
                    message = annotate_summary(message, note, archived)
                    await manager.sessions.replace_transcript_message(sid, key, message)
                history.append(message)
            return history
        await self._progress(job, "summarise", gap=len(plan.gap))
        parts: list[str] = []
        if plan.summary >= 0:
            own = re.sub(r"</?compacted-turn[^>]*>", "", entries[plan.summary].transcript.text).strip()
            parts.append(f"## What {harness_name(harness)} summarised at its last compaction\n\n{own}")
        gap = [entries[i].history for i in plan.gap if entries[i].history is not None]
        if gap:
            try:
                ours = await manager.summarise_with_fallback(state, gap, IMPORT_SUMMARY_INSTRUCTIONS,
                                                             progress=lambda **f: self._progress(job, **{k: v for k, v in f.items() if isinstance(v, int)}))
                parts.append(f"## The turns after it\n\n{ours}" if parts else ours)
            except Exception as exc:  # noqa: BLE001 — the import stands without our summary; the turns are in the transcript
                logger.warning("import of %s: the turns before the kept tail could not be summarised: %s", sid, exc)
                parts.append(f"[{len(gap)} earlier messages could not be summarised at import ({str(exc)[:200]}); HistoryExpand returns them]")
            parts.append(operator_quotes(gap) + identifier_index(gap))
        note = f"\n\n[archived turns seq {archived[0]}–{archived[-1]}: HistoryExpand({archived[0]}, {archived[-1]}) returns them verbatim]" if archived else ""
        body = manager.redactor.redact("\n\n".join(p for p in parts if p.strip()).strip())
        kept = [entries[i].tail_history or entries[i].history for i in plan.kept]
        last = max((e.transcript.created_at for e in entries), default=datetime.now(UTC))
        summary = Message(
            role=MessageRole.user,
            content_blocks=[TextBlock(text=f"<compacted-turn id='import:{harness}'>{body}{note}</compacted-turn>")],
            created_at=max(datetime.now(UTC), last),
            metadata={
                COMPACTION_SUMMARY_METADATA_KEY: True,
                # ``messages`` is everything the summary stands for, which the chat says came "before it";
                # ``summarised`` is the part a model of ours condensed.
                "daedalus.compaction": {"reason": "import", "source": harness, "messages": len(archived) or len(gap), "summarised": len(gap), "kept": len(kept), "at": _now()},
                "daedalus.archived": {"from_seq": archived[0], "to_seq": archived[-1], "seqs": archived} if archived else {"seqs": []},
                IMPORTED_KEY: {"harness": harness, "ext_id": "", "summary": True},
            },
        )
        await manager.sessions.append_transcript(sid, [summary])
        return [summary, *[m for m in kept if m is not None]]

    @staticmethod
    def _cursor(conversion: Conversion, read: ReadResult) -> dict[str, Any]:
        return {"turn": conversion.last_seq, "next": read.next, "ext_last": conversion.last_ext,
                "at": conversion.last_at.isoformat() if conversion.last_at else None}

    async def _set_imported(self, state: SessionState, imported: dict[str, Any]) -> None:
        metadata = {**state.session.metadata, "imported": imported}
        await self.manager.sessions.update_metadata(state.session.id, metadata)
        state.session = state.session.model_copy(update={"metadata": metadata})
        state.metadata["imported"] = imported

    # -- later ----------------------------------------------------------------------------------

    async def refresh(self, session_id: str) -> dict[str, Any]:
        """Append what the program wrote to the session since it was imported or last refreshed.

        Turns are taken past the last one imported, by their number in the session; a turn the
        program was still writing when the snapshot was taken keeps the part it had then. The
        session must be idle: its working history is rewritten with the new turns at its end."""
        manager = self.manager
        row = await self.store.for_session(session_id)
        if row is None:
            state = await manager.get_state(session_id)
            if state is None:
                raise ImportRefused("missing", "no such session", status=404)
            if state.session.metadata.get("imported"):
                raise ImportRefused("superseded", "a later import of the same session pulls in what is new; this chat is a copy")
            raise ImportRefused("not_imported", "this session was not imported from another program")
        cursor = row["cursor"]
        last_turn = int(cursor.get("turn") if cursor.get("turn") is not None else -1)
        start = cursor.get("next") if cursor.get("next") not in (None, "") else 0
        read = await self.read(row["harness"], row["ext_id"], start=start)
        fresh = [t for t in read.turns if int(t.get("seq") or 0) > last_turn]
        state = await manager.get_state(session_id)
        if state is None:
            raise ImportRefused("missing", "no such session", status=404)
        if not fresh:
            await self.store.advance(row["harness"], row["ext_id"], {**cursor, "next": read.next})
            return {"session_id": session_id, "added": 0, "turns": 0, "masked": 0, "live": read.live}
        conversion = Converter(row["harness"], redactor=manager.redactor, after=parse_time(cursor.get("at"))).convert(fresh)
        async with state.lock:
            try:
                await manager._wait_until_quiet(state)
            except RuntimeError as exc:
                raise ImportRefused("busy", str(exc)) from exc
            for data, mime in conversion.blobs.values():
                await manager.blobs.put(TENANT, data, content_type=mime)
            transcripts = [e.transcript for e in conversion.entries]
            for begin in range(0, len(transcripts), TRANSCRIPT_BATCH):
                await manager.sessions.append_transcript(session_id, transcripts[begin:begin + TRANSCRIPT_BATCH])
            added = [e.history for e in conversion.entries if e.history is not None]
            current = list(state.engine.history) if state.engine is not None else list(await manager.sessions.list_messages(session_id, TENANT, limit=100_000))
            history = [*current, *added]
            await manager.sessions.replace_messages(session_id, TENANT, history)
            if state.engine is not None:
                state.engine.history = history
            state.history_keys = [manager.sessions.transcript_key(m) for m in history]
            _forget_persisted(state)
        merged = {**cursor, **self._cursor(conversion, read)}
        await self.store.advance(row["harness"], row["ext_id"], merged)
        imported = dict(state.session.metadata.get("imported") or {})
        imported.update(cursor=merged, refreshed_at=_now(), live=read.live, masked=int(imported.get("masked") or 0) + read.masked + conversion.masked)
        await self._set_imported(state, imported)
        return {"session_id": session_id, "added": len(conversion.entries), "turns": len(fresh), "masked": read.masked + conversion.masked, "live": read.live}

    async def original(self, session_id: str) -> tuple[bytes, str] | None:
        """The masked copy of the program's own file an import kept, and its file name."""
        state = await self.manager.get_state(session_id)
        if state is None:
            return None
        stored = (state.session.metadata.get("imported") or {}).get("original") or {}
        ref = stored.get("ref")
        if not stored.get("stored") or not isinstance(ref, str):
            return None
        data = await self.manager.blobs.get(TENANT, ref)
        return gzip.decompress(data), str(stored.get("name") or f"{session_id}.jsonl")


def imported_view(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """What a session's header and details show of where it came from: the program, its session, the
    model there, how it was brought in, whether that program was still writing it, and whether the
    original is kept. The blob reference and the cursor stay on the server."""
    imported = metadata.get("imported") if isinstance(metadata, dict) else None
    if not isinstance(imported, dict):
        return None
    original = imported.get("original") if isinstance(imported.get("original"), dict) else {}
    harness = str(imported.get("harness") or "")
    return {
        "harness": harness, "harness_name": harness_name(harness), "id": imported.get("ext_id"), "cwd": imported.get("cwd"),
        "source_model": imported.get("source_model") or "", "branch": imported.get("branch") or "", "mode": imported.get("mode"),
        "live": bool(imported.get("live")), "complete": imported.get("complete", True), "imported_at": imported.get("imported_at"),
        "refreshed_at": imported.get("refreshed_at"), "masked": int(imported.get("masked") or 0), "counts": imported.get("counts") or {},
        "transcript_dropped": int(imported.get("transcript_dropped") or 0),
        "original": {"stored": bool(original.get("stored")), "bytes": original.get("bytes"), "name": original.get("name"), "reason": original.get("reason")},
    }


def ordered_harnesses(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The programs in the order the import window lists them: Claude Code, Codex, then the rest as
    the daemon gave them."""
    def rank(row: dict[str, Any]) -> int:
        harness = str(row.get("id") or "")
        return HARNESS_ORDER.index(harness) if harness in HARNESS_ORDER else len(HARNESS_ORDER)

    return sorted(rows, key=rank)


__all__ = ["ORIGINAL_MAX_BYTES", "Destination", "ImportJob", "ImportRefused", "ImportRequest", "ImportStore", "SessionImporter",
           "basename", "imported_view", "normalise_cwd", "ordered_harnesses"]
