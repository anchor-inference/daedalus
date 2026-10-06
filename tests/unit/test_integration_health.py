"""An auth probe cannot infer provider permissions or leak credentials, and the Health screen's
integration rows say what is wrong without calling a model provider."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from daedalus.config import ProviderConfig
from daedalus.extensions.integration_health import (
    capability_matrix,
    github_auth_probe,
    github_row,
    integration_rows,
    mcp_rows,
    provider_rows,
)
from daedalus.stores.database import Database


async def test_probe_distinguishes_authenticated_expired_and_unknown() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/user"
        if request.headers["Authorization"] == "Bearer valid":
            return httpx.Response(200, json={"login": "someone"})
        return httpx.Response(401, json={"message": "Bad credentials"})

    transport = httpx.MockTransport(respond)
    good = await github_auth_probe("valid", transport=transport)
    assert good["state"] == "authenticated"
    assert "someone" not in str(good) and "valid" not in str(good)
    expired = await github_auth_probe("expired", transport=transport)
    assert expired["state"] == "needs_reconnect"
    absent = await github_auth_probe("", transport=transport)
    assert absent["reason"] == "no_token"
    unknown = await github_auth_probe("valid", transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    assert unknown["state"] == "unknown"


def test_matrix_does_not_infer_other_provider_parity() -> None:
    matrix = capability_matrix(github_configured=True, webhooks={"github"})
    assert matrix[0]["capabilities"]["pull_request_write"]
    assert not matrix[1]["capabilities"]["pull_request_write"]
    assert not matrix[2]["capabilities"]["issue_sync"]


def _settings(**over: str) -> SimpleNamespace:
    values = {"github_token": "", "deepseek_api_key": "", "openrouter_api_key": "", "vllm_api_key": ""} | over
    return SimpleNamespace(**values)


async def _refusal(db: Database, provider_id: str, failure_class: str, status: int, observed_at: str) -> None:
    """A refusal as the adapter records it, on a run that exists, since the table insists on one."""
    await db.execute("INSERT OR IGNORE INTO projects(id,name,created_at) VALUES ('project','work',?)", (observed_at,))
    await db.execute("INSERT OR IGNORE INTO sessions(id,tenant_id,title,created_at,last_message_at,metadata,project_id)"
                     " VALUES ('session','tenant','work',?,?,'{}','project')", (observed_at, observed_at))
    await db.execute("INSERT OR IGNORE INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                     " VALUES ('run','tenant','session','error',?,?)", (observed_at, observed_at))
    await db.execute("INSERT INTO provider_failure_observations(id,session_id,run_id,provider_id,provider_kind,model,"
                     "status,failure_class,evidence_digest,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (f"{provider_id}-{observed_at}", "session", "run", provider_id, "openai_compat", "model",
                      status, failure_class, "a" * 64, observed_at))


async def test_github_row_reads_the_probe_without_leaking_the_token() -> None:
    rejected = httpx.MockTransport(lambda _: httpx.Response(401))
    assert (await github_row("", probe=True))["state"] == "not_configured"
    assert (await github_row("secret-token", probe=False))["state"] == "configured"
    row = await github_row("secret-token", probe=True, transport=rejected)
    assert (row["state"], row["severity"]) == ("token_rejected", "fail")
    assert "secret-token" not in str(row)
    offline = await github_row("secret-token", probe=True, transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    assert (offline["state"], offline["severity"], offline["detail"]) == ("unreachable", "warn", "probe_rejected (HTTP 503)")


def test_mcp_rows_tell_a_failed_server_from_one_never_started() -> None:
    rows = mcp_rows([
        {"name": "calendar", "state": "ready", "connected": True, "tools": ["a", "b"], "error": None},
        {"name": "notes", "state": "auth_required", "connected": False, "tools": [], "error": "authorization required"},
        {"name": "search", "state": "unavailable", "connected": False, "tools": [], "error": "spawn failed: no such file"},
        {"name": "files", "state": "disabled", "connected": False, "tools": [], "error": None},
        {"name": "maps", "state": "connecting", "connected": False, "tools": [], "error": None},
    ])
    assert [(r["name"], r["state"], r["severity"]) for r in rows] == [
        ("calendar", "connected", "ok"),
        ("notes", "auth_required", "fail"),
        ("search", "error", "fail"),
        ("files", "idle", "info"),
        ("maps", "connecting", "info"),
    ]
    assert rows[2]["detail"] == "spawn failed: no such file"


async def test_provider_rows_report_a_missing_key_and_only_an_unanswered_refusal(db: Database) -> None:
    config = SimpleNamespace(providers={
        "deepseek": ProviderConfig(kind="deepseek", base_url="https://api.deepseek.com"),
        "router": ProviderConfig(kind="openrouter", base_url="https://openrouter.ai/api/v1", api_key="k"),
        "vendor": ProviderConfig(kind="openai_compat", name="Vendor", base_url="https://vendor.example/v1", api_key="k"),
        "local": ProviderConfig(kind="llamacpp", base_url="http://127.0.0.1:8080"),
    })
    await _refusal(db, "router", "auth", 401, "2026-10-05T10:00:00+00:00")
    await _refusal(db, "vendor", "quota", 429, "2026-10-05T09:00:00+00:00")
    # The vendor answered after its refusal, so the refusal is history, not the current state.
    await db.execute("INSERT INTO usage_events(at,provider_id,model,purpose,raw) VALUES (?,?,?,?,?)",
                     ("2026-10-05T11:00:00+00:00", "vendor", "model", "chat", "{}"))
    rows = await provider_rows(_settings(), config, db)
    assert [(r["name"], r["state"], r["severity"]) for r in rows] == [
        ("deepseek", "no_key", "fail"),
        ("router", "key_rejected", "fail"),
        ("Vendor", "ready", "ok"),
        ("local", "ready", "ok"),
    ]
    assert rows[1]["detail"].startswith("HTTP 401 at 2026-10-05T10:00:00")
    # The environment's key counts as the provider's own.
    assert (await provider_rows(_settings(deepseek_api_key="env"), SimpleNamespace(providers={"deepseek": config.providers["deepseek"]}), db))[0]["state"] == "ready"


async def test_integration_rows_come_in_card_order_and_never_call_a_provider(db: Database) -> None:
    calls: list[str] = []

    def github(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200)

    config = SimpleNamespace(providers={"vendor": ProviderConfig(kind="openai_compat", base_url="https://vendor.example/v1", api_key="k")})
    rows = await integration_rows(_settings(github_token="t"), config, db=db, probe=True, transport=httpx.MockTransport(github),
                                  mcp_status=[{"name": "notes", "state": "unavailable", "error": "bad token sk-abcdefghijklmnopqrstuvwxyz0123456789"}])
    assert [r["kind"] for r in rows] == ["github", "mcp", "provider"]
    assert rows[0]["state"] == "authenticated"
    assert calls == ["api.github.com"]
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in rows[1]["detail"]
