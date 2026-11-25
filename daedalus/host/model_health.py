"""Which configured models are broken or slow, learnt from the calls the host already makes.

A route can be healthy while a model on it is dead: the Codex route refuses a model the ChatGPT plan
does not carry with a 400, OpenRouter answers a free slug that was withdrawn with a 404, and until now
the operator learnt it from a helper that silently did nothing or a translation that never came. Two
marks are kept, and nothing else:

* **broken** — a real call was refused in words that say the model itself is unavailable
  (:func:`model_refusal`). The upstream's own message is kept with it. The next answer from the same
  provider and model clears it, so a model that comes back is not shunned for ever.
* **slow** — the median time to the first byte over the most recent real calls is past
  :data:`SLOW_MEDIAN_MS`. Read from ``usage_events`` when asked, never stored, because the calls
  themselves are the record.

A check of every preset (:meth:`ModelHealth.check`) runs only when the operator presses the button:
each probe is a request on someone's plan, and a timer that spent allowance unasked was rejected.

A helper whose chosen preset is marked broken is given another one (:meth:`ModelHealth.usable`) and the
log says so; the setting itself is left as the operator wrote it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from daedalus.config import RuntimeConfig
    from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

KV_KEY = "model_health"

SLOW_MEDIAN_MS = 8_000
"""A healthy route answers a helper in one to six seconds; a median past eight is a route that queues.
Measured on OpenCode Go in November 2026: twelve identical translations took 2.5 to 84 s."""

SLOW_MIN_CALLS = 5
"""Fewer measured calls than this say nothing: one slow answer is luck, not a route."""

SLOW_SAMPLE = 20
"""The most recent calls the median is taken over, so a route that recovered stops being slow soon."""

SLOW_WINDOW_HOURS = 6

SHORT_ANSWER_TOKENS = 300
"""A call sent without streaming has no first byte of its own; when its answer was this short, its
whole duration is the wait, which is what a slow route makes long. A long answer's duration is the
writing, and is left out."""

CHECK_PROMPT = "Answer with the single word OK."
CHECK_PURPOSE = "model_check"
CHECK_SECONDS = 30.0
CHECK_PARALLEL = 4
"""Probes in flight at once: enough that twenty presets take a minute, few enough that one endpoint is
not hit with all of its presets at the same instant."""

_DEAD = re.compile(
    "|".join((
        # The model named before the verdict: "The 'gpt-6-sol' model is not supported when using Codex…".
        # Not "image input is not supported by this model", which refuses the request, not the model.
        r"\bmodel\b[^.\n]{0,80}?\b(?:is not supported|not supported|does not exist|not found|is not available|unavailable|deprecated|decommissioned|retired)",
        r"\b(?:unknown|invalid|unsupported) model\b",
        r"model_not_found|model_not_supported",
        r"unavailable for free",  # OpenRouter's withdrawn ":free" slug, 404
        r"no endpoints found",  # OpenRouter's model with nobody serving it
    )),
    re.IGNORECASE,
)


def _message(text: str) -> str:
    """The upstream's own sentence out of an error body, else the body's start."""
    try:
        data = json.loads(text)
    except ValueError:
        return " ".join(text.split())[:300]
    error = data.get("error", data) if isinstance(data, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"].strip()[:300]
    if isinstance(error, str):
        return error.strip()[:300]
    return " ".join(text.split())[:300]


def model_refusal(status: int, text: str) -> str | None:
    """The upstream's message when a refusal says the model itself is unavailable, else ``None``.

    Only 400, 404 and 410 qualify, and only in words about the model: a 400 for a malformed request, a
    rate limit or an empty account is a different failure with its own handling."""
    if status not in (400, 404, 410):
        return None
    if not _DEAD.search(text):
        return None
    return _message(text) or f"HTTP {status}"


_DATE_SUFFIX = re.compile(r"(?:[-_@](?:\d{4}-\d{2}-\d{2}|\d{8}|\d{6}|\d{4}))+$")
_VERSION = re.compile(r"^v?\d+(?:\.\d+)*$")


def model_name(model: str) -> tuple[frozenset[str], str]:
    """A model id as ``(its words, its version)``, for telling the same model on two providers.

    ``deepseek-v4.1-flash``, ``deepseek/deepseek-v4.1-flash-1017`` and ``deepseek-flash`` all read as
    ``({deepseek, flash}, …)``: the vendor prefix, a ``:free`` or ``:beta`` tag and a dated build suffix go,
    and the numbers left are the version, joined (``claude-haiku-5-5`` and ``claude-haiku-5.5`` agree)."""
    name = model.strip().lower().rsplit("/", 1)[-1].split(":", 1)[0]
    name = _DATE_SUFFIX.sub("", name)
    words: set[str] = set()
    version: list[str] = []
    for part in re.split(r"[-_\s]+", name):
        if not part:
            continue
        if _VERSION.match(part):
            version.append(part.lstrip("v"))
        elif part != "latest":
            words.add(part)
    return frozenset(words), ".".join(version)


def same_model(a: str, b: str) -> bool:
    """Whether two ids name the same model. A missing version matches any (a provider's ``deepseek-flash``
    is its current flash); two versions that differ do not (``gpt-6`` is not ``gpt-6.1``)."""
    words_a, version_a = model_name(a)
    words_b, version_b = model_name(b)
    if not words_a or words_a != words_b:
        return False
    return not version_a or not version_b or version_a == version_b


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ModelHealth:
    """The marks of one installation, held in memory for the synchronous questions a call asks and
    written through to the ``kv`` table so a restart remembers what is broken."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._broken: dict[tuple[str, str], dict[str, Any]] = {}
        self._checks: dict[str, dict[str, Any]] = {}
        self._checked_at = ""
        self._waits: dict[tuple[str, str], deque[tuple[str, float]]] = {}
        self._saving: set[asyncio.Task[None]] = set()

    async def load(self) -> None:
        stored = await self.db.kv_get(KV_KEY, {}) or {}
        for mark in stored.get("broken") or []:
            if isinstance(mark, dict) and mark.get("provider") and mark.get("model"):
                self._broken[(str(mark["provider"]), str(mark["model"]))] = dict(mark)
        checks = stored.get("checks")
        self._checks = dict(checks) if isinstance(checks, dict) else {}
        self._checked_at = str(stored.get("checked_at") or "")

    async def save(self) -> None:
        await self.db.kv_set(KV_KEY, {"broken": list(self._broken.values()), "checks": self._checks, "checked_at": self._checked_at})

    def _save_soon(self) -> None:
        # Told from inside a provider call, which must not wait on the database for a side note.
        try:
            task = asyncio.get_running_loop().create_task(self.save())
        except RuntimeError:
            return
        self._saving.add(task)
        task.add_done_callback(self._saving.discard)

    # -- what the calls say ------------------------------------------------------------

    def refused(self, provider_id: str, model: str, status: int, text: str) -> None:
        """Every refusal a provider adapter observes; only one about the model itself marks it."""
        message = model_refusal(status, text)
        if message is None:
            return
        known = self._broken.get((provider_id, model))
        if known is None:
            logger.warning("model %s on %s is marked broken: HTTP %s %s", model, provider_id, status, message)
        self._broken[(provider_id, model)] = {"provider": provider_id, "model": model, "status": status, "detail": message, "at": _now()}
        self._save_soon()

    def answered(self, provider_id: str, model: str) -> None:
        """Every answered call: a model that answers is not broken any more."""
        if self._broken.pop((provider_id, model), None) is not None:
            logger.info("model %s on %s answered again; its broken mark is cleared", model, provider_id)
            self._save_soon()

    def waited(self, provider_id: str, model: str, ms: float) -> None:
        """A call abandoned after ``ms`` without an answer: a hedged translation's loser, which books no
        usage. Only a wait past :data:`SLOW_MEDIAN_MS` is kept, as the "at least this long" it is; a
        short one says only that the other attempt won first. Held in memory, so a restart forgets it."""
        if ms < SLOW_MEDIAN_MS:
            return
        self._waits.setdefault((provider_id, model), deque(maxlen=SLOW_SAMPLE)).append((_now(), float(ms)))

    def broken(self, config: RuntimeConfig, preset_id: str) -> str | None:
        """The upstream's message when this preset's model is marked broken, else ``None``."""
        preset = config.presets.get(preset_id)
        if preset is None:
            return None
        mark = self._broken.get((preset.provider, preset.model))
        return str(mark.get("detail") or "broken") if mark else None

    def usable(self, config: RuntimeConfig, preset_id: str, *, task: str = "") -> str:
        """``preset_id``, or a stand-in when it is marked broken: the same model on another provider, else
        the helpers' cheap preset, else the first preset of the table that is not broken. The log names
        both, so a helper that answers from a different model is never a mystery."""
        if not preset_id or self.broken(config, preset_id) is None:
            return preset_id
        stand_in = self.stand_in(config, preset_id)
        if stand_in != preset_id:
            logger.warning("%s: preset %s is marked broken (%s); using %s instead", task or "a helper", preset_id, self.broken(config, preset_id), stand_in)
        return stand_in

    def stand_in(self, config: RuntimeConfig, preset_id: str, ready: Iterable[str] | None = None) -> str:
        primary = config.presets.get(preset_id)
        pool = [pid for pid in config.presets if pid != preset_id and self.broken(config, pid) is None]
        if ready is not None:
            have = set(ready)
            pool = [pid for pid in pool if config.presets[pid].provider in have]
        if primary is not None:
            for pid in pool:
                if config.presets[pid].provider != primary.provider and same_model(config.presets[pid].model, primary.model):
                    return pid
        helper = config.assist.predict_preset if config.assist.predict_preset in config.presets else (config.middle_preset() or "")
        if helper in pool:
            return helper
        return pool[0] if pool else preset_id

    # -- slowness, from the calls already made ----------------------------------------------

    async def speeds(self, *, now: datetime | None = None) -> dict[tuple[str, str], dict[str, Any]]:
        """``(provider, model) → {median_ms, calls}`` over the most recent measured calls of each."""
        since = ((now or datetime.now(UTC)) - timedelta(hours=SLOW_WINDOW_HOURS)).isoformat()
        rows = await self.db.fetchall(
            "SELECT provider_id, model, purpose, first_byte_ms, duration_ms, output_tokens FROM usage_events"
            " WHERE at >= ? ORDER BY seq DESC",
            (since,),
        )
        samples: dict[tuple[str, str], list[float]] = {}
        for key, waits in self._waits.items():
            samples[key] = [ms for at, ms in waits if at >= since]
        for row in rows:
            key = (str(row["provider_id"]), str(row["model"]))
            taken = samples.setdefault(key, [])
            if len(taken) >= SLOW_SAMPLE:
                continue
            if row["first_byte_ms"] is not None:
                taken.append(float(row["first_byte_ms"]))
            elif row["purpose"] != "stream" and int(row["output_tokens"] or 0) <= SHORT_ANSWER_TOKENS and int(row["duration_ms"] or 0) > 0:
                taken.append(float(row["duration_ms"]))
        return {key: {"median_ms": round(statistics.median(values)), "calls": len(values)} for key, values in samples.items() if values}

    async def view(self, config: RuntimeConfig) -> dict[str, dict[str, Any]]:
        """Each preset's mark for Settings and the model pickers: ``state`` is ``broken``, ``slow``, ``ok``
        or ``""`` (nothing known), with the reason and the last check's result beside it."""
        try:
            speeds = await self.speeds()
        except Exception:  # noqa: BLE001 — a mark is a hint; the settings page must still open
            logger.warning("could not read the models' speeds", exc_info=True)
            speeds = {}
        out: dict[str, dict[str, Any]] = {}
        for pid, preset in config.presets.items():
            entry: dict[str, Any] = {"state": "", "detail": ""}
            speed = speeds.get((preset.provider, preset.model))
            if speed is not None:
                entry.update(median_ms=speed["median_ms"], calls=speed["calls"])
                entry["state"] = "slow" if speed["calls"] >= SLOW_MIN_CALLS and speed["median_ms"] >= SLOW_MEDIAN_MS else "ok"
            if pid in self._checks:
                entry["check"] = self._checks[pid]
                if not entry["state"]:
                    entry["state"] = {"ok": "ok", "slow": "slow"}.get(self._checks[pid].get("state"), "")
            mark = self._broken.get((preset.provider, preset.model))
            if mark is not None:
                entry.update(state="broken", detail=mark.get("detail") or "", at=mark.get("at") or "")
            out[pid] = entry
        return out

    # -- the operator's check -----------------------------------------------------------

    async def check(self, config: RuntimeConfig, call: Callable[[str], Any]) -> dict[str, Any]:
        """One tiny request to every preset, ``call(preset_id)`` making it; the results by preset.

        A probe that is refused in words about the model marks it broken through the adapter, as a real
        call would; one that answers clears the mark; one that does not answer in :data:`CHECK_SECONDS`
        is slow; any other failure is reported as it is and marks nothing."""
        gate = asyncio.Semaphore(CHECK_PARALLEL)

        async def probe(pid: str) -> tuple[str, dict[str, Any]]:
            preset = config.presets[pid]
            async with gate:
                started = time.monotonic()
                try:
                    await asyncio.wait_for(call(pid), timeout=CHECK_SECONDS)
                except TimeoutError:
                    return pid, {"state": "slow", "detail": f"no answer within {CHECK_SECONDS:g} s", "at": _now()}
                except Exception as exc:  # noqa: BLE001 — every failure is a result to show, not an error of the check
                    ms = round((time.monotonic() - started) * 1000)
                    broken = self._broken.get((preset.provider, preset.model))
                    if broken is not None:
                        return pid, {"state": "broken", "detail": broken.get("detail") or "", "ms": ms, "at": _now()}
                    return pid, {"state": "failed", "detail": f"{type(exc).__name__}: {exc}"[:300], "ms": ms, "at": _now()}
                ms = round((time.monotonic() - started) * 1000)
            self.answered(preset.provider, preset.model)
            return pid, {"state": "slow" if ms >= SLOW_MEDIAN_MS else "ok", "detail": "", "ms": ms, "at": _now()}

        results = dict(await asyncio.gather(*(probe(pid) for pid in config.presets)))
        self._checks = results
        self._checked_at = _now()
        await self.save()
        return {"checked_at": self._checked_at, "results": results}


class HealthUsageSink:
    """The usage sink the adapters write to, telling the marks that a model answered on the way."""

    def __init__(self, inner: Any, health: ModelHealth) -> None:
        self.inner = inner
        self.health = health

    async def record(self, record: Any) -> None:
        self.health.answered(str(record.provider_id), str(record.model))
        await self.inner.record(record)


__all__ = ["CHECK_PROMPT", "CHECK_PURPOSE", "HealthUsageSink", "ModelHealth", "model_name", "model_refusal", "same_model"]
