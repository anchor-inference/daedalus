"""Opt-in local embeddings, one inference at a time, yielding to agent work."""
from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from daedalus.search.store import Index, search
from daedalus.speech.embedding_catalog import MODEL, get, resolve
from daedalus.speech.models import Downloads
from daedalus.stores.database import Database


async def offload(function, *args, **kwargs):
    """Cancellation must drain native inference before its lock or database can be released."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


class Encoder:
    def __init__(self, directory: Path) -> None:
        import onnxruntime as ort  # Lazy: the optional speech extra is unnecessary for exact search.
        from tokenizers import Tokenizer  # Lazy: load the tokenizer only with a local model.

        self.tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=512)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / 'model.onnx'), sess_options=options, providers=['CPUExecutionProvider'])

    def encode(self, text: str, *, query: bool = False) -> list[float]:
        import numpy as np  # Lazy: lexical search works without the optional tensor runtime.
        import onnxruntime as ort  # Lazy: the optional speech extra is unnecessary for exact search.

        tokens = self.tokenizer.encode(('query: ' if query else 'passage: ') + text)
        ids = np.array([tokens.ids], dtype=np.int64)
        mask = np.array([tokens.attention_mask], dtype=np.int64)
        inputs = {'input_ids': ids, 'attention_mask': mask, 'token_type_ids': np.zeros_like(ids)}
        options = ort.RunOptions()
        timer = threading.Timer(0.8, lambda: setattr(options, 'terminate', True))
        timer.start()
        try:
            hidden = self.session.run(None, {i.name: inputs[i.name] for i in self.session.get_inputs()}, options)[0]
        finally:
            timer.cancel()
        pooled = (hidden * mask[:, :, None]).sum(axis=1) / mask.sum(axis=1)[:, None]
        value = pooled[0]
        return (value / np.linalg.norm(value)).tolist()


class SearchBusy(RuntimeError):
    """The bounded reader pool is occupied; retry instead of queueing more database scans."""


class ConversationSearch:
    def __init__(self, db: Database, state_dir: Path, *, manager: Any = None, tenant: str = 'daedalus') -> None:
        self.db = db
        self.index = Index(db)
        self.manager = manager
        self.tenant = tenant
        self.downloads = Downloads(state_dir / 'models' / 'embeddings', lookup=get, resolver=resolve)
        self.encoder: Encoder | None = None
        self.lock = asyncio.Lock()
        self.idle_work = asyncio.Lock()
        self.query_lock = asyncio.Lock()
        self.error = False
        self.readers = 0
        self.task: asyncio.Task[None] | None = None
        self.settings: dict[str, Any] = {'mode': 'off', 'paused': False}

    async def load(self) -> None:
        self.settings.update(await self.db.kv_get('conversation_search', {}))

    def busy(self) -> bool:
        return bool(self.manager and (self.manager.busy_sessions() or any(s.compacting for s in getattr(self.manager, "_states", {}).values())))

    def reason(self) -> str:
        if self.settings['mode'] != 'local':
            return 'off'
        if not self.downloads.is_installed(MODEL.id):
            return 'no_model'
        if not all(importlib.util.find_spec(name) for name in ('onnxruntime', 'tokenizers')):
            return 'no_runtime'
        if self.error:
            return 'error'
        return 'ready' if self.encoder else 'warming'

    async def status(self) -> dict[str, Any]:
        row = await self.db.fetchone('SELECT (SELECT count(*) FROM search_vectors) indexed_count, (SELECT count(*) FROM search_pending) pending')
        return {**self.settings, 'reason': self.reason(), 'busy': self.busy(), 'indexed': row['indexed_count'], 'pending': row['pending'],
                'model': MODEL.id, 'label': MODEL.label, 'size_bytes': MODEL.size_bytes, 'dimension': MODEL.dimension,
                'licence': MODEL.licence, 'installed': self.downloads.is_installed(MODEL.id),
                'progress': asdict(p) if (p := self.downloads.progress().get(MODEL.id)) else None}

    async def configure(self, mode: str, paused: bool) -> dict[str, Any]:
        self.settings = {'mode': mode, 'paused': paused}
        self.error = False
        await self.db.kv_set('conversation_search', self.settings)
        if mode == 'off':
            async with self.lock:
                self.encoder = None
        return await self.status()

    async def step(self, *, passages: int = 8, seconds: float = 0.5) -> int:
        """At most eight passages and half a second per pass; every chunk commits its own cursor."""
        if self.busy() or self.reason() not in ('ready', 'warming') or self.query_lock.locked():
            return 0
        guard = self.manager.idle_work if self.manager else self.idle_work
        done = 0
        deadline = time.monotonic() + seconds
        for _ in range(min(passages, 8)):
            async with guard, self.lock:
                if self.busy() or self.settings['mode'] != 'local' or self.query_lock.locked():
                    break
                if self.encoder is None:
                    self.encoder = await offload(Encoder, self.downloads.directory(MODEL.id))
                if self.settings['paused']:
                    break
                await self.index.prepare(MODEL.space, MODEL.dimension)
                title = await self.index.title(self.tenant)
                if title:
                    vector = await offload(self.encoder.encode, title['title'])
                    await self.index.save_title(title['id'], title['title'], vector, MODEL.space, MODEL.dimension)
                    done += 1
                else:
                    passage = await self.index.next()
                    if passage is None:
                        break
                    vector = await offload(self.encoder.encode, passage.text) if passage.text else None
                    await self.index.save(passage, vector, MODEL.space, MODEL.dimension)
                    done += 1
            if time.monotonic() >= deadline:
                break
        return done

    async def run(self) -> None:
        await self.load()
        while True:
            try:
                count = await self.step()
            except Exception:
                self.error = True
                count = 0
            await asyncio.sleep(0.1 if count else 2)

    async def query(self, query: str, *, project: str = '', limit: int = 30, session: str = '') -> dict[str, Any]:
        if self.readers >= 2:
            raise SearchBusy("conversation search is busy; try again")
        self.readers += 1
        try:
            return await self._query(query, project=project, limit=limit, session=session)
        finally:
            self.readers -= 1

    async def _query(self, query: str, *, project: str = '', limit: int = 30, session: str = '') -> dict[str, Any]:
        # No unbounded queue of ONNX calls when someone types quickly. The UI debounces and cancels;
        # concurrent callers get lexical results with the reason, rather than waiting behind work.
        reason = self.reason()
        vector = None
        if self.query_lock.locked():
            reason = 'busy'
        else:
            async with self.query_lock:
                if reason == 'ready' and self.encoder is not None:
                    try:
                        async with self.lock:
                            vector = await offload(self.encoder.encode, query[:500], query=True)
                    except Exception:
                        reason = 'error'
                        self.error = True
                result = await offload(search, self.db.path, self.tenant, query, vector=vector,
                                                 model=MODEL.space, dimension=MODEL.dimension, project=project, limit=limit, session=session)
                return {**result, 'semantic': reason == 'ready' and vector is not None, 'reason': reason}
        result = await offload(search, self.db.path, self.tenant, query, project=project, limit=limit, session=session)
        return {**result, 'semantic': False, 'reason': reason}

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.downloads.close()
