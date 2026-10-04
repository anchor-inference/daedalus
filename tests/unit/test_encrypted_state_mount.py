"""Encrypted state must refuse a missing mount before either container starts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("state_mount_guard", ROOT / "launcher" / "state_mount_guard.py")
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def test_state_guard_requires_the_mapped_source_and_matching_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    identity = "a" * 32
    (tmp_path / ".encrypted-state-id").write_text(identity + "\n")
    monkeypatch.setattr(guard, "STATE", tmp_path)
    monkeypatch.setenv("DAEDALUS_ENCRYPTED_STATE_REQUIRED", "1")
    monkeypatch.setenv("DAEDALUS_ENCRYPTED_STATE_ID", identity)
    monkeypatch.setenv("DAEDALUS_ENCRYPTED_STATE_SOURCE", "/dev/mapper/state")
    monkeypatch.setattr(guard, "mounted_source", lambda path: "/dev/mapper/state")
    guard.check_state_mount()

    monkeypatch.setattr(guard, "mounted_source", lambda path: "/dev/mapper/other")
    with pytest.raises(RuntimeError, match="wrong source"):
        guard.check_state_mount()

    monkeypatch.setattr(guard, "mounted_source", lambda path: "/dev/mapper/state")
    (tmp_path / ".encrypted-state-id").write_text("b" * 32)
    with pytest.raises(RuntimeError, match="identity"):
        guard.check_state_mount()

    (tmp_path / ".encrypted-state-id").unlink()
    with pytest.raises(RuntimeError, match="identity"):
        guard.check_state_mount()


def test_state_guard_rejects_incomplete_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEDALUS_ENCRYPTED_STATE_REQUIRED", "1")
    monkeypatch.delenv("DAEDALUS_ENCRYPTED_STATE_ID", raising=False)
    monkeypatch.setenv("DAEDALUS_ENCRYPTED_STATE_SOURCE", "/dev/mapper/state")
    with pytest.raises(RuntimeError, match="configuration"):
        guard.check_state_mount()

    monkeypatch.setenv("DAEDALUS_ENCRYPTED_STATE_REQUIRED", "0")
    with pytest.raises(RuntimeError, match="require the mount guard"):
        guard.check_state_mount()
    monkeypatch.delenv("DAEDALUS_ENCRYPTED_STATE_SOURCE")
    monkeypatch.setenv("DAEDALUS_ENCRYPTED_STATE_ID", "a" * 32)
    with pytest.raises(RuntimeError, match="require the mount guard"):
        guard.check_state_mount()
    monkeypatch.delenv("DAEDALUS_ENCRYPTED_STATE_ID")
    guard.check_state_mount()


def test_both_state_consumers_keep_the_guard_when_the_overlay_is_omitted() -> None:
    base = yaml.safe_load((ROOT / "deploy" / "compose.yaml").read_text())["services"]
    overlay = yaml.safe_load((ROOT / "deploy" / "compose.encrypted-state.yaml").read_text())["services"]
    for name in ("daedalus", "keyproxy"):
        service = base[name]
        assert service["entrypoint"][:2] == ["python3", "/opt/launcher/state_mount_guard.py"]
        assert service["environment"]["DAEDALUS_ENCRYPTED_STATE_REQUIRED"] == "${DAEDALUS_ENCRYPTED_STATE_REQUIRED:-0}"
        assert any(volume.startswith("daedalus-state:/srv/state") for volume in service["volumes"])
        state_bind = overlay[name]["volumes"][0]
        assert state_bind["target"] == "/srv/state"
        assert state_bind["bind"]["create_host_path"] is False
        assert overlay[name]["environment"]["DAEDALUS_ENCRYPTED_STATE_REQUIRED"] == "1"
