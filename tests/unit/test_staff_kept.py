"""How each staff setting is kept: by the host, by the CLI's own mode, only in the prompt, or not at all."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.extensions.staff_kept import how_kept


def member(harness: str, isolation: str = "shared", mode: str = "") -> Any:
    return SimpleNamespace(name="Vera", harness=harness, isolation=isolation, permission_mode=mode)


def folder(env: str = "container", readonly: bool = False) -> Any:
    return SimpleNamespace(env=env, readonly=readonly, local=lambda local_env: env == local_env)


def kept(m: Any, f: Any | None = None, level: str = "edits") -> dict[str, tuple[str, str, str]]:
    lines = how_kept(m, f or folder(), local_env="container", permission_level=level)
    return {line["setting"]: (line["kept"], line["reason"], line["mode"]) for line in lines}


@pytest.mark.parametrize(("harness", "mode"), [("claude", "plan"), ("codex", "read-only"), ("grok", "plan"), ("cursor", "ask")])
def test_a_read_only_cli_member_is_kept_by_its_cli_mode(harness: str, mode: str) -> None:
    assert kept(member(harness, "readonly", "acceptEdits" if harness == "claude" else ""))["folder"] == ("cli", "readonly", mode)


def test_a_read_only_member_of_a_cli_without_such_a_mode_is_not_kept_at_all() -> None:
    lines = kept(member("opencode", "readonly"))
    assert lines["folder"] == ("none", "readonly_refused", "")
    assert lines["scope"] == ("prompt", "brief", "")


def test_daedalus_members_are_walled_by_the_host_in_their_own_environment() -> None:
    assert kept(member("daedalus", "readonly"))["folder"] == ("host", "readonly", "")
    assert kept(member("daedalus", "worktree"))["folder"] == ("host", "worktree", "")
    assert kept(member("daedalus", "shared"))["folder"] == ("host", "shared", "")
    assert kept(member("daedalus", "shared"), folder(readonly=True))["folder"] == ("host", "readonly", "")
    assert kept(member("daedalus"))["asking"] == ("host", "policy", "")


def test_a_daedalus_member_in_a_host_folder_keeps_only_the_read_only_promise() -> None:
    # Its tools run through the host terminal with no walls of this process.
    assert kept(member("daedalus", "worktree"), folder(env="host"))["folder"] == ("prompt", "remote", "")
    assert kept(member("daedalus", "readonly"), folder(env="host"))["folder"] == ("host", "readonly", "")


def test_codex_keeps_its_folder_and_network_with_its_sandbox() -> None:
    lines = kept(member("codex", "worktree"))
    assert lines["folder"] == ("cli", "sandbox", "workspace-write")
    assert lines["network"] == ("cli", "off", "workspace-write")
    assert lines["asking"] == ("cli", "mode", "on-request")
    full = kept(member("codex", "shared", "danger-full-access"))
    assert full["folder"] == ("none", "anywhere", "danger-full-access")
    assert full["network"] == ("none", "open", "")
    assert full["asking"] == ("none", "bypass", "danger-full-access")


def test_a_cli_without_a_sandbox_is_only_asked_to_stay_in_its_folder() -> None:
    lines = kept(member("claude", "worktree"), level="ask")
    assert lines["folder"] == ("prompt", "brief_worktree", "")
    assert lines["asking"] == ("cli", "mode", "manual")
    assert lines["network"] == ("none", "open", "")
    assert kept(member("claude", "shared", "bypassPermissions"))["asking"] == ("none", "bypass", "bypassPermissions")
    assert kept(member("grok"), level="all")["asking"] == ("none", "bypass", "bypassPermissions")


def test_an_explicit_no_write_mode_is_kept_even_without_read_only_isolation() -> None:
    assert kept(member("claude", "shared", "plan"))["folder"] == ("cli", "readonly", "plan")


def test_pi_never_asks_and_opencode_follows_its_permission_rules() -> None:
    assert kept(member("pi"))["asking"] == ("none", "never", "")
    assert kept(member("opencode"), level="ask")["asking"] == ("cli", "rules", "ask")
