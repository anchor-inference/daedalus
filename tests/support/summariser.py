"""A summariser fake written as a plain request-to-response function, served as the stream the host reads."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from protocore.contracts.llm import LLMResponse, ProviderDelta, ProviderDeltaKind
from protocore.contracts.types import TextBlock


def streamed(complete: Callable[[Any], Awaitable[LLMResponse]]) -> Callable[[Any], AsyncIterator[ProviderDelta]]:
    """``stream_with_tools`` for a provider whose answer ``complete(request)`` gives whole.

    The host streams its summaries; the tests that only care what a summariser is asked and answers
    keep writing the answer as one response."""

    async def stream_with_tools(request: Any) -> AsyncIterator[ProviderDelta]:
        response = await complete(request)
        text = "".join(b.text for b in response.message.content_blocks if isinstance(b, TextBlock))
        if text:
            yield ProviderDelta(kind=ProviderDeltaKind.text, content=text)
        yield ProviderDelta(kind=ProviderDeltaKind.usage, usage={"input_tokens": 0, "output_tokens": 0})
        yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="stop")

    return stream_with_tools
