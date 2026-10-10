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
from daedalus.search.store import PASSAGE_CHARS, Index, collapse, fusion, pack, search
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
    answer = await service.query('bicycle')
    assert answer['semantic'] is False and answer['reason'] == 'off'
    assert len(answer['hits']) == 1
    await service.configure('local', False)
    assert (await service.query('bicycle'))['reason'] == 'no_model'


async def test_chunks_resume_after_restart_and_replacement_invalidates_vectors(db):
    store, message = await add(db, text='river ' * 800)
    index = Index(db)
    await index.prepare('model-a', 2)
    first = await index.next()
    assert len(first.text) == PASSAGE_CHARS
    await index.save(first, [1, 0], 'model-a', 2)
    resumed = Index(db)
    second = await resumed.next()
    assert second.offset > first.offset and len(second.text) <= PASSAGE_CHARS
    await resumed.save(second, [1, 0], 'model-a', 2)
    assert (await db.fetchone('SELECT count(*) FROM search_vectors'))[0] == 2
    updated = message.model_copy(update={'content_blocks': [TextBlock(text='Now a mountain trail')]})
    await store.replace_transcript_message('one', store.transcript_key(message), updated)
    assert (await db.fetchone('SELECT count(*) FROM search_vectors'))[0] == 0
    assert not await index.save(first, [1, 0], 'model-a', 2)
    current = await index.next()
    assert current.offset == 0 and 'mountain' in current.text
    await index.save(current, [0, 1], 'model-a', 2)
    await index.prepare('model-b', 3)
    assert (await db.fetchone('SELECT count(*) FROM search_vectors'))[0] == 0
    assert (await index.next()).offset == 0


async def test_new_message_becomes_semantically_findable_and_deleted_vectors_go(db):
    store, _ = await add(db)
    index = Index(db)
    await index.prepare('test', 2)
    passage = await index.next()
    await index.save(passage, [1, 0], 'test', 2)
    # Different words; only the embedding half can retrieve this passage.
    found = complete_search(db.path, 'daedalus', 'cycling by water', vector=[1, 0], model='test', dimension=2)
    assert found['hits'][0]['session_id'] == 'one'
    assert 'bicycle' in found['hits'][0]['snippet']
    await store.append_transcript('one', [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text='A mountain trail')])])
    for _ in range(8):
        passage = await index.next()
        if passage is None:
            break
        await index.save(passage, [0, 1] if passage.text else None, 'test', 2)
    found = complete_search(db.path, 'daedalus', 'hill walking', vector=[0, 1], model='test', dimension=2)
    assert 'mountain' in found['hits'][0]['snippet']
    await db.execute("DELETE FROM transcript WHERE session_id='one'")
    assert (await db.fetchone('SELECT count(*) FROM search_vectors'))[0] == 0
    assert (await db.fetchone('SELECT count(*) FROM search_pending'))[0] == 0


async def test_backfill_limits_pause_busy_and_resume(db, tmp_path, monkeypatch):
    await add(db, text='long passage ' * 3000)
    service = ConversationSearch(db, tmp_path)
    service.settings = {'mode': 'local', 'paused': False}
    service.encoder = SimpleNamespace(encode=lambda *a, **k: [1.0] * MODEL.dimension)
    monkeypatch.setattr(service, 'reason', lambda: 'ready')
    calls = await service.step(passages=100, seconds=10)
    assert calls == 8
    pending = dict(await db.fetchone('SELECT * FROM search_pending'))
    service.settings['paused'] = True
    assert await service.step() == 0
    service.settings['paused'] = False
    service.manager = SimpleNamespace(busy_sessions=lambda: {'active'}, idle_work=asyncio.Lock())
    assert await service.step() == 0
    assert dict(await db.fetchone('SELECT * FROM search_pending')) == pending
    service.manager = None
    assert await service.step(passages=1) == 1
    assert dict(await db.fetchone('SELECT * FROM search_pending')) != pending


async def test_scan_budget_is_honest_and_model_spaces_do_not_mix(db):
    await add(db)
    index = Index(db)
    await index.prepare('one', 2)
    passage = await index.next()
    await index.save(passage, [1, 0], 'one', 2)
    assert search(db.path, 'daedalus', 'paraphrase', vector=[1, 0], model='two', dimension=2)['hits'] == []
    result = search(db.path, 'daedalus', 'paraphrase', vector=[1, 0], model='one', dimension=2, max_vectors=0)
    assert result['partial'] is True
    with pytest.raises(ValueError):
        pack([float('nan')], 1)


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
    await index.prepare('test', 2)
    await index.save(await index.next(), [1, 0], 'test', 2)
    await index.save_title('one', 'one', [1, 0], 'test', 2)
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
    service.settings = {'mode': 'local', 'paused': False}
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
