"""Codex reads at most 32 KiB of a project's AGENTS.md files and drops the rest without a word.

The doctor and a Codex launch say so instead, and the other CLIs get an output-token ceiling high
enough that a large file written in one call is not cut.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import HarnessConfig, RuntimeConfig, Settings
from daedalus.doctor import DoctorContext, run_checks
from daedalus.harness.codex import INSTRUCTIONS_LIMIT, CodexAdapter, instructions_size
from daedalus.harness.runtime import CliStaffRuntime
from daedalus.stores.database import Database
from daedalus.stores.projects import ProjectStore


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_the_size_counts_every_agents_file_from_the_repository_top_down_to_the_folder(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / "AGENTS.md").write_bytes(b"a" * 20_000)
    (repo / "api").mkdir()
    (repo / "api" / "AGENTS.md").write_bytes(b"b" * 15_000)
    (tmp_path / "AGENTS.md").write_bytes(b"outside the repository, not read")

    assert instructions_size(repo) == 20_000
    assert instructions_size(repo / "api") == 35_000 > INSTRUCTIONS_LIMIT
    assert instructions_size(tmp_path / "missing") == 0


def test_an_override_file_takes_the_place_of_agents_md_in_its_directory(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / "AGENTS.md").write_bytes(b"a" * 40_000)
    (repo / "AGENTS.override.md").write_bytes(b"o" * 100)

    assert instructions_size(repo) == 100


async def test_the_doctor_warns_about_a_project_whose_agents_md_codex_would_cut(settings: Settings, db: Database, tmp_path: Path) -> None:
    big = _repo(tmp_path / "home" / "big")
    (big / "AGENTS.md").write_bytes(b"x" * (INSTRUCTIONS_LIMIT + 1))
    small = _repo(tmp_path / "home" / "small")
    (small / "AGENTS.md").write_bytes(b"short\n")
    store = ProjectStore(db)
    await store.create("Big", [str(big)])
    await store.create("Small", [str(small)])

    checks = await run_checks(DoctorContext(settings=settings, config=RuntimeConfig(), db=db))

    found = [c for c in checks if c.name == "project instructions"]
    assert len(found) == 1 and not found[0].ok and found[0].severity == "warn"
    assert "Big" in found[0].message and "Small" not in found[0].message


class _Terminals:
    def subscribe(self, callback: Any) -> Any:
        return lambda: None


def _runtime(adapter: Any, **config: Any) -> CliStaffRuntime:
    cfg = HarnessConfig(**config)
    return CliStaffRuntime(adapter, terminals=_Terminals(), store=SimpleNamespace(), ingress=SimpleNamespace(), lookup=lambda _id: None, config=lambda: cfg)  # type: ignore[arg-type]


def _request(cwd: Path) -> Any:
    staff = SimpleNamespace(name="Ada", role="writer", id="s1")
    return SimpleNamespace(
        staff=staff, project=SimpleNamespace(name="Bakery", id="p1"), task=None, worktree=None, cwd=cwd, env="container", brief_text="",
        first_message="", model="", effort="", agent="", permission_mode="", permission_level="ask", allow_rules=(),
    )


def test_the_configured_output_ceiling_reaches_the_launch_spec(tmp_path: Path) -> None:
    assert HarnessConfig().max_output_tokens == 64_000
    spec = _runtime(CodexAdapter(), max_output_tokens=100_000)._spec(_request(tmp_path), "l1", "")
    assert spec.max_output_tokens == 100_000


async def test_a_codex_launch_into_a_folder_with_oversized_instructions_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / "AGENTS.md").write_bytes(b"x" * (INSTRUCTIONS_LIMIT + 1))
    with caplog.at_level(logging.WARNING):
        await _runtime(CodexAdapter())._warn_long_instructions(_request(repo))
    assert any("AGENTS.md" in r.getMessage() and "32" in r.getMessage() for r in caplog.records)
