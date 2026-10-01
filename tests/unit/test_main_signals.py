"""The bot's start must not ask for signals the platform does not have."""

from __future__ import annotations

import signal

import pytest

from daedalus.__main__ import _install_task_dump


def test_the_task_dump_is_skipped_where_there_is_no_sigusr1(monkeypatch: pytest.MonkeyPatch) -> None:
    # As on Windows: the attribute is simply missing, and the start goes on without the dump.
    monkeypatch.delattr(signal, "SIGUSR1", raising=False)
    _install_task_dump()


def test_the_task_dump_is_installed_where_there_is_sigusr1() -> None:
    if not hasattr(signal, "SIGUSR1"):
        pytest.skip("no SIGUSR1 here")
    before = signal.getsignal(signal.SIGUSR1)
    try:
        _install_task_dump()
        assert callable(signal.getsignal(signal.SIGUSR1))
    finally:
        signal.signal(signal.SIGUSR1, before)
