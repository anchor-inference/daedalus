"""The supervisor takes the daily cap from the operator's settings, not from its own environment.

The cap once came from ``USD_PER_DAY`` in the container's environment: raising it in the app changed
nothing, and runs stayed refused at a cap the operator believed was gone.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "launcher" / "supervisor.py"


@pytest.fixture
def supervisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("supervisor_daily_cap_under_test", LAUNCHER)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["supervisor_daily_cap_under_test"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    monkeypatch.setattr(module, "CONFIG", tmp_path / "config.toml")
    monkeypatch.setattr(module, "log", lambda message: None)
    yield module
    sys.modules.pop("supervisor_daily_cap_under_test", None)


def test_the_cap_is_read_from_the_settings_file_each_time(supervisor, tmp_path: Path) -> None:
    assert supervisor.daily_cap() == 0.0
    (tmp_path / "config.toml").write_text("[limits]\nusd_per_day = 20\n", encoding="utf-8")
    assert supervisor.daily_cap() == 20.0
    (tmp_path / "config.toml").write_text("[limits]\nusd_per_day = 100.5\n", encoding="utf-8")
    assert supervisor.daily_cap() == 100.5


def test_the_environment_no_longer_sets_a_cap(supervisor, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USD_PER_DAY", "5")
    assert supervisor.daily_cap() == 0.0
    assert not hasattr(supervisor, "USD_PER_DAY")


def test_an_unreadable_file_means_no_cap_rather_than_a_crash(supervisor, tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[limits\nusd_per_day = ", encoding="utf-8")
    assert supervisor.daily_cap() == 0.0
