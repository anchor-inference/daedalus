"""How the host's summariser calls are made: streamed, judged by silence rather than by length, and
paced together so the parts of a long history neither crowd a provider nor wait behind each other.

A compaction of a 980k-token session once took 501 s. Its last merge was a call that writes a few
thousand words, and it was still writing when a 120 s wall-clock limit killed it; the retry started
from nothing with 240 s, and two minutes of output were thrown away. A summariser call is therefore
streamed, and what ends it early is silence — no byte before the first one, or none for as long again
between two — never the length of an answer that is still arriving. A ceiling scaled by the output
cap remains, so a call that trickles forever still ends.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager, suppress
from typing import Any

from protocore.contracts.llm import LLMRateLimitError, LLMRequest, LLMTimeoutError, ProviderDeltaKind

SUMMARY_MIN_TOKENS_PER_SECOND = 8.0
"""The slowest steady writing the ceiling allows for. Opus behind the subscription proxy writes 40 to 80
tokens a second; a ceiling at a tenth of that is reached only by a call that is not really answering."""
RATE_LIMIT_WAIT_MAX = 60.0
"""A rate limit that asks for a longer wait than this is handed to the next model of the ladder instead."""
RATE_LIMIT_PAUSES = (5.0, 15.0, 30.0)
"""The waits after a rate limit that stated none, one per retry of the same call."""


class SummaryStalled(LLMTimeoutError):
    """A summariser call that went silent, or ran past its ceiling (then ``retryable`` is false)."""


def ceiling_for(max_tokens: int, silence: float) -> float:
    """The most a summariser call may take in all: its silence bound plus its output cap written slowly."""
    return silence + max_tokens / SUMMARY_MIN_TOKENS_PER_SECOND


async def stream_text(provider: Any, request: LLMRequest, *, silence: float, ceiling: float) -> str:
    """The text of one streamed call; raises :class:`SummaryStalled` when it went quiet or ran past ``ceiling``.

    Every delta counts as life, a keep-alive or a reasoning token included: a model that thinks before
    it writes (Grok spends four to six thousand reasoning tokens on a part) is not silent while it does.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    hard = started + ceiling
    pieces: list[str] = []
    heard = False
    stream: AsyncIterator[Any] = provider.stream_with_tools(request)
    try:
        async with asyncio.timeout_at(min(started + silence, hard)) as limit:
            async for delta in stream:
                heard = True
                limit.reschedule(min(loop.time() + silence, hard))
                if delta.kind is ProviderDeltaKind.text and delta.content:
                    pieces.append(delta.content)
    except TimeoutError as exc:
        if not limit.expired():
            raise  # the provider's own timeout, which it reports itself
        took = loop.time() - started
        if loop.time() >= hard:
            # Still writing at the ceiling: asking the same model again would only write as slowly.
            raise SummaryStalled(f"still writing after {took:.0f}s, past the ceiling for {request.max_tokens} tokens", retryable=False) from exc
        if not heard:
            raise SummaryStalled(f"no byte within {silence:.0f}s") from exc
        raise SummaryStalled(f"went silent for {silence:.0f}s after {took - silence:.0f}s of answer") from exc
    finally:
        closing = getattr(stream, "aclose", None)
        if closing is not None:
            await closing()
    return "".join(pieces)


def _stated_wait(exc: LLMRateLimitError) -> float | None:
    verdict = getattr(exc, "classified", None)
    wait = getattr(verdict, "retry_after_seconds", None)
    return float(wait) if wait is not None else None


class SummaryGate:
    """At most ``width`` summariser calls at once, and all of them held back together after a rate limit.

    A rate limit answered by asking again at once, from twelve calls side by side, is a storm: each
    refusal narrows the gate by half for the rest of the compaction and closes it for the stated wait
    (or a short default), and the refused call is asked again once it reopens. A refusal that means
    the allowance is spent (not retryable) or names a long wait goes back to the caller, whose ladder
    moves on to the next model sooner than this one would answer.
    """

    def __init__(self, width: int) -> None:
        self.width = max(1, width)
        self.active = 0
        self.resume_at = 0.0
        self._changed = asyncio.Condition()

    @asynccontextmanager
    async def _slot(self) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        async with self._changed:
            while True:
                closed_for = self.resume_at - loop.time()
                if closed_for > 0:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self._changed.wait(), closed_for)
                    continue
                if self.active < self.width:
                    break
                await self._changed.wait()
            self.active += 1
        try:
            yield
        finally:
            async with self._changed:
                self.active -= 1
                self._changed.notify_all()

    async def _rate_limited(self, wait: float) -> None:
        loop = asyncio.get_running_loop()
        async with self._changed:
            self.width = max(1, self.width // 2)
            self.resume_at = max(self.resume_at, loop.time() + wait)
            self._changed.notify_all()

    async def call[R](self, make: Callable[[], Awaitable[R]]) -> R:
        for attempt in range(len(RATE_LIMIT_PAUSES) + 1):
            async with self._slot():
                try:
                    return await make()
                except LLMRateLimitError as exc:
                    stated = _stated_wait(exc)
                    if not exc.retryable or attempt == len(RATE_LIMIT_PAUSES) or (stated is not None and stated > RATE_LIMIT_WAIT_MAX):
                        raise
                    wait = stated if stated is not None else RATE_LIMIT_PAUSES[attempt]
            await self._rate_limited(wait)
        raise AssertionError("unreachable")


async def all_or_nothing[R](coros: Iterable[Awaitable[R]]) -> list[R]:
    """Await every one, and cancel the rest as soon as one fails.

    ``asyncio.gather`` leaves the others running when one raises: a compaction whose part failed went on
    paying for the eleven calls beside it while the ladder had already moved on to the next model.
    """
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


__all__ = ["SummaryGate", "SummaryStalled", "all_or_nothing", "ceiling_for", "stream_text"]
