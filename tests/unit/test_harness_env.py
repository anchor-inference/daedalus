"""The environment a command-line agent is launched with: the traces of other tools removed, the
one variable that says where a host's Claude keeps its sign-in kept."""

from __future__ import annotations

from daedalus.harness.capabilities import CAPABILITIES
from daedalus.harness.env import DAEMON_STRIP, launch_environment, staff_inherited_env, terminal_environment

BASE = {
    "PATH": "/usr/bin",
    "HOME": "/home/someone",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_CONFIG_DIR": "/home/someone/.config/claude",
    "TMUX": "/tmp/tmux-1000/default,1,0",
    "TMUX_PANE": "%3",
    "TERM": "screen-256color",
    "LANG": "POSIX",
    "LC_ALL": "C",
}


def test_nesting_and_multiplexer_traces_go_and_the_claude_configuration_stays() -> None:
    env = launch_environment(BASE, {})
    assert env["CLAUDE_CONFIG_DIR"] == "/home/someone/.config/claude"
    for gone in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "TMUX", "TMUX_PANE"):
        assert gone not in env
    assert (env["PATH"], env["HOME"]) == ("/usr/bin", "/home/someone")
    assert (env["TERM"], env["COLORTERM"]) == ("xterm-256color", "truecolor")


def test_the_locale_becomes_utf8_only_when_it_is_not_already() -> None:
    env = launch_environment(BASE, {})
    assert env["LANG"] == "C.UTF-8" and "LC_ALL" not in env
    kept = launch_environment({"LANG": "ru_RU.UTF-8"}, {})
    assert kept["LANG"] == "ru_RU.UTF-8"


def test_the_updater_is_switched_off_and_the_launch_adds_its_identity() -> None:
    extra = {"DAEDALUS_LAUNCH_ID": "l-1", "DAEDALUS_HOOK_URL": "http://127.0.0.1:9/hook", "DAEDALUS_HOOK_TOKEN": "t"}
    env = launch_environment(BASE, extra, capabilities=CAPABILITIES["claude"])
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["DAEDALUS_LAUNCH_ID"] == "l-1"
    # The launch has the last word, even over the fixed settings.
    assert launch_environment(BASE, {"TERM": "dumb"})["TERM"] == "dumb"
    for caps in CAPABILITIES.values():
        switched = launch_environment({}, {}, capabilities=caps)
        assert all(switched[name] == value for name, value in caps.autoupdate_off)


def test_the_daemon_is_given_patterns_to_strip_and_values_to_set() -> None:
    spec = terminal_environment({"DAEDALUS_LAUNCH_ID": "l-1"}, capabilities=CAPABILITIES["opencode"])
    # CLAUDE* is left to the daemon, whose list keeps CLAUDE_CONFIG_DIR; a pattern passed here would not.
    assert spec.strip == DAEMON_STRIP and not any(p.startswith("CLAUDE") for p in spec.strip)
    assert spec.set == {"OPENCODE_DISABLE_AUTOUPDATE": "1", "DAEDALUS_LAUNCH_ID": "l-1"}
    # Terminal type and locale are the daemon's own rule, which keeps a UTF-8 locale the user chose.
    assert "LANG" not in spec.set and "TERM" not in spec.set


def test_staff_inheritance_selects_only_the_clis_configuration_locator() -> None:
    claude = staff_inherited_env("claude")
    codex = staff_inherited_env("codex")
    assert "CLAUDE_CONFIG_DIR" in claude and "CLAUDE_CONFIG_DIR" not in codex
    for names in (claude, codex):
        assert "HOME" in names and "PATH" in names
        assert not any("TOKEN" in name or "KEY" in name or "SECRET" in name for name in names)
