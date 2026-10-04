#!/usr/bin/env python3
"""Start a state consumer only when its configured encrypted filesystem is mounted."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

STATE = Path("/srv/state")
MARKER = ".encrypted-state-id"


def mounted_source(path: Path) -> str | None:
    """Return the exact mount source; a bind mount still exposes its backing filesystem."""
    target = str(path)
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        source_fields = after.split()
        if len(fields) >= 5 and len(source_fields) >= 2 and fields[4] == target:
            return source_fields[1]
    return None


def check_state_mount() -> None:
    required = os.environ.get("DAEDALUS_ENCRYPTED_STATE_REQUIRED", "")
    if required not in ("", "0", "1"):
        raise RuntimeError("invalid encrypted state configuration")
    expected_id = os.environ.get("DAEDALUS_ENCRYPTED_STATE_ID", "")
    expected_source = os.environ.get("DAEDALUS_ENCRYPTED_STATE_SOURCE", "")
    if required != "1":
        if expected_id or expected_source:
            raise RuntimeError("encrypted state settings require the mount guard")
        return
    if not re.fullmatch(r"[0-9a-f]{32}", expected_id) or not expected_source.startswith("/dev/mapper/"):
        raise RuntimeError("invalid encrypted state configuration")
    if mounted_source(STATE) != expected_source:
        raise RuntimeError("encrypted state mount is absent or has the wrong source")
    marker = STATE / MARKER
    if marker.is_symlink() or not marker.is_file() or marker.read_text().strip() != expected_id:
        raise RuntimeError("encrypted state mount identity does not match")


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--":
        print("usage: state_mount_guard.py -- command [args...]", file=sys.stderr)
        return 2
    try:
        check_state_mount()
    except (OSError, RuntimeError) as exc:
        print(f"state mount guard: {exc}", file=sys.stderr)
        return 1
    os.execvp(sys.argv[2], sys.argv[2:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
