"""The provider crosses one durable admission boundary before each inference send."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from protocore.contracts.llm import LLMRequest

    from daedalus.providers.openai_compat import ProviderEndpoint


class InferenceAdmission(Protocol):
    async def start(self, endpoint: ProviderEndpoint, request: LLMRequest, body: dict[str, Any]) -> str | None: ...

    async def cost(self, reservation_id: str, raw: dict[str, Any], normalized: dict[str, Any]) -> float | None: ...

    async def interrupted(self, reservation_id: str, reason: str) -> None: ...
