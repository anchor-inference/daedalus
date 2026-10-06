"""Architecture invariants, as tests.

A self-modifying agent can dissolve its own layering one accepted change at a time. These
tests pin the boundaries: which packages may import the Telegram or HTTP frameworks, that
the launcher and the core know nothing about the host, and that every in-function import
states why it is not at the top of the module.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "daedalus"
CORE = ROOT.parent / "protocore-exp" / "protocore"

TELEGRAM_ONLY = ("aiogram",)
HTTP_FRAMEWORK = ("fastapi", "uvicorn", "starlette")
NO_UPWARD = ("daedalus.transport", "daedalus.extensions", "daedalus.app", *TELEGRAM_ONLY, *HTTP_FRAMEWORK)


def _modules(base: Path):  # type: ignore[no-untyped-def]
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _package_of(path: Path) -> str:
    """Dotted package a module belongs to (``daedalus/host/x.py`` → ``daedalus.host``)."""
    parts = path.relative_to(ROOT).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts[:-1])


def _imports(tree: ast.AST, path: Path):  # type: ignore[no-untyped-def]
    """Runtime imports, resolved to absolute names: relative imports and ``importlib.import_module``
    with a literal count too. An ``if TYPE_CHECKING:`` block never executes, so it crosses no boundary."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            skip.update(id(n) for n in ast.walk(node))
    package = _package_of(path)
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = ".".join(package.split(".")[: len(package.split(".")) - node.level + 1])
                name = f"{base}.{node.module}" if node.module else base
            else:
                name = node.module or ""
            if name:
                yield name, node
        elif isinstance(node, ast.Call):
            func = node.func
            literal = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) else None
            if literal and ((isinstance(func, ast.Attribute) and func.attr == "import_module") or (isinstance(func, ast.Name) and func.id == "__import__")):
                yield literal, node


def _violations(base: Path, forbidden: tuple[str, ...], *, allowed_dirs: tuple[str, ...] = ()) -> list[str]:
    out = []
    for path, tree in _modules(base):
        rel = path.relative_to(ROOT).as_posix()
        if any(rel.startswith(d) for d in allowed_dirs):
            continue
        for name, node in _imports(tree, path):
            if any(name == f or name.startswith(f + ".") for f in forbidden):
                out.append(f"{rel}:{node.lineno} imports {name}")
    return out


def test_only_the_telegram_transport_imports_aiogram() -> None:
    assert _violations(PKG, TELEGRAM_ONLY, allowed_dirs=("daedalus/transport/telegram/",)) == []


def test_only_the_api_modules_import_the_http_framework() -> None:
    assert _violations(PKG, HTTP_FRAMEWORK, allowed_dirs=("daedalus/extensions/api.py", "daedalus/extensions/api_projects.py", "daedalus/extensions/api_harnesses.py", "daedalus/extensions/api_staff.py", "daedalus/extensions/api_files.py", "daedalus/extensions/api_browsers.py", "daedalus/extensions/api_workspace.py", "daedalus/extensions/api_diagrams.py")) == []


def test_lower_layers_do_not_import_upward() -> None:
    for layer in ("tools", "providers", "stores", "security", "mcp", "host"):
        assert _violations(PKG / layer, NO_UPWARD) == [], layer


def test_redaction_depends_on_nothing_in_the_host() -> None:
    assert _violations(PKG / "security", ("daedalus",)) == []


def test_launcher_knows_nothing_about_the_host_package() -> None:
    assert _violations(ROOT / "launcher", ("daedalus",)) == []


def test_core_never_mentions_the_host() -> None:
    if not CORE.exists():
        pytest.skip(f"core checkout not found at {CORE}")
    hits = [p.relative_to(CORE).as_posix() for p in CORE.rglob("*.py") if "daedalus" in p.read_text(encoding="utf-8", errors="ignore")]
    assert hits == []


def test_environment_is_read_in_one_place() -> None:
    # settings come from daedalus.config.Settings; these modules only pass the environment on to child processes.
    # config.py is the one place itself: a handful of defaults depend on what kind of installation this is —
    # a container or the operator's own machine — and that is decided before any configuration is read.
    # The Cursor bridge copies inherited variables into its child ACP process; it does not read settings.
    allowed = {"daedalus/config.py", "daedalus/tools/shell.py", "daedalus/tools/verify.py", "daedalus/host/gitrun.py", "daedalus/host/checkpoints.py", "daedalus/harness/cursor_bridge.py"}
    hits = []
    for path, tree in _modules(PKG):
        rel = path.relative_to(ROOT).as_posix()
        if rel in allowed:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "putenv") and isinstance(node.value, ast.Name) and node.value.id == "os":
                hits.append(f"{rel}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "os" and any(a.name in ("environ", "getenv", "putenv") for a in node.names):
                hits.append(f"{rel}:{node.lineno}")
    assert hits == [], "read settings through daedalus.config.Settings, not os.environ"


LAZY_RE = re.compile(r"#\s*Lazy:\s*\S")


def test_in_function_imports_state_their_reason() -> None:
    """An import hidden inside a function is how an import cycle gets papered over; each one says why."""
    offenders = []
    for path, tree in list(_modules(PKG)) + list(_modules(ROOT / "launcher")):
        lines = path.read_text(encoding="utf-8").splitlines()
        rel = path.relative_to(ROOT).as_posix()
        seen: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Import | ast.ImportFrom) or inner.lineno in seen:
                    continue
                seen.add(inner.lineno)
                span = "\n".join(lines[inner.lineno - 1 : (inner.end_lineno or inner.lineno)])
                if not LAZY_RE.search(span):
                    offenders.append(f"{rel}:{inner.lineno}")
    assert offenders == [], "annotate with '# Lazy: <reason>' or move the import to the top"


def test_no_source_file_is_stored_with_a_nul_byte() -> None:
    """A NUL makes git call the file binary, and the change lands as a byte count.

    The mini app's markdown renderer stashed code spans behind a NUL sentinel for a
    while, which cost every edit to that file its diff. Nothing is reviewable that
    nobody can read, so the sentinel is a private-use code point written as an escape.
    """
    roots = (PKG, ROOT / "miniapp" / "src", ROOT / "tests")
    suffixes = {".py", ".ts", ".tsx", ".js", ".css", ".md", ".toml"}
    binary = [
        path
        for root in roots
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in suffixes and b"\x00" in path.read_bytes()
    ]
    assert binary == []
