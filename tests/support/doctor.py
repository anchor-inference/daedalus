"""The doctor's one probe that leaves the machine, answered in place for tests that run the doctor.

In server mode the git probe proves the push right with ``git push --dry-run`` against each
checkout's real ``origin``. The suite's guard against the network watches this process's sockets, so
the subprocess slipped past it: every test that ran the doctor asked GitHub twice, about a second a
time, and on a machine without the credentials or the network it would get a different answer than
the one the test was written against. The push is answered as an accepted dry run, which is what a
checkout with a push right hears; every other git command still runs against the checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus import doctor


def answer_the_push_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the doctor's push dry run an accepted answer instead of sending it to the remote."""
    real = doctor._git_cmd

    def git_cmd(repo: Path, *args: str) -> tuple[int, str]:
        if args[:2] == ("push", "--dry-run"):
            return 0, f"To {repo.name}\n * [new branch]      {args[-1].replace(':refs/heads/', ' -> ')}"
        return real(repo, *args)

    monkeypatch.setattr(doctor, "_git_cmd", git_cmd)
