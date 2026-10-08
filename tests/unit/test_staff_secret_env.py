"""A secret handed to a running coding CLI: what it is told matches what it has, Claude Code re-reads the
launch's secret files into variables before each command, and a value handed again replaces the old one."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.extensions.staff import Team
from daedalus.harness.claude import SECRET_CLASSIFIER_RULE, SECRET_ENV_HOOK, ClaudeCodeAdapter
from daedalus.security import operator_secrets
from daedalus.security.operator_secrets import ENV_SCRIPT, ENV_SCRIPT_FILE, Secret, staff_section
from tests.unit.test_harness_claude import spec


def _secret(name: str = "router_admin", value: str = "v-1", note: str = "") -> Secret:
    return Secret(id="s1", name=name, scope_kind="project", scope_id="p1", note=note, created_at="", updated_at="", value=value)


def test_a_member_told_mid_launch_is_told_only_what_its_environment_has() -> None:
    secret = _secret()
    started = staff_section([secret])
    assert '"$DAEDALUS_SECRET_ROUTER_ADMIN"' in started
    # A CLI whose environment is fixed: the file, and a plain word that the variable is not there.
    fixed = staff_section([secret], mid_launch=True)
    assert '"$DAEDALUS_LAUNCH_DIR/secret-router_admin"' in fixed
    assert '"$DAEDALUS_SECRET_ROUTER_ADMIN"' not in fixed and "never search the environment" in fixed
    # Claude Code re-reads the file into the variable, so both are named, the variable from the next command.
    live = staff_section([secret], mid_launch=True, live_variables=True)
    assert '"$DAEDALUS_SECRET_ROUTER_ADMIN" from your next command on' in live and "never search the environment" not in live
    # One the launch could not take is said to be missing, not left for the member to hunt.
    missing = staff_section([secret], mid_launch=True, live_variables=True, undelivered=["router_admin"])
    assert "not delivered" in missing and "$DAEDALUS_SECRET_ROUTER_ADMIN" not in missing.split("\n")[2]
    assert "v-1" not in started + fixed + live + missing


def test_a_claude_launch_sources_the_secret_script_and_tells_the_classifier_whose_secrets_they_are() -> None:
    plan = ClaudeCodeAdapter().launch_plan(spec(permission_mode="auto"))
    settings = json.loads(plan.files["settings.json"])
    commands = [hook["command"] for entry in settings["hooks"]["SessionStart"] for hook in entry["hooks"]]
    assert SECRET_ENV_HOOK in commands and any("hook SessionStart" in c for c in commands)
    assert plan.files[ENV_SCRIPT_FILE] == ENV_SCRIPT.encode()
    # The built-in rules stay; only the operator's handed secrets are added, and no permission rule grows.
    assert settings["autoMode"]["allow"] == ["$defaults", SECRET_CLASSIFIER_RULE]
    assert not any(rule.startswith("Bash") for rule in settings["permissions"]["allow"])
    assert settings.get("defaultMode") is None and "bypassPermissions" not in plan.argv


def _sh(env_file: Path, launch: Path, command: str) -> str:
    """One command the way Claude Code runs it: the session's environment file first, then the command."""
    script = env_file.read_text() + "\n" + command
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "DAEDALUS_LAUNCH_DIR": str(launch)}
    return subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, check=True).stdout


def test_the_session_start_line_makes_a_later_or_changed_secret_a_variable_of_the_next_command(tmp_path: Path) -> None:
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / ENV_SCRIPT_FILE).write_text(ENV_SCRIPT)
    env_file = tmp_path / "sessionstart-hook-0.sh"
    env_file.write_text("")
    subprocess.run(["sh", "-c", SECRET_ENV_HOOK], env={"PATH": os.environ.get("PATH", ""), "CLAUDE_ENV_FILE": str(env_file)}, check=True)
    # No secret yet: every command still runs.
    assert _sh(env_file, launch, 'echo "[${DAEDALUS_SECRET_ROUTER_ADMIN-unset}]"') == "[unset]\n"
    (launch / "secret-router_admin").write_text("first value")
    assert _sh(env_file, launch, 'echo "$DAEDALUS_SECRET_ROUTER_ADMIN|$DAEDALUS_SECRET_ROUTER_ADMIN_FILE"') == f"first value|{launch}/secret-router_admin\n"
    (launch / "secret-router_admin").write_text("corrected value\n")
    assert _sh(env_file, launch, 'printf "[%s]" "$DAEDALUS_SECRET_ROUTER_ADMIN"') == "[corrected value\n]"
    # Nothing but secret files becomes a variable, and the loop leaves no names of its own behind.
    (launch / "settings.json").write_text("{}")
    assert "_daedalus_" not in _sh(env_file, launch, "env")
    # Without the variable Claude Code sets, the hook does nothing and still succeeds.
    assert subprocess.run(["sh", "-c", SECRET_ENV_HOOK], env={"PATH": os.environ.get("PATH", "")}).returncode == 0


class _Term:
    def __init__(self, refuse: bool = False) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, bool]] = []
        self.refuse = refuse

    async def put_file(self, name: str, data: bytes, *, replace: bool = False) -> str:
        self.calls.append((name, replace))
        if self.refuse or (name in self.files and not replace):
            raise RuntimeError("file exists")
        self.files[name] = data
        return f"/launch/{name}"


def _team(term: _Term, adapter: Any) -> Any:
    live = SimpleNamespace(id="ss-1")

    async def live_of(_member: Any) -> Any:
        return live

    runtime = SimpleNamespace(sessions={"ss-1": SimpleNamespace(term=term)}, adapter=adapter)
    return SimpleNamespace(live_of=live_of, runtimes={"claude": runtime, "codex": runtime})


@pytest.mark.parametrize("harness, live", [("claude", True), ("codex", False)])
async def test_a_secret_handed_again_replaces_the_running_launchs_file(harness: str, live: bool) -> None:
    term = _Term()
    team = _team(term, ClaudeCodeAdapter() if live else SimpleNamespace())
    member = SimpleNamespace(harness=harness, name="port")
    await Team.secrets_note(team, member, [_secret(value="old")])
    note = await Team.secrets_note(team, member, [_secret(value="new")])
    assert term.files["secret-router_admin"] == b"new"
    assert term.calls == [("secret-router_admin", True), ("secret-router_admin", True)]
    assert ("from your next command on" in note) is live and ("never search the environment" in note) is not live


async def test_a_launch_that_refuses_the_file_is_reported_not_hidden() -> None:
    team = _team(_Term(refuse=True), ClaudeCodeAdapter())
    note = await Team.secrets_note(team, SimpleNamespace(harness="claude", name="port"), [_secret()])
    assert "not delivered to this running session" in note


def test_the_launch_file_prefix_is_what_the_script_reads() -> None:
    assert operator_secrets.LAUNCH_FILE_PREFIX == "secret-" and "secret-*) ;;" in ENV_SCRIPT
