"""The host starts where the POSIX-only modules do not exist.

A top-level ``import fcntl`` in the guest sandbox once kept the native Windows host from starting at all,
and only the Windows first-run smoke of a release caught it. Each of these modules is fine imported lazily
inside the code path that needs it, or under a platform check; bare at the top of a module, it stops the
whole application from loading on Windows.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POSIX_ONLY = {"fcntl", "termios", "pwd", "grp", "resource", "pty", "tty"}


def bare_imports(path: Path) -> list[str]:
    found = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        else:
            continue
        found += [f"{path.relative_to(ROOT)}:{node.lineno} {name}" for name in names if name in POSIX_ONLY]
    return found


def test_no_module_of_the_host_imports_a_posix_only_module_at_its_top() -> None:
    found = [hit for folder in ("daedalus", "launcher") for path in sorted((ROOT / folder).rglob("*.py")) for hit in bare_imports(path)]
    assert not found, "import these lazily or under a platform check:\n" + "\n".join(found)
