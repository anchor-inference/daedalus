"""The secrets the operator hands the agent: sealed at rest, substituted on the way out, masked on the way back."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from daedalus.security import operator_secrets
from daedalus.security.operator_secrets import OperatorSecrets, SecretError, prompt_section
from daedalus.security.redact import Redactor
from daedalus.stores.database import Database

PASSWORD = "Tr0ub4dor-and-3"


async def _scopes(db: Database) -> None:
    for project_id in ("proj", "elsewhere"):
        await db.execute("INSERT INTO projects(id,name,created_at,settings,system) VALUES(?,?,'2026-01-01','{}','')", (project_id, project_id))
    for session_id in ("chat", "other", "staff-chat"):
        await db.execute(
            "INSERT INTO sessions(id,tenant_id,title,created_at,last_message_at,project_id) VALUES(?,?,?,?,?,?)",
            (session_id, "operator", session_id, "2026-01-01", "2026-01-01", "proj" if session_id != "other" else "elsewhere"),
        )


@pytest.fixture
async def store(db: Database, tmp_path: Path) -> OperatorSecrets:
    await _scopes(db)
    secrets = OperatorSecrets(db, tmp_path / "secrets" / operator_secrets.KEY_FILE, Redactor(), files_dir=tmp_path / "runtime" / "secrets")
    secrets.project_of = {"chat": "proj", "staff-chat": "proj", "other": "elsewhere"}.get
    await secrets.load()
    return secrets


async def test_a_value_is_sealed_at_rest_and_opens_again(db: Database, store: OperatorSecrets, tmp_path: Path) -> None:
    secret = await store.put("session", "chat", "Router admin", PASSWORD, "ISP router web admin, user admin")
    assert secret.name == "router_admin" and secret.placeholder == "«secret:router_admin»"
    row = await db.fetchone("SELECT sealed, note FROM operator_secrets WHERE id = ?", (secret.id,))
    assert row is not None and PASSWORD.encode() not in bytes(row["sealed"])
    key = tmp_path / "secrets" / operator_secrets.KEY_FILE
    assert stat.S_IMODE(key.stat().st_mode) == 0o600 and len(key.read_bytes()) == 32
    # A fresh store, as after a restart, opens what the first sealed.
    again = OperatorSecrets(db, key, Redactor())
    await again.load()
    assert again.get(secret.id) is not None and again.get(secret.id).value == PASSWORD  # type: ignore[union-attr]
    assert "value" not in again.get(secret.id).public()  # type: ignore[union-attr]


async def test_a_sealed_value_moved_to_another_row_does_not_open(db: Database, store: OperatorSecrets) -> None:
    first = await store.put("session", "chat", "router_admin", PASSWORD)
    second = await store.put("session", "chat", "wifi", "another-value-1")
    await db.execute("UPDATE operator_secrets SET sealed = (SELECT sealed FROM operator_secrets WHERE id = ?) WHERE id = ?", (first.id, second.id))
    await store.load()
    assert store.get(second.id).value is None and store.get(second.id).public()["readable"] is False  # type: ignore[union-attr]
    assert [s.name for s in store.available("chat")] == ["router_admin"]


async def test_names_and_values_are_checked(store: OperatorSecrets) -> None:
    for bad in ("", "1abc", "a" * 49, "пароль"):
        with pytest.raises(SecretError):
            await store.put("session", "chat", bad, PASSWORD)
    with pytest.raises(SecretError):
        await store.put("session", "chat", "empty", "   ")
    with pytest.raises(SecretError):
        await store.put("team", "chat", "x", PASSWORD)


async def test_scope_a_chat_sees_its_own_and_its_projects(store: OperatorSecrets) -> None:
    await store.put("session", "chat", "router_admin", PASSWORD)
    await store.put("project", "proj", "nas", "nas-password-77")
    await store.put("session", "other", "elsewhere", "not-for-chat-1")
    assert [s.name for s in store.available("chat")] == ["nas", "router_admin"]
    assert [s.name for s in store.available("staff-chat")] == ["nas"]
    assert [s.name for s in store.available("other")] == ["elsewhere"]
    # A chat's own secret of the same name shadows the project's.
    await store.put("session", "staff-chat", "nas", "closer-value-9")
    assert store.available("staff-chat")[0].value == "closer-value-9"


async def test_substitution_puts_the_value_back_only_where_the_scope_allows(store: OperatorSecrets) -> None:
    await store.put("session", "chat", "router_admin", PASSWORD)
    arguments = {"text": " '«secret:router_admin»' ", "nested": ["user admin, pass «secret:router_admin»"], "n": 3}
    out, used = store.substitute(arguments, "chat", used_by="test")
    assert out == {"text": PASSWORD, "nested": [f"user admin, pass {PASSWORD}"], "n": 3}
    assert [s.name for s in used] == ["router_admin"]
    # Another chat names the same placeholder and gets nothing.
    out, used = store.substitute(arguments, "other", used_by="test")
    assert out == arguments and used == []
    await store.settle()


async def test_output_carrying_the_value_comes_back_as_the_placeholder(store: OperatorSecrets) -> None:
    await store.put("session", "chat", "router_admin", PASSWORD + "\nsecond line")
    redactor = store.redactor
    printed = f"login ok as admin:{PASSWORD}\nsecond line and more"
    assert redactor.redact(printed) == "login ok as admin:«secret:router_admin» and more"
    assert redactor.redact('{"password": "' + PASSWORD + '\\nsecond line"}') == '{"password": "«secret:router_admin»"}'
    assert redactor.redact("https://x/?p=Tr0ub4dor-and-3%0Asecond%20line") == "https://x/?p=«secret:router_admin»"
    # The placeholder itself is never masked, whatever key it sits under.
    assert redactor.redact('PASSWORD="«secret:router_admin»"') == 'PASSWORD="«secret:router_admin»"'


async def test_deletion_revokes_use_but_keeps_masking(store: OperatorSecrets) -> None:
    secret = await store.put("session", "chat", "router_admin", PASSWORD)
    assert "DAEDALUS_SECRET_ROUTER_ADMIN" in store.environment("chat")
    assert await store.delete(secret.id)
    assert store.environment("chat") == {}
    out, used = store.substitute({"text": "«secret:router_admin»"}, "chat", used_by="test")
    assert out == {"text": "«secret:router_admin»"} and used == []
    assert PASSWORD not in store.redactor.redact(f"echo {PASSWORD}")
    assert not list((store.files_dir or Path("/nonexistent")).rglob("router_admin"))


async def test_environment_and_file_for_a_process(store: OperatorSecrets) -> None:
    await store.put("session", "chat", "router_admin", PASSWORD)
    await store.put("project", "proj", "nas", "nas-password-77")
    env = store.environment("chat")
    assert env["DAEDALUS_SECRET_ROUTER_ADMIN"] == PASSWORD
    path = Path(env["DAEDALUS_SECRET_ROUTER_ADMIN_FILE"])
    assert path.read_text() == PASSWORD and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    # Narrowed to what a staff member was handed.
    assert set(store.environment("chat", names=["nas"])) == {"DAEDALUS_SECRET_NAS", "DAEDALUS_SECRET_NAS_FILE"}


async def test_use_is_audited(db: Database, store: OperatorSecrets) -> None:
    secret = await store.put("session", "chat", "router_admin", PASSWORD)
    store.substitute("«secret:router_admin»", "chat", used_by="BrowserAct in chat")
    await store.settle()
    row = await db.fetchone("SELECT last_used_by, uses, last_used_at FROM operator_secrets WHERE id = ?", (secret.id,))
    assert row is not None and row["last_used_by"] == "BrowserAct in chat" and row["uses"] == 1 and row["last_used_at"]


async def test_a_secret_whose_scope_is_gone_goes_with_it(db: Database, store: OperatorSecrets) -> None:
    await store.put("session", "other", "elsewhere", "not-for-chat-1")
    await db.execute("DELETE FROM sessions WHERE id = 'other'")
    await store.load()
    assert store.listed() == []


async def test_the_prompt_names_never_values(store: OperatorSecrets) -> None:
    await store.put("session", "chat", "router_admin", PASSWORD, "ISP router web admin, user admin")
    text = prompt_section(store.available("chat"))
    assert "«secret:router_admin»" in text and "ISP router web admin" in text and "$DAEDALUS_SECRET_ROUTER_ADMIN" in text
    assert PASSWORD not in text and "Never try to print" in text
    assert prompt_section([]) == ""
