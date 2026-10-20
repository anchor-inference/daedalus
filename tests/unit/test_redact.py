from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from protocore.contracts.hooks import HookActionKind
from protocore.contracts.types import HookEvent

from daedalus.host.hooks import DaedalusHookManager
from daedalus.providers.wire import parse_json_arguments
from daedalus.security.redact import MASK, RedactingFilter, Redactor


def test_configured_values_are_masked_whatever_they_look_like() -> None:
    r = Redactor(["plain-looking-secret-value", "short"])
    assert r.redact("key=plain-looking-secret-value done") == f"key={MASK} done"
    assert r.redact("short stays") == "short stays"  # below the minimum length: too collision-prone


def test_secret_shapes_are_masked() -> None:
    r = Redactor()
    cases = {
        "token 1234567890:AAHfiqksKZ8WmR2zSjiQ7_v4TI7IqhIzLHo here": f"token {MASK} here",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345": f"Authorization: Bearer {MASK}",
        "export OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz": f"export OPENAI_API_KEY={MASK}",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789": MASK,
        "AKIAIOSFODNN7EXAMPLE": MASK,
        "postgres://user:s3cretpass@db.local/x": f"postgres://user:{MASK}@db.local/x",
        "DB_PASSWORD='hunter22'": f"DB_PASSWORD='{MASK}'",
        '{"api_key": "8f3c1d2e9a0b7c6d5e4f3a2b1c0d9e8f"}': f'{{"api_key": "{MASK}"}}',
        '{"password": "hunter2hunter2"}': f'{{"password": "{MASK}"}}',
        "X-Api-Key: 8f3c1d2e9a0b7c6d5e4f3a2b": f"X-Api-Key: {MASK}",
        'curl -H "Authorization: token 8f3c1d2e9a0b7c6d5e4f"': f'curl -H "Authorization: token {MASK}"',
        "glpat-ABCDEFGHIJKLMNOPQRST": MASK,
        "api_key: 8f3c1d2e9a0b7c6d5e4f": f"api_key: {MASK}",
    }
    for raw, expected in cases.items():
        assert r.redact(raw) == expected, raw


def test_pem_blocks_are_masked_whole() -> None:
    text = "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\nafter"
    assert Redactor().redact(text) == f"before\n{MASK}\nafter"


def test_ordinary_text_and_source_code_are_untouched() -> None:
    r = Redactor(["real-secret-value-1"])
    for text in (
        "git status shows 3 files; the token bucket refills at 10/s; user=alice",
        'input_tokens=normalized["input_tokens"],',
        "tokens = response.json()",
        "access_token: Optional[str] = None",
        "self.password = derive(salt)",
        "TOKEN_BUDGET = 128000",
        'api_key = os.environ["OPENAI_API_KEY"]',
        "https://github.com/x/y/commit/9f86d081884c7d659a2feaa0c55ad015a3bf4f1b",
        "data:image/png;base64,eyJhbGciOiJIUzI1NiJ9AAAA",
    ):
        assert r.redact(text) == text, text


def test_own_source_files_survive_redaction_byte_identical() -> None:
    """The agent reads and rewrites its own code through the redactor; it must not mangle it."""
    root = Path(__file__).resolve().parents[2]
    r = Redactor()
    for rel in ("daedalus/providers/openai_compat.py", "daedalus/mcp/oauth.py", "daedalus/extensions/api.py", "daedalus/config.py", "launcher/supervisor.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert r.redact(text) == text, rel


def test_nested_arguments_are_redacted() -> None:
    r = Redactor(["real-secret-value-1"])
    out = r.redact_any({"command": "curl -H 'x: real-secret-value-1'", "env": {"A": ["real-secret-value-1"]}, "n": 3})
    assert out == {"command": f"curl -H 'x: {MASK}'", "env": {"A": [MASK]}, "n": 3}


def test_redact_any_uses_dict_key_as_context() -> None:
    """A credential under a secret-named key must be masked even though the bare value
    matches no self-contained shape (the JSON-string path catches it via the key)."""
    r = Redactor()
    fake = "DEMO_CREDENTIAL_1234567890"
    assert fake not in r.redact(json.dumps({"api_key": fake}))  # string path already worked
    assert r.redact_any({"api_key": fake}) == {"api_key": MASK}
    assert r.redact_any({"config": {"password": fake}}) == {"config": {"password": MASK}}
    # Conservative: no secret-named key -> not masked; short value -> not credential-looking.
    assert r.redact_any({"data": fake}) == {"data": fake}
    assert r.redact_any({"api_key": "test"}) == {"api_key": "test"}


def test_logging_filter_masks_exception_text() -> None:
    """A secret carried by the exception itself (not the log message) must not leak via
    ``log.exception``: the formatter builds ``exc_text`` after filters run."""
    fake = "DEMO_CREDENTIAL_1234567890"
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(RedactingFilter(Redactor([fake])))
    logger = logging.Logger("synthetic-review", logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        raise ValueError(fake)
    except ValueError:
        logger.exception("synthetic exception")
    out = buf.getvalue()
    assert fake not in out
    assert MASK in out


def test_logging_filter_handles_bool_exc_info() -> None:
    """``exc_info=True`` (the "capture current exception" value) must not crash the filter
    even when there is no active exception."""
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "msg", None, True)
    assert RedactingFilter(Redactor()).filter(record) is True
    assert record.getMessage() == "msg"


def test_logging_filter_masks_records() -> None:
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "key %s", ("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",), None)
    assert RedactingFilter(Redactor()).filter(record) is True
    assert record.getMessage() == f"key {MASK}"


async def test_post_tool_hook_rewrites_output() -> None:
    hooks = DaedalusHookManager(Redactor(["real-secret-value-1"]))
    result = await hooks.invoke(HookEvent.post_tool_use, {"tool_name": "Exec", "tool_output": "TOKEN=real-secret-value-1"}, "t")
    assert result.action == HookActionKind.MODIFY
    assert result.modifications["tool_output"] == f"TOKEN={MASK}"
    clean = await hooks.invoke(HookEvent.post_tool_use, {"tool_name": "Exec", "tool_output": "all good"}, "t")
    assert clean.action == HookActionKind.ALLOW
    assert hooks.redacted_calls == 1


def test_tool_argument_repairs() -> None:
    assert parse_json_arguments('{"a": 1,}') == {"a": 1}
    assert parse_json_arguments('```json\n{"a": [1, 2,]}\n```') == {"a": [1, 2]}
    assert parse_json_arguments("{'path': 'x.py'}") == {"path": "x.py"}
    assert parse_json_arguments('Here you go: {"q": "x"} thanks') == {"q": "x"}
    assert parse_json_arguments('{"command": "ls -la') == {"command": "ls -la"}
    assert parse_json_arguments("") == {}


async def test_a_tool_that_raised_is_masked_in_the_event_the_history_and_the_next_request(
        settings: Any, db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool that raised used to skip the core's ``post_tool_use`` hook, and the session runner patched
    the result already in the history after the fact. The core now runs the hook on every failure, so
    the redacting hook masks the error text before it enters the history at all."""
    from protocore.contracts.types import ToolResultBlock
    from protocore.runtime.events.types import EventType

    from daedalus.tools import shell
    from tests.unit.test_session_runner import ScriptedProvider, _manager, _wait_finished

    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz"

    async def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"401 for https://api.x.test/?key={secret}")

    monkeypatch.setattr(shell, "_run", refused)
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "true"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    events: list[Any] = []

    async def sink(_session_id: str, event: Any) -> None:
        events.append(event)

    manager.add_sink(sink)
    try:
        state = await manager.create_session("t")
        waiter = asyncio.create_task(_wait_finished(manager))
        await manager.submit(state.session.id, "run it")
        await waiter
        [result] = [e for e in events if e.type is EventType.TOOL_RESULT]
        assert result.payload.get("is_error") and secret not in json.dumps(result.payload, default=str)
        history = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
        [block] = [b for m in history for b in m.content_blocks if isinstance(b, ToolResultBlock)]
        assert block.is_error and secret not in block.content and MASK in block.content
        sent = json.dumps([m.model_dump(mode="json") for m in provider.requests[-1].messages], ensure_ascii=False)
        assert secret not in sent and MASK in sent
    finally:
        await manager.close()


def test_nested_containers_inherit_the_secret_key_context() -> None:
    r = Redactor([])
    assert r.redact_any({"credentials": {"v": "AbCdEf1234567890xyz"}}) == {"credentials": {"v": MASK}}
    assert r.redact_any({"api_key": ["AbCdEf1234567890xyz", "short"]}) == {"api_key": [MASK, "short"]}
    assert r.redact_any({"h": ("Authorization: Bearer sk-abcdefgh12345678abcd",)}) == {"h": (f"Authorization: Bearer {MASK}",)}
    assert r.redact_any({"s": frozenset({"sk-abcdefghij1234567890"})}) == {"s": frozenset({MASK})}
    assert r.redact_any(b"key sk-abcdefghij1234567890 end") == f"key {MASK} end".encode()


def test_auth_needs_a_word_boundary_so_ordinary_keys_survive() -> None:
    r = Redactor([])
    plain = {"author": "gpt-4o-2024-08-06", "authority": "sha256-1a2b3c4d5e6f7g8h", "oauth_provider": "github-enterprise-2024", "auth_user_id": "0123456789abcdef01"}
    assert r.redact_any(plain) == plain
    assert r.redact_any({"auth": "AbCdEf1234567890xyz", "auth_token": "AbCdEf1234567890xyz"}) == {"auth": MASK, "auth_token": MASK}


def test_logging_filter_masks_stack_info() -> None:
    import logging

    secret = "sk-abcdefghij1234567890"
    log = logging.getLogger("redact-stack-test")
    record = log.makeRecord("redact-stack-test", logging.WARNING, __file__, 1, "hello", (), None, sinfo=f"Stack:\n  token={secret}")
    RedactingFilter(Redactor([])).filter(record)
    assert secret not in (record.stack_info or "")


def test_vault_hands_over_foreign_secrets_and_keeps_ours_masked() -> None:
    from daedalus.security.redact import MASK, REF_RE, Redactor

    r = Redactor(["our-configured-key-123456"])
    kept: list[str] = []

    def keep(value: str) -> str:
        kept.append(value)
        return f"«ref:{len(kept):010x}»"

    text = '{"edit_token": "e9f1a2b3c4d5e6f7a8b9", "author": "someone", "key": "our-configured-key-123456"}\n-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----'
    out = r.vault(text, keep)
    assert kept == ["e9f1a2b3c4d5e6f7a8b9"]
    assert '"edit_token": "«ref:0000000001»"' in out and "our-configured-key-123456" not in out and "BEGIN PRIVATE KEY" not in out
    assert out.count(MASK) == 2
    # a placeholder is not a secret: redaction leaves it where it is, whatever key it sits under
    assert r.redact('{"edit_token": "«ref:0123456789»"}') == '{"edit_token": "«ref:0123456789»"}'
    assert REF_RE.fullmatch("«ref:0123456789»")
    # an API documenting its own request shape: {edit_token} is a slot, not a value to keep
    assert r.vault('"body": {"edit_token": "{edit_token}"}', keep) == '"body": {"edit_token": "{edit_token}"}' 
