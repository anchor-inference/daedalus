"""Changing endpoint settings must not close the adapter an active engine still uses."""
from __future__ import annotations

import asyncio
import json

import httpx

from daedalus.config import Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.models import model_config
from tests.unit.test_providers import _chunk, _sse
from tests.unit.test_session_runner import _wait_finished


async def test_settings_reload_between_tool_rounds_keeps_the_run_alive(settings: Settings, db: Database) -> None:
    config = model_config()
    config.model.chain = []
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    provider = manager.providers.get("deepseek")
    requests = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        body = json.loads(request.content)
        if not body.get("stream"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "Result"}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 2, "completion_tokens": 1}})
        requests += 1
        if requests == 1:
            changed = config.model_copy(deep=True)
            changed.providers["deepseek"].timeout_seconds += 1
            manager.providers.reload(changed)
            await manager.providers.close_retired()
            assert not provider._client.is_closed
            chunks = [_chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "Exec", "arguments": '{"command":"printf result"}'}}]}, finish="tool_calls")]
        else:
            chunks = [_chunk({"content": "The command returned result."}, finish="stop")]
        chunks.append({"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}})
        return httpx.Response(200, text=_sse(chunks), headers={"content-type": "text/event-stream"})

    await provider._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        state = await manager.create_session("Client lifetime")
        waiter = asyncio.create_task(_wait_finished(manager))
        await manager.submit(state.session.id, "Print result and report it")
        assert (await waiter)[0][2] == "completed"
        assert requests >= 2
        assert provider._client.is_closed
        assert state.provider_hold is None
    finally:
        await manager.close()
