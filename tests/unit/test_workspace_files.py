"""Portable files are explicit, bounded, and never read through filesystem links."""

from __future__ import annotations

import hashlib
import os

import pytest

from daedalus.extensions import workspace_files
from daedalus.extensions.workspace_files import (
    WorkspaceFileRefused,
    capture_selected,
    stage_selected,
    validate_selected,
)


def test_selected_relative_file_roundtrip(tmp_path) -> None:
    source = tmp_path / "source"
    (source / "notes" / "sub").mkdir(parents=True)
    (source / "notes" / "sub" / "draft.txt").write_bytes(b"Draft with arbitrary relative layout")
    records, content = capture_selected({"folder": source}, [{"folder_id": "folder", "path": "notes/sub/draft.txt"}])
    validate_selected(records, content, {"folder"})
    target, created = stage_selected(tmp_path / "restored", "a" * 32, records, content, {"folder"})
    assert created
    assert (target / "folder" / "notes" / "sub" / "draft.txt").read_bytes() == b"Draft with arbitrary relative layout"
    assert stage_selected(tmp_path / "restored", "a" * 32, records, content, {"folder"}) == (target, False)


@pytest.mark.parametrize("path", ["../secret", "notes/../../secret", "/absolute", "a//b", "a/./b", "a\\b", "", ".git/config", "notes/.ssh/key"])
def test_selected_paths_reject_escape(tmp_path, path) -> None:
    with pytest.raises(WorkspaceFileRefused):
        capture_selected({"folder": tmp_path}, [{"folder_id": "folder", "path": path}])


def test_selected_paths_refuse_symlink_at_each_level(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("no")
    (source / "linked").symlink_to(outside)
    (source / "direct").symlink_to(outside / "secret")
    for path in ("linked/secret", "direct"):
        with pytest.raises(WorkspaceFileRefused):
            capture_selected({"folder": source}, [{"folder_id": "folder", "path": path}])


def test_selected_file_manifest_rejects_changed_bytes_and_extra_entry(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_bytes(b"a")
    records, content = capture_selected({"folder": source}, [{"folder_id": "folder", "path": "a.txt"}])
    with pytest.raises(WorkspaceFileRefused):
        validate_selected(records, {"folder/a.txt": b"changed"}, {"folder"})
    with pytest.raises(WorkspaceFileRefused):
        validate_selected(records, {**content, "folder/extra.txt": b"extra"}, {"folder"})
    assert records[0]["sha256"] == hashlib.sha256(b"a").hexdigest()


def test_existing_restore_tree_refuses_changed_bytes(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_bytes(b"a")
    records, content = capture_selected({"folder": source}, [{"folder_id": "folder", "path": "a.txt"}])
    target, _ = stage_selected(tmp_path / "restored", "a" * 32, records, content, {"folder"})
    (target / "folder" / "a.txt").write_bytes(b"wrong")
    with pytest.raises(WorkspaceFileRefused):
        stage_selected(tmp_path / "restored", "a" * 32, records, content, {"folder"})


def test_restore_root_refuses_symlinked_ancestor(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside)
    with pytest.raises(WorkspaceFileRefused):
        stage_selected(tmp_path / "linked" / "restored", "a" * 32, [], {}, {"folder"})
    assert list(outside.iterdir()) == []


def test_restore_removes_published_tree_if_directory_sync_fails(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_bytes(b"a")
    records, content = capture_selected({"folder": source}, [{"folder_id": "folder", "path": "a.txt"}])
    original = os.fsync
    calls = 0

    def fail_directory_sync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory sync failed")
        original(descriptor)

    monkeypatch.setattr(workspace_files.os, "fsync", fail_directory_sync)
    with pytest.raises(OSError, match="directory sync failed"):
        stage_selected(tmp_path / "restored", "a" * 32, records, content, {"folder"})
    assert not (tmp_path / "restored" / ("a" * 32)).exists()
