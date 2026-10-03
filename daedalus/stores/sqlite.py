"""SQLite implementations of the core store contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import aiosqlite
from protocore.contracts.events import IEventStream
from protocore.contracts.run import IRunStore, RunNotFoundError
from protocore.contracts.session import ISessionStore, SessionNotFoundError
from protocore.contracts.types import (
    COMPACTION_SUMMARY_METADATA_KEY,
    Event,
    Message,
    MessageRole,
    Run,
    RunStatus,
    Session,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from daedalus.providers.openai_compat import UsageRecord, UsageSink
from daedalus.stores.database import Database
from daedalus.stores.inference_budget import InferenceBudget
from daedalus.stores.projects import ProjectSettings

UNFINISHED_RUN_STATUSES = ", ".join(f"'{status.value}'" for status in (RunStatus.queued, RunStatus.running, RunStatus.paused))
"""The statuses of a run that is not over. Its events are what the live view reads and what a resume
replays, so nothing sweeps them by age."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _like_literal(value: str) -> str:
    """``value`` as a LIKE pattern that matches itself: a call id is an opaque string and may hold
    a wildcard as easily as any other character."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _mentions_call(message: Message, call_id: str) -> bool:
    return any(getattr(block, "tool_call_id", None) == call_id for block in message.content_blocks)


def message_text(message: Message) -> str:
    """Everything searchable in a message: text, thinking is skipped, tool calls as name+args, tool results."""
    parts: list[str] = []
    for block in message.content_blocks:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            parts.append(f"{block.name} {block.arguments_json or ''}")
        elif isinstance(block, ToolResultBlock):
            parts.append(block.content or "")
    return "\n".join(p for p in parts if p).strip()


_FTS_TOKEN_RE = re.compile(r"[\w][\w'.-]*", re.UNICODE)


def fts_query(query: str) -> str:
    """Turn free text into an FTS5 MATCH expression: every word required, as a prefix.

    Quotes each token so punctuation in identifiers (``config.toml``, ``run-cap``) cannot
    be read as FTS syntax; an explicitly quoted phrase is passed through as a phrase.
    """
    query = query.strip()
    if query.startswith('"') and query.endswith('"') and len(query) > 2 and '"' not in query[1:-1]:
        return '"' + query[1:-1] + '"'
    tokens = _FTS_TOKEN_RE.findall(query)
    return " ".join(f'"{t.replace(chr(34), "")}"*' for t in tokens if t)


def snippet(text: str, query: str, *, window: int = 160) -> str:
    """A window of ``text`` around the first query word, with every query word bracketed.

    fts5 renders this itself when it keeps a copy of the indexed text; a contentless index
    does not keep one, and the transcript row it points at has the text anyway.
    """
    words = [w for w in (t.lower() for t in _FTS_TOKEN_RE.findall(query)) if w]
    flat = " ".join(text.split())
    if not words or not flat:
        return flat[:window]
    lowered = flat.lower()
    at = min((p for p in (lowered.find(w) for w in words) if p >= 0), default=-1)
    start = max(0, at - window // 3) if at >= 0 else 0
    start = flat.rfind(" ", 0, start) + 1 if start else 0
    cut = flat[start : start + window]
    marked = _FTS_TOKEN_RE.sub(lambda m: f"[{m.group(0)}]" if m.group(0).lower().startswith(tuple(words)) else m.group(0), cut)
    return ("… " if start else "") + marked + (" …" if start + window < len(flat) else "")


class TranscriptView(Protocol):
    """What turns a transcript row into the shape a client draws, and names its own version.

    The store keeps the result beside the row and never looks inside it; the host supplies
    the implementation (:class:`daedalus.host.transcript_view.TranscriptViewBuilder`), which
    is why the view can depend on redaction and prompts without the store doing so.
    """

    def key(self) -> str: ...

    def build(self, message: Message) -> dict[str, Any]: ...


class SqliteSessionStore(ISessionStore):
    def __init__(self, db: Database, *, view: TranscriptView | None = None, media: Any | None = None) -> None:
        self._db = db
        self._view = view
        self._media = media

    async def create(self, session: Session, *, project_id: str | None = None) -> None:
        if project_id is None:
            project_id = session.id
            async with self._db.transaction() as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO projects(id, name, created_at, settings, system) VALUES (?, ?, ?, ?, '')",
                    (
                        project_id,
                        session.title.strip() or "Project",
                        session.created_at.isoformat(),
                        json.dumps(ProjectSettings(snapshots=True, default_env=self._db.local_env).dump()),
                    ),
                )
                await conn.execute(
                    "INSERT OR IGNORE INTO project_folders(id, project_id, path, env, created_at) VALUES (?, ?, ?, ?, ?)",
                    (f"f-{project_id}", project_id, str(self._db.workspaces_dir / session.id), self._db.local_env, session.created_at.isoformat()),
                )
        await self._db.execute(
            "INSERT OR REPLACE INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session.id,
                session.tenant_id,
                session.title,
                session.created_at.isoformat(),
                session.last_message_at.isoformat(),
                json.dumps(session.metadata),
                project_id,
            ),
        )

    async def get(self, session_id: str, tenant_id: str) -> Session:
        row = await self._db.fetchone(
            "SELECT * FROM sessions WHERE id = ? AND tenant_id = ?", (session_id, tenant_id)
        )
        if row is None:
            raise SessionNotFoundError(session_id)
        return _row_to_session(row)

    CURRENT_GEN = "(SELECT coalesce(max(gen), 0) FROM session_messages WHERE session_id = ? AND tenant_id = ?)"
    """The generation a session's working history is currently in; older ones are not read. Filtered by
    tenant like every read and every write of this table: a read that missed the filter would answer
    with an empty history and the write after it would delete another tenant's generations."""

    async def append_message(self, session_id: str, tenant_id: str, message: Message) -> None:
        await self._db.execute(
            f"INSERT INTO session_messages(session_id, tenant_id, gen, key, message) VALUES (?, ?, {self.CURRENT_GEN}, ?, ?)",
            (session_id, tenant_id, session_id, tenant_id, self.transcript_key(message), message.model_dump_json()),
        )
        await self._db.execute(
            "UPDATE sessions SET last_message_at = ? WHERE id = ?", (_now(), session_id)
        )

    async def list_messages(
        self, session_id: str, tenant_id: str, *, limit: int = 100, offset: int = 0
    ) -> Sequence[Message]:
        rows = await self._db.fetchall(
            f"SELECT message FROM session_messages WHERE session_id = ? AND tenant_id = ? AND gen = {self.CURRENT_GEN}"
            " ORDER BY seq LIMIT ? OFFSET ?",
            (session_id, tenant_id, session_id, tenant_id, limit, offset),
        )
        return [Message.model_validate_json(r["message"]) for r in rows]

    # -- host extras --------------------------------------------------------------

    async def list_sessions(self, tenant_id: str, *, limit: int = 100, ids: list[str] | None = None) -> list[Session]:
        scope = ""
        params: list[Any] = [tenant_id]
        if ids is not None:
            if not ids:
                return []
            scope = " AND id IN (" + ",".join("?" for _ in ids[:50]) + ")"
            params.extend(ids[:50])
        rows = await self._db.fetchall(
            f"SELECT * FROM sessions WHERE tenant_id = ?{scope} ORDER BY last_message_at DESC, id LIMIT ?",
            (*params, limit),
        )
        return [_row_to_session(r) for r in rows]

    async def update_metadata(self, session_id: str, metadata: dict[str, Any]) -> None:
        await self._db.execute(
            "UPDATE sessions SET metadata = ? WHERE id = ?", (json.dumps(metadata), session_id)
        )

    async def update_title(self, session_id: str, title: str) -> None:
        await self._db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))

    # -- transcript (display history, append-only) ------------------------------

    @staticmethod
    def transcript_key(message: Message) -> str:
        """Identity of a message across history rewrites: role + creation time (+ tool call id).

        A message that asks for two things at once carries two call ids and is named by the first of
        them — the key is an identity, not an index, and changing its shape would make every message
        already stored a different message. ``messages_for_call`` is where the other calls of a batch
        are found, and it does not assume the key names the one it was asked about.
        """
        extra = ""
        for block in message.content_blocks:
            call_id = getattr(block, "tool_call_id", None)
            if call_id:
                extra = f":{call_id}"
                break
        return f"{message.role.value}:{message.created_at.isoformat()}{extra}"

    async def append_transcript(self, session_id: str, messages: Sequence[Message], *, from_history: bool = False) -> int:
        """Append messages not yet in the transcript (by key); returns how many were added.

        ``from_history`` marks a sync from the model's working history: a user-role message
        there that the host did not tag as the operator's was injected by the core (a nudge,
        a budget notice) and is recorded as such so the UI can hide it.
        """
        if not messages:
            return 0
        if from_history:
            messages = [
                m.model_copy(update={"metadata": {**m.metadata, "daedalus.origin": "core"}})
                if m.role is MessageRole.user and "daedalus.origin" not in m.metadata and not m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY)
                else m
                for m in messages
            ]
        seen: set[str] = set()
        candidates: list[tuple[str, Message]] = []
        for message in messages:
            key = self.transcript_key(message)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((key, message))
        # The caller hands over the whole history every round and almost all of it is already
        # here. Asking which keys are present is one indexed query; without it every round
        # serialised — and, since the view is built here, redacted — a history's worth of
        # messages to write one. INSERT OR IGNORE below still settles a race.
        present = await self.transcript_keys_present(session_id, [key for key, _ in candidates])
        candidates = [(key, message) for key, message in candidates if key not in present]
        if not candidates:
            return 0
        added = 0
        last_seq = 0
        async with self._db.transaction() as conn:
            # UNIQUE(session_id, key) + INSERT OR IGNORE is the dedup; rowcount says whether the row is new.
            for key, message in candidates:
                if self._media is not None:
                    message = await self._media.bind_message(conn, session_id, key, message)
                cursor = await conn.execute(
                    "INSERT OR IGNORE INTO transcript(session_id, key, message, view, view_key) VALUES (?, ?, ?, ?, ?)",
                    (session_id, key, message.model_dump_json(), *self._view_of(message)),
                )
                if cursor.rowcount:
                    await conn.execute("UPDATE sessions SET last_message_at = ? WHERE id = ?", (_now(), session_id))
                if cursor.rowcount:
                    added += 1
                    last_seq = max(last_seq, int(cursor.lastrowid or 0))
                    text = message_text(message)
                    if text:
                        await conn.execute("INSERT INTO transcript_fts(rowid, text) VALUES (?, ?)", (cursor.lastrowid, text))
            if added:
                # Rows written here are indexed here; the backfill watermark must never fall behind them.
                await conn.execute(
                    "INSERT INTO kv(key, value) VALUES ('transcript_fts_watermark', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = CASE WHEN CAST(kv.value AS INTEGER) < excluded.value THEN excluded.value ELSE kv.value END",
                    (json.dumps(last_seq),),
                )
        return added

    BACKFILL_BATCH = 500

    async def backfill_transcript_index(self, progress: Callable[[int, int], None] | None = None) -> int:
        """Index transcript rows written before the full-text table existed; returns how many.

        Works up from a watermark in ``kv`` in batches, each in its own short transaction, so a
        large transcript is indexed without holding the connection for the whole pass. ``progress``
        is called with (rows walked, rows to walk) after each batch: while this runs a search finds
        nothing in what it has not reached yet, and "nothing" reads as "that was never said".
        """
        watermark_row = await self._db.kv_get("transcript_fts_watermark", None)
        counts = await self._db.fetchone(
            "SELECT (SELECT count(*) FROM transcript_fts) fts, (SELECT count(*) FROM transcript) rows,"
            " (SELECT count(*) FROM transcript WHERE seq > ?) pending",
            (int(watermark_row or 0),),
        )
        total = int(counts["pending"]) if counts else 0
        if watermark_row is None or (counts is not None and int(counts["fts"]) > int(counts["rows"])):
            # No watermark yet, or more index rows than transcript rows (an older indexer double-counted):
            # the index is derived data, so rebuild it from the transcript rather than reason about it.
            await self._db.execute("DELETE FROM transcript_fts")
            await self._db.kv_set("transcript_fts_watermark", 0)
            watermark_row = 0
            total = int(counts["rows"]) if counts else 0
        watermark = int(watermark_row or 0)
        indexed = 0
        walked = 0
        if progress is not None and total:
            progress(0, total)
        while True:
            rows = await self._db.fetchall(
                "SELECT seq, session_id, message FROM transcript WHERE seq > ? ORDER BY seq LIMIT ?", (watermark, self.BACKFILL_BATCH)
            )
            if not rows:
                break
            async with self._db.transaction() as conn:
                for row in rows:
                    message = Message.model_validate_json(row["message"])
                    text = message_text(message)
                    if text:
                        await conn.execute("INSERT INTO transcript_fts(rowid, text) VALUES (?, ?)", (row["seq"], text))
                        indexed += 1
                watermark = int(rows[-1]["seq"])
                await conn.execute(
                    "INSERT INTO kv(key, value) VALUES ('transcript_fts_watermark', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (json.dumps(watermark),),
                )
            walked += len(rows)
            if progress is not None and total:
                progress(min(walked, total), total)
            await asyncio.sleep(0)  # let the loop breathe between batches
        if progress is not None and total:
            progress(total, total)
        return indexed

    SNIPPET_WINDOW = 160
    """Characters of context a search hit is shown with, centred on the first match."""

    async def search_transcript(self, query: str, *, session_id: str | None, limit: int = 10) -> list[dict[str, Any]]:
        """Full-text search; ``session_id=None`` searches every session. Returns seq, role, session, snippet.

        The index is contentless, so the hit names a transcript row and the row supplies the
        text — which is where it was all along. A handful of rows are read per search.
        """
        match = fts_query(query)
        if not match:
            return []
        where = " AND t.session_id = ?" if session_id is not None else ""
        params: list[Any] = [match] + ([session_id] if session_id is not None else []) + [limit]
        rows = await self._db.fetchall(
            "SELECT t.seq, t.session_id, t.message, s.title FROM transcript_fts f"
            " JOIN transcript t ON t.seq = f.rowid LEFT JOIN sessions s ON s.id = t.session_id"
            f" WHERE transcript_fts MATCH ?{where} ORDER BY bm25(transcript_fts) LIMIT ?",
            tuple(params),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            message = Message.model_validate_json(row["message"])
            out.append(
                {
                    "seq": int(row["seq"]),
                    "session_id": row["session_id"],
                    "role": message.role.value,
                    "title": row["title"] if session_id is None else None,
                    "snippet": snippet(message_text(message), query, window=self.SNIPPET_WINDOW),
                    "queued": bool(message.metadata.get("daedalus.queued")),
                }
            )
        return out

    async def expand_transcript(self, session_id: str, from_seq: int, to_seq: int) -> list[tuple[int, Message]]:
        rows = await self._db.fetchall(
            "SELECT seq, message FROM transcript WHERE session_id = ? AND seq BETWEEN ? AND ? ORDER BY seq",
            (session_id, from_seq, to_seq),
        )
        return [(int(r["seq"]), Message.model_validate_json(r["message"])) for r in rows]

    async def transcript_seqs(self, session_id: str, keys: Sequence[str]) -> list[int]:
        out: list[int] = []
        for start in range(0, len(keys), 500):  # stay under SQLite's bind-parameter limit
            chunk = list(keys[start : start + 500])
            marks = ",".join("?" for _ in chunk)
            rows = await self._db.fetchall(
                f"SELECT seq FROM transcript WHERE session_id = ? AND key IN ({marks})", (session_id, *chunk)
            )
            out.extend(int(r["seq"]) for r in rows)
        return sorted(out)

    def _view_of(self, message: Message) -> tuple[str | None, str]:
        """The stored view of a message and the key naming what produced it, for the row's two columns."""
        if self._view is None:
            return None, ""
        return json.dumps(self._view.build(message), default=str, ensure_ascii=False), self._view.key()

    async def list_transcript_views(self, session_id: str, *, limit: int = 0, before_seq: int = 0) -> list[dict[str, Any]]:
        """One page of the session as the app draws it, without parsing a message.

        Rows whose stored view was produced by older code or an older redactor are rebuilt
        from the message and written back, so the next read of the page is free again.
        """
        if self._view is None:
            return [self._view_only_fallback(m) for m in await self.list_transcript(session_id, limit=limit, before_seq=before_seq)]
        params: list[Any] = [session_id]
        where = "session_id = ?"
        if before_seq > 0:
            where += " AND seq < ?"
            params.append(before_seq)
        if limit > 0:
            params.append(limit)
            rows = await self._db.fetchall(
                f"SELECT seq, view, view_key, message FROM (SELECT seq, view, view_key, message FROM transcript WHERE {where} ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                tuple(params),
            )
        else:
            rows = await self._db.fetchall(f"SELECT seq, view, view_key, message FROM transcript WHERE {where} ORDER BY seq", tuple(params))
        out, stale = await asyncio.to_thread(self._views_of, rows, self._view.key())
        if stale:
            await self._db.executemany("UPDATE transcript SET view = ?, view_key = ? WHERE seq = ?", stale)
        return out

    def _views_of(self, rows: Sequence[Any], current: str) -> tuple[list[dict[str, Any]], list[tuple[str, str, int]]]:
        """Read the stored views, and build the ones that are stale. Off the loop, because of the second.

        A page whose builder has moved on is parsed and rebuilt message by message, which on a page
        of six hundred turns is a sixth of a second of straight Python — long enough, on the loop, to
        stall every stream the installation is serving while somebody scrolls back.
        """
        out: list[dict[str, Any]] = []
        stale: list[tuple[str, str, int]] = []
        for row in rows:
            seq = int(row["seq"])
            if row["view"] and row["view_key"] == current:
                view = json.loads(row["view"])
            else:
                message = Message.model_validate_json(row["message"])
                view = self._view.build(message) if self._view is not None else {}
                stale.append((json.dumps(view, default=str, ensure_ascii=False), current, seq))
            view["seq"] = seq
            out.append(view)
        return out, stale

    def _view_only_fallback(self, message: Message) -> dict[str, Any]:
        """A store built without a view builder still answers, with the message itself."""
        return {"seq": message.metadata.get("daedalus.seq"), "role": message.role.value, "text": "".join(b.text for b in message.content_blocks if isinstance(b, TextBlock))}

    async def transcript_keys_present(self, session_id: str, keys: Sequence[str]) -> set[str]:
        """Which of ``keys`` the transcript already holds; the UNIQUE(session_id, key) index answers it."""
        found: set[str] = set()
        for start in range(0, len(keys), 500):  # stay under SQLite's bind-parameter limit
            chunk = list(keys[start : start + 500])
            marks = ",".join("?" for _ in chunk)
            rows = await self._db.fetchall(
                f"SELECT key FROM transcript WHERE session_id = ? AND key IN ({marks})", (session_id, *chunk)
            )
            found.update(str(r["key"]) for r in rows)
        return found

    async def list_transcript(self, session_id: str, *, limit: int = 0, before_seq: int = 0) -> list[Message]:
        """Transcript rows as messages; each carries its row number in ``metadata["daedalus.seq"]``.

        ``limit`` is the newest N rows and ``before_seq`` the page older than a row already
        shown: both are answered by the ``(session_id, seq)`` index, so the cost is the page
        and not the session. An unbounded call reads the whole transcript and is only for the
        few places that genuinely want all of it (the Markdown export, the bench runner).
        """
        params: list[Any] = [session_id]
        where = "session_id = ?"
        if before_seq > 0:
            where += " AND seq < ?"
            params.append(before_seq)
        if limit > 0:
            params.append(limit)
            rows = await self._db.fetchall(
                f"SELECT seq, message FROM (SELECT seq, message FROM transcript WHERE {where} ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                tuple(params),
            )
        else:
            rows = await self._db.fetchall(f"SELECT seq, message FROM transcript WHERE {where} ORDER BY seq", tuple(params))
        out = []
        for r in rows:
            message = Message.model_validate_json(r["message"])
            out.append(message.model_copy(update={"metadata": {**message.metadata, "daedalus.seq": int(r["seq"])}}))
        return out

    async def transcript_bounds(self, session_id: str) -> tuple[int, int]:
        """Lowest and highest transcript seq of a session; ``(0, 0)`` when it has none."""
        row = await self._db.fetchone("SELECT min(seq) lo, max(seq) hi FROM transcript WHERE session_id = ?", (session_id,))
        return (int(row["lo"] or 0), int(row["hi"] or 0)) if row else (0, 0)

    TOOL_CALL_SCAN_PAGE = 200
    TOOL_CALL_BATCH_PAGES = 5
    """How far back a key hit is followed to find the call it belongs to. A tool call and its result are
    neighbours in the transcript; this is room for the batch around them and not for the session."""

    async def messages_for_call(self, session_id: str, call_id: str) -> list[Message]:
        """Transcript messages carrying a block of ``call_id``, newest first — without loading the session.

        The transcript key names one call of a message that carries any, so a result row is found from
        the key index alone. The message that *made* the call may be keyed by another call of the same
        batch — a model that asks for two things at once is keyed by the first of them — and is then
        found by walking back from the hit a page at a time. Both are needed: one carries the result and
        the other the arguments, and the file a ``SendFile`` handed over is named in the arguments.
        """
        hits = [
            int(r["seq"])
            for r in await self._db.fetchall(
                "SELECT seq FROM transcript WHERE session_id = ? AND key LIKE ? ESCAPE '\\' ORDER BY seq DESC",
                (session_id, "%:" + _like_literal(call_id)),
            )
        ]
        found = [m for m in await self._messages_at(session_id, hits) if _mentions_call(m, call_id)]
        if any(isinstance(b, ToolUseBlock) and b.tool_call_id == call_id for m in found for b in m.content_blocks):
            return found
        # Either nothing was keyed by this call at all, or what was is the result of a call made in a
        # batch. A hit gives the scan a place to start and a reason to stop early; without one there is
        # nowhere to start but the end of the transcript.
        before = min(hits) if hits else 0
        pages = self.TOOL_CALL_BATCH_PAGES if hits else 0
        seen = set(hits)
        page = 0
        while not pages or page < pages:
            params: list[Any] = [session_id]
            where = "session_id = ?"
            if before:
                where += " AND seq < ?"
                params.append(before)
            rows = await self._db.fetchall(f"SELECT seq FROM transcript WHERE {where} ORDER BY seq DESC LIMIT ?", (*params, self.TOOL_CALL_SCAN_PAGE))
            if not rows:
                break
            seqs = [int(r["seq"]) for r in rows]
            before, page = seqs[-1], page + 1
            for message in await self._messages_at(session_id, [q for q in seqs if q not in seen]):
                if _mentions_call(message, call_id):
                    found.append(message)
            if found and any(isinstance(b, ToolUseBlock) and b.tool_call_id == call_id for m in found for b in m.content_blocks):
                break
        return found

    async def _messages_at(self, session_id: str, seqs: Sequence[int]) -> list[Message]:
        """The named transcript rows, in the order given."""
        if not seqs:
            return []
        marks = ",".join("?" for _ in seqs)
        rows = await self._db.fetchall(
            f"SELECT seq, message FROM transcript WHERE session_id = ? AND seq IN ({marks})", (session_id, *seqs)
        )
        by_seq = {int(r["seq"]): Message.model_validate_json(r["message"]) for r in rows}
        return [by_seq[s] for s in seqs if s in by_seq]

    async def replace_transcript_message(self, session_id: str, key: str, message: Message) -> bool:
        """Rewrite one transcript row in place (same key, same seq); the search index follows."""
        row = await self._db.fetchone("SELECT seq FROM transcript WHERE session_id = ? AND key = ?", (session_id, key))
        if row is None:
            return False
        view, view_key = self._view_of(message)
        async with self._db.transaction() as conn:
            await conn.execute("UPDATE transcript SET message = ?, view = ?, view_key = ? WHERE session_id = ? AND key = ?", (message.model_dump_json(), view, view_key, session_id, key))
            await conn.execute("DELETE FROM transcript_fts WHERE rowid = ?", (int(row["seq"]),))
            text = message_text(message)
            if text:
                await conn.execute("INSERT INTO transcript_fts(rowid, text) VALUES (?, ?)", (int(row["seq"]), text))
        return True

    async def transcript_row(self, session_id: str, seq: int) -> tuple[str, Message] | None:
        row = await self._db.fetchone("SELECT key, message FROM transcript WHERE session_id = ? AND seq = ?", (session_id, seq))
        return (row["key"], Message.model_validate_json(row["message"])) if row else None

    async def truncate_history(self, session_id: str, tenant_id: str, seq: int, messages: Sequence[Message]) -> tuple[int, int]:
        """Delete a transcript tail and replace working history atomically, without archiving it."""
        rows = [(session_id, tenant_id, 1, self.transcript_key(m), m.model_dump_json()) for m in messages]
        async with self._db.transaction() as conn:
            cursor = await conn.execute("SELECT count(*), coalesce(max(seq), 0), min(json_extract(message, '$.created_at')) FROM transcript WHERE session_id = ? AND seq >= ?", (session_id, seq))
            count, through, cut_at = await cursor.fetchone()
            # Snapshots and event payloads can contain the whole old prompt, not just the answer.
            # Accounting remains: removing conversation text does not refund work already done.
            await conn.execute("DELETE FROM snapshots WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM events WHERE run_id IN (SELECT id FROM runs WHERE session_id = ?) AND julianday(created_at) >= julianday(?)", (session_id, cut_at))
            await conn.execute("DELETE FROM checkpoints WHERE session_id = ? AND (seq >= ? OR julianday(at) >= julianday(?))", (session_id, seq, cut_at))
            if self._media is not None:
                await self._media.drop_tail(conn, session_id, seq)
            await conn.execute("DELETE FROM transcript_fts WHERE rowid IN (SELECT seq FROM transcript WHERE session_id = ? AND seq >= ?)", (session_id, seq))
            await conn.execute("DELETE FROM transcript WHERE session_id = ? AND seq >= ?", (session_id, seq))
            await conn.execute("DELETE FROM session_messages WHERE session_id = ? AND tenant_id = ?", (session_id, tenant_id))
            await conn.executemany("INSERT INTO session_messages(session_id, tenant_id, gen, key, message) VALUES (?, ?, ?, ?, ?)", rows)
        return int(count), int(through)

    async def replace_messages(self, session_id: str, tenant_id: str, messages: Sequence[Message]) -> None:
        """Start a new generation of the working history: the sequence itself changed.

        A compaction, a revert or a clear replaces the history rather than continuing it. The
        new generation is written whole and the one before it dropped, in one transaction, so a
        reader never sees a half-written history and never sees two.
        """
        row = await self._db.fetchone("SELECT coalesce(max(gen), 0) + 1 next FROM session_messages WHERE session_id = ? AND tenant_id = ?", (session_id, tenant_id))
        gen = int(row["next"]) if row else 1
        rows = [(session_id, tenant_id, gen, self.transcript_key(m), m.model_dump_json()) for m in messages]
        async with self._db.transaction() as conn:
            await conn.executemany(
                "INSERT INTO session_messages(session_id, tenant_id, gen, key, message) VALUES (?, ?, ?, ?, ?)", rows
            )
            await conn.execute("DELETE FROM session_messages WHERE session_id = ? AND tenant_id = ? AND gen < ?", (session_id, tenant_id, gen))

    async def append_messages(self, session_id: str, tenant_id: str, messages: Sequence[Message]) -> int:
        """Add messages to the end of the current generation; returns rows written.

        What a round does, and all it does. The caller already knows these messages are new —
        the engine told it so — which is why this does not read the stored keys back to find
        out: that read walked every row of the session, once per round, to learn something
        nobody was in doubt about. Only a caller that cannot say what changed needs
        :meth:`sync_messages`.
        """
        fresh = list(messages)
        if not fresh:
            return 0
        await self._db.executemany(
            "INSERT INTO session_messages(session_id, tenant_id, gen, key, message)"
            f" VALUES (?, ?, {self.CURRENT_GEN}, ?, ?)",
            [(session_id, tenant_id, session_id, tenant_id, self.transcript_key(m), m.model_dump_json()) for m in fresh],
        )
        return len(fresh)

    async def sync_messages(self, session_id: str, tenant_id: str, messages: Sequence[Message]) -> int:
        """Bring the stored working history up to what the engine holds; returns rows written.

        Nearly every round adds to the end of the same sequence, so what is already stored is
        left where it is and only the new messages are appended. When the sequence did not grow
        but changed — a compaction rewrote it — the whole thing becomes a new generation, which
        is the only time this table is rewritten.
        """
        keys = [self.transcript_key(m) for m in messages]
        rows = await self._db.fetchall(
            f"SELECT key FROM session_messages WHERE session_id = ? AND tenant_id = ? AND gen = {self.CURRENT_GEN} ORDER BY seq", (session_id, tenant_id, session_id, tenant_id)
        )
        stored = [str(r["key"]) for r in rows]
        if stored != keys[: len(stored)]:
            await self.replace_messages(session_id, tenant_id, messages)
            return len(messages)
        fresh = list(messages)[len(stored) :]
        if not fresh:
            return 0
        gen_row = await self._db.fetchone("SELECT coalesce(max(gen), 0) gen FROM session_messages WHERE session_id = ? AND tenant_id = ?", (session_id, tenant_id))
        gen = int(gen_row["gen"]) if gen_row else 0
        await self._db.executemany(
            "INSERT INTO session_messages(session_id, tenant_id, gen, key, message) VALUES (?, ?, ?, ?, ?)",
            [(session_id, tenant_id, gen, self.transcript_key(m), m.model_dump_json()) for m in fresh],
        )
        return len(fresh)


def _row_to_session(row: Any) -> Session:
    return Session(
        id=row["id"],
        tenant_id=row["tenant_id"],
        title=row["title"],
        created_at=datetime.fromisoformat(row["created_at"]),
        last_message_at=datetime.fromisoformat(row["last_message_at"]),
        metadata=json.loads(row["metadata"] or "{}"),
    )


class SqliteRunStore(IRunStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, run: Run) -> None:
        async with self._db.transaction() as conn:
            await self.create_in(conn, run)

    @staticmethod
    async def create_in(conn: aiosqlite.Connection, run: Run) -> None:
        """Create a run alongside the caller's durable admission or continuation receipt."""
        await conn.execute(
            "INSERT OR REPLACE INTO runs(id, tenant_id, session_id, status, created_at, updated_at, detail_blob_ref)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.tenant_id,
                run.session_id,
                run.status.value,
                run.created_at.isoformat(),
                run.updated_at.isoformat(),
                run.detail_blob_ref,
            ),
        )

    async def get(self, run_id: str, tenant_id: str) -> Run:
        row = await self._db.fetchone(
            "SELECT * FROM runs WHERE id = ? AND tenant_id = ?", (run_id, tenant_id)
        )
        if row is None:
            raise RunNotFoundError(run_id)
        return _row_to_run(row)

    async def update_status(self, run_id: str, tenant_id: str, status: RunStatus) -> None:
        await self._db.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ? AND tenant_id = ?",
            (status.value, _now(), run_id, tenant_id),
        )

    async def list(
        self,
        tenant_id: str,
        *,
        session_id: str | None = None,
        status: RunStatus | None = None,
        limit: int = 100,
    ) -> Sequence[Run]:
        sql = "SELECT * FROM runs WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status.value)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [_row_to_run(r) for r in await self._db.fetchall(sql, params)]

    async def flush_terminal(self, run_id: str, tenant_id: str, **_: Any) -> None:
        return None

    async def increment_tool_errors_count(self, run_id: str, by: int = 1) -> None:
        return None


def _row_to_run(row: Any) -> Run:
    return Run(
        id=row["id"],
        tenant_id=row["tenant_id"],
        session_id=row["session_id"],
        status=RunStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        detail_blob_ref=row["detail_blob_ref"],
    )


class SqliteEventStream(IEventStream):
    """Durable per-run event log with in-process live subscribers.

    ``state_snapshot`` events are the engine's resume points; they are stored in
    their own table (latest wins) rather than appended to the log.
    """

    LIVE_ONLY = ("content_block_delta", "tool_use_input_delta")
    """Streaming fragments: a subscriber that is watching wants them, and nothing else ever does.

    They were the larger part of the event log — one autocommit transaction per token, kept
    for the fifteen minutes until the run ended and the log was trimmed — and the text they
    carry is in the transcript by the time the message stops. They are fanned out and dropped.
    """

    BY_REFERENCE = {"tool_surface_advertised": "tools"}
    """Events whose payload repeats the same large value run after run: the value is kept once
    under its digest and the row names the digest. The whole tool surface is thirty kilobytes
    and identical across hundreds of runs."""

    SURFACE_DIGEST = "tool_surface_digest"
    SURFACE_DESCRIBED = "tool_surface_described"
    """The runtime names the surface it advertised and says whether this advertisement carries
    the descriptions. It sends them the first time it sees a digest and not again, so the copy
    kept here is the copy that must last: an advertisement without them is the same surface
    under the same digest, not a smaller one, and it must not replace what is stored. Where
    the runtime says nothing — an older one — every advertisement is full and the digest is
    taken from the value itself."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._subscribers: dict[str, list[asyncio.Queue[Event | None]]] = {}
        self._session_of_run: dict[str, str] = {}
        self._live_offsets: dict[tuple[str, str, str], int] = {}

    SUBSCRIBER_CAP = 512

    def bind_run(self, run_id: str, session_id: str) -> None:
        self._session_of_run[run_id] = session_id

    async def _payload_by_reference(self, event: Event) -> dict[str, Any]:
        """The payload as it is stored: a repeated large member replaced by its digest.

        The digest is the runtime's when it named one, so every advertisement of one surface
        lands on one key whether or not it carried the descriptions. A described advertisement
        is the full copy and overwrites; an undescribed one fills the key only if nothing is
        there, so the names-only copy can never displace the descriptions.
        """
        field = self.BY_REFERENCE.get(event.name)
        value = event.payload.get(field) if field else None
        if field is None or value is None:
            return event.payload
        body = json.dumps(value, default=str, sort_keys=True)
        named = event.payload.get(self.SURFACE_DIGEST)
        digest = str(named)[:32] if named else hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        described = bool(event.payload.get(self.SURFACE_DESCRIBED, True))
        if described:
            await self._db.execute(
                "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"event_blob:{digest}", body),
            )
        else:
            await self._db.execute("INSERT OR IGNORE INTO kv(key, value) VALUES (?, ?)", (f"event_blob:{digest}", body))
        return {**event.payload, field: {"digest": digest, "count": len(value) if isinstance(value, (list, dict)) else 1}}

    async def tool_surface(self, digest: str) -> list[dict[str, Any]] | None:
        """The tool surface kept under ``digest``, descriptions and all, or ``None``.

        What a reader of an advertisement uses when the advertisement did not carry the
        schema: the surface travels once per digest and is looked up by name after that.
        """
        row = await self._db.fetchone("SELECT value FROM kv WHERE key = ?", (f"event_blob:{str(digest)[:32]}",))
        if row is None:
            return None
        loaded = json.loads(row["value"])
        return loaded if isinstance(loaded, list) else None

    async def emit(self, event: Event) -> None:
        tenant_id = str(event.payload.get("tenant_id") or "default")
        if event.name in self.LIVE_ONLY:
            for queue in list(self._subscribers.get(event.run_id, [])):
                self._offer(queue, event)
            return
        if event.name == "state_snapshot":
            snapshot = event.payload.get("snapshot") or {}
            await self._db.execute(
                "INSERT INTO snapshots(run_id, tenant_id, session_id, state, snapshot, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id) DO UPDATE SET state = excluded.state,"
                " snapshot = excluded.snapshot, updated_at = excluded.updated_at",
                (
                    event.run_id,
                    tenant_id,
                    self._session_of_run.get(event.run_id) or str(snapshot.get("session_id") or ""),
                    str(snapshot.get("state") or ""),
                    json.dumps(snapshot, default=str),
                    _now(),
                ),
            )
            return
        await self._db.execute(
            "INSERT INTO events(id, run_id, tenant_id, name, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.run_id,
                tenant_id,
                event.name,
                json.dumps(await self._payload_by_reference(event), default=str),
                event.created_at.isoformat(),
            ),
        )
        for queue in list(self._subscribers.get(event.run_id, [])):
            self._offer(queue, event)

    @staticmethod
    def _offer(queue: asyncio.Queue[Event | None], event: Event) -> None:
        """A slow observer resubscribes from durable events; it never backpressures the agent loop."""
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)

    async def subscribe(
        self, run_id: str, tenant_id: str, *, from_event_id: str | None = None
    ) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=self.SUBSCRIBER_CAP)
        self._subscribers.setdefault(run_id, []).append(queue)
        try:
            rows = await self._db.fetchall(
                "SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
            )
            started = from_event_id is None
            for row in rows:
                if not started:
                    if row["id"] == from_event_id:
                        started = True
                    continue
                yield _row_to_event(row)
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            subscribers = self._subscribers.get(run_id, [])
            if queue in subscribers:
                subscribers.remove(queue)

    async def trim(self, run_id: str, tenant_id: str, *, max_len: int) -> None:
        await self._db.execute(
            "DELETE FROM events WHERE run_id = ? AND seq NOT IN"
            " (SELECT seq FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT ?)",
            (run_id, run_id, max_len),
        )

    async def prune(self, *, keep_days: int, max_rows: int) -> int:
        """Drop the event log of runs that are over and old; returns how many rows went.

        "Over" is read from the run's status, not from the age of the row: the age sweep is what
        keeps the table bounded and the status is what keeps it from taking a run in progress.

        ``trim`` bounds a run, and every finished run sits at its cap — but runs accumulate
        for as long as the installation does, so the log has no ceiling without this. The
        transcript is the record; events are what the live view was fed.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat()
        before = await self._db.fetchone("SELECT count(*) c FROM events")
        # Only the events of runs that are over. By wall-clock age alone, a run still going after
        # ``keep_days`` — a loop, a long-running agent — would lose its early events while it runs.
        await self._db.execute(
            "DELETE FROM events WHERE created_at < ? AND (run_id IS NULL OR run_id NOT IN"
            f" (SELECT id FROM runs WHERE status IN ({UNFINISHED_RUN_STATUSES})))",
            (cutoff,),
        )
        await self._db.execute(
            "DELETE FROM events WHERE seq NOT IN (SELECT seq FROM events ORDER BY seq DESC LIMIT ?)", (max_rows,)
        )
        # A kept blob is the tool surface of some run; one that no surviving row names is gone with it.
        await self._db.execute(
            "DELETE FROM kv WHERE key LIKE 'event_blob:%'"
            " AND NOT EXISTS (SELECT 1 FROM events WHERE payload LIKE '%' || substr(kv.key, 12) || '%')"
        )
        after = await self._db.fetchone("SELECT count(*) c FROM events")
        return int(before["c"] or 0) - int(after["c"] or 0) if before and after else 0

    def close_run(self, run_id: str) -> None:
        for queue in self._subscribers.get(run_id, []):
            queue.put_nowait(None)

    # -- host extras --------------------------------------------------------------

    async def list_events(
        self, run_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[tuple[int, Event]]:
        rows = await self._db.fetchall(
            "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (run_id, after_seq, limit),
        )
        return [(int(r["seq"]), _row_to_event(r)) for r in rows]

    async def publish_session(
        self,
        session_id: str,
        event: Any,
        *,
        history_rewrite: bool = False,
    ) -> dict[str, Any]:
        """Persist one session envelope before it is offered to live listeners.

        Token fragments remain live-only. They carry a bounded offset inside the current durable
        boundary, while lifecycle events advance the session cursor in the same transaction that
        stores their envelope. A reconnect can therefore replay through a watermark and then join
        the reserved live tail without a database row per token.
        """
        kind = str(getattr(getattr(event, "type", None), "value", None) or getattr(event, "name", ""))
        run_id = str(getattr(event, "run_id", "") or "")
        payload = dict(getattr(event, "payload", {}) or {})
        if kind in self.LIVE_ONLY:
            row = await self._db.fetchone(
                "SELECT event_seq, history_revision FROM session_stream_state WHERE session_id = ?",
                (session_id,),
            )
            message_id = str(payload.get("message_id") or run_id)
            block_id = str(payload.get("block_id") or payload.get("index") or kind)
            key = (session_id, message_id, block_id)
            offset = self._live_offsets.get(key, 0)
            delta = str(payload.get("delta") or payload.get("text") or payload.get("content") or "")
            self._live_offsets[key] = offset + len(delta.encode("utf-8"))
            return {
                "session_id": session_id,
                "run_id": run_id or None,
                "event_seq": int(row["event_seq"]) if row else 0,
                "history_revision": int(row["history_revision"]) if row else 0,
                "kind": kind,
                "payload": payload,
                "ephemeral": {
                    "message_id": message_id,
                    "block_id": block_id,
                    "delta_offset": offset,
                    "generation": run_id,
                },
            }
        now = _now()
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT event_seq, history_revision FROM session_stream_state WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            event_seq = (int(row["event_seq"]) if row else 0) + 1
            revision = (int(row["history_revision"]) if row else 0) + (1 if history_rewrite else 0)
            await conn.execute(
                "INSERT INTO session_stream_state(session_id, event_seq, history_revision) VALUES (?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET event_seq = excluded.event_seq,"
                " history_revision = excluded.history_revision",
                (session_id, event_seq, revision),
            )
            await conn.execute(
                "INSERT INTO session_events(session_id, event_seq, run_id, history_revision, kind, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, event_seq, run_id or None, revision, kind, json.dumps(payload, default=str), now),
            )
        return {
            "session_id": session_id,
            "run_id": run_id or None,
            "event_seq": event_seq,
            "history_revision": revision,
            "kind": kind,
            "payload": payload,
        }

    async def session_tool_result(self, session_id: str, call_id: str) -> tuple[str, bool] | None:
        """The text and error flag of one tool result as the session's stream carried it, or None.

        The stream's envelope is stored before the transcript is: while a run is going it is the
        only durable copy of a result, and the full-result endpoint falls back to it.
        """
        row = await self._db.fetchone(
            "SELECT payload FROM session_events WHERE session_id = ? AND kind = 'tool_result'"
            " AND json_extract(payload, '$.tool_call_id') = ? ORDER BY event_seq DESC LIMIT 1",
            (session_id, call_id),
        )
        if row is None:
            return None
        payload = json.loads(row["payload"])
        content = payload.get("content")
        if not isinstance(content, str):
            blocks = payload.get("content_blocks") or []
            content = "".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict))
        return content, bool(payload.get("is_error"))

    async def session_replay(
        self,
        session_id: str,
        *,
        after: int,
        through: int | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Return a bounded cursor replay, explicitly naming a cursor lost to retention."""
        state = await self._db.fetchone(
            "SELECT event_seq, history_revision, min_event_seq, runtime_epoch"
            " FROM session_stream_state WHERE session_id = ?",
            (session_id,),
        )
        watermark = int(state["event_seq"]) if state else 0
        revision = int(state["history_revision"]) if state else 0
        minimum = int(state["min_event_seq"]) if state else 1
        if after and after < minimum - 1:
            return {
                "resync_required": True,
                "reason": "cursor_trimmed",
                "watermark": watermark,
                "history_revision": revision,
                "runtime_epoch": str(state["runtime_epoch"] or "") if state else "",
                "events": [],
            }
        upper = watermark if through is None else min(watermark, through)
        rows = await self._db.fetchall(
            "SELECT event_seq, run_id, history_revision, kind, payload FROM session_events"
            " WHERE session_id = ? AND event_seq > ? AND event_seq <= ? ORDER BY event_seq LIMIT ?",
            (session_id, after, upper, max(1, min(limit, 1000))),
        )
        return {
            "resync_required": False,
            "watermark": watermark,
            "history_revision": revision,
            "runtime_epoch": str(state["runtime_epoch"] or "") if state else "",
            "events": [
                {
                    "session_id": session_id,
                    "run_id": row["run_id"],
                    "event_seq": int(row["event_seq"]),
                    "history_revision": int(row["history_revision"]),
                    "kind": row["kind"],
                    "payload": json.loads(row["payload"] or "{}"),
                }
                for row in rows
            ],
        }

    async def trim_session(self, session_id: str, *, keep: int) -> None:
        """Trim only the replay projection; the materialized transcript remains authoritative."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT event_seq FROM session_events WHERE session_id = ? ORDER BY event_seq DESC LIMIT 1 OFFSET ?",
                (session_id, max(0, keep - 1)),
            )
            boundary = await cursor.fetchone()
            await cursor.close()
            if boundary is None:
                return
            minimum = int(boundary["event_seq"])
            await conn.execute(
                "DELETE FROM session_events WHERE session_id = ? AND event_seq < ?",
                (session_id, minimum),
            )
            await conn.execute(
                "UPDATE session_stream_state SET min_event_seq = ? WHERE session_id = ?",
                (minimum, session_id),
            )

    async def load_snapshot(self, run_id: str) -> dict[str, Any] | None:
        row = await self._db.fetchone("SELECT snapshot FROM snapshots WHERE run_id = ?", (run_id,))
        return json.loads(row["snapshot"]) if row else None

    async def unfinished_snapshots(self) -> list[dict[str, Any]]:
        rows = await self._db.fetchall(
            "SELECT run_id, session_id, state, snapshot, updated_at FROM snapshots"
            " WHERE state IN ('running', 'compacting', 'pending', 'awaiting') ORDER BY updated_at DESC"
        )
        return [
            {
                "run_id": r["run_id"],
                "session_id": r["session_id"],
                "state": r["state"],
                "snapshot": json.loads(r["snapshot"]),
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    async def delete_snapshot(self, run_id: str) -> None:
        await self._db.execute("DELETE FROM snapshots WHERE run_id = ?", (run_id,))


def _row_to_event(row: Any) -> Event:
    return Event(
        id=row["id"],
        run_id=row["run_id"],
        name=row["name"],
        payload=json.loads(row["payload"] or "{}"),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class SqliteUsageSink(UsageSink):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(self, record: UsageRecord) -> None:
        n = record.normalized
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO usage_events(at,provider_id,model,purpose,run_id,session_id,input_tokens,"
                " output_tokens,cache_read_tokens,reasoning_tokens,cost_usd,duration_ms,raw,inference_reservation_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_now(), record.provider_id, record.model, record.purpose, record.run_id, record.session_id,
                 int(n.get("input_tokens", 0)), int(n.get("output_tokens", 0)),
                 int(n.get("cache_read_tokens", 0)), int(n.get("reasoning_tokens", 0)),
                 record.cost_usd, record.duration_ms, json.dumps(record.raw), record.inference_reservation_id),
            )
            seq = cursor.lastrowid
            await cursor.close()
            if record.inference_reservation_id is not None:
                assert seq is not None
                await InferenceBudget(self._db).settle_in(conn, record.inference_reservation_id, seq)



def _queue_item_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or "")


def _merged_queue(kept: list[dict[str, Any]], stored: list[dict[str, Any]], seen: Sequence[str]) -> list[dict[str, Any]]:
    """What a run hands back, followed by whatever the store gained while the run was holding it."""
    known = {_queue_item_id(item) for item in kept} | set(seen)
    return list(kept) + [item for item in stored if _queue_item_id(item) not in known]


class ReceiptConflict(ValueError):
    """One client message id was reused for different input."""


class LiveControlStore:
    """Per-session steer / follow-up queues and live model overrides."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(self, session_id: str) -> dict[str, Any]:
        row = await self._db.fetchone("SELECT * FROM live_control WHERE session_id = ?", (session_id,))
        if row is None:
            return {"steer": [], "follow_up": [], "queue_revision": 0, "model_name": None, "provider": None, "preset": None, "thinking_enabled": None, "reasoning_effort": None}
        return {
            "steer": json.loads(row["steer_queue"] or "[]"),
            "follow_up": json.loads(row["follow_up_queue"] or "[]"),
            "queue_revision": int(row["queue_revision"] or 0),
            "model_name": row["model_name"],
            "provider": row["provider"],
            "preset": row["preset"],
            "thinking_enabled": None if row["thinking_enabled"] is None else bool(row["thinking_enabled"]),
            "reasoning_effort": row["reasoning_effort"],
        }

    async def load_models(self, session_ids: list[str]) -> dict[str, dict[str, Any]]:
        """The model overrides of many sessions in one query: {session_id: {model_name, provider, preset}} for those that have any."""
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(session_ids), 400):
            chunk = session_ids[start : start + 400]
            if not chunk:
                continue
            marks = ",".join("?" for _ in chunk)
            rows = await self._db.fetchall(f"SELECT session_id, model_name, provider, preset FROM live_control WHERE session_id IN ({marks})", tuple(chunk))
            for row in rows:
                out[row["session_id"]] = {"model_name": row["model_name"], "provider": row["provider"], "preset": row["preset"]}
        return out

    async def save_queues(self, session_id: str, steer: list[dict[str, Any]], follow_up: list[dict[str, Any]]) -> None:
        await self._db.execute(
            "INSERT INTO live_control(session_id, steer_queue, follow_up_queue, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET steer_queue = excluded.steer_queue,"
            " follow_up_queue = excluded.follow_up_queue, updated_at = excluded.updated_at",
            (session_id, json.dumps(steer), json.dumps(follow_up), _now()),
        )

    async def replace_seen(
        self,
        session_id: str,
        steer: list[dict[str, Any]],
        follow_up: list[dict[str, Any]],
        *,
        seen_steer: Sequence[str],
        seen_follow_up: Sequence[str],
    ) -> list[str]:
        """Write back the queues a run is holding without losing what was enqueued while it ran.

        ``steer`` and ``follow_up`` are what the run has left; ``seen_*`` are the ids it was handed
        when it last read this row. An item in the store that is in neither list arrived after that
        read: it is kept, appended behind the run's own remainder so the order stays the order the
        operator queued things in. A blind write of the run's lists would drop it, and the operator
        would be told it had been delivered.

        Returns the steer ids that are genuinely gone — handed to the run and not handed back — so
        the caller can say ``consumed`` about those and only those.
        """
        async with self._db.transaction() as conn:
            cursor = await conn.execute("SELECT steer_queue, follow_up_queue FROM live_control WHERE session_id = ?", (session_id,))
            row = await cursor.fetchone()
            await cursor.close()
            stored_steer = json.loads(row["steer_queue"] or "[]") if row else []
            stored_follow_up = json.loads(row["follow_up_queue"] or "[]") if row else []
            merged_steer = _merged_queue(steer, stored_steer, seen_steer)
            merged_follow_up = _merged_queue(follow_up, stored_follow_up, seen_follow_up)
            await conn.execute(
                "INSERT INTO live_control(session_id, steer_queue, follow_up_queue, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET steer_queue = excluded.steer_queue,"
                " follow_up_queue = excluded.follow_up_queue, updated_at = excluded.updated_at",
                (session_id, json.dumps(merged_steer), json.dumps(merged_follow_up), _now()),
            )
        kept = {_queue_item_id(item) for item in merged_steer}
        return [item_id for item_id in seen_steer if item_id not in kept]

    async def enqueue(self, session_id: str, kind: str, item: dict[str, Any]) -> None:
        column = "steer_queue" if kind == "steer" else "follow_up_queue"
        async with self._db.transaction() as conn:
            cursor = await conn.execute("SELECT steer_queue, follow_up_queue FROM live_control WHERE session_id = ?", (session_id,))
            row = await cursor.fetchone()
            await cursor.close()
            current = json.loads(row[column] or "[]") if row else []
            current.append(item)
            await conn.execute(
                f"INSERT INTO live_control(session_id, {column}, queue_revision, updated_at) VALUES (?, ?, 1, ?)"
                f" ON CONFLICT(session_id) DO UPDATE SET {column} = excluded.{column},"
                " queue_revision = live_control.queue_revision + 1, updated_at = excluded.updated_at",
                (session_id, json.dumps(current), _now()),
            )

    @staticmethod
    def _receipt_digest(kind: str, payload: dict[str, Any]) -> str:
        encoded = json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _receipt(row: Any) -> dict[str, Any]:
        return {
            "client_message_id": row["client_message_id"],
            "kind": row["kind"],
            "status": row["status"],
            "run_id": row["run_id"],
            "step_id": row["step_id"],
            "message_seq": row["message_seq"],
            "error": row["error"],
            "queue_revision": int(row["queue_revision"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def accept(
        self,
        session_id: str,
        client_message_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        queue_item: dict[str, Any] | None = None,
        queue_kind: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Accept input once; a repeated request returns the original receipt.

        When ``queue_item`` is supplied, receipt creation and queue insertion share one transaction.
        The boolean is true only for the request that created the receipt, so callers never repeat
        transcript or run-start side effects after an acknowledgement is lost.
        """
        digest = self._receipt_digest(kind, payload)
        now = _now()
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM input_receipts WHERE session_id = ? AND client_message_id = ?",
                (session_id, client_message_id),
            )
            existing = await cursor.fetchone()
            await cursor.close()
            if existing is not None:
                if existing["content_digest"] != digest or existing["kind"] != kind:
                    raise ReceiptConflict("client_message_id already belongs to different input")
                return self._receipt(existing), False
            cursor = await conn.execute(
                "SELECT steer_queue, follow_up_queue, queue_revision FROM live_control WHERE session_id = ?",
                (session_id,),
            )
            control = await cursor.fetchone()
            await cursor.close()
            revision = int(control["queue_revision"] or 0) if control else 0
            status = "accepted"
            if queue_item is not None:
                column = "steer_queue" if (queue_kind or kind) == "steer" else "follow_up_queue"
                current = json.loads(control[column] or "[]") if control else []
                current.append(queue_item)
                revision += 1
                await conn.execute(
                    f"INSERT INTO live_control(session_id, {column}, queue_revision, updated_at) VALUES (?, ?, ?, ?)"
                    f" ON CONFLICT(session_id) DO UPDATE SET {column} = excluded.{column},"
                    " queue_revision = excluded.queue_revision, updated_at = excluded.updated_at",
                    (session_id, json.dumps(current), revision, now),
                )
                status = "queued"
            await conn.execute(
                "INSERT INTO input_receipts(session_id, client_message_id, kind, content_digest, payload,"
                " status, queue_revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, client_message_id, kind, digest, json.dumps(payload), status, revision, now, now),
            )
            cursor = await conn.execute(
                "SELECT * FROM input_receipts WHERE session_id = ? AND client_message_id = ?",
                (session_id, client_message_id),
            )
            created = await cursor.fetchone()
            await cursor.close()
        assert created is not None
        return self._receipt(created), True

    async def check_receipt(
        self,
        session_id: str,
        client_message_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return matching prior input, rejecting id reuse before callers repeat file effects."""
        row = await self._db.fetchone(
            "SELECT * FROM input_receipts WHERE session_id = ? AND client_message_id = ?",
            (session_id, client_message_id),
        )
        if row is None:
            return None
        if row["content_digest"] != self._receipt_digest(kind, payload) or row["kind"] != kind:
            raise ReceiptConflict("client_message_id already belongs to different input")
        return self._receipt(row)

    async def receipt(self, session_id: str, client_message_id: str) -> dict[str, Any] | None:
        row = await self._db.fetchone(
            "SELECT * FROM input_receipts WHERE session_id = ? AND client_message_id = ?",
            (session_id, client_message_id),
        )
        return self._receipt(row) if row is not None else None

    async def consume(
        self,
        session_id: str,
        client_message_id: str,
        *,
        run_id: str,
        step_id: str,
        message_seq: int | None,
    ) -> dict[str, Any]:
        """Fence a receipt at its durable context placement boundary."""
        now = _now()
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM input_receipts WHERE session_id = ? AND client_message_id = ?",
                (session_id, client_message_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KeyError(client_message_id)
            if row["status"] == "withdrawn":
                raise ReceiptConflict("withdrawn input cannot be consumed")
            if row["status"] != "consumed":
                await conn.execute(
                    "UPDATE input_receipts SET status = 'consumed', run_id = ?, step_id = ?,"
                    " message_seq = ?, updated_at = ? WHERE session_id = ? AND client_message_id = ?",
                    (run_id, step_id, message_seq, now, session_id, client_message_id),
                )
            cursor = await conn.execute(
                "SELECT * FROM input_receipts WHERE session_id = ? AND client_message_id = ?",
                (session_id, client_message_id),
            )
            settled = await cursor.fetchone()
            await cursor.close()
        assert settled is not None
        return self._receipt(settled)

    async def withdraw(self, session_id: str, client_message_id: str) -> dict[str, Any] | None:
        """Withdraw queued input, or return its consumed receipt without lying about removal."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM input_receipts WHERE session_id = ? AND client_message_id = ?",
                (session_id, client_message_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None or row["status"] == "consumed":
                return self._receipt(row) if row is not None else None
            cursor = await conn.execute(
                "SELECT steer_queue, follow_up_queue, queue_revision FROM live_control WHERE session_id = ?",
                (session_id,),
            )
            control = await cursor.fetchone()
            await cursor.close()
            revision = int(control["queue_revision"] or 0) if control else int(row["queue_revision"])
            if control is not None:
                for column in ("steer_queue", "follow_up_queue"):
                    current = json.loads(control[column] or "[]")
                    kept = [item for item in current if _queue_item_id(item) != client_message_id]
                    if len(kept) != len(current):
                        revision += 1
                        await conn.execute(
                            f"UPDATE live_control SET {column} = ?, queue_revision = ?, updated_at = ? WHERE session_id = ?",
                            (json.dumps(kept), revision, _now(), session_id),
                        )
            await conn.execute(
                "UPDATE input_receipts SET status = 'withdrawn', queue_revision = ?, updated_at = ?"
                " WHERE session_id = ? AND client_message_id = ?",
                (revision, _now(), session_id, client_message_id),
            )
            cursor = await conn.execute(
                "SELECT * FROM input_receipts WHERE session_id = ? AND client_message_id = ?",
                (session_id, client_message_id),
            )
            withdrawn = await cursor.fetchone()
            await cursor.close()
        assert withdrawn is not None
        return self._receipt(withdrawn)

    async def remove(self, session_id: str, kind: str, item_id: str) -> bool:
        """Drop one queued item by its id; ``False`` when it is no longer there (consumed, or never was)."""
        column = "steer_queue" if kind == "steer" else "follow_up_queue"
        async with self._db.transaction() as conn:
            cursor = await conn.execute(f"SELECT {column} FROM live_control WHERE session_id = ?", (session_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return False
            current = json.loads(row[column] or "[]")
            kept = [item for item in current if str(item.get("id") or "") != item_id]
            if len(kept) == len(current):
                return False
            await conn.execute(
                f"UPDATE live_control SET {column} = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(kept), _now(), session_id),
            )
        return True

    async def set_model(
        self,
        session_id: str,
        *,
        model_name: str | None = None,
        provider: str | None = None,
        preset: str | None = None,
        thinking_enabled: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        state = await self.load(session_id)
        if preset is not None or provider is not None or model_name is not None:
            # A new model choice replaces the previous one wholesale (preset xor manual pair).
            state.update({"preset": None, "provider": None, "model_name": None})
        await self._db.execute(
            "INSERT INTO live_control(session_id, steer_queue, follow_up_queue, model_name, provider, preset, thinking_enabled, reasoning_effort, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET model_name = excluded.model_name, provider = excluded.provider, preset = excluded.preset,"
            " thinking_enabled = excluded.thinking_enabled, reasoning_effort = excluded.reasoning_effort,"
            " updated_at = excluded.updated_at",
            (
                session_id,
                json.dumps(state["steer"]),
                json.dumps(state["follow_up"]),
                model_name if model_name is not None else state["model_name"],
                provider if provider is not None else state["provider"],
                preset if preset is not None else state["preset"],
                None if thinking_enabled is None and state["thinking_enabled"] is None else int(
                    thinking_enabled if thinking_enabled is not None else state["thinking_enabled"]
                ),
                reasoning_effort if reasoning_effort is not None else state["reasoning_effort"],
                _now(),
            ),
        )

    async def clear_overrides(self, session_id: str) -> None:
        await self._db.execute(
            "UPDATE live_control SET model_name = NULL, provider = NULL, preset = NULL, thinking_enabled = NULL, reasoning_effort = NULL WHERE session_id = ?",
            (session_id,),
        )


__all__ = [
    "LiveControlStore",
    "ReceiptConflict",
    "SqliteEventStream",
    "SqliteRunStore",
    "SqliteSessionStore",
    "SqliteUsageSink",
]


class DeliveryLedger:
    """Rows around the send of a final answer: pending → attempting → delivered | failed.

    Honest at-least-once: a row left ``attempting`` when the process died is re-sent after
    a restart with a visible "recovered" marker, because Telegram may already have it.
    Poison rows cannot spin — a few attempts, a day of staleness measured from the first
    attempt — and the ledger never blocks a send: every method swallows its own errors.
    """

    MAX_ATTEMPTS = 3
    MAX_AGE_HOURS = 24
    KEEP_DAYS = 7
    MAX_TEXT_CHARS = 200_000

    def __init__(self, db: Database, *, max_attempts: int = MAX_ATTEMPTS, max_age_hours: int = MAX_AGE_HOURS, keep_days: int = KEEP_DAYS) -> None:
        self._db = db
        self.max_attempts = max_attempts
        self.max_age_hours = max_age_hours
        self.keep_days = keep_days

    async def begin(self, run_id: str, session_id: str, text: str) -> None:
        if len(text) > self.MAX_TEXT_CHARS:
            text = text[: self.MAX_TEXT_CHARS] + "\n\n[… the recovered copy is truncated; the full answer is in the Mini App transcript]"
        try:
            await self._db.execute(
                "INSERT OR REPLACE INTO deliveries(run_id, session_id, text, status, attempts, created_at, updated_at)"
                " VALUES (?, ?, ?, 'attempting', COALESCE((SELECT attempts FROM deliveries WHERE run_id = ?), 0) + 1,"
                " COALESCE((SELECT created_at FROM deliveries WHERE run_id = ?), ?), ?)",
                (run_id, session_id, text, run_id, run_id, _now(), _now()),
            )
        except Exception:  # noqa: BLE001
            pass

    async def settle(self, run_id: str, *, delivered: bool, error: str = "") -> None:
        try:
            await self._db.execute(
                "UPDATE deliveries SET status = ?, error = ?, updated_at = ? WHERE run_id = ?",
                ("delivered" if delivered else "failed", error[:500] or None, _now(), run_id),
            )
        except Exception:  # noqa: BLE001
            pass

    async def recoverable(self) -> list[dict[str, Any]]:
        """Rows a restart should re-send: not delivered, under the attempt cap, not stale."""
        cutoff = (datetime.now(UTC) - timedelta(hours=self.max_age_hours)).isoformat()
        try:
            rows = await self._db.fetchall(
                "SELECT * FROM deliveries WHERE status IN ('attempting', 'failed') AND attempts < ? AND created_at >= ? ORDER BY created_at",
                (self.max_attempts, cutoff),
            )
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    async def prune(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=self.keep_days)).isoformat()
        try:
            await self._db.execute("DELETE FROM deliveries WHERE updated_at < ?", (cutoff,))
        except Exception:  # noqa: BLE001
            pass
