"""The order a call tries its subscription accounts in, and the sign-in a new account is opened with."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daedalus.extensions.api_subscription_accounts import account_home, sign_in_command
from daedalus.stores.database import Database
from daedalus.stores.subscription_accounts import SubscriptionAccounts


async def test_one_account_is_the_default_login_and_nothing_is_named(db: Database) -> None:
    store = SubscriptionAccounts(db)
    await store.load()
    assert [a.id for a in store.accounts("claude")] == ["default"]
    assert store.several("claude") is False
    assert store.chain("claude") == ["default"]


async def test_the_chain_starts_where_the_model_or_the_session_says_and_skips_a_resting_account(db: Database) -> None:
    store = SubscriptionAccounts(db)
    await store.load()
    work = await store.add("claude", "work")
    spare = await store.add("claude", "spare")
    assert store.chain("claude") == ["default", work.id, spare.id]
    assert store.chain("claude", preferred=spare.id) == [spare.id, "default", work.id]
    assert store.chain("claude", preferred="gone") == ["default", work.id, spare.id], "a removed account is the default again"

    await store.set_choice("claude", default_account=work.id)
    assert store.chain("claude")[0] == work.id

    # A session that moved to the spare stays there, for its cache, until that account rests too.
    store.served("claude", spare.id, "s1")
    assert store.chain("claude", session_id="s1")[0] == spare.id
    assert store.chain("claude", session_id="s2")[0] == work.id

    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await store.rest("claude", spare.id, "quota", later)
    assert store.chain("claude", session_id="s1") == [work.id, "default"]
    assert store.chain("claude", session_id="s1", at=datetime.now(UTC) + timedelta(hours=2))[0] == spare.id, "a rest ends when it says"

    await store.rest("claude", work.id, "auth", None)
    await store.rest("claude", "default", "rate", later)
    assert store.chain("claude", session_id="s1") == [spare.id], "every account resting: the first is tried for the vendor's own answer"

    await store.set_choice("claude", fallover=False)
    assert store.chain("claude", session_id="s1") == [work.id], "with fallover off the model's own account is all there is"


async def test_the_choices_survive_a_restart_and_a_removed_default_falls_back(db: Database) -> None:
    store = SubscriptionAccounts(db)
    await store.load()
    work = await store.add("codex", "work")
    await store.update("codex", "default", label="personal")
    await store.set_choice("codex", default_account=work.id, fallover=False)
    again = SubscriptionAccounts(db)
    await again.load()
    assert [(a.id, a.label) for a in again.accounts("codex")] == [("default", "personal"), (work.id, "work")]
    assert again.choice("codex").default_account == work.id and again.choice("codex").fallover is False
    await again.remove("codex", work.id)
    assert again.choice("codex").default_account == "default"


def test_the_sign_in_points_the_cli_at_the_accounts_own_home_on_every_system() -> None:
    assert account_home("/home/someone", "codex", "w1") == "/home/someone/.codex/daedalus-accounts/w1"
    assert account_home("/Users/someone", "claude", "default") == "/Users/someone/.claude"
    assert account_home("C:\\Users\\someone", "grok", "w1") == "C:\\Users\\someone\\.grok\\daedalus-accounts\\w1"
    assert sign_in_command("codex", "w1", "/home/someone") == 'CODEX_HOME="/home/someone/.codex/daedalus-accounts/w1" codex login --device-auth'
    assert sign_in_command("claude", "w1", "C:\\Users\\someone") == '$env:CLAUDE_CONFIG_DIR="C:\\Users\\someone\\.claude\\daedalus-accounts\\w1"; claude auth login --claudeai'
    assert sign_in_command("grok", "default", "/home/someone") == "grok login --device-auth"
