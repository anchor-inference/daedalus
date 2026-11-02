"""Every CLI's transcript lies under a root the host asks the daemon to let it read.

The daemon reads nothing outside its roots, and the host names, of each environment's home, only
``CATALOG_ROOTS``. The paths below are the transcripts the real CLIs reported in the recorded
sessions (a hook's ``transcript_path``, a Codex thread's ``path``, pi's ``sessionFile``): each must be
readable under those roots, whatever the home is called. OpenCode keeps its transcripts in its own
store and is read through ``opencode export``, which the daemon's program list allows.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from daedalus.extensions.harness import CATALOG_ROOTS, catalog_roots

RECORDED = Path(__file__).resolve().parents[1] / "support" / "fake_cli" / "recorded"
DAEMON_PROGRAMS = Path(__file__).resolve().parents[2] / "ptyd" / "sidechan" / "allow.go"


def _recorded(cli: str, name: str, key: str) -> list[str]:
    text = (RECORDED / cli / name).read_text()
    return sorted(set(re.findall(rf'"{key}": *"([^"]+)"', text)))


TRANSCRIPTS = [
    ("claude", _recorded("claude", "hooks.jsonl", "transcript_path")),
    ("codex", [p for p in _recorded("codex", "app_server.jsonl", "path") if "/sessions/" in p]),
    ("pi", _recorded("pi", "bridge.jsonl", "sessionFile")),
    ("grok", _recorded("grok", "hooks.jsonl", "transcript_path")),
]


@pytest.mark.parametrize(("cli", "paths"), TRANSCRIPTS, ids=[cli for cli, _ in TRANSCRIPTS])
def test_a_recorded_transcript_is_under_the_roots(cli: str, paths: list[str]) -> None:
    assert paths, f"no transcript path recorded for {cli}"
    for path in paths:
        home = re.match(r"^(/home/[^/]+|/root)/", path)
        assert home is not None, path
        roots = catalog_roots(home.group(1))
        assert any(path.startswith(root + "/") for root in roots), f"{cli}: {path} is outside {roots}"


def test_the_roots_are_directories_of_a_home_and_never_the_home_itself() -> None:
    for root in CATALOG_ROOTS:
        assert root and not root.startswith("/") and root not in (".", "..") and ".." not in root.split("/")
    assert catalog_roots("/home/someone/") == [f"/home/someone/{root}" for root in CATALOG_ROOTS]


def test_opencode_is_read_through_a_program_the_daemon_runs() -> None:
    allowed = re.search(r"DefaultExecAllow = \[\]string\{([^}]*)\}", DAEMON_PROGRAMS.read_text(), re.S)
    assert allowed is not None
    assert '"opencode"' in allowed.group(1)
    assert json.loads((RECORDED / "opencode" / "export.json").read_text())
