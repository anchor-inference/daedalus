"""Saving the settings writes what differs from the defaults, privately, and loads back the same."""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path

from daedalus.config import SEEDS, RuntimeConfig


def _written(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def test_a_save_writes_only_what_differs_from_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = RuntimeConfig(seeded=list(SEEDS))  # as a new file starts, so loading applies no seed
    config.compaction.auto_ratio = 0.42
    config.save(path)
    raw = _written(path)
    assert raw["compaction"] == {"auto_ratio": 0.42}
    # A section left at its defaults is not written at all, so a default changed in the code reaches
    # this installation; before, the first save froze every default into the file.
    assert "scheduler" not in raw and "telegram" not in raw
    assert RuntimeConfig.load(path) == config


def test_a_map_with_default_entries_is_written_whole_so_a_removal_survives(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = RuntimeConfig(seeded=list(SEEDS))
    removed = next(iter(config.providers))
    del config.providers[removed]
    config.save(path)
    loaded = RuntimeConfig.load(path)
    assert removed not in loaded.providers
    assert loaded.providers == config.providers


def test_the_file_is_private_and_replaced_whole(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("# an old file\n")
    path.chmod(0o644)
    RuntimeConfig().save(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp*"))
