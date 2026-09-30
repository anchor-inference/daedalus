"""Passage indexing without loading transcripts; reciprocal-rank fusion without length bias."""
from __future__ import annotations

import heapq
import json
import math
import sqlite3
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from daedalus.security.redact import redact
from daedalus.stores.database import Database
from daedalus.stores.sqlite import fts_query, snippet

PASSAGE_CHARS = 1000
STRIDE = 872
CANDIDATES = 200
MAX_VECTORS = 50_000
QUERY_SECONDS = 0.5
MIN_SIMILARITY = 0.78
# Extract one block inside SQLite, then return only one bounded passage to Python. Thinking,
# images and audio never enter the index. Tool results remain searchable just as in HistorySearch.
TEXT = ("coalesce(json_extract(b.value, '$.text'), json_extract(b.value, '$.content'),"
        " CASE WHEN json_extract(b.value, '$.kind')='tool_use' THEN"
        " coalesce(json_extract(b.value, '$.name'),'') || ' ' || coalesce(json_extract(b.value, '$.arguments_json'),'') END, '')")
BLOCKS = "json_each(t.message, '$.content_blocks') b"
KINDS = "json_extract(b.value, '$.kind') IN ('text', 'tool_result', 'tool_use')"


def pack(vector: list[float], dimension: int) -> bytes:
    if len(vector) != dimension or not all(math.isfinite(x) for x in vector):
        raise ValueError("invalid embedding dimension or values")
    norm = math.sqrt(sum(x * x for x in vector))
    if not norm:
        raise ValueError("empty embedding")
    return struct.pack(f"<{dimension}f", *(x / norm for x in vector))


def fusion(exact_rank: int | None, semantic_rank: int | None) -> float:
    """Equal-weight RRF, k=60, one-based ranks; a missing half contributes zero."""
    return sum(1 / (60 + r) for r in (exact_rank, semantic_rank) if r is not None and r > 0)


def collapse(hits: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """The best passage wins; repeating text in a long conversation buys no extra score."""
    best: dict[str, dict[str, Any]] = {}
    for hit in sorted(hits, key=lambda h: (-h['score'], h['session_id'], h.get('seq', 0), h.get('offset', 0))):
        best.setdefault(hit['session_id'], hit)
    return list(best.values())[:max(1, min(limit, 50))]


def collapse_passages(hits: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """The best hit per message of one conversation, the conversation's title left out."""
    best: dict[int, dict[str, Any]] = {}
    for hit in sorted(hits, key=lambda h: (-h['score'], h.get('seq', 0), h.get('offset', 0))):
        if hit.get('seq'):
            best.setdefault(int(hit['seq']), hit)
    return list(best.values())[:max(1, min(limit, 50))]


@dataclass
class Passage:
    seq: int
    block: int
    offset: int
    text: str
    length: int
    revision: str


class Index:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.newest = False

    async def prepare(self, model: str, dimension: int) -> None:
        space = [model, dimension]
        if await self.db.kv_get('search_space') == space:
            return
        async with self.db.transaction() as conn:
            await conn.execute('DELETE FROM search_vectors')
            await conn.execute('DELETE FROM search_titles')
            await conn.execute('DELETE FROM search_pending')
            await conn.execute('INSERT INTO search_pending(seq) SELECT seq FROM transcript')
            await conn.execute("INSERT INTO kv(key,value) VALUES ('search_space',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(space),))

    async def next(self) -> Passage | None:
        # Alternate old and new work: an old import cannot starve a newly settled answer.
        self.newest = not self.newest
        order = 'DESC' if self.newest else 'ASC'
        pending = await self.db.fetchone(f'SELECT * FROM search_pending ORDER BY seq {order} LIMIT 1')
        if pending is None:
            return None
        row = await self.db.fetchone(
            f"SELECT b.key block, length({TEXT}) length, substr({TEXT}, CASE WHEN b.key=? THEN ? ELSE 1 END, ?) text"
            f" FROM transcript t, {BLOCKS} WHERE t.seq=? AND b.key>=? AND json_extract(b.value, '$.kind')='text'"
            f" AND length({TEXT})>0 ORDER BY b.key LIMIT 1",
            (pending['block'], pending['offset'] + 1, PASSAGE_CHARS, pending['seq'], pending['block']),
        )
        return Passage(pending['seq'], row['block'] if row else -1,
                       pending['offset'] if row and row['block'] == pending['block'] else 0,
                       row['text'] if row else '', row['length'] if row else 0, pending['revision'])

    async def save(self, passage: Passage, vector: list[float] | None, model: str, dimension: int) -> bool:
        blob = pack(vector, dimension) if vector is not None else None
        async with self.db.transaction() as conn:
            row = await (await conn.execute('SELECT revision FROM search_pending WHERE seq=?', (passage.seq,))).fetchone()
            if row is None or row['revision'] != passage.revision:
                return False
            if blob is not None:
                await conn.execute('INSERT OR REPLACE INTO search_vectors VALUES (?, ?, ?, ?, ?, ?)',
                                   (passage.seq, passage.block, passage.offset, model, dimension, blob))
            if passage.block == -1:
                await conn.execute('DELETE FROM search_pending WHERE seq=?', (passage.seq,))
            else:
                more = passage.offset + PASSAGE_CHARS < passage.length
                await conn.execute('UPDATE search_pending SET block=?, offset=? WHERE seq=?',
                                   (passage.block if more else passage.block + 1, passage.offset + STRIDE if more else 0, passage.seq))
        return True

    async def title(self, tenant: str) -> Any:
        return await self.db.fetchone(
            'SELECT s.id, substr(s.title,1,1000) title FROM sessions s LEFT JOIN search_titles v ON v.session_id=s.id'
            ' WHERE s.tenant_id=? AND (v.session_id IS NULL OR v.title!=substr(s.title,1,1000)) ORDER BY s.last_message_at DESC LIMIT 1', (tenant,))

    async def save_title(self, sid: str, title: str, vector: list[float], model: str, dimension: int) -> None:
        await self.db.execute(
            'INSERT OR REPLACE INTO search_titles SELECT id, substr(title,1,1000), ?, ?, ? FROM sessions WHERE id=? AND substr(title,1,1000)=?',
            (model, dimension, pack(vector, dimension), sid, title))


def search(path: Path, tenant: str, query: str, *, vector: list[float] | None = None,
           model: str = '', dimension: int = 0, project: str = '', limit: int = 30,
           seconds: float = QUERY_SECONDS, max_vectors: int = MAX_VECTORS, session: str = '') -> dict[str, Any]:
    """Dedicated read connection, SQL deadline, streaming vector pages and bounded candidate sets."""
    if not fts_query(query.strip()[:500]):
        return {'hits': [], 'partial': False}
    deadline = time.monotonic() + seconds
    conn = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True, timeout=0.05)
    conn.row_factory = sqlite3.Row
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    exact: dict[int | str, int] = {}
    exact_sessions: dict[int | str, str] = {}
    semantic: list[tuple[float, str, int, int, int]] = []
    hits: list[dict[str, Any]] = []
    partial = False
    scope = 's.tenant_id=?' + (' AND s.project_id=?' if project else '') + (' AND s.id=?' if session else '')
    params = (tenant,) + ((project,) if project else ()) + ((session,) if session else ())
    query = query.strip()[:500]
    try:
        match = fts_query(query)
        # Titles participate in the exact half too, including sessions without a transcript.
        literal = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        titles = conn.execute(f"SELECT s.id FROM sessions s WHERE {scope} AND s.title LIKE ? ESCAPE '\\' ORDER BY s.last_message_at DESC, s.id LIMIT ?", (*params, '%' + literal + '%', CANDIDATES))
        for row in titles:
            exact[row['id']] = len(exact) + 1
            exact_sessions[row['id']] = row['id']
        rows = conn.execute(
            'SELECT t.seq, s.id FROM transcript_fts f JOIN transcript t ON t.seq=f.rowid JOIN sessions s ON s.id=t.session_id'
            f' WHERE transcript_fts MATCH ? AND {scope} ORDER BY bm25(transcript_fts), t.seq LIMIT ?', (match, *params, CANDIDATES))
        for row in rows:
            exact[row['seq']] = len(exact) + 1
            exact_sessions[row['seq']] = row['id']
        if vector is not None:
            import numpy as np  # Lazy: lexical search works without the optional tensor runtime.

            q = np.frombuffer(pack(vector, dimension), dtype='<f4')
            cursor = conn.execute(
                'SELECT v.vector, s.id, v.seq, v.block, v.offset FROM search_vectors v'
                ' JOIN transcript t ON t.seq=v.seq JOIN sessions s ON s.id=t.session_id'
                f' WHERE {scope} AND v.model=? AND v.dimension=? ORDER BY v.seq DESC, v.block, v.offset LIMIT ?',
                (*params, model, dimension, max_vectors + 1))
            scanned = 0
            while True:
                batch = cursor.fetchmany(256)
                if not batch:
                    break
                for row in batch:
                    scanned += 1
                    if scanned > max_vectors or time.monotonic() > deadline:
                        partial = True
                        break
                    score = float(np.dot(np.frombuffer(row['vector'], dtype='<f4'), q))
                    entry = (score, row['id'], row['seq'], row['block'], row['offset'])
                    if score >= MIN_SIMILARITY:
                        if len(semantic) < CANDIDATES:
                            heapq.heappush(semantic, entry)
                        elif entry > semantic[0]:
                            heapq.heapreplace(semantic, entry)
                if partial:
                    break
            for row in conn.execute(f'SELECT v.vector, s.id FROM search_titles v JOIN sessions s ON s.id=v.session_id WHERE {scope} AND v.model=? AND v.dimension=? LIMIT ?', (*params, model, dimension, max_vectors)):
                score = float(np.dot(np.frombuffer(row['vector'], dtype='<f4'), q))
                if score >= MIN_SIMILARITY:
                    heapq.heappush(semantic, (score, row['id'], 0, 0, 0))
                    if len(semantic) > CANDIDATES:
                        heapq.heappop(semantic)
    except sqlite3.OperationalError as exc:
        if 'interrupt' not in str(exc) and 'locked' not in str(exc):
            raise
        partial = True
    finally:
        for rank, (_, sid, seq, block, offset) in enumerate(sorted(semantic, reverse=True), 1):
            hits.append(dict(session_id=sid, seq=seq, block=block, offset=offset, score=fusion(exact.get(seq or sid), rank)))
        for key, rank in exact.items():
            hits.append(dict(session_id=exact_sessions[key], seq=key if isinstance(key, int) else 0,
                             block=-1 if isinstance(key, int) else 0, offset=0, score=fusion(rank, None)))
        # Snippets get their own small deadline so a partial scan can still display its answers.
        conn.set_progress_handler(None, 0)
    try:
        # Inside one conversation every passage is an answer of its own; across conversations the
        # best one stands for its conversation.
        result = collapse_passages(hits, limit) if session else collapse(hits, limit)
        snippet_deadline = time.monotonic() + 0.15
        conn.set_progress_handler(lambda: int(time.monotonic() > snippet_deadline), 1000)
        visible = []
        for hit in result:
            # Recheck scope at the disclosure boundary; deletion or membership changes win.
            row = conn.execute(f'SELECT substr(s.title,1,200) title FROM sessions s WHERE s.id=? AND {scope}', (hit['session_id'], *params)).fetchone()
            if row is None:
                continue
            text = row['title']
            if hit['seq']:
                if hit['block'] >= 0:
                    passage = conn.execute(f'SELECT substr({TEXT},?,?) FROM transcript t, {BLOCKS} WHERE t.seq=? AND b.key=?', (hit['offset'] + 1, PASSAGE_CHARS, hit['seq'], hit['block'])).fetchone()
                else:
                    # The FTS table is contentless. Locate the best word inside a transcript block
                    # in SQL and return its neighbourhood, never the full serialized message.
                    word = query.split()[0].strip('"')
                    locate = f"max(instr({TEXT},?), instr({TEXT},?), instr({TEXT},?))"
                    variants = (word.lower(), word.capitalize(), word.upper())
                    passage = conn.execute(
                        f'SELECT substr({TEXT}, max(1,{locate}-50),?) FROM transcript t, {BLOCKS}'
                        f' WHERE t.seq=? AND {KINDS} ORDER BY ({locate}>0) DESC, b.key LIMIT 1',
                        (*variants, PASSAGE_CHARS, hit['seq'], *variants)).fetchone()
                if passage:
                    text = passage[0]
            hit['snippet'] = redact(snippet(text, query, window=180))
            visible.append(hit)
        return {'hits': visible, 'partial': partial}
    except sqlite3.OperationalError:
        return {'hits': [], 'partial': True}
    finally:
        conn.close()
