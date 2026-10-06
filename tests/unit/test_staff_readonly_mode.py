"""A read-only command-line member starts in its CLI's own no-write mode, or does not start."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from daedalus.extensions.staff import StaffError, Team


def member(harness: str, isolation: str = "readonly", mode: str = "") -> SimpleNamespace:
    return SimpleNamespace(name="Vera", harness=harness, isolation=isolation, permission_mode=mode)


@pytest.mark.parametrize(("harness", "expected"), [("claude", "plan"), ("codex", "read-only"),
                                                   ("grok", "plan"), ("cursor", "ask")])
def test_read_only_members_start_in_the_cli_no_write_mode(harness: str, expected: str) -> None:
    assert Team.launch_permission_mode(member(harness)) == expected  # type: ignore[arg-type]


def test_a_writing_mode_does_not_survive_read_only_isolation() -> None:
    assert Team.launch_permission_mode(member("claude", mode="acceptEdits")) == "plan"  # type: ignore[arg-type]
    assert Team.launch_permission_mode(member("cursor", mode="plan")) == "plan"  # type: ignore[arg-type]


def test_shared_and_native_members_keep_their_own_mode() -> None:
    assert Team.launch_permission_mode(member("claude", "shared", "acceptEdits")) == "acceptEdits"  # type: ignore[arg-type]
    assert Team.launch_permission_mode(member("daedalus")) == ""  # type: ignore[arg-type]


def test_a_cli_without_a_no_write_mode_is_refused() -> None:
    with pytest.raises(StaffError, match="no mode that keeps it from writing"):
        Team.launch_permission_mode(member("opencode"))  # type: ignore[arg-type]
