"""Text that is not in the machine's code page, and stops that come in the right order.

On Windows a pipe or a log file takes the locale's code page — cp1252 on an English machine —
unless Python runs in UTF-8 mode. `daedalus check` ended on the arrow of its no-model sentence, and
because it ended half-way it closed the database under the background work its manager had started.
"""

from __future__ import annotations

import io
import logging
import sys
from types import SimpleNamespace

import pytest

from daedalus import __main__ as cli
from daedalus.config import Settings


def cp1252_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", write_through=True)


def test_a_stream_in_a_code_page_takes_any_text_after_the_entry_point_fixes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = cp1252_stream(), cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    with pytest.raises(UnicodeEncodeError):
        print("Settings → Models")
    cli._utf8_streams()
    print("Settings → Models")
    print("Настройки — модели", file=sys.stderr)
    assert out.buffer.getvalue().decode("utf-8").rstrip().endswith("Settings → Models")  # type: ignore[attr-defined]
    assert "Настройки" in err.buffer.getvalue().decode("utf-8")  # type: ignore[attr-defined]


async def test_a_check_that_fails_half_way_closes_its_manager_before_the_database(settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(sys, "stdout", cp1252_stream())
    args = SimpleNamespace(state_dir=str(settings.state_dir), workspaces_dir=str(settings.workspaces_dir))
    with caplog.at_level(logging.WARNING), pytest.raises(UnicodeEncodeError):
        await cli.cmd_check(args)
    told = "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    # What the background work said when the database closed under it.
    assert "closed database" not in told and "no active connection" not in told and "transcript-index failed" not in told
