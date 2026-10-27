"""Local embeddings for conversation search, on by default, indexed in the background on a low-priority
thread that yields to agent work."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from daedalus.search.chats import Reach
from daedalus.search.store import Index, Passage, pack, search
from daedalus.speech.embedding_catalog import MODEL, EmbeddingModel, get, resolve
from daedalus.speech.models import Downloads
from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

BATCH = 32
"""Queued rows read, encoded and committed together in one pass of the indexer."""
BATCH_SECONDS = 1.0
"""Encoding time one pass may take. An agent that starts meanwhile waits at most this long for the
pass to finish (the pass holds the manager's idle-work lock), and a pass that runs out of time keeps
what it encoded and leaves the rest queued."""
DOWNLOAD_RETRY_SECONDS = 600
"""How long after a failed download the indexer asks for the model again on its own. Each attempt
first looks for a copy placed in the model's directory, so a seeded file is picked up within this."""
FAILURES_TO_STOP = 3
QUERY_RUN_SECONDS = 0.8
PASSAGE_RUN_SECONDS = 10.0
"""A query is cut off quickly so typing never waits on a stuck inference. A passage is not: on a
loaded machine one may take a second, and a cut-off passage would stop the index (see ``run``)."""


async def offload(function, *args, executor: ThreadPoolExecutor | None = None, **kwargs):
    """Cancellation must drain native inference before its lock or database can be released."""
    if executor is None:
        task = asyncio.ensure_future(asyncio.to_thread(function, *args, **kwargs))
    else:
        task = asyncio.ensure_future(asyncio.get_running_loop().run_in_executor(executor, lambda: function(*args, **kwargs)))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


def lower_priority() -> None:
    """Make the calling thread the last to get a CPU. Linux schedules threads individually and accepts a
    thread id here; elsewhere the call would renice the whole process, the bot included, so it is not made.
    One-thread inference runs on the calling thread, so this covers the model's work as well."""
    if sys.platform.startswith('linux'):
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
        except OSError:
            pass


class Encoder:
    def __init__(self, directory: Path, model: EmbeddingModel = MODEL) -> None:
        import onnxruntime as ort  # Lazy: the optional speech extra is unnecessary for exact search.
        from tokenizers import Tokenizer  # Lazy: load the tokenizer only with a local model.

        self.model = model
        self.tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
        self.tokenizer.no_padding()
        self.tokenizer.enable_truncation(max_length=model.max_tokens)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / 'model.onnx'), sess_options=options, providers=['CPUExecutionProvider'])

    def encode(self, text: str, *, query: bool = False) -> list[float]:
        """One text at a time: on a CPU, padded batches of these lengths ran 15–25% slower than single ones."""
        import numpy as np  # Lazy: lexical search works without the optional tensor runtime.
        import onnxruntime as ort  # Lazy: the optional speech extra is unnecessary for exact search.

        tokens = self.tokenizer.encode((self.model.query_prefix if query else self.model.passage_prefix) + text)
        ids = np.array([tokens.ids], dtype=np.int64)
        mask = np.array([tokens.attention_mask], dtype=np.int64)
        inputs = {'input_ids': ids, 'attention_mask': mask, 'token_type_ids': np.zeros_like(ids)}
        options = ort.RunOptions()
        timer = threading.Timer(QUERY_RUN_SECONDS if query else PASSAGE_RUN_SECONDS, lambda: setattr(options, 'terminate', True))
        timer.start()
        try:
            hidden = self.session.run(None, {i.name: inputs[i.name] for i in self.session.get_inputs()}, options)[0]
        finally:
            timer.cancel()
        pooled = (hidden * mask[:, :, None]).sum(axis=1) / mask.sum(axis=1)[:, None]
        value = pooled[0]
        return (value / np.linalg.norm(value)).tolist()


def encode_until(encoder: Any, texts: list[str], seconds: float) -> list[list[float]]:
    """As many of ``texts`` as fit in ``seconds``, in order, and at least one."""
    deadline = time.monotonic() + seconds
    out: list[list[float]] = []
    for text in texts:
        out.append(encoder.encode(text))
        if time.monotonic() >= deadline:
            break
    return out


def runtime_installed() -> bool:
    return all(importlib.util.find_spec(name) for name in ('onnxruntime', 'tokenizers'))


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
        self.settings: dict[str, Any] = {'mode': 'local', 'paused': False}
        self.asked_for_model = 0.0
        self.background: ThreadPoolExecutor | None = None

    async def load(self) -> None:
        self.settings.update(await self.db.kv_get('conversation_search', {}))

    def busy(self) -> bool:
        return bool(self.manager and (self.manager.busy_sessions() or any(s.compacting for s in getattr(self.manager, "_states", {}).values())))

    def reason(self) -> str:
        if self.settings['mode'] != 'local':
            return 'off'
        if not self.downloads.is_installed(MODEL.id):
            return 'no_model'
        if not runtime_installed():
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

    def fetch_model(self) -> None:
        """Start the model's download when the search is on and the model is missing; after a failure, only
        once :data:`DOWNLOAD_RETRY_SECONDS` have passed. The operator's own button starts one at any time."""
        progress = self.downloads.progress().get(MODEL.id)
        if progress is not None and progress.state in ('queued', 'downloading', 'verifying', 'unpacking'):
            return
        if self.asked_for_model and time.monotonic() - self.asked_for_model < DOWNLOAD_RETRY_SECONDS:
            return
        self.asked_for_model = time.monotonic()
        self.downloads.start(MODEL.id)

    def _executor(self) -> ThreadPoolExecutor:
        if self.background is None:
            self.background = ThreadPoolExecutor(max_workers=1, thread_name_prefix='conversation-index', initializer=lower_priority)
        return self.background

    async def step(self, *, passages: int = BATCH, seconds: float = BATCH_SECONDS) -> int:
        """One pass: up to ``passages`` queued rows read, encoded within ``seconds`` and committed together.
        Answers how many rows moved on. Nothing runs while an agent works or a query is encoding."""
        reason = self.reason()
        if reason == 'no_model':
            # An installation without the runtime could not run the model it fetched.
            if runtime_installed():
                self.fetch_model()
            return 0
        if self.busy() or reason not in ('ready', 'warming') or self.query_lock.locked():
            return 0
        guard = self.manager.idle_work if self.manager else self.idle_work
        async with guard, self.lock:
            if self.busy() or self.settings['mode'] != 'local' or self.query_lock.locked():
                return 0
            if self.encoder is None:
                self.encoder = await offload(Encoder, self.downloads.directory(MODEL.id))
            if self.settings['paused']:
                return 0
            await self.index.prepare(MODEL.space)
            encoder = self.encoder
            titles = await self.index.titles(self.tenant, passages)
            if titles:
                vectors = await offload(encode_until, encoder, [t['title'] for t in titles], seconds, executor=self._executor())
                await self.index.save_titles([(t['id'], t['title'], pack(v)) for t, v in zip(titles, vectors, strict=False)])
                return len(vectors)
            batch = await self.index.batch(passages)
            if not batch:
                return 0
            vectors = await self.index.known([p.text for p in batch if not p.settled])
            fresh = list(dict.fromkeys(p.text for p in batch if not p.settled and p.text not in vectors))
            if fresh:
                encoded = await offload(encode_until, encoder, fresh, seconds, executor=self._executor())
                vectors.update((text, pack(v)) for text, v in zip(fresh, encoded, strict=False))
            # A passage whose text did not fit this pass stays queued exactly where it was.
            ready: list[tuple[Passage, tuple[float, bytes] | None]] = [
                (p, vectors.get(p.text)) for p in batch if p.settled or p.text in vectors]
            return await self.index.save(ready)

    async def run(self) -> None:
        await self.load()
        failures = 0
        while True:
            try:
                count = await self.step()
                failures = 0
            except Exception:
                # One failed pass is usually the database busy with a long write; a model that cannot run
                # fails every pass. Only the second kind stops the index, until the operator retries.
                failures += 1
                logger.warning("conversation index: pass failed (%d in a row)", failures, exc_info=True)
                if failures >= FAILURES_TO_STOP:
                    self.error = True
                await asyncio.sleep(30)
                continue
            await asyncio.sleep(0.05 if count else 2)

    async def query(self, query: str, *, project: str = '', limit: int = 30, session: str = '',
                    reach: Reach | None = None) -> dict[str, Any]:
        """Ranked conversations for ``query``: ``hits`` (session, seq, snippet, score), ``partial``, and how it
        was answered — ``semantic`` true when meaning was matched beside the words, ``reason`` the state of
        the semantic half (``ready``, ``off``, ``no_model``, ``warming``, ``busy``, ...). The words half
        always runs, so a caller never needs to know which model, if any, stands behind the other."""
        if self.readers >= 2:
            raise SearchBusy("conversation search is busy; try again")
        self.readers += 1
        try:
            return await self._query(query, project=project, limit=limit, session=session, reach=reach)
        finally:
            self.readers -= 1

    async def _query(self, query: str, *, project: str = '', limit: int = 30, session: str = '',
                     reach: Reach | None = None) -> dict[str, Any]:
        # No unbounded queue of ONNX calls when someone types quickly. The UI debounces and cancels;
        # concurrent callers get lexical results with the reason, rather than waiting behind work.
        reason = self.reason()
        vector = None
        if self.query_lock.locked():
            reason = 'busy'
        else:
            async with self.query_lock:
                encoder = self.encoder
                if reason == 'ready' and encoder is not None:
                    # Not under the indexer's lock: a session runs concurrently, and waiting for a pass
                    # of the backfill would put up to a second in front of every keystroke.
                    try:
                        vector = await offload(encoder.encode, query[:500], query=True)
                    except Exception:
                        # This answer goes without meaning; the index decides whether the model is broken.
                        logger.warning("conversation search: encoding a query failed", exc_info=True)
                        reason = 'error'
                result = await offload(search, self.db.path, self.tenant, query, vector=vector, space=MODEL.space,
                                       min_similarity=MODEL.min_similarity, project=project, limit=limit, session=session, reach=reach)
                return {**result, 'semantic': reason == 'ready' and vector is not None, 'reason': reason}
        result = await offload(search, self.db.path, self.tenant, query, project=project, limit=limit, session=session, reach=reach)
        return {**result, 'semantic': False, 'reason': reason}

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.downloads.close()
        if self.background is not None:
            self.background.shutdown(wait=True)
