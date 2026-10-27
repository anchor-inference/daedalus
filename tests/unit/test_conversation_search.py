"""Retrieval ranks, privacy, bounded cursors, and semantic fallback."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from protocore.contracts.types import Message, MessageRole, Session, TextBlock

from daedalus.extensions.api import SESSION_PAGE, build_app
from daedalus.host.session_runner import SessionManager
from daedalus.search.service import ConversationSearch
from daedalus.search.store import PASSAGE_CHARS, Index, collapse, fusion, pack, search, unpack
from daedalus.speech.embedding_catalog import MODEL
from daedalus.stores.sqlite import SqliteSessionStore


async def add(db, sid='one', text='A bicycle route along the river', tenant='daedalus'):
    store = SqliteSessionStore(db)
    await store.create(Session(id=sid, tenant_id=tenant, title=sid))
    message = Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)])
    await store.append_transcript(sid, [message])
    return store, message


def complete_search(*args, **kwargs):
    """Retry on ``partial`` instead of trusting one attempt against ``search()``'s wall-clock budget.

    ``search()`` bounds its own SQLite work by real time so a loaded host returns something rather
    than blocking; under heavy scheduling contention that budget can lapse before the process even
    gets a turn to run the trivial query this test builds, which interrupts the scan and reports it
    honestly as ``partial``. That flag is the actual completion signal — retrying gives the next
    attempt a fresh budget, which is what fixed the intermittent ``IndexError`` here without ever
    widening the deadline itself (that would just move the same race, not remove it).
    """
    for _ in range(10):
        result = search(*args, **kwargs)
        if not result['partial']:
            return result
    raise AssertionError('search() kept returning a partial scan across 10 attempts')


def test_reciprocal_ranks_and_collapse_do_not_reward_long_conversations():
    assert fusion(1, 1) == pytest.approx(2 / 61)
    assert fusion(None, 2) == pytest.approx(1 / 62)
    assert fusion(None, None) == 0
    hits = [dict(session_id='one', seq=i, score=fusion(2, None)) for i in range(100)]
    hits += [dict(session_id='two', seq=101, score=fusion(1, 1))]
    assert [h['session_id'] for h in collapse(hits, 30)] == ['two', 'one']
    assert len(collapse(hits, 1)) == 1


async def index_all(index, vector=(1, 0), space='test'):
    """Every queued row embedded with the same vector, as the indexer would with a constant model."""
    await index.prepare(space)
    while batch := await index.batch(8):
        await index.save([(p, pack(list(vector)) if p.text else None) for p in batch])


async def test_lexical_fallback_is_bounded_and_scoped(db, tmp_path):
    await add(db)
    await add(db, 'private', tenant='someone-else')
    result = search(db.path, 'daedalus', 'bicycle')
    assert [h['session_id'] for h in result['hits']] == ['one']
    assert 'bicycle' in result['hits'][0]['snippet']
    assert len(result['hits'][0]['snippet']) < 220
    assert search(db.path, 'daedalus', 'bicycle', project='private')['hits'] == []
    assert search(db.path, 'daedalus', '"')['hits'] == []
    service = ConversationSearch(db, tmp_path)
    # On by default: without the model the words still answer, and the reason says what is missing.
    answer = await service.query('bicycle')
    assert answer['semantic'] is False and answer['reason'] == 'no_model'
    assert len(answer['hits']) == 1
    await service.configure('off', False)
    assert (await service.query('bicycle'))['reason'] == 'off'


async def test_a_question_in_plain_words_finds_the_message_holding_some_of_them(db):
    await add(db, 'one', 'The nightly backup runs from cron at three')
    await add(db, 'two', 'A recipe for bread')
    found = search(db.path, 'daedalus', 'how did we schedule the backup again')
    assert [h['session_id'] for h in found['hits']] == ['one']
    # Inside one conversation hits are listed in transcript order, so each must hold every word.
    assert search(db.path, 'daedalus', 'schedule backup', session='one')['hits'] == []
    assert len(search(db.path, 'daedalus', 'nightly backup', session='one')['hits']) == 1


async def test_chunks_resume_after_restart_and_replacement_invalidates_vectors(db):
    store, message = await add(db, text='river ' * 800)
    index = Index(db)
    await index.prepare('model-a')
    [first] = await index.batch(4)
    assert len(first.text) == PASSAGE_CHARS
    assert await index.save([(first, pack([1, 0]))]) == 1
    resumed = Index(db)
    [second] = await resumed.batch(4)
    assert second.offset > first.offset and len(second.text) <= PASSAGE_CHARS
    await resumed.save([(second, pack([1, 0]))])
    assert (await db.fetchone('SELECT count(*) FROM search_vectors'))[0] == 2
    updated = message.model_copy(update={'content_blocks': [TextBlock(text='Now a mountain trail')]})
    await store.replace_transcript_message('one', store.transcript_key(message), updated)
    assert (await db.fetchone('SELECT count(*) FROM search_vectors'))[0] == 0
    # A passage read before the message changed is not stored over the new revision.
    assert await index.save([(first, pack([1, 0]))]) == 0
    [current] = await index.batch(4)
    assert current.offset == 0 and 'mountain' in current.text
    await index.save([(current, pack([0, 1]))])
    await index.prepare('model-b')
    assert (await db.fetchone('SELECT count(*) FROM search_vectors'))[0] == 0
    assert (await index.batch(4))[0].offset == 0


async def test_new_message_becomes_semantically_findable_and_deleted_vectors_go(db):
    store, _ = await add(db)
    index = Index(db)
    await index_all(index)
    # Different words; only the embedding half can retrieve this passage.
    found = complete_search(db.path, 'daedalus', 'cycling by water', vector=[1, 0], space='test')
    assert found['hits'][0]['session_id'] == 'one'
    assert 'bicycle' in found['hits'][0]['snippet']
    await store.append_transcript('one', [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text='A mountain trail')])])
    await index_all(index, (0, 1))
    found = complete_search(db.path, 'daedalus', 'hill walking', vector=[0, 1], space='test')
    assert 'mountain' in found['hits'][0]['snippet']
    await db.execute("DELETE FROM transcript WHERE session_id='one'")
    assert (await db.fetchone('SELECT count(*) FROM search_vectors'))[0] == 0
    assert (await db.fetchone('SELECT count(*) FROM search_pending'))[0] == 0
    # The in-memory copy notices the deletion rather than answering from a vector that is gone.
    assert complete_search(db.path, 'daedalus', 'hill walking', vector=[0, 1], space='test')['hits'] == []


async def test_the_whole_index_is_compared_and_kept_current_in_memory(db):
    """No cap on the vectors a query compares (one stopped at 50,000 while the index held 91,000): the
    oldest passage is found among many, and additions, deletions and replacements are all seen."""
    sessions = [f's{i}' for i in range(40)]
    for sid in sessions:
        await add(db, sid, f'unrelated words {sid}')
    index = Index(db)
    await index_all(index, (0, 1))
    oldest = (await db.fetchone("SELECT min(seq) seq FROM transcript"))['seq']
    await db.execute('UPDATE search_vectors SET scale=?, vector=? WHERE seq=?', (*pack([1, 0]), oldest))
    found = complete_search(db.path, 'daedalus', 'zeppelin', vector=[1, 0], space='test', min_similarity=0.5)
    assert [h['session_id'] for h in found['hits']] == ['s0']
    await add(db, 'late', 'one more')
    await index_all(index, (1, 0))
    found = complete_search(db.path, 'daedalus', 'zeppelin', vector=[1, 0], space='test', min_similarity=0.5)
    assert {h['session_id'] for h in found['hits']} == {'s0', 'late'}
    await db.execute("DELETE FROM transcript WHERE session_id='s0'")
    found = complete_search(db.path, 'daedalus', 'zeppelin', vector=[1, 0], space='test', min_similarity=0.5)
    assert [h['session_id'] for h in found['hits']] == ['late']
    # A replaced row has a new id: the old copy is noticed by count and read again whole.
    row = await db.fetchone("SELECT v.seq, block, offset, digest FROM search_vectors v JOIN transcript t ON t.seq=v.seq WHERE t.session_id='s1'")
    await db.execute('INSERT OR REPLACE INTO search_vectors(seq, block, offset, digest, scale, vector) VALUES (?, ?, ?, ?, ?, ?)',
                     (row['seq'], row['block'], row['offset'], row['digest'], *pack([1, 0])))
    found = complete_search(db.path, 'daedalus', 'zeppelin', vector=[1, 0], space='test', min_similarity=0.5)
    assert {h['session_id'] for h in found['hits']} == {'s1', 'late'}


async def test_vectors_of_another_model_are_never_compared(db):
    await add(db)
    await index_all(Index(db), space='one')
    assert search(db.path, 'daedalus', 'paraphrase', vector=[1, 0], space='two')['hits'] == []
    assert complete_search(db.path, 'daedalus', 'paraphrase', vector=[1, 0], space='one')['hits']
    # Below the model's floor a passage is no semantic answer.
    assert complete_search(db.path, 'daedalus', 'paraphrase', vector=[0, 1], space='one', min_similarity=0.5)['hits'] == []


def test_vectors_are_stored_as_bytes_with_one_scale():
    scale, blob = pack([3.0, -4.0, 0.0])
    assert len(blob) == 3
    assert unpack(scale, blob) == pytest.approx([0.6, -0.8, 0.0], abs=0.01)
    with pytest.raises(ValueError):
        pack([float('nan')])
    with pytest.raises(ValueError):
        pack([0.0, 0.0])


class Counting:
    """A fake model that records what it was asked to encode."""

    def __init__(self, delay: float = 0.0) -> None:
        self.texts: list[str] = []
        self.delay = delay

    def encode(self, text, **kwargs):
        import time

        time.sleep(self.delay)
        self.texts.append(text)
        return [1.0] * MODEL.dimension


async def test_a_pass_commits_a_batch_and_encodes_a_repeated_passage_once(db, tmp_path, monkeypatch):
    for i in range(6):
        await add(db, f's{i}', 'the very same passage, imported twice' if i < 4 else f'a passage of its own {i}')
    service = ConversationSearch(db, tmp_path)
    service.encoder = Counting()
    monkeypatch.setattr(service, 'reason', lambda: 'ready')
    assert await service.step() == 6  # the titles, in one pass
    assert await service.step() == 6  # every message, in one pass
    assert await service.step() == 6  # each found to have nothing more, and settled
    assert await service.step() == 0
    passages = [t for t in service.encoder.texts if not t.startswith('s')]
    assert sorted(passages) == ['a passage of its own 4', 'a passage of its own 5', 'the very same passage, imported twice']
    assert (await db.fetchone('SELECT count(*) FROM search_vectors'))[0] == 6
    # A message added later that repeats an indexed passage reuses its vector.
    await add(db, 's9', 'the very same passage, imported twice')
    while await service.step():
        pass
    assert service.encoder.texts.count('the very same passage, imported twice') == 1
    assert (await db.fetchone('SELECT count(*) FROM search_pending'))[0] == 0


async def test_a_pass_that_runs_out_of_time_keeps_the_rest_queued(db, tmp_path, monkeypatch):
    for i in range(5):
        await add(db, f's{i}', f'passage number {i}')
    service = ConversationSearch(db, tmp_path)
    service.encoder = Counting(delay=0.02)
    monkeypatch.setattr(service, 'reason', lambda: 'ready')
    while (await db.fetchone('SELECT count(*) FROM search_titles'))[0] < 5:
        await service.step()
    assert await service.step(seconds=0.001) == 1
    assert (await db.fetchone('SELECT count(*) FROM search_vectors'))[0] == 1
    assert (await db.fetchone('SELECT count(*) FROM search_pending WHERE block=0 AND offset=0'))[0] == 4
    while await service.step():
        pass
    assert (await db.fetchone('SELECT count(*) FROM search_vectors'))[0] == 5


async def test_backfill_limits_pause_busy_and_resume(db, tmp_path, monkeypatch):
    await add(db, text='long passage ' * 3000)
    service = ConversationSearch(db, tmp_path)
    service.encoder = Counting()
    monkeypatch.setattr(service, 'reason', lambda: 'ready')
    await service.step()  # the title
    assert await service.step() == 1  # one passage of the one long message per pass
    pending = dict(await db.fetchone('SELECT * FROM search_pending'))
    service.settings['paused'] = True
    assert await service.step() == 0
    service.settings['paused'] = False
    service.manager = SimpleNamespace(busy_sessions=lambda: {'active'}, idle_work=asyncio.Lock())
    assert await service.step() == 0
    assert dict(await db.fetchone('SELECT * FROM search_pending')) == pending
    service.manager = None
    assert await service.step() == 1
    assert dict(await db.fetchone('SELECT * FROM search_pending')) != pending


async def test_on_by_default_the_model_is_fetched_once_then_retried_after_a_pause(db, tmp_path, monkeypatch):
    from daedalus.search import service as module

    service = ConversationSearch(db, tmp_path)
    await service.load()
    assert service.settings['mode'] == 'local' and service.reason() == 'no_model'
    started = []
    monkeypatch.setattr(service.downloads, 'start', lambda model_id: started.append(model_id))
    assert await service.step() == 0
    assert await service.step() == 0
    assert started == [MODEL.id]
    monkeypatch.setattr(module, 'DOWNLOAD_RETRY_SECONDS', 0)
    await service.step()
    assert started == [MODEL.id, MODEL.id]
    await service.configure('off', False)
    await service.step()
    assert len(started) == 2


async def test_the_backfill_thread_runs_at_the_lowest_priority(db, tmp_path):
    import os
    import sys

    if not sys.platform.startswith('linux'):
        pytest.skip('per-thread priority is a Linux scheduler feature')
    service = ConversationSearch(db, tmp_path)
    try:
        niceness = await asyncio.get_running_loop().run_in_executor(service._executor(), lambda: os.getpriority(os.PRIO_PROCESS, 0))
        assert niceness == 19
        assert os.getpriority(os.PRIO_PROCESS, 0) < 19  # the bot's own threads keep theirs
    finally:
        await service.close()


async def test_api_auth_limits_and_retrieval_beyond_the_listing_page(db, settings, config):
    manager = SessionManager(settings, config, db=db)
    await add(db, 'old', 'a distinctive bicycle')
    await add(db, 'hidden', 'a distinctive bicycle', tenant='elsewhere')
    await db.executemany(
        "INSERT INTO sessions(id,tenant_id,title,created_at,last_message_at,metadata,project_id)"
        " SELECT ?,tenant_id,'newer',created_at,'2099-01-01','{}',project_id FROM sessions WHERE id='old'",
        [(f'new-{i}',) for i in range(205)],
    )
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={})
    api = build_app(app, 'test-token')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test') as client:
        assert (await client.get('/api/sessions/search', params={'q': 'bicycle'})).status_code == 401
        headers = {'X-Daedalus-Token': 'test-token'}
        page = (await client.get('/api/sessions', headers=headers)).json()
        assert len(page['sessions']) == SESSION_PAGE and page['next_cursor'] and all(s['id'] != 'old' for s in page['sessions'])
        assert (await client.get('/api/sessions/search', params={'q': 'x', 'limit': 51}, headers=headers)).status_code == 422
        answer = await client.get('/api/sessions/search', params={'q': 'bicycle'}, headers=headers)
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert [row['id'] for row in body['sessions']] == ['old']
        assert 'bicycle' in body['sessions'][0]['match']['snippet']
        assert body['semantic'] is False


async def test_passages_come_from_the_row_even_far_into_a_large_message(db):
    await add(db, text='ordinary text ' * 25000 + 'Загородный велосипедный маршрут у реки')
    result = search(db.path, 'daedalus', 'загородный', seconds=1)
    assert 'Загородный' in result['hits'][0]['snippet']
    assert len(result['hits'][0]['snippet']) < 220


async def test_deleting_an_agent_removes_its_passage_and_title_vectors(db, settings, config):
    manager = SessionManager(settings, config, db=db)
    await add(db)
    index = Index(db)
    await index_all(index)
    await index.save_titles([('one', 'one', pack([1, 0]))])
    assert (await db.fetchone('SELECT count(*) FROM search_titles'))[0] == 1
    assert await manager.delete_session('one', delete_workspace=False)
    for table in ('search_vectors', 'search_titles', 'search_pending'):
        assert (await db.fetchone(f'SELECT count(*) FROM {table}'))[0] == 0


async def test_fresh_child_activity_moves_a_project_even_when_its_leader_is_old(db):
    import json

    from daedalus.stores.projects import ProjectStore

    await add(db, 'leader')
    store = SqliteSessionStore(db)
    await store.create(Session(id='child', tenant_id='daedalus', title='child'), project_id='leader')
    await db.execute("UPDATE sessions SET last_message_at='2026-01-01' WHERE id='leader'")
    await db.execute("UPDATE sessions SET last_message_at='2026-09-19',metadata=? WHERE id='child'", (json.dumps({'subagent_of': 'leader'}),))
    folder = (await ProjectStore(db).summary())['leader']
    assert folder['last_message_at'] == '2026-09-19'
    assert folder['members'] == 2 and folder['total'] == 1


async def test_cancellation_keeps_the_inference_lock_until_the_native_call_finishes():
    import threading

    from daedalus.search.service import offload

    started, release = threading.Event(), threading.Event()
    lock = asyncio.Lock()

    def native():
        started.set()
        release.wait(2)

    async def work():
        async with lock:
            await offload(native)

    task = asyncio.create_task(work())
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    assert lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lock.locked()


async def test_an_agent_start_waits_for_the_current_chunk_then_prevents_more(db, settings, config, tmp_path, monkeypatch):
    import threading

    await add(db)
    manager = SessionManager(settings, config, db=db)
    active = set()
    monkeypatch.setattr(manager, 'busy_sessions', lambda: active)
    service = ConversationSearch(db, tmp_path, manager=manager)
    started, release = threading.Event(), threading.Event()

    def encode(*args, **kwargs):
        started.set()
        release.wait(2)
        return [1.0] * MODEL.dimension

    service.encoder = SimpleNamespace(encode=encode)
    monkeypatch.setattr(service, 'reason', lambda: 'ready')

    async def run(*args, **kwargs):
        active.add('one')
        return 'run'

    monkeypatch.setattr(manager, '_start_run_ready', run)
    indexing = asyncio.create_task(service.step(passages=1))
    while not started.is_set():
        await asyncio.sleep(0.01)
    agent = asyncio.create_task(manager._start_run_locked(None, None))
    await asyncio.sleep(0.01)
    assert not agent.done()
    release.set()
    await indexing
    assert await agent == 'run'
    assert await service.step() == 0


@pytest.mark.parametrize('damaged', [False, True])
async def test_embedding_files_use_the_shared_verified_downloads(tmp_path, damaged):
    import hashlib
    from dataclasses import replace

    from daedalus.speech.embedding_catalog import ModelFile, resolve
    from daedalus.speech.models import Downloads

    payloads = {'model.onnx': b'weights', 'tokenizer.json': b'tokens'}
    files = tuple(ModelFile(MODEL.id, name, f'https://models.example/{name}', len(body), hashlib.sha256(body).hexdigest()) for name, body in payloads.items())
    model = replace(MODEL, files=files)
    downloads = Downloads(tmp_path, lookup=lambda _: model, resolver=resolve)

    def fetch(request):
        body = payloads[request.url.path.lstrip('/')]
        if damaged:
            body = b'x' * len(body)
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        downloads.start(MODEL.id, client=client)
        await asyncio.gather(*list(downloads._running.values()))
    progress = downloads.progress()[MODEL.id]
    assert progress.state == ('failed' if damaged else 'installed')
    assert downloads.is_installed(MODEL.id) is (not damaged)
    if not damaged:
        assert progress.done_bytes == model.size_bytes
        assert (downloads.directory(MODEL.id) / 'model.onnx').read_bytes() == b'weights'
        assert await downloads.delete(MODEL.id)
        assert not downloads.is_installed(MODEL.id)


@pytest.mark.parametrize('seed', ['exact', 'wrong'])
async def test_a_file_seeded_into_the_model_directory_is_taken_without_a_download(tmp_path, seed):
    """How a machine gets the model while its download host is not settled: copy the file in by hand."""
    import hashlib
    from dataclasses import replace

    from daedalus.speech.embedding_catalog import ModelFile, resolve
    from daedalus.speech.models import Downloads

    payloads = {'model.onnx': b'weights', 'tokenizer.json': b'tokens'}
    files = tuple(ModelFile(MODEL.id, name, f'https://models.example/{name}', len(body), hashlib.sha256(body).hexdigest()) for name, body in payloads.items())
    model = replace(MODEL, files=files)
    downloads = Downloads(tmp_path, lookup=lambda _: model, resolver=resolve)
    directory = downloads.directory(MODEL.id)
    directory.mkdir(parents=True)
    (directory / 'model.onnx').write_bytes(b'weights' if seed == 'exact' else b'Weights')
    fetched = []

    def fetch(request):
        fetched.append(request.url.path.lstrip('/'))
        return httpx.Response(200, content=payloads[request.url.path.lstrip('/')])

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        downloads.start(MODEL.id, client=client)
        await asyncio.gather(*list(downloads._running.values()))
    assert downloads.is_installed(MODEL.id)
    assert (directory / 'model.onnx').read_bytes() == b'weights'
    # The tokenizer was not seeded, so it alone is fetched; a seed with the wrong bytes is replaced.
    assert fetched == (['tokenizer.json'] if seed == 'exact' else ['model.onnx', 'tokenizer.json'])


async def test_a_failed_download_leaves_a_seeded_file_where_it_was(tmp_path):
    import hashlib
    from dataclasses import replace

    from daedalus.speech.embedding_catalog import ModelFile, resolve
    from daedalus.speech.models import Downloads

    payloads = {'model.onnx': b'weights', 'tokenizer.json': b'tokens'}
    files = tuple(ModelFile(MODEL.id, name, f'https://models.example/{name}', len(body), hashlib.sha256(body).hexdigest()) for name, body in payloads.items())
    downloads = Downloads(tmp_path, lookup=lambda _: replace(MODEL, files=files), resolver=resolve)
    directory = downloads.directory(MODEL.id)
    directory.mkdir(parents=True)
    (directory / 'model.onnx').write_bytes(b'weights')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))) as client:
        downloads.start(MODEL.id, client=client)
        await asyncio.gather(*list(downloads._running.values()))
    assert downloads.progress()[MODEL.id].state == 'failed'
    assert (directory / 'model.onnx').read_bytes() == b'weights'


async def test_overlapping_queries_have_a_bounded_pool(db, tmp_path, monkeypatch):
    from daedalus.search.service import SearchBusy

    service = ConversationSearch(db, tmp_path)
    release = asyncio.Event()

    async def query(*args, **kwargs):
        await release.wait()
        return {}

    monkeypatch.setattr(service, '_query', query)
    first = asyncio.create_task(service.query('one'))
    second = asyncio.create_task(service.query('two'))
    await asyncio.sleep(0)
    with pytest.raises(SearchBusy):
        await service.query('three')
    release.set()
    await asyncio.gather(first, second)
    assert service.readers == 0


async def test_the_model_change_empties_the_old_vectors_and_queues_every_message_again(settings):
    """An installation indexed by the previous model, opened by this build: nothing of the old vectors
    survives to be compared with the new model's, every message is queued, and search is on."""
    import json

    from daedalus.stores.database import MIGRATIONS, Database

    before = next(i for i, m in enumerate(MIGRATIONS) if isinstance(m, str) and 'search_vectors_digest' in m)
    db = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
    await db.open()
    await add(db, 'one')
    await add(db, 'two', 'another message')
    await db.conn.executescript(f"""
        DROP TABLE search_vectors;
        DROP TABLE search_titles;
        CREATE TABLE search_vectors (seq INTEGER NOT NULL REFERENCES transcript(seq) ON DELETE CASCADE, block INTEGER NOT NULL,
            offset INTEGER NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(seq, block, offset));
        CREATE TABLE search_titles (session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE, title TEXT NOT NULL,
            model TEXT NOT NULL, dimension INTEGER NOT NULL, vector BLOB NOT NULL);
        INSERT INTO search_vectors SELECT seq, 0, 0, 'multilingual-e5-small', 384, zeroblob(1536) FROM transcript;
        INSERT INTO search_titles SELECT id, title, 'multilingual-e5-small', 384, zeroblob(1536) FROM sessions;
        DELETE FROM search_pending;
        INSERT OR REPLACE INTO kv(key, value) VALUES ('conversation_search', '{json.dumps({"mode": "off", "paused": True})}'),
            ('search_space', '["multilingual-e5-small", 384]');
        UPDATE schema_version SET version = {before};
    """)
    await db.close()
    db = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
    await db.open()
    try:
        assert (await db.fetchone('SELECT count(*) FROM search_vectors'))[0] == 0
        assert (await db.fetchone('SELECT count(*) FROM search_titles'))[0] == 0
        assert (await db.fetchone('SELECT count(*) FROM search_pending'))[0] == (await db.fetchone('SELECT count(*) FROM transcript'))[0] == 2
        columns = {r['name'] for r in await db.fetchall('PRAGMA table_info(search_vectors)')}
        assert {'id', 'digest', 'scale', 'vector'} <= columns and 'model' not in columns
        assert await db.kv_get('search_space') is None
        service = ConversationSearch(db, settings.state_dir)
        await service.load()
        assert service.settings == {'mode': 'local', 'paused': False}
    finally:
        await db.close()
