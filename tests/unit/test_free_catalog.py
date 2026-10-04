import httpx
import pytest

from daedalus.providers.free_catalog import FreeCatalog, approved_endpoint, free_rows, probe_agent_cycle, zen_free_ids


def test_free_endpoint_must_match_the_provider_whose_price_was_checked() -> None:
    canonical = "https://openrouter.ai/api/v1"
    assert approved_endpoint("openrouter", canonical + "/", canonical)
    assert not approved_endpoint("openrouter", "https://other.example/v1", canonical)
    assert not approved_endpoint("openrouter", "https://api.kilo.ai/api/gateway", canonical)


def test_free_rows_require_positive_evidence() -> None:
    kilo = {"data": [
        {"id": "free-without-suffix", "isFree": True, "mayTrainOnYourPrompts": True, "supported_parameters": ["tools"]},
        {"id": "looks-free:free", "isFree": False},
    ]}
    rows = free_rows("kilo", kilo)
    assert [row["id"] for row in rows] == ["free-without-suffix"]
    assert rows[0]["tools_reported"] and rows[0]["may_train"]
    assert rows[0]["agent_ready"] is False
    router = {"data": [
        {"id": "zero", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "missing", "pricing": {"prompt": "0"}},
        {"id": "paid", "pricing": {"prompt": "0", "completion": "0.01"}},
    ]}
    assert [row["id"] for row in free_rows("openrouter", router)] == ["zero"]


def test_zen_requires_documented_free_price_and_chat_endpoint() -> None:
    docs = """| Model | Model ID | Endpoint |
| --- | --- | --- |
| Big Pickle | big-pickle | `https://opencode.ai/zen/v1/chat/completions` |
| Wrong API Free | wrong-free | `https://opencode.ai/zen/v1/responses` |
| Model | Input | Output |
| --- | --- | --- |
| Big Pickle | Free | Free |
| Wrong API Free | Free | Free |
| Paid Free | $1 | $2 |
"""
    ids = zen_free_ids(docs)
    assert ids == {"big-pickle"}
    payload = {"data": [{"id": "big-pickle"}, {"id": "wrong-free"}, {"id": "new-free"}]}
    assert [row["id"] for row in free_rows("opencode_zen", payload, zen_ids=ids)] == ["big-pickle"]


@pytest.mark.asyncio
async def test_catalog_keeps_last_good_snapshot_on_source_error() -> None:
    fail = False

    def respond(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(503)
        if "kilo" in request.url.host:
            return httpx.Response(200, json={"data": [{"id": "one", "isFree": True}]})
        return httpx.Response(200, json={"data": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    catalog = FreeCatalog()
    try:
        first = await catalog.get(client=client)
        assert first["providers"][0]["models"][0]["id"] == "one"
        fail = True
        second = await catalog.get(client=client, force=True)
        assert second["stale"] is True
        assert second["providers"][0]["models"][0]["id"] == "one"
        assert second["providers"][0]["fresh"] is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_agent_probe_requires_a_valid_tool_call_and_followup() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "add_numbers", "arguments": '{"a":2,"b":3}'}}
            ]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "The answer is 5."}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await probe_agent_cycle("https://example.invalid/v1", "free", client=client)
    assert calls == 2

    def invalid(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "add_numbers", "arguments": '{"a":2,"b":4}'}}
        ]}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(invalid)) as client:
        assert not await probe_agent_cycle("https://example.invalid/v1", "free", client=client)
