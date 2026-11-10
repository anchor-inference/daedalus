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


PROBE = r'''
import importlib, os, pkgutil, signal, sys
import daedalus
names = sorted(m.name for m in pkgutil.walk_packages(daedalus.__path__, "daedalus.") if not m.name.endswith("__main__"))
for name in names:
    importlib.import_module(name)
for loaded in [m for m in sys.modules if m == "daedalus" or m.startswith("daedalus.")]:
    del sys.modules[loaded]
# What Windows lacks: the third-party and standard modules stay loaded as they are, and the host's own
# modules are loaded again over an os and a signal without these names.
for attribute in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK", "O_NOCTTY", "O_PATH", "getuid", "geteuid",
                  "getgid", "getpgid", "setsid", "setpgid", "killpg", "fork", "openpty", "getgroups", "chown", "fchown",
                  "fchmod", "sysconf", "statvfs", "mkfifo", "WNOHANG", "pipe2", "sched_getaffinity", "getloadavg", "uname"):
    if hasattr(os, attribute):
        delattr(os, attribute)
for attribute in ("SIGKILL", "SIGHUP", "SIGCHLD", "SIGUSR1", "SIGUSR2", "SIGQUIT", "SIGALRM", "SIGPIPE", "SIGSTOP", "SIGCONT"):
    if hasattr(signal, attribute):
        delattr(signal, attribute)
for module in POSIX_ONLY:
    sys.modules[module] = None
failed = []
for name in names:
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed.append(f"{name}: {type(exc).__name__}: {exc}")
print("\n".join(failed))
sys.exit(1 if failed else 0)
'''


def test_every_host_module_loads_where_the_posix_names_are_missing() -> None:
    # The import above was only the first: the next start on Windows stopped at os.O_DIRECTORY named at
    # the top of the same module. This loads every module of the host the way Windows would see it.
    import subprocess
    import sys

    probe = f"POSIX_ONLY = {sorted(POSIX_ONLY)!r}\n" + PROBE
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=600, cwd=ROOT)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
