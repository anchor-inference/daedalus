"""Passage indexing without loading transcripts; reciprocal-rank fusion without length bias."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from daedalus.memory.privacy import public_sessions_sql
from daedalus.search.chats import Reach
from daedalus.security.redact import redact
from daedalus.stores.database import Database
from daedalus.stores.sqlite import _FTS_TOKEN_RE, fts_query, snippet

PASSAGE_CHARS = 1000
STRIDE = 872
CANDIDATES = 200
QUERY_SECONDS = 0.5
LOAD_SECONDS = 5.0
"""Budget for bringing the in-memory copy of the vectors up to date, apart from the query's own. The first
query after a start reads every vector once (about 30 MB for 90,000 passages), which takes longer than a
query may; every later one reads only what was added since."""
MIN_SIMILARITY = 0.0
"""The default cosine floor of :func:`search`; the service passes the loaded model's own
(:attr:`daedalus.speech.embedding_catalog.EmbeddingModel.min_similarity`)."""
SHORT_WORD = 2
"""Words of this many characters or fewer are left out of an any-word query: as a prefix, "в" or "to"
matches nearly every message and buys nothing but time."""
FUNCTION_WORDS = frozenset("""
the and for are but not was were been being have has had does did doing will would could should shall can may might must
this that these those there here what which who whom whose when where why how than then them they their there's its it's
you your yours our ours his her him she from into onto with without about over under after before again also just only
very more most some any all each every other such own same too out off yes both few nor one ago get got
это эта эти этот этого этой этом эту как что чтобы для при над под или его её она они оно был была были было быть будет
есть нет так там тут уже ещё еще только также если когда где кто чем тоже ему ней них того тому через про без между
после перед потом сейчас можно нужно надо очень мне меня мой моя мои наш наша наши ваш ваша вас нас вам нам себя себе
свой своя свои который которая которые которых весь вся всё все всех какой какая какие каким почему зачем тогда
""".split())
"""Words that say nothing about a topic, left out of an any-word query (unless they are all there is).
BM25 already gives them almost no weight, so leaving them out hardly changes the order; what it changes
is the time, because as a prefix "which" or "как" matches most of the transcript and every one of those
rows is scored before the best two hundred are kept."""
# Extract one block inside SQLite, then return only one bounded passage to Python. Thinking,
# images and audio never enter the index. Tool results remain searchable just as in HistorySearch.
TEXT = ("coalesce(json_extract(b.value, '$.text'), json_extract(b.value, '$.content'),"
        " CASE WHEN json_extract(b.value, '$.kind')='tool_use' THEN"
        " coalesce(json_extract(b.value, '$.name'),'') || ' ' || coalesce(json_extract(b.value, '$.arguments_json'),'') END, '')")
BLOCKS = "json_each(t.message, '$.content_blocks') b"
KINDS = "json_extract(b.value, '$.kind') IN ('text', 'tool_result', 'tool_use')"


def pack(vector: list[float]) -> tuple[float, bytes]:
    """A unit vector as signed bytes and the one scale that turns them back into floats.

    A scale per vector rather than per dimension: per dimension needs statistics over a corpus that is
    still being written, and with 312 dimensions the difference was below what the evaluation can see
    (every chat and public score moved by at most 0.003 against float32). A quarter of the space."""
    import numpy as np  # Lazy: lexical search works without the optional tensor runtime.

    value = np.asarray(vector, dtype=np.float32)
    if value.ndim != 1 or not value.size or not np.isfinite(value).all():
        raise ValueError("invalid embedding dimension or values")
    norm = float(np.linalg.norm(value))
    if not norm:
        raise ValueError("empty embedding")
    value /= norm
    scale = float(np.abs(value).max()) / 127
    return scale, np.round(value / scale).astype(np.int8).tobytes()


def unpack(scale: float, blob: bytes) -> list[float]:
    import numpy as np  # Lazy: lexical search works without the optional tensor runtime.

    return (np.frombuffer(blob, dtype=np.int8).astype(np.float32) * scale).tolist()


def digest(text: str) -> bytes:
    """What identifies a passage's text: equal text has an equal vector, so it is encoded once."""
    return hashlib.blake2b(text.encode(), digest_size=12).digest()


def fusion(exact_rank: int | None, semantic_rank: int | None) -> float:
    """Equal-weight RRF, k=60, one-based ranks; a missing half contributes zero.

    Equal, because nothing measured better: on real conversations, weighting the words half at 0.5 or
    0.75 of the meaning half gave the same nDCG@10 (0.239 and 0.240 against 0.240) and hit@10 within
    noise. The fusion itself pays: it beat the meaning half alone by +0.021 nDCG@10 (95% CI
    +0.005..+0.037)."""
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

    @property
    def settled(self) -> bool:
        """Nothing left to embed in this row: the queue entry goes, with no vector."""
        return self.block == -1


class Index:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def prepare(self, space: str) -> None:
        """Start over when the vectors on disk come from another model or another way of storing them."""
        if await self.db.kv_get('search_space') == space:
            return
        async with self.db.transaction() as conn:
            await conn.execute('DELETE FROM search_vectors')
            await conn.execute('DELETE FROM search_titles')
            await conn.execute('DELETE FROM search_pending')
            await conn.execute('INSERT INTO search_pending(seq) SELECT seq FROM transcript')
            await conn.execute("INSERT INTO kv(key,value) VALUES ('search_space',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(space),))

    async def batch(self, limit: int) -> list[Passage]:
        """The next passage of up to ``limit`` queued rows, half the newest and half the oldest, so an old
        import cannot starve a newly settled answer. A row of a private chat, or one with no text left,
        comes back settled (see :attr:`Passage.settled`)."""
        newest = await self.db.fetchall('SELECT * FROM search_pending ORDER BY seq DESC LIMIT ?', (max(1, limit // 2),))
        oldest = await self.db.fetchall('SELECT * FROM search_pending ORDER BY seq ASC LIMIT ?', (limit,))
        rows = {r['seq']: r for r in newest}
        for row in oldest:
            if len(rows) >= limit:
                break
            rows.setdefault(row['seq'], row)
        if not rows:
            return []
        marks = ','.join('?' for _ in rows)
        public = {r['seq']: bool(r['public']) for r in await self.db.fetchall(
            f'SELECT t.seq, {public_sessions_sql()} public FROM transcript t JOIN sessions s ON s.id=t.session_id WHERE t.seq IN ({marks})', tuple(rows))}
        out: list[Passage] = []
        for seq, pending in rows.items():
            if not public.get(seq, True):
                # Settled with no vector, so a private chat's row leaves the queue in one step.
                out.append(Passage(seq, -1, 0, '', 0, pending['revision']))
                continue
            row = await self.db.fetchone(
                f"SELECT b.key block, length({TEXT}) length, substr({TEXT}, CASE WHEN b.key=? THEN ? ELSE 1 END, ?) text"
                f" FROM transcript t, {BLOCKS} WHERE t.seq=? AND b.key>=? AND json_extract(b.value, '$.kind')='text'"
                f" AND length({TEXT})>0 ORDER BY b.key LIMIT 1",
                (pending['block'], pending['offset'] + 1, PASSAGE_CHARS, seq, pending['block']),
            )
            out.append(Passage(seq, row['block'] if row else -1,
                               pending['offset'] if row and row['block'] == pending['block'] else 0,
                               row['text'] if row else '', row['length'] if row else 0, pending['revision']))
        return out

    async def known(self, texts: list[str]) -> dict[str, tuple[float, bytes]]:
        """Vectors already stored for any of these texts. A quarter of the passages of a real corpus repeat
        an earlier one word for word (re-imported chats, a message quoted back), and each is encoded once."""
        wanted = {digest(t): t for t in texts if t}
        if not wanted:
            return {}
        marks = ','.join('?' for _ in wanted)
        rows = await self.db.fetchall(f'SELECT digest, scale, vector FROM search_vectors WHERE digest IN ({marks}) GROUP BY digest', tuple(wanted))
        return {wanted[bytes(r['digest'])]: (float(r['scale']), bytes(r['vector'])) for r in rows}

    async def save(self, entries: list[tuple[Passage, tuple[float, bytes] | None]]) -> int:
        """Store a batch of passages and advance each row's cursor, in one transaction. A row whose message
        changed since it was read is skipped: its new revision is queued again. Answers how many were kept."""
        kept = 0
        async with self.db.transaction() as conn:
            for passage, packed in entries:
                row = await (await conn.execute('SELECT revision FROM search_pending WHERE seq=?', (passage.seq,))).fetchone()
                if row is None or row['revision'] != passage.revision:
                    continue
                kept += 1
                if packed is not None and not passage.settled:
                    await conn.execute('INSERT OR REPLACE INTO search_vectors(seq, block, offset, digest, scale, vector) VALUES (?, ?, ?, ?, ?, ?)',
                                       (passage.seq, passage.block, passage.offset, digest(passage.text), packed[0], packed[1]))
                if passage.settled:
                    await conn.execute('DELETE FROM search_pending WHERE seq=?', (passage.seq,))
                else:
                    more = passage.offset + PASSAGE_CHARS < passage.length
                    await conn.execute('UPDATE search_pending SET block=?, offset=? WHERE seq=?',
                                       (passage.block if more else passage.block + 1, passage.offset + STRIDE if more else 0, passage.seq))
        return kept

    async def titles(self, tenant: str, limit: int) -> list[Any]:
        return await self.db.fetchall(
            'SELECT s.id, substr(s.title,1,1000) title FROM sessions s LEFT JOIN search_titles v ON v.session_id=s.id'
            f' WHERE s.tenant_id=? AND {public_sessions_sql()} AND (v.session_id IS NULL OR v.title!=substr(s.title,1,1000))'
            ' ORDER BY s.last_message_at DESC LIMIT ?', (tenant, limit))

    async def private(self, seq: int) -> bool:
        """Whether a transcript row belongs to a private chat, whose words are never embedded."""
        row = await self.db.fetchone(f'SELECT {public_sessions_sql()} public FROM transcript t JOIN sessions s ON s.id=t.session_id WHERE t.seq=?', (seq,))
        return row is not None and not row['public']

    async def save_titles(self, entries: list[tuple[str, str, tuple[float, bytes]]]) -> None:
        """Title vectors by session, each kept only while the session still has that title."""
        async with self.db.transaction() as conn:
            for sid, title, (scale, blob) in entries:
                await conn.execute(
                    'INSERT OR REPLACE INTO search_titles(session_id, title, scale, vector)'
                    ' SELECT id, substr(title,1,1000), ?, ? FROM sessions WHERE id=? AND substr(title,1,1000)=?',
                    (scale, blob, sid, title))


def any_words(query: str) -> str:
    """An FTS expression any one word of ``query`` satisfies, as a prefix, for BM25 to rank: a message
    with more of the words, and rarer ones, comes first. A quoted phrase stays one phrase.

    Requiring every word, as the search once did, answered almost nothing to a question in plain
    language: no single message holds every word of "how did we set up the backup", and on real
    conversations that scored 0.06 nDCG@10 against 0.21 for this."""
    match = fts_query(query)
    if not match or (match.startswith('"') and not match.endswith('*')):
        return match
    words = [w.replace('"', '') for w in _FTS_TOKEN_RE.findall(query.strip())]
    words = [w for w in words if len(w) > SHORT_WORD and w.lower() not in FUNCTION_WORDS] or words
    return ' OR '.join(f'"{w}"*' for w in dict.fromkeys(words) if w)


@dataclass
class Vectors:
    """Every passage vector of one database in memory, kept current by reading only what was added.

    Rows are numbered by an autoincrement id that is never reused, so "what was added" is everything past
    the highest id read. A deletion is noticed by count: the rows read before plus the rows added must
    be all there is, and when they are not, something was removed and the copy is read again whole."""
    space: str = ''
    upto: int = 0
    count: int = 0
    seqs: Any = None
    blocks: Any = None
    offsets: Any = None
    sessions: Any = None
    scales: Any = None
    matrix: Any = None
    names: list[str] = field(default_factory=list)
    codes: dict[str, int] = field(default_factory=dict)

    def reset(self, space: str) -> None:
        self.__dict__.update(Vectors(space=space).__dict__)

    def _read(self, conn: sqlite3.Connection) -> tuple[list[Any], int]:
        conn.execute('BEGIN')
        try:
            # One read snapshot for the count and the new rows, or a row written in between would be
            # counted and not read, and taken for a deletion. LEFT JOIN so that every counted row is
            # read, even one whose message is somehow gone: it is kept under no session, unreachable.
            rows = conn.execute('SELECT v.id, v.seq, v.block, v.offset, v.scale, v.vector, coalesce(t.session_id, \'\') FROM search_vectors v'
                                ' LEFT JOIN transcript t ON t.seq=v.seq WHERE v.id>? ORDER BY v.id', (self.upto,)).fetchall()
            total = conn.execute('SELECT count(*) FROM search_vectors').fetchone()[0]
        finally:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
        return rows, total

    def refresh(self, conn: sqlite3.Connection, space: str, dimension: int) -> None:
        import numpy as np  # Lazy: lexical search works without the optional tensor runtime.

        if space != self.space or (self.matrix is not None and self.matrix.shape[1] != dimension):
            self.reset(space)
        rows, total = self._read(conn)
        if total != self.count + len(rows):
            self.reset(space)
            rows, total = self._read(conn)
        if not rows:
            return
        self.upto = rows[-1][0]
        self.count = total
        rows = [r for r in rows if len(r[5]) == dimension]
        if not rows:
            return
        codes = [self.codes.setdefault(r[6], len(self.codes)) for r in rows]
        self.names.extend(list(self.codes)[len(self.names):])
        fresh = {
            'seqs': np.fromiter((r[1] for r in rows), dtype=np.int64, count=len(rows)),
            'blocks': np.fromiter((r[2] for r in rows), dtype=np.int32, count=len(rows)),
            'offsets': np.fromiter((r[3] for r in rows), dtype=np.int32, count=len(rows)),
            'sessions': np.asarray(codes, dtype=np.int32),
            'scales': np.fromiter((r[4] for r in rows), dtype=np.float32, count=len(rows)),
            'matrix': np.frombuffer(b''.join(r[5] for r in rows), dtype=np.int8).reshape(len(rows), dimension),
        }
        for name, value in fresh.items():
            current = getattr(self, name)
            setattr(self, name, value if current is None else np.concatenate([current, value]))

    def scores(self, query: Any, allowed: set[str]) -> tuple[Any, Any]:
        """Cosine of every allowed passage to ``query``, as (row indices, scores)."""
        import numpy as np  # Lazy: lexical search works without the optional tensor runtime.

        if self.matrix is None or not allowed:
            return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float32)
        codes = np.fromiter((self.codes[s] for s in allowed if s in self.codes), dtype=np.int32)
        rows = np.flatnonzero(np.isin(self.sessions, codes))
        out = np.empty(len(rows), dtype=np.float32)
        # In slices, so the float copy of the int8 matrix stays a few megabytes whatever the corpus.
        for start in range(0, len(rows), 16384):
            part = rows[start:start + 16384]
            out[start:start + len(part)] = (self.matrix[part].astype(np.float32) @ query) * self.scales[part]
        return rows, out


_VECTORS: OrderedDict[str, Vectors] = OrderedDict()
_VECTORS_LOCK = threading.Lock()


def _vectors(path: Path, conn: sqlite3.Connection, space: str, dimension: int) -> Vectors:
    """The process's copy for this database, brought up to date, as a snapshot the caller may read while
    another reader refreshes: arrays are only ever replaced, never changed in place, so holding the
    references holds a consistent copy, and the two growing lookups are copied."""
    key = str(path.resolve())
    with _VECTORS_LOCK:
        vectors = _VECTORS.pop(key, None) or Vectors()
        _VECTORS[key] = vectors
        while len(_VECTORS) > 4:
            _VECTORS.popitem(last=False)
        vectors.refresh(conn, space, dimension)
        return replace(vectors, names=list(vectors.names), codes=dict(vectors.codes))


def search(path: Path, tenant: str, query: str, *, vector: list[float] | None = None, space: str = '',
           project: str = '', limit: int = 30, seconds: float = QUERY_SECONDS, session: str = '',
           reach: Reach | None = None, min_similarity: float = MIN_SIMILARITY) -> dict[str, Any]:
    """Dedicated read connection, SQL deadline, the whole vector index in memory, bounded candidate sets.

    The words half matches any word and lets BM25 rank (see :func:`any_words`), except inside one
    conversation (``session``), whose hits are shown in transcript order rather than by score and so
    must each hold every word. ``reach`` narrows both halves and the disclosure check to what the
    agent's chat search may answer from. ``space`` names the model ``vector`` came from; vectors stored
    by another are never compared with it."""
    if not fts_query(query.strip()[:500]):
        return {'hits': [], 'partial': False}
    conn = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True, timeout=0.05, isolation_level=None)
    conn.row_factory = sqlite3.Row
    exact: dict[int | str, int] = {}
    exact_sessions: dict[int | str, str] = {}
    semantic: list[tuple[float, str, int, int, int]] = []
    hits: list[dict[str, Any]] = []
    partial = False
    scope = 's.tenant_id=?' + (' AND s.project_id=?' if project else '') + (' AND s.id=?' if session else '')
    params: tuple[str, ...] = (tenant,) + ((project,) if project else ()) + ((session,) if session else ())
    if reach is not None:
        clause, extra = reach.where()
        scope += f' AND {clause}'
        params += extra
    query = query.strip()[:500]
    vectors: Vectors | None = None
    if vector is not None:
        row = conn.execute("SELECT value FROM kv WHERE key='search_space'").fetchone()
        if row is not None and json.loads(row['value']) == space:
            load_deadline = time.monotonic() + LOAD_SECONDS
            conn.set_progress_handler(lambda: int(time.monotonic() > load_deadline), 1000)
            try:
                vectors = _vectors(path, conn, space, len(vector))
            except sqlite3.OperationalError as exc:
                if 'interrupt' not in str(exc) and 'locked' not in str(exc):
                    raise
                partial = True
    deadline = time.monotonic() + seconds
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        match = fts_query(query) if session else any_words(query)
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
        if vector is not None and vectors is not None:
            import numpy as np  # Lazy: lexical search works without the optional tensor runtime.

            q = np.asarray(vector, dtype=np.float32)
            q = q / np.linalg.norm(q)
            allowed = {r['id'] for r in conn.execute(f'SELECT s.id FROM sessions s WHERE {scope}', params)}
            indices, scores = vectors.scores(q, allowed)
            keep = np.flatnonzero(scores >= min_similarity)
            if len(keep) > CANDIDATES:
                keep = keep[np.argpartition(-scores[keep], CANDIDATES)[:CANDIDATES]]
            for k in keep:
                i = indices[k]
                semantic.append((float(scores[k]), vectors.names[vectors.sessions[i]], int(vectors.seqs[i]), int(vectors.blocks[i]), int(vectors.offsets[i])))
            for row in conn.execute(f'SELECT v.scale, v.vector, s.id FROM search_titles v JOIN sessions s ON s.id=v.session_id WHERE {scope}', params):
                if len(row['vector']) != len(q):
                    continue
                score = float(np.frombuffer(row['vector'], dtype=np.int8).astype(np.float32) @ q) * row['scale']
                if score >= min_similarity:
                    semantic.append((score, row['id'], 0, 0, 0))
            semantic = sorted(semantic, reverse=True)[:CANDIDATES]
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
                    # The FTS table is contentless. Locate a word inside a transcript block in SQL
                    # and return its neighbourhood, never the full serialized message. The longest
                    # word, since any one of them may be what matched and the longest is the least
                    # likely to sit in every message.
                    word = max((w.replace('"', '') for w in _FTS_TOKEN_RE.findall(query)), key=len, default='') or query.split()[0].strip('"')
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
