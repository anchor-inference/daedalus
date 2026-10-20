"""Inbound events (payload flattening, signatures, dedupe, standing intents), voice transcription, session modes."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import AsrConfig, RuntimeConfig, Settings
from daedalus.extensions.inbound import Inbound, flatten_payload, verify_signature
from daedalus.host.engine_factory import runtime_constants
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.transport.telegram.voice import TranscriptionError, transcribe
from tests.support.notifications import RecordingNotifications

KEYPROXY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "keyproxy"


def _keyproxy_module(name: str = "proxy") -> Any:
    """Load a key-proxy module the way the proxy's own image runs it.

    ``proxy.py`` imports its siblings by bare name (``from claude import ...``), which is what
    ``PYTHONPATH=/app`` gives it in the container. Loading it by file path alone leaves those
    imports unresolvable, so the two tests below failed at import with ``No module named 'claude'``
    rather than on anything they assert. The other key-proxy tests already put the directory on
    ``sys.path`` first; this does the same, and reuses an already-loaded module so two tests in one
    session do not execute the file twice.
    """
    import importlib.util
    import sys

    if str(KEYPROXY_DIR) not in sys.path:
        sys.path.insert(0, str(KEYPROXY_DIR))
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, KEYPROXY_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_flatten_payload_is_bounded_and_readable() -> None:
    text = flatten_payload({"action": "opened", "pull_request": {"title": "Fix", "user": {"login": "x"}, "labels": [{"name": "bug"}]}})
    assert "action: opened" in text and "pull_request.user.login: x" in text and "pull_request.labels[0].name: bug" in text
    huge = flatten_payload({"k": ["v" * 100] * 500})
    assert len(huge) <= 2100 and huge.endswith("…")


def test_signature_schemes() -> None:
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_signature("github", "s3cret", body, {"x-hub-signature-256": sig})
    assert not verify_signature("github", "s3cret", body, {"x-hub-signature-256": "sha256=bad"})
    assert verify_signature("bearer", "tok", body, {"authorization": "Bearer tok"})
    assert not verify_signature("bearer", "", body, {"authorization": "Bearer "})


@pytest.fixture
async def app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    submitted: list[tuple[str, str, str]] = []

    async def fake_submit(session_id: str, text: str, attachments=(), *, steer=False, as_answer=True, origin="operator") -> str:  # type: ignore[no-untyped-def]
        submitted.append((session_id, text, origin))
        return "run-x"

    manager.submit = fake_submit  # type: ignore[method-assign]
    # Without a front Application.create_session is the manager's, so the stand-in hands it straight over.
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={}, notifications=RecordingNotifications(), submitted=submitted, create_session=manager.create_session)
    yield app
    await manager.close()


async def test_webhook_deliveries_dedupe(app: Any) -> None:
    inbound = Inbound(app)
    assert await inbound.record_delivery("github", "d1") is True
    assert await inbound.record_delivery("github", "d1") is False
    assert await inbound.record_delivery("ci", "d1") is True


async def test_inbound_delivery_creates_a_standing_session_and_fires_intents(app: Any) -> None:
    inbound = Inbound(app)
    created = await inbound.create_intent(pattern=r"build\s+failed", action="investigate the failure", session_id=None, cooldown_minutes=60, max_fires=2, expires_in_hours=None, created_by=None)
    with pytest.raises(ValueError):
        await inbound.create_intent(pattern="(", action="x", session_id=None, cooldown_minutes=1, max_fires=1, expires_in_hours=None, created_by=None)
    result = await inbound.deliver(source="ci", text="Build FAILED on main", session_ref=None, default_title="[inbound]")
    assert result["run_id"] == "run-x"
    titles = [s["title"] for s in await app.manager.list_sessions()]
    assert "[inbound]" in titles
    # the event went to the standing session; the intent fired into its own (created_by is None → a fresh task session is needed)
    assert app.submitted[0][2] == "inbound:ci" and "[inbound event from ci]" in app.submitted[0][1]
    row = await app.db.fetchone("SELECT fired_count, last_fired_at FROM intents WHERE id = ?", (created["id"],))
    # without a scheduler extension the intent cannot start a task session; it is reported, not counted
    assert row["fired_count"] == 0
    # cooldown and budget: give the intent a session, fire twice, then it is exhausted
    app.extensions["scheduler"] = None
    state = await app.manager.create_session("watcher")
    await app.db.execute("UPDATE intents SET session_id = ? WHERE id = ?", (state.session.id, created["id"]))
    assert await inbound.match_intents("ci", "build failed again") == [created["id"]]
    assert await inbound.match_intents("ci", "build failed again") == []  # cooldown
    await app.db.execute("UPDATE intents SET last_fired_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (created["id"],))
    assert await inbound.match_intents("ci", "build failed once more") == [created["id"]]
    await app.db.execute("UPDATE intents SET last_fired_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (created["id"],))
    assert await inbound.match_intents("ci", "build failed yet again") == []  # budget of 2 used up
    row = await app.db.fetchone("SELECT enabled FROM intents WHERE id = ?", (created["id"],))
    assert row["enabled"] == 0
    assert [s for s in app.submitted if s[2] == "intent"]


async def test_transcribe_uses_an_openai_compatible_endpoint(tmp_path: Path) -> None:
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS fake")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"text": "  hello there  "})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    text = await transcribe(audio, AsrConfig(url="http://asr.local/v1", api_key="k1", model="whisper-1"), client=client)
    assert text == "hello there" and seen["url"] == "http://asr.local/v1/audio/transcriptions" and seen["auth"] == "Bearer k1"
    failing = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(TranscriptionError):
        await transcribe(audio, AsrConfig(url="http://asr.local/v1"), client=failing)
    with pytest.raises(TranscriptionError):
        await transcribe(audio, AsrConfig())


async def test_modes_override_limits_and_prompt(settings: Settings, db: Database) -> None:
    config = RuntimeConfig()
    rc = runtime_constants(config, context_window=100_000, max_output_tokens=8_000, thinking=False, mode=config.modes["quick"])
    assert rc.max_turns_per_run == 25
    rc = runtime_constants(config, context_window=100_000, max_output_tokens=8_000, thinking=False, mode=None)
    assert rc.max_turns_per_run == config.limits.max_iterations
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    state = await manager.create_session("m")
    assert await manager.set_mode(state.session.id, "deep") == "deep"
    assert manager.mode_for(state) is config.modes["deep"]
    with pytest.raises(ValueError):
        await manager.set_mode(state.session.id, "nonsense")
    assert await manager.set_mode(state.session.id, None) == "" and manager.mode_for(state) is None
    refreshed = await manager.sessions.get(state.session.id, "daedalus")
    assert "mode" not in refreshed.metadata
    await manager.close()
    assert json.dumps(config.modes["careful"].model_dump())


def test_keyless_vendor_kinds_are_allowed_only_behind_a_proxy(settings: Settings) -> None:
    from daedalus.config import ProviderConfig
    from daedalus.providers.registry import ProviderRegistry

    settings.deepseek_api_key = ""
    registry = ProviderRegistry(settings, RuntimeConfig())
    assert registry._endpoint("deepseek", ProviderConfig(kind="deepseek", base_url="https://api.deepseek.com")) is None
    proxied = registry._endpoint("deepseek", ProviderConfig(kind="deepseek", base_url="http://keyproxy:3200/deepseek"))
    assert proxied is not None and proxied.api_key == "" and proxied.kind == "deepseek"


async def test_sandbox_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.config import ExecToolsConfig
    from daedalus.tools import shell

    ws = tmp_path / "ws"
    ws.mkdir()
    argv, sandboxed = await shell.sandbox_argv("ls", ExecToolsConfig(sandbox="off"), writable=[ws])
    assert argv == ["bash", "-lc", "ls"] and not sandboxed
    monkeypatch.setattr(shell, "bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(shell, "bwrap_status", lambda: "ok")
    argv, sandboxed = await shell.sandbox_argv("ls", ExecToolsConfig(sandbox="workspace"), writable=[ws])
    assert sandboxed and argv[0] == "/usr/bin/bwrap" and "--unshare-pid" in argv and argv[argv.index("--bind") + 1] == str(ws) and argv[-3:] == ["bash", "-lc", "ls"]
    # Patch the probe's answer, not only its cached value: ``_bwrap_state`` is re-probed for real
    # whenever ``_bwrap_probed_at`` is stale, so injecting the state alone made this test depend on
    # the machine — it asserted the fail-closed path only where bwrap genuinely does not work.
    monkeypatch.setattr(shell, "bwrap_status", lambda: "bwrap cannot create namespaces here")
    with pytest.raises(shell.SandboxUnavailable, match="configured .* but unavailable"):
        await shell.sandbox_argv("ls", ExecToolsConfig(sandbox="workspace"), writable=[ws])


def test_key_proxy_routing_and_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = _keyproxy_module()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dk")
    monkeypatch.setenv("KEYPROXY_UPSTREAM_MYLLM", "http://10.0.0.1:9000/v1/")
    monkeypatch.setenv("KEYPROXY_KEY_MYLLM", "")
    table = proxy.upstreams()
    assert table["deepseek"] == ("https://api.deepseek.com", "dk") and table["myllm"] == ("http://10.0.0.1:9000/v1", "")
    assert proxy.target_url("https://api.deepseek.com", "chat/completions", "a=1") == "https://api.deepseek.com/chat/completions?a=1"
    flag = tmp_path / "BUDGET_EXCEEDED"
    monkeypatch.setattr(proxy, "BUDGET_FLAG", flag)
    assert not proxy.budget_exceeded()
    flag.write_text("x")
    assert proxy.budget_exceeded()
    assert proxy.budget_exempt("user/balance") and not proxy.budget_exempt("chat/completions")


def test_signature_rejects_non_ascii_and_accepts_any_scheme_case() -> None:
    assert verify_signature("bearer", "tok", b"x", {"authorization": "Bearer \u00fc"}) is False
    assert verify_signature("github", "s", b"x", {"x-hub-signature-256": "sha256=\u00fc"}) is False
    assert verify_signature("bearer", "tok", b"x", {"authorization": "bearer tok"}) is True


def test_pathological_intent_patterns_are_bounded_and_refused() -> None:
    import time

    from daedalus.extensions.inbound import _NESTED_QUANTIFIER, search_bounded

    started = time.monotonic()
    assert search_bounded("(a+)+$", "a" * 40 + "b", seconds=0.5) is None
    # Twenty times the bound it was given: what is being told apart is "it stopped" from "it is
    # still backtracking", which without the bound is minutes and not a tenth of a second.
    assert time.monotonic() - started < 10.0
    assert search_bounded(r"build\s+failed", "the BUILD failed", seconds=1.0) is True
    assert search_bounded("(", "x", seconds=1.0) is None
    assert _NESTED_QUANTIFIER.search("(a+)+$") and _NESTED_QUANTIFIER.search(r"(\w*)*x") and not _NESTED_QUANTIFIER.search(r"build\s+failed|(foo)+")


async def test_intent_creation_rejects_overlong_and_nested_patterns(app: Any) -> None:
    inbound = Inbound(app)
    with pytest.raises(ValueError, match="limited"):
        await inbound.create_intent(pattern="(" + "x" * 500 + ")", action="a", session_id=None, cooldown_minutes=1, max_fires=1, expires_in_hours=None, created_by=None)
    with pytest.raises(ValueError, match="quantifier"):
        await inbound.create_intent(pattern="(a+)+$", action="a", session_id=None, cooldown_minutes=1, max_fires=1, expires_in_hours=None, created_by=None)


async def test_intent_in_the_receiving_session_rides_with_the_event(app: Any) -> None:
    inbound = Inbound(app)
    standing = await inbound.resolve_session(None, default_title="[inbound]")
    created = await inbound.create_intent(pattern="deploy", action="check the deploy log", session_id=standing.session.id, cooldown_minutes=1, max_fires=5, expires_in_hours=None, created_by=None)
    await inbound.deliver(source="ci", text="deploy finished", session_ref=None, default_title="[inbound]")
    assert len(app.submitted) == 1 and "check the deploy log" in app.submitted[0][1] and app.submitted[0][2] == "inbound:ci"
    row = await app.db.fetchone("SELECT fired_count FROM intents WHERE id = ?", (created["id"],))
    assert row["fired_count"] == 1
    # the standing session is remembered by id, not found by title
    assert await app.db.kv_get("inbound_session:[inbound]", None) == standing.session.id
    await inbound.record_delivery("ci", "d9")
    await inbound.forget_delivery("ci", "d9")
    assert await inbound.record_delivery("ci", "d9") is True


def test_flatten_payload_stays_linear_on_wide_dicts() -> None:
    import time

    started = time.monotonic()
    text = flatten_payload({f"k{i}": "v" for i in range(200_000)})
    # Linear against quadratic, which on two hundred thousand keys is minutes: a generous bound
    # answers that question and a tight one only measures how busy the host is.
    assert time.monotonic() - started < 10.0 and len(text) <= 2100


def test_mode_limits_are_bounded_and_zero_means_nothing() -> None:
    from pydantic import ValidationError

    from daedalus.config import ModeConfig

    with pytest.raises(ValidationError):
        ModeConfig(max_iterations=0)
    with pytest.raises(ValidationError):
        ModeConfig(usd_per_run=-1)
    assert ModeConfig(usd_per_run=0).usd_per_run == 0


async def test_sandbox_never_widens_to_the_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.config import ExecToolsConfig
    from daedalus.tools import shell

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(shell, "bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(shell, "_bwrap_state", "ok")
    argv, sandboxed = await shell.sandbox_argv("ls", ExecToolsConfig(sandbox="workspace"), writable=[ws])
    assert sandboxed and argv.count("--bind") == 1 and argv[argv.index("--bind") + 1] == str(ws)
    with pytest.raises(ValueError):
        ExecToolsConfig(sandbox_extra_writable=["/"])
    with pytest.raises(ValueError):
        ExecToolsConfig(sandbox_extra_writable=["/srv/state"])
    assert ExecToolsConfig(sandbox_extra_writable=["/srv/state/worktrees"]).sandbox_extra_writable == ["/srv/state/worktrees"]


def test_vendor_host_detection_uses_the_real_hostname() -> None:
    from daedalus.providers.registry import _is_vendor_host

    assert _is_vendor_host("deepseek", "https://user:pw@API.deepseek.com:443/v1#x")
    assert not _is_vendor_host("deepseek", "https://api.deepseek.com.evil.tld")
    assert not _is_vendor_host("deepseek", "http://keyproxy:3200/deepseek")


async def test_key_proxy_decodes_compressed_upstreams_and_replaces_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    import gzip

    from aiohttp.test_utils import TestClient, TestServer

    proxy = _keyproxy_module()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "real-key")
    seen: dict[str, Any] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["cookie"] = request.headers.get("cookie")
        seen["url"] = str(request.url)
        return httpx.Response(200, content=gzip.compress(b'{"data": [1, 2]}'), headers={"content-type": "application/json", "content-encoding": "gzip"})

    app = proxy.make_app()
    await app["client"].aclose()
    app["client"] = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/deepseek/v1/models?x=1", headers={"authorization": "Bearer leaked", "cookie": "c=1"})
        assert response.status == 200 and await response.json() == {"data": [1, 2]} and response.headers.get("content-encoding") is None
        assert seen["auth"] == "Bearer real-key" and seen["cookie"] is None and seen["url"] == "https://api.deepseek.com/v1/models?x=1"
        assert (await client.get("/nowhere/models")).status == 404
    assert proxy.budget_exempt("v1/user/balance") and proxy.budget_exempt("models") and not proxy.budget_exempt("chat/completions") and not proxy.budget_exempt("models_delete")


async def test_asr_can_borrow_a_configured_provider(tmp_path: Path) -> None:
    from daedalus.transport.telegram.voice import VOICE_NOTE_PREFIX, asr_configured, effective_asr, voice_note_text

    class Endpoint:
        base_url = "http://keys.local/openrouter/v1"
        api_key = "proxied"

    class Registry:
        def get(self, provider_id: str) -> Any:
            if provider_id != "openrouter":
                raise KeyError(provider_id)
            return SimpleNamespace(endpoint=Endpoint())

    manager = SimpleNamespace(providers=Registry())
    plain = AsrConfig(url="http://asr.local/v1", api_key="k")
    assert effective_asr(plain, manager) is plain
    borrowed = effective_asr(AsrConfig(provider="openrouter", model="whisper-large-v3"), manager)
    assert borrowed.url == "http://keys.local/openrouter/v1" and borrowed.api_key == "proxied" and borrowed.model == "whisper-large-v3"
    with pytest.raises(TranscriptionError, match="not configured"):
        effective_asr(AsrConfig(provider="nope"), manager)
    assert not asr_configured(AsrConfig()) and asr_configured(AsrConfig(provider="openrouter")) and asr_configured(AsrConfig(url="http://x"))
    # what the agent reads: the words, marked as a transcript, after the caption when there is one
    assert voice_note_text(" hello ") == f"{VOICE_NOTE_PREFIX}\nhello"
    assert voice_note_text("hello", "see the photo") == f"see the photo\n\n{VOICE_NOTE_PREFIX}\nhello"
