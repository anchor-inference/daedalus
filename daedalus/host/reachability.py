"""Which modules of the host are reachable from its entry points.

A change to the agent's own code is only a change if the running agent executes it. This module
answers, from the source tree alone, whether a module is on any execution path: it parses every
``import`` in the ``daedalus`` package and walks the graph from the entry points the process
actually starts from. Tests are not importers: a module that only its tests import is dead.

The walk is static (``ast``), so it works on a worktree that is not importable in this process,
and it is deliberately generous: a dynamic import by string (``importlib.import_module(name)``)
counts when the string is a literal and the module it names is resolvable from the source — an
absolute name, or a relative one resolved against the package the source names: a literal
``package=`` (keyword or positional), the file's own ``__package__``, or its bare ``__name__``. When
the call names no package at all the literal is kept as written: ``importlib.import_module(".x")``
raises ``TypeError``, so the call has no module to point at and substituting the caller's own package
asserted an edge the interpreter cannot follow. When the package is an expression
the source does not name — a variable, another module's attribute — the literal is kept and no edge
is drawn: a module reached only that way needs a path the walk can see. A relative name that would
leave the package root keeps its literal form too, since there is no module it could name.
Package-level discovery is named in ``DISCOVERED_PACKAGES``: for those packages, every direct
submodule that a loader actually imports counts — the modules ``discover_tools`` loads by name, and
an underscore-prefixed module is loaded explicitly by its importer or not at all, so it is not counted
as discovered. A relative ``from`` import resolves against the file's package. Generosity is the
right error: the gate exists to catch modules nothing mentions, not to argue about how they are
mentioned.
What generosity does not cover is a call the interpreter refuses or a branch it never enters: a
keyword shape Python rejects, an arity it rejects, a receiver that is not the import system, and the
bodies of ``if TYPE_CHECKING:``, ``if False:``, ``if 1 == 2:`` or ``while False:`` draw no edge,
because nothing is loadable from them.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path

PACKAGE = "daedalus"
ENTRY_POINTS = ("daedalus.__main__", "daedalus.app", "daedalus.bench.harbor")
"""Where a process starts: the CLI, the application, and the Harbor benchmark adapter (loaded by
``harbor run -a daedalus.bench.harbor:DaedalusAgent``); everything else is reached from them."""

DISCOVERED_PACKAGES = ("daedalus.tools",)
"""Packages whose submodules are loaded by ``pkgutil`` discovery: every module directly inside them is
reachable once the package is. Extensions are not discovered: they are named one by one in the
``EXTENSIONS`` tuple, which the walk reads as a list of literal imports — from the one module that
declares and iterates it, ``EXTENSIONS_MODULE``, not from any assignment to that name."""

EXTENSIONS_MODULE = "daedalus.extensions"
"""Where the extension registry lives: this module assigns ``EXTENSIONS`` and iterates it at
start-up, so the names in it are loaded. Reading that name from every file in the tree made a local
variable a registry — a one-proposal way to give a module nothing imports an incoming edge."""

# A rule the walk applies also lives in the code it audits, so a *merged* change to it can widen the
# walk before the next proposal is judged (this is why ``selfdev`` keeps its own boot set rather than
# reading ``ENTRY_POINTS``). ``DISCOVERED_PACKAGES`` above is of that kind: widening it is a real
# change to the walk, and nothing inside a single proposal can do it. Saying that plainly is the
# honest form; pretending the walk is free of the trust it applies would not be.


def module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(module: str, is_package: bool, level: int, target: str | None) -> str | None:
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    if level > len(module.split(".")):
        return None
    return ".".join(base + ([target] if target else []))


def _package_of(module: str, is_package: bool) -> str:
    """What ``__package__`` holds inside this file: the file's own package, not its module name."""
    return module if is_package else module.rsplit(".", 1)[0]


def _resolve_from_package(package: str, dotted: str) -> str | None:
    """Resolve a relative name the way ``importlib.import_module(name, package=...)`` does:
    ``.mod`` against the package itself, ``..mod`` one level up. ``None`` when it leaves the root."""
    level = len(dotted) - len(dotted.lstrip("."))
    rest = dotted[level:]
    parts = package.split(".")
    if level > 1:
        if len(parts) < level - 1:
            return None
        parts = parts[: len(parts) - (level - 1)]
    base = ".".join(parts)
    if not base:
        return None
    return f"{base}.{rest}" if rest else base


def _import_module_call(node: ast.Call) -> tuple[str, ast.expr | None] | None:
    """``(literal name, package expression)`` for an ``importlib.import_module`` call, else ``None``.

    ``None`` means *no edge may be drawn*: the name is not a literal, or the call has a shape CPython
    refuses, so nothing is loadable from it. Arity was the first such shape; keyword binding is the
    rest of them. ``import_module`` takes ``name`` (positionally or by keyword), at most one
    ``package``, and no other keyword, so each of these is a ``TypeError`` at run time and each used
    to be read as a real import:

        import_module('.p', 'daedalus', package='daedalus')   # package given twice
        import_module('daedalus.p', level=1)                  # unexpected keyword
        import_module('daedalus.p', name='q')                 # name given twice

    ``name=`` as a keyword is legal and is read: ``import_module(name='daedalus.p')``. A ``**mapping``
    is not a shape the walk can judge, so it draws nothing.
    """
    if len(node.args) > 2:
        return None
    name_positional = len(node.args) >= 1
    package_positional = len(node.args) >= 2
    literal: str | None = None
    package: ast.expr | None = None
    if name_positional:
        if not (isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            return None
        literal = node.args[0].value
    if package_positional:
        package = node.args[1]
    for kw in node.keywords:
        if kw.arg is None or kw.arg not in ("name", "package"):
            return None
        if kw.arg == "name":
            if name_positional:
                return None
            if not (isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)):
                return None
            literal = kw.value.value
        else:
            if package_positional:
                return None
            package = kw.value
    if literal is None:
        return None
    return literal, package


def _static_package(package: ast.expr | None, module: str, is_package: bool) -> str | None:
    """The package a dynamic relative import resolves against, when the source says it.

    ``package=`` may be given by keyword or positionally (``import_module(".x", "pkg")`` is legal).
    It is knowable when it is a string literal, the file's own ``__package__``, or the bare
    ``__name__``. Anything else — a variable, a call, another module's attribute — is *not* knowable,
    and ``None`` is returned so the caller keeps the literal: guessing here is how a live module ends
    up declared unreached, and the one thing this walk must not do is draw an edge to a module the
    source never names."""
    return _named_package(package, module, is_package) if package is not None else None


def _named_package(node: ast.expr, module: str, is_package: bool) -> str | None:
    """The package argument when the source states it, in any legal position.

    ``import_module(".x", "pkg")`` and ``import_module(".x", package="pkg")`` are the same call, and
    so are ``__package__``/``__name__`` in either position. Keyword-only handling was a gap: the
    positional form is legal, unambiguous, and the interpreter resolves it, so a module reached that
    way read as unreached and the gate refused a change that runs.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "__package__":
            return _package_of(module, is_package)
        if node.id == "__name__":
            return module
    # No attribute branch. ``__package__.__name__`` is attribute access on a ``str`` and raises
    # ``AttributeError``; ``other.__name__`` is the unnameable case this function must refuse. A
    # reviewer falsified the first half of that in four lines — ``__name__`` and ``__package__`` are
    # ordinary globals, so a file may rebind ``__name__`` to an object whose ``__package__`` is a
    # string, and the expression then resolves — which is exactly why the branch is gone: under the
    # interpreter's ordinary binding the call raises, and under a rebound name the answer exists only
    # at run time, where a static walk cannot reach it. Returning ``None`` for both keeps the literal
    # and draws no edge, which is the failure direction this module chose everywhere else, and it no
    # longer reports a *wrong* name (the removed branch produced ``daedalus.app.live`` for a package
    # it had guessed). The branch is untested in either direction; the reason to omit it is that a
    # guess about a value the file rebinds is not knowable here, not that no program runs.
    return None


def _statically_false(test: ast.expr) -> bool:
    """A test the interpreter can never pass, in the forms that cannot mean anything else.

    A literal false constant; ``TYPE_CHECKING`` (or ``typing.TYPE_CHECKING``), whose whole purpose is
    a block that never runs; and a comparison of two literal constants that is simply untrue
    (``if 1 == 2:``). The last one was a gap a reviewer pointed at: pruning only ``False`` and
    ``TYPE_CHECKING`` left the same one-line, one-proposal escape available through a comparison, and
    a comparison of literals is a decidable fact about the source, not a guess. Only ``ast.Constant``
    operands are folded — no names, no calls — so nothing here evaluates source.
    """
    if isinstance(test, ast.Constant):
        return not bool(test.value)
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1:
        left, right = test.left, test.comparators[0]
        if isinstance(left, ast.Constant) and isinstance(right, ast.Constant):
            # A literal comparison is either true or false in every run; say so only when it is false.
            # ``is``/``is not`` on literals is folded by the same rule CPython applies to them.
            try:
                if isinstance(test.ops[0], ast.Eq):
                    return bool(left.value == right.value) is False
                if isinstance(test.ops[0], ast.NotEq):
                    return bool(left.value != right.value) is False
                if isinstance(test.ops[0], (ast.Is, ast.IsNot)):
                    return True
            except Exception:  # pragma: no cover - a comparison that raises is not statically true
                return False
        return False
    return False


def _live_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Every node the interpreter can execute.

    ``ast.walk`` visits dead branches as well, which handed the gate a one-line, one-proposal escape:
    ``if TYPE_CHECKING: from daedalus.parasite import X`` in any already-reached module gave a module
    nothing imports an incoming edge, and the gate accepted it. Pruning such branches can only remove
    edges — it can refuse a change that runs, never accept one that does not, which is this module's
    standing failure direction. The condition itself is still visited; only the branch it forbids is
    dropped, and an ``else`` still runs, so it is kept. The same holds for ``while <false>``: its body
    never runs, so it is not walked (a ``while`` has no ``else`` that runs either in that case)."""
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.If) and _statically_false(node.test):
            stack.append(node.test)
            stack.extend(node.orelse)
            continue
        if isinstance(node, ast.While) and _statically_false(node.test):
            stack.append(node.test)
            continue
        stack.extend(ast.iter_child_nodes(node))


def _import_module_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """``(receivers, bare_names)``: the names in this file that hold the import system's function.

    A call is the import system's only if the *name* it is called through was bound to it. Read from
    the file, not guessed: ``import importlib`` (under any alias) gives a receiver, and
    ``from importlib import import_module`` (under any alias) gives a bare name. A name the file also
    binds to something else is dropped — a reviewer's counterexample defined a local
    ``def import_module``, called it with a module name, and the gate accepted a module nothing
    imports. *Every* binding counts, not only assignment: a parameter, a ``for`` target, a ``with
    ... as``, an ``except ... as``, a walrus, an ``AnnAssign`` or a ``del`` all rebind the name, and a
    check that reads only ``Assign`` is the same half-fix as reading only arity. Unknowable forms
    (``from importlib import *``, a name reached through a variable) bind nothing, which draws no edge.
    """
    receivers = {"importlib"}
    bare: set[str] = set()
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    receivers.add(alias.asname or "importlib")
                else:
                    bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        bare.add(alias.asname or "import_module")
                    else:
                        bound.add(alias.asname or alias.name)
            else:
                for alias in node.names:
                    bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                bound.update(_bound_names(target))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            bound.update(_bound_names(node.target))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    bound.update(_bound_names(item.optional_vars))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                bound.update(_bound_names(target))
    return receivers - bound, bare - bound


def _bound_names(target: ast.expr) -> set[str]:
    """Every bare name a binding target introduces, including inside a tuple or ``*rest``."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in target.elts:
            names |= _bound_names(element)
        return names
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    return set()


def imports_of(path: Path, module: str, *, is_package: bool) -> set[str]:
    """Module names ``path`` mentions: static imports, ``from x import y`` (both ``x`` and ``x.y``,
    since ``y`` may be a submodule) and literal strings handed to ``importlib.import_module``.

    A *relative* literal (``import_module(".feature", package=__package__)``) is resolved against the
    package the source names: a literal ``package=``, the file's own ``__package__`` or its ``__name__``.
    When the call names no package at all the literal is kept as written: ``import_module('.x')``
    raises ``TypeError`` — the package argument is required for a relative import — so the call has no
    module to point at, and substituting the caller's own package asserted an edge the interpreter
    cannot follow. When the package is an expression the source does not name (a
    variable, another module's attribute), the literal is kept as written and no edge is drawn: the walk
    cannot tell what it loads, it does not guess, and a module reached only that way needs a path the
    walk can see. A relative name that would leave the package root is kept as written too — there is no
    module it could name. An attribute call counts only when its receiver is ``importlib`` or a name
    this file binds to it: ``registry.import_module('.x', 'pkg')`` is somebody's method, and treating
    it as the import system's drew an edge the walk had no warrant for, and so did a *bare* name the
    file binds to a local function of the same name: both count only when the file's own imports bind
    them to ``importlib``. A call whose keyword shape
    CPython refuses is in the same class as the arity case and also draws nothing. Branches the
    interpreter never enters (``if TYPE_CHECKING:``, ``if False:``) are not walked.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return set()
    key = (hashlib.blake2b(source.encode(), digest_size=16).digest(), module, is_package)
    if key not in _IMPORTS_SEEN:
        if len(_IMPORTS_SEEN) >= IMPORTS_REMEMBERED:
            _IMPORTS_SEEN.clear()
        _IMPORTS_SEEN[key] = _imports_in(source, module, is_package)
    return set(_IMPORTS_SEEN[key])


IMPORTS_REMEMBERED = 4096
_IMPORTS_SEEN: dict[tuple[bytes, str, bool], frozenset[str]] = {}
"""What :func:`imports_of` found, by a digest of the file's text, its module name and its kind.

Parsing and walking every file is most of what one graph costs, about a second and a half for this
tree, and one gate builds the graph several times over the same files (the whole tree, then a path
for each candidate). The key is a digest of the text, not the path or its timestamp, so an edit is
always read, including one that keeps the size and the clock; and it is the digest rather than the
text so that what is remembered is a few hundred kilobytes, not a copy of every tree ever walked.
"""


def _imports_in(source: str, module: str, is_package: bool) -> frozenset[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset()
    importlib_names, bare_names = _import_module_bindings(tree)
    found: set[str] = set()
    for node in _live_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(module, is_package, node.level, node.module) if node.level else node.module
            if base:
                found.add(base)
                for alias in node.names:
                    found.add(f"{base}.{alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                # ``x.import_module(...)`` is the import system's call only when ``x`` is importlib
                # (under any alias) and the attribute is the function's own name. Before this check
                # any receiver counted, so a method that happens to share the name
                # (``self.import_module``, a loader object) drew a resolved edge to a module its own
                # body may never import — an invented edge of the same family as the missing-package
                # form, and one that hides a dead module rather than refusing a live one.
                if not (isinstance(func.value, ast.Name) and func.value.id in importlib_names):
                    continue
                if func.attr != "import_module":
                    continue
            else:
                # A bare name, counted only when this file binds it from importlib and does not bind
                # it to anything else — ``from importlib import import_module``, under any alias, is
                # the real form, and a local function of the same name is not the import system. It
                # used to be counted whatever it was bound to, which turned one local definition into
                # a reachability edge.
                if getattr(func, "id", "") not in bare_names:
                    continue
            call = _import_module_call(node)
            if call is None:
                continue
            literal, package_expr = call
            if literal.startswith("."):
                # No package argument and none named in the source: the interpreter refuses this call
                # (``importlib.import_module('.x')`` raises TypeError), so there is no module to point
                # at. Substituting the caller's own package here asserted an edge the runtime cannot
                # follow, which is the one thing this walk must not do: a tree where a module is named
                # only that way read as fully reached.
                given = _static_package(package_expr, module, is_package)
                resolved = _resolve_from_package(given, literal) if given else None
                found.add(resolved if resolved else literal)
            else:
                found.add(literal)
    if module == EXTENSIONS_MODULE:
        # The registry: a tuple of dotted names the module loops over at start-up. Only a top-level
        # assignment counts, because ``ast``-walking the file also finds a local of the same name
        # inside a function — the module that declares the registry is not every function in it.
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "EXTENSIONS" for t in node.targets
            ) and isinstance(node.value, (ast.Tuple, ast.List)):
                found.update(
                    e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
                )
    return frozenset(found)


def import_graph(root: Path) -> dict[str, set[str]]:
    """``module -> modules it imports`` for every module of the package under ``root``."""
    package_dir = root / PACKAGE
    graph: dict[str, set[str]] = {}
    for path in sorted(package_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        name = module_name(root, path)
        graph[name] = imports_of(path, name, is_package=path.name == "__init__.py")
    return graph


def reachable(graph: dict[str, set[str]], entry_points: Iterable[str] = ENTRY_POINTS) -> set[str]:
    """Every module reachable from the entry points through the graph, plus the parents of each
    reached module (importing ``a.b.c`` runs ``a`` and ``a.b``) and the discovered packages' children."""
    known = set(graph)
    seen: set[str] = set()
    stack = [e for e in entry_points if e in known]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        parts = current.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if parent in known and parent not in seen:
                stack.append(parent)
        if current in DISCOVERED_PACKAGES:
            prefix = current + "."
            # ``discover_tools`` imports every direct submodule that is not underscore-prefixed
            # (``_common`` is imported by name instead). Counting the underscore-prefixed ones as
            # discovered made a file nothing imports — ``daedalus/tools/_whatever.py`` — read as
            # reached, which is a one-proposal way through the gate. The rule now matches the loader
            # it stands for.
            stack.extend(
                m for m in known
                if m.startswith(prefix) and "." not in m[len(prefix):] and not m[len(prefix):].startswith("_")
            )
        for target in graph.get(current, ()):
            if target in known:
                stack.append(target)
            else:
                # ``from a.b import name`` where ``name`` is a symbol, not a module: reach ``a.b``.
                head = target.rsplit(".", 1)[0]
                if head in known and head not in seen:
                    stack.append(head)
    return seen


def unreachable_modules(root: Path) -> list[str]:
    graph = import_graph(root)
    return sorted(set(graph) - reachable(graph))


def modules_for_files(root: Path, files: Iterable[str]) -> list[str]:
    """The package modules among ``files`` (repo-relative paths); tests, docs and assets are not modules."""
    out = []
    for rel in files:
        path = Path(rel)
        if path.suffix == ".py" and path.parts and path.parts[0] == PACKAGE:
            out.append(module_name(root, root / path))
    return out


def path_reaches(root: Path, execution_path: str, targets: Iterable[str]) -> tuple[bool, list[str]]:
    """Whether the module named by ``execution_path`` (``pkg.mod`` or ``pkg.mod:symbol``) exists and
    reaches every target module; returns the targets it does not reach."""
    graph = import_graph(root)
    start = execution_path.split(":", 1)[0].strip()
    if start not in graph:
        return False, [f"no module {start!r}"]
    reached = reachable(graph, (start,)) | {start}
    missing = [t for t in targets if t not in reached]
    return not missing, missing


__all__ = ["DISCOVERED_PACKAGES", "ENTRY_POINTS", "EXTENSIONS_MODULE", "import_graph", "modules_for_files",
           "path_reaches", "reachable", "unreachable_modules"]
