"""Getting a chosen model onto the disk, and off it again.

A model is half a gigabyte over a link that may not survive it, so a download here is resumable: the
archive lands in a part file, an interrupted one is continued with a range request rather than begun
again, and the operator can stop one mid-flight. What arrives is checked before it is believed — the
published size always, the sha256 for all but the two archives that have none, the pinned file list
for those two, and in every case the unpacked directory against what its kind actually needs to load.
Only then does it become installed; a download that fails any of those leaves nothing behind but a
line in the log.

Progress is a stream of events rather than a number to poll, because the Mini App shows a bar and the
bar should move. :meth:`Downloads.watch` hands out a queue that the API turns into SSE.

Nothing here imports the engine's wheel: downloading a model must work on an installation that has not
installed the extra yet, since that is exactly the installation that is about to.

Two catalogs use this, and it belongs to neither. Recognition models and synthesis voices are fetched,
resumed, verified, unpacked and deleted identically — the only differences are which catalog an id is
looked up in and which loader is asked whether the unpacked directory makes sense — so both are
constructor arguments and the rest of the file never asks which kind it is holding. The two live in
separate trees (``models/stt/`` and ``models/tts/``), so an id that exists in both catalogs would still
be two directories and two manifests.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import tarfile
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from daedalus.speech.catalog import get as get_speech_model

logger = logging.getLogger(__name__)

CHUNK = 1 << 20
"""A megabyte at a time: big enough that the progress callback is not the expensive part."""

PROGRESS_INTERVAL = 0.4
"""How often a running download reports itself. A bar does not need more, and a queue should not get it."""

MAX_CONCURRENT = 1
"""Models are fetched one at a time. Two in parallel share a link that neither finishes faster on, and
the picker's twelve Download buttons make an accidental click-through of several of them easy."""

DISK_HEADROOM = 256 << 20
"""Left free after the archive and the unpacked tree, which coexist until the archive is deleted. This
runs on small machines whose state volume also holds the database and the snapshots."""

ALLOWED_HOSTS = ("github.com", "githubusercontent.com")
"""Where a download from the zoo may end up after redirects. A GitHub release redirects once to a
signed asset host on ``githubusercontent.com``; anything else means the chain was steered, and for an
archive with no digest the chain is a large part of what says these are the right bytes."""

MANIFEST = "installed.json"


class DownloadError(RuntimeError):
    """A download that will not become a working model, with the reason in the message."""


@dataclass
class Progress:
    """One download, as the picker draws it."""

    id: str
    state: str
    """``queued``, ``downloading``, ``verifying``, ``unpacking``, ``installed``, ``failed``,
    ``cancelled`` or ``deleted`` — the last published by :meth:`Downloads.delete` so the picker can
    drop the card's progress without re-fetching the whole view."""
    done_bytes: int = 0
    total_bytes: int = 0
    error: str = ""

    @property
    def fraction(self) -> float:
        return min(1.0, self.done_bytes / self.total_bytes) if self.total_bytes else 0.0


@dataclass
class Installed:
    """What the manifest remembers about a model that is on the disk."""

    id: str
    archive: str
    sha256: str
    disk_bytes: int
    installed_at: float = field(default_factory=time.time)


class Downloads:
    """The models directory: what is in it, what is arriving, and what to throw away.

    One per kind per process, held by the application. It owns the directory layout — ``<id>/`` for an
    installed model, ``.part/`` for archives in flight — and nothing else writes there.

    ``lookup`` turns an id into a catalog entry and ``resolver`` decides whether an unpacked directory
    is loadable. Both default to the recognition catalog, which is what every existing caller wants;
    passing the synthesis pair is the whole of what makes this manager serve voices too.
    """

    def __init__(
        self,
        root: Path,
        *,
        lookup: Callable[[str], Any] = get_speech_model,
        resolver: Callable[[Path, Any], None] | None = None,
        installed: Callable[[str], None] | None = None,
    ) -> None:
        self.root = root
        self.parts = root / ".part"
        self.lookup = lookup
        self.resolver = resolver
        self.installed = installed
        """Called with a model id the moment its files are on disk and in the manifest. What holds a
        loaded copy of that model needs to know: a load that failed on a half-written archive stays
        failed, and re-downloading the archive is exactly the operator saying "try again"."""
        self._running: dict[str, asyncio.Task[None]] = {}
        self._progress: dict[str, Progress] = {}
        self._watchers: list[asyncio.Queue[Progress]] = []

    # -- what is on the disk ----------------------------------------------------------------

    def directory(self, model_id: str) -> Path:
        return self.root / model_id

    def manifest(self) -> dict[str, Installed]:
        """Everything installed, read from disk each time: the file is small and another process may write it."""
        path = self.root / MANIFEST
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        out: dict[str, Installed] = {}
        for entry in raw.get("models", []):
            try:
                record = Installed(**entry)
            except TypeError:
                continue
            if self.directory(record.id).is_dir():
                out[record.id] = record
        return out

    def _write_manifest(self, records: dict[str, Installed]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / MANIFEST
        body = {"models": [asdict(r) for r in sorted(records.values(), key=lambda r: r.id)]}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, indent=1))
        tmp.replace(path)

    def is_installed(self, model_id: str) -> bool:
        return model_id in self.manifest()

    def disk_usage(self) -> int:
        """Bytes the models directory occupies, part files included — what the operator would get back."""
        total = 0
        for path in self.root.rglob("*"):
            with contextlib.suppress(OSError):
                if path.is_file():
                    total += path.stat().st_size
        return total

    async def delete(self, model_id: str) -> bool:
        """Remove an installed model and everything it left behind. Answers whether there was anything.

        The id goes through the catalog first, so nothing that is not a model name can ever reach the
        ``rmtree`` below — the directory is built by joining, and a joined ``..`` climbs out of the
        models tree. Over HTTP the router does not currently let one through, but the router is not
        what this should be relying on.

        A download in flight is stopped and waited for before anything is removed. Otherwise the task
        carries on, writes its manifest record over ours and unpacks the model back into place: a
        deleted model that returns by itself, which is worse than one that would not delete.
        """
        model = self.lookup(model_id)
        self.cancel(model.id)
        task = self._running.get(model.id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        records = self.manifest()
        directory = self.directory(model.id)
        existed = directory.is_dir()
        shutil.rmtree(directory, ignore_errors=True)
        # The part file and any interrupted staging tree are counted by ``disk_usage`` and are not
        # reachable from the picker, so leaving them would show the operator bytes nothing can free.
        (self.parts / model.archive).unlink(missing_ok=True)
        for entry in getattr(model, "files", ()):
            (self.parts / entry.archive).unlink(missing_ok=True)
        shutil.rmtree(self.parts / f"{model.id}.staging", ignore_errors=True)
        if records.pop(model.id, None) is not None or existed:
            self._write_manifest(records)
            self._publish(Progress(id=model.id, state="deleted"))
            return True
        return False

    # -- progress ----------------------------------------------------------------------------

    def progress(self) -> dict[str, Progress]:
        """Every download this process knows about, running or finished."""
        return dict(self._progress)

    def _publish(self, update: Progress) -> None:
        self._progress[update.id] = update
        for queue in list(self._watchers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(update)

    @contextlib.asynccontextmanager
    async def watch(self) -> AsyncIterator[asyncio.Queue[Progress]]:
        """A queue of progress updates for as long as the caller holds it.

        Bounded, and a full queue drops rather than blocks: a client that stopped reading must not be
        able to stall a download.
        """
        queue: asyncio.Queue[Progress] = asyncio.Queue(maxsize=64)
        self._watchers.append(queue)
        try:
            yield queue
        finally:
            with contextlib.suppress(ValueError):
                self._watchers.remove(queue)

    # -- downloading -------------------------------------------------------------------------

    def start(self, model_id: str, *, client: httpx.AsyncClient | None = None, url: str = "") -> Progress:
        """Begin, or report the one already running. Returns at once; the work is a background task."""
        model = self.lookup(model_id)
        if model_id in self._running and not self._running[model_id].done():
            return self._progress.get(model_id, Progress(id=model_id, state="downloading"))
        running = [mid for mid, task in self._running.items() if not task.done()]
        if len(running) >= MAX_CONCURRENT:
            state = Progress(id=model_id, state="queued", total_bytes=model.size_bytes, error=f"{running[0]} is downloading")
            self._publish(state)
            return state
        if self.is_installed(model_id):
            state = Progress(id=model_id, state="installed", done_bytes=model.size_bytes, total_bytes=model.size_bytes)
            self._publish(state)
            return state
        state = Progress(id=model_id, state="downloading", total_bytes=model.size_bytes)
        self._publish(state)
        self._running[model_id] = asyncio.create_task(self._run(model, client, url or model.url))
        return state

    def cancel(self, model_id: str) -> bool:
        """Stop a download in flight. The part file stays, so starting again resumes where it stopped."""
        model_id = self.lookup(model_id).id
        task = self._running.get(model_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def close(self) -> None:
        """Stop everything in flight and wait for it, so shutdown does not leave tasks decoding."""
        for task in list(self._running.values()):
            task.cancel()
        for task in list(self._running.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._running.clear()

    async def _run(self, model: Any, client: httpx.AsyncClient | None, url: str) -> None:
        try:
            await asyncio.to_thread(self._check_room, model)
            if getattr(model, "files", ()):
                disk = await self._fetch_files(model, client)
                archive = None
            else:
                archive = await self._fetch(model, client, url)
            if archive is not None:
                self._publish(Progress(id=model.id, state="verifying", done_bytes=model.size_bytes, total_bytes=model.size_bytes))
                await asyncio.to_thread(verify, archive, model)
                self._publish(Progress(id=model.id, state="unpacking", done_bytes=model.size_bytes, total_bytes=model.size_bytes))
                disk = await asyncio.to_thread(self._unpack, archive, model)
            records = self.manifest()
            records[model.id] = Installed(id=model.id, archive=model.archive, sha256=model.sha256, disk_bytes=disk)
            self._write_manifest(records)
            if archive is not None:
                archive.unlink(missing_ok=True)
            self._publish(Progress(id=model.id, state="installed", done_bytes=model.size_bytes, total_bytes=model.size_bytes))
            logger.warning("local model %s installed (%d MB on disk)", model.id, disk >> 20)
            if self.installed is not None:
                try:
                    self.installed(model.id)
                except Exception:  # noqa: BLE001 - the model is installed either way; this is bookkeeping
                    logger.warning("the installed hook for %s failed", model.id, exc_info=True)
        except asyncio.CancelledError:
            self._publish(Progress(id=model.id, state="cancelled", total_bytes=model.size_bytes))
            raise
        except Exception as exc:  # noqa: BLE001 - every failure becomes one message for the operator
            if not getattr(model, "files", ()):
                # An archive unpacks in place and may leave half a tree. A model of single files is
                # assembled in staging and renamed only once complete, so its directory holds either
                # the last good install or files the operator seeded by hand: neither is ours to remove.
                shutil.rmtree(self.directory(model.id), ignore_errors=True)
            logger.warning("local model %s failed to install: %s", model.id, exc)
            self._publish(Progress(id=model.id, state="failed", total_bytes=model.size_bytes, error=str(exc)))
        finally:
            self._running.pop(model.id, None)

    async def _fetch_files(self, model: Any, client: httpx.AsyncClient | None) -> int:
        staging = self.parts / f"{model.id}.staging"
        staging.mkdir(parents=True, exist_ok=True)
        done = 0
        try:
            for entry in model.files:
                target = await asyncio.to_thread(self._seeded, model, entry)
                if target is None:
                    target = await self._fetch(entry, client, entry.url, offset=done, total=model.size_bytes)
                    self._publish(Progress(id=model.id, state="verifying", done_bytes=done + entry.size_bytes, total_bytes=model.size_bytes))
                    try:
                        await asyncio.to_thread(verify, target, entry)
                    except DownloadError:
                        # A part file of the full size that is not these bytes would otherwise be
                        # "resumed" as complete and fail the same way on every later attempt.
                        target.unlink(missing_ok=True)
                        raise
                await asyncio.to_thread(shutil.copyfile, target, staging / entry.archive)
                done += entry.size_bytes
            if self.resolver:
                await asyncio.to_thread(self.resolver, staging, model)
            directory = self.directory(model.id)
            shutil.rmtree(directory, ignore_errors=True)
            staging.rename(directory)
            for entry in model.files:
                (self.parts / entry.archive).unlink(missing_ok=True)
            return done
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _seeded(self, model: Any, entry: Any) -> Path | None:
        """A copy of ``entry`` someone placed in the model's directory by hand, when it is exactly the pinned
        file. Blocking; reads the whole file once.

        This is how a machine that cannot reach the download host, or a model whose host is not settled
        yet, gets installed: copy the file in, and the next start of the download takes it without a
        byte of traffic. A copy with the wrong size or digest is left alone and the file is downloaded.
        """
        seed = self.directory(model.id) / entry.archive
        if not entry.sha256 or not seed.is_file() or seed.stat().st_size != entry.size_bytes:
            return None
        if sha256_of(seed) != entry.sha256:
            logger.warning("%s in %s is not the pinned file; downloading it instead", entry.archive, seed.parent)
            return None
        return seed

    async def _fetch(self, model: Any, client: httpx.AsyncClient | None, url: str, *, offset: int = 0, total: int = 0) -> Path:
        """The archive on disk, continuing a part file where one is already there."""
        self.parts.mkdir(parents=True, exist_ok=True)
        target = self.parts / model.archive
        have = target.stat().st_size if target.exists() else 0
        if have > model.size_bytes:
            # A part file longer than the published size is not this archive; start it over rather
            # than resume into something that can never verify.
            target.unlink()
            have = 0
        if have == model.size_bytes:
            return target
        owns = client is None
        client = client or httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0), follow_redirects=True)
        headers = {"range": f"bytes={have}-"} if have else {}
        last = 0.0
        try:
            async with client.stream("GET", url, headers=headers) as response:
                landed = (response.url.host or "").lower()
                if not _host_allowed(str(url), landed):
                    raise DownloadError(f"the download was redirected to {landed}, which is not where it started")
                if have and response.status_code == 200:
                    # The host ignored the range and is sending the whole file: take it from the top.
                    have = 0
                    target.unlink(missing_ok=True)
                elif have and response.status_code != 206:
                    raise DownloadError(f"the download host answered HTTP {response.status_code} to a resume")
                elif response.status_code >= 400:
                    raise DownloadError(f"the download host answered HTTP {response.status_code}")
                with target.open("ab" if have else "wb") as fh:
                    async for chunk in response.aiter_bytes(CHUNK):
                        fh.write(chunk)
                        have += len(chunk)
                        if have > model.size_bytes:
                            raise DownloadError("the download exceeds the catalog size")
                        now = time.monotonic()
                        if now - last >= PROGRESS_INTERVAL:
                            last = now
                            self._publish(Progress(id=model.id, state="downloading", done_bytes=offset + have, total_bytes=total or model.size_bytes))
        except httpx.HTTPError as exc:
            raise DownloadError(f"the download did not finish: {type(exc).__name__}") from exc
        finally:
            if owns:
                await client.aclose()
        return target

    def _check_room(self, model: Any) -> None:
        """Refuse before the first byte where the disk cannot hold the result. Blocking; cheap."""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            free = shutil.disk_usage(self.root).free
        except OSError:  # pragma: no cover - a disk that cannot be measured is not a reason to refuse
            return
        needed = model.size_bytes + model.unpacked_bytes + DISK_HEADROOM
        if free < needed:
            raise DownloadError(
                f"{model.label} needs {needed >> 20} MB free while the model directory is on a disk with "
                f"{free >> 20} MB — the archive and the unpacked model are both on it until the last step"
            )

    def _unpack(self, archive: Path, model: Any) -> int:
        """Unpack into place and answer with the bytes it took. Blocking; runs on a worker thread.

        The archive holds one top-level directory whose name is the zoo's, not ours, so the contents
        are lifted out of it into ``<id>/``: the catalog id is what the config stores and what the
        engine looks for, and it must not depend on how a release happened to be named.
        """
        directory = self.directory(model.id)
        shutil.rmtree(directory, ignore_errors=True)
        staging = self.parts / f"{model.id}.staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            with tarfile.open(archive, "r:bz2") as tar:
                members = [m for m in tar.getmembers() if _safe_member(m.name)]
                if not members:
                    raise DownloadError("the archive is empty or its paths are not safe to unpack")
                tar.extractall(staging, members=members, filter="data")
            roots = [p for p in staging.iterdir() if p.is_dir()]
            source = roots[0] if len(roots) == 1 and not any(p.is_file() for p in staging.iterdir()) else staging
            directory.parent.mkdir(parents=True, exist_ok=True)
            source.rename(directory)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        check_contents(directory, model)
        (self.resolver or _check_loadable)(directory, model)
        return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


def _host_allowed(start: str, landed: str) -> bool:
    """Whether a redirect chain that began at ``start`` may end at ``landed``.

    A download from the zoo may cross only onto GitHub's own signed asset host — that is the one hop a
    release asset makes, and anything further is a chain somebody else is steering. A download from
    anywhere else (a mirror, a test) may not change host at all. Nothing here trusts the redirect to
    be harmless because the bytes are checked afterwards: for the two archives with no published
    digest, where the bytes came from is a real part of the answer.
    """
    begun = (httpx.URL(start).host or "").lower()
    if any(begun == allowed or begun.endswith("." + allowed) for allowed in ALLOWED_HOSTS):
        return any(landed == allowed or landed.endswith("." + allowed) for allowed in ALLOWED_HOSTS)
    if begun == "huggingface.co":
        return landed == begun or landed.endswith(".hf.co") or landed.endswith(".huggingface.co")
    return landed == begun


def _safe_member(name: str) -> bool:
    """Whether a tar entry may be written: no absolute paths and nothing climbing out of the directory."""
    if name.startswith("/") or name.startswith("\\"):
        return False
    return ".." not in Path(name).parts


def sha256_of(path: Path) -> str:
    """The file's digest. Blocking, and worth a worker thread for anything this size."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def verify(archive: Path, model: Any) -> None:
    """Refuse an archive that is not what the catalog describes.

    Size is checked always, and the digest wherever there is one — which, since GitHub began reporting
    a ``digest`` per release asset, is every entry but two. Those two are checked by their size and by
    the pinned file list :func:`check_contents` compares after unpacking. That is weaker than a hash
    and the picker says so on the card; it is not nothing, because a substitution then has to match a
    byte count and a file list at once rather than only be padded to a length.
    """
    size = archive.stat().st_size
    if size != model.size_bytes:
        raise DownloadError(f"the download is {size} bytes where the catalog says {model.size_bytes}")
    if model.sha256:
        actual = sha256_of(archive)
        if actual != model.sha256:
            raise DownloadError(f"the download's checksum is {actual[:16]}…, not the published one")


def check_contents(directory: Path, model: Any) -> None:
    """The unpacked tree holds exactly the files the catalog pinned, for an archive with no digest.

    Nothing to do where ``contents`` is empty: there is a digest in that case and it is a stronger
    statement than this one. The comparison is exact in both directions — a missing file is a short
    download, an extra one is not the archive the catalog was written against.
    """
    if not model.contents:
        return
    found = sorted(str(p.relative_to(directory)).replace("\\", "/") for p in directory.rglob("*") if p.is_file())
    expected = sorted(model.contents)
    if found != expected:
        missing = [n for n in expected if n not in found]
        extra = [n for n in found if n not in expected]
        detail = f"missing {', '.join(missing[:3])}" if missing else f"unexpected {', '.join(extra[:3])}"
        raise DownloadError(f"the archive does not hold the files the catalog pinned for {model.id}: {detail}")


def _check_loadable(directory: Path, model: Any) -> None:
    """The unpacked directory holds what its kind needs. Imported late: this must work without the extra."""
    from daedalus.speech.engine import (  # Lazy: downloading must work before the engine's wheel is installed
        SpeechError,
        resolve,
    )

    try:
        resolve(directory, model.kind)
    except SpeechError as exc:
        raise DownloadError(str(exc)) from exc


def view(
    downloads: Downloads,
    *,
    selected: str = "",
    entries: Sequence[Any] | None = None,
    to_json: Callable[[Any], dict[str, Any]] | None = None,
    all_languages: Callable[[], list[str]] | None = None,
) -> dict[str, Any]:
    """The whole picker in one object: the catalog, what is installed, what is arriving, what it costs.

    The three catalog arguments default to the recognition one. The synthesis picker passes its own,
    which is why there is one view function and not two nearly identical ones drifting apart.
    """
    from daedalus.speech.catalog import (  # Lazy: only this view joins the catalog to the manager
        MODELS,
        as_json,
        languages,
    )

    models = MODELS if entries is None else entries
    as_json = as_json if to_json is None else to_json
    languages = languages if all_languages is None else all_languages
    records = downloads.manifest()
    running = downloads.progress()
    cards: list[dict[str, Any]] = []
    for model in models:
        entry = as_json(model)
        record = records.get(model.id)
        progress = running.get(model.id)
        entry["installed"] = record is not None
        entry["installed_bytes"] = record.disk_bytes if record else 0
        entry["selected"] = model.id == selected
        if progress is not None and progress.state in ("downloading", "verifying", "unpacking", "failed"):
            entry["progress"] = {"state": progress.state, "fraction": progress.fraction, "error": progress.error}
        cards.append(entry)
    return {
        "models": cards,
        "languages": languages(),
        "selected": selected,
        "disk_bytes": downloads.disk_usage(),
        "root": str(downloads.root),
    }


__all__ = [
    "ALLOWED_HOSTS",
    "MANIFEST",
    "MAX_CONCURRENT",
    "DownloadError",
    "Downloads",
    "Installed",
    "Progress",
    "check_contents",
    "sha256_of",
    "verify",
    "view",
]
