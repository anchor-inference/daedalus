"""Servers described in the ssh config reach the prompt; the supervisor installs the material with ssh's modes."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from daedalus.host import prompts

HELPER_DIR = Path(__file__).resolve().parents[2] / "launcher"


def test_described_hosts_are_listed_and_wildcards_skipped(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text(
        "# A small cloud VM: 2 vCPU, 12 GB RAM, ports 22/80/443 open.\n# Yours for hosting.\nHost oracle\n  HostName 203.0.113.9\n  User ubuntu\n\n"
        "Host *\n  ServerAliveInterval 30\n\nHost lab\n  HostName 10.0.0.2\n",
        encoding="utf-8",
    )
    hosts = prompts.ssh_hosts(config)
    assert hosts == [("oracle", "A small cloud VM: 2 vCPU, 12 GB RAM, ports 22/80/443 open. Yours for hosting."), ("lab", "")]
    section = prompts.environment_section(workspace=tmp_path, bot_repo=tmp_path, core_repo=tmp_path, model="m", ssh_hosts=hosts)
    assert "`ssh oracle` — A small cloud VM" in section and "`ssh lab`" in section
    assert prompts.ssh_hosts(tmp_path / "missing") == []


def test_supervisor_installs_ssh_material_with_the_modes_ssh_wants(tmp_path: Path, monkeypatch) -> None:
    source, home = tmp_path / "src", tmp_path / "home" / ".ssh"
    source.mkdir()
    (source / "id_test").write_text("PRIVATE", encoding="utf-8")
    (source / "id_test.pub").write_text("PUBLIC", encoding="utf-8")
    (source / "config").write_text("Host x\n", encoding="utf-8")
    (source / "known_hosts").write_text("x ssh-ed25519 AAAA\n", encoding="utf-8")
    monkeypatch.setenv("DAEDALUS_SSH_SOURCE", str(source))
    monkeypatch.setenv("DAEDALUS_SSH_HOME", str(home))
    spec = importlib.util.spec_from_file_location("supervisor", HELPER_DIR / "supervisor.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.install_ssh()
    assert (home / "id_test").read_text(encoding="utf-8") == "PRIVATE"
    assert oct(os.stat(home).st_mode & 0o777) == "0o700"
    assert oct(os.stat(home / "id_test").st_mode & 0o777) == "0o600"
    assert oct(os.stat(home / "config").st_mode & 0o777) == "0o644"
    assert oct(os.stat(home / "id_test.pub").st_mode & 0o777) == "0o644"
