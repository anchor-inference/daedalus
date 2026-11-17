"""Every request the app sends names a route the host really serves, with that route's method.

The schedule card in a conversation's side panel ran and deleted its schedule through
``/api/schedules/{id}/run`` and ``DELETE /api/schedules/{id}`` for weeks after those routes had moved
to ``/api/recurring``: FastAPI answered 404 with its bare reason, the toast said "Not Found", and
nothing in either test suite noticed, because the browser stub still answered the old paths. A stub
drifts with the app it serves; this compares the app against the router the host builds.

The app's sources are read as text. A path reaches a request in a few shapes, and each is resolved
to the templates it can stand for:

* a literal, ``api.post(`/api/x/${id}/run`)``;
* a constant or a one-line helper, ``api.get(watchesKey(projectId))``, ``api.delete(`${NOTES_KEY}/${id}`)``;
* a raw ``fetch`` with its ``method:`` beside it.

A path the scan cannot resolve (one passed around as a parameter) is not checked here; the
literals that build it are still checked against the route table by path alone, so a request to a
place the host does not serve at all is caught wherever its path was written down.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.api_panel import FORWARDED, OWN
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

APP = Path(__file__).resolve().parents[2] / "miniapp" / "src"
HOLE = "\x00"
"""Where a ``${...}`` stood in a template: any text the route's parameter accepts."""

VERBS = {"get": "GET", "getText": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE"}
HEADS = re.compile(r"\b(?:api\.(?P<verb>get|getText|post|put|patch|delete|request|fetch|upload)|(?P<plain>useQuery|fetch|upload|call))\b")


@dataclass(frozen=True)
class Call:
    where: str
    method: str | None
    """None for a path written down with no request beside it: a cache key, a base, a constant."""
    paths: tuple[str, ...]
    """What the path can be. A helper that picks a path by its arguments (``storagePath(s, c, key?)``)
    stands for several; the call is fine when one of them is served, as the scan cannot see which
    one these arguments pick. A ternary written at the call itself is one call per branch instead."""


def _literal(src: str, i: int) -> tuple[str, int]:
    """The string or template literal opening at ``src[i]``, its holes as HOLE, and where it ends."""
    quote, j, out = src[i], i + 1, []
    while j < len(src):
        c = src[j]
        if c == "\\":
            out.append(src[j + 1 : j + 2])
            j += 2
        elif c == quote:
            return "".join(out), j + 1
        elif quote == "`" and src.startswith("${", j):
            depth, j, start = 1, j + 2, j + 2
            while j < len(src) and depth:
                if src[j] in "`\"'":
                    _, j = _literal(src, j)
                    continue
                depth += {"{": 1, "}": -1}.get(src[j], 0)
                j += 1
            out.append(HOLE + src[start : j - 1] + HOLE)
        else:
            out.append(c)
            j += 1
    return "".join(out), j


def _argument(src: str, i: int) -> tuple[str, int]:
    """The text of the argument starting at ``src[i]``, up to its top-level comma or closing paren."""
    depth, j = 0, i
    while j < len(src):
        c = src[j]
        if c in "`\"'":
            _, j = _literal(src, j)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            break
        j += 1
    return src[i:j].strip(), j


def _skip_type_arguments(src: str, i: int) -> int:
    while i < len(src) and src[i].isspace():
        i += 1
    if i >= len(src) or src[i] != "<":
        return i
    depth = 0
    while i < len(src):
        depth += {"<": 1, ">": -1}.get(src[i], 0)
        i += 1
        if depth == 0:
            break
    return i


def _in_comment(src: str, i: int) -> bool:
    line = src[src.rfind("\n", 0, i) + 1 : i]
    stripped = line.lstrip()
    if stripped.startswith(("//", "*", "/*")):
        return True
    return re.search(r"(^|\s)//", line) is not None


def _sources() -> Iterator[tuple[str, str]]:
    for path in sorted(APP.rglob("*.ts*")):
        if ".test." not in path.name:
            yield str(path.relative_to(APP)), path.read_text(encoding="utf-8")


def _expression(src: str, i: int) -> str:
    """The expression starting at ``src[i]``, up to its top-level ``;``, comma or line end."""
    depth, j = 0, i
    while j < len(src):
        c = src[j]
        if c in "`\"'":
            _, j = _literal(src, j)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and (c in ";," or (c == "\n" and not src[j:].lstrip().startswith(("?", ":", "+")))):
            break
        j += 1
    return src[i:j].strip()


Definitions = dict[str, list[tuple[str, str]]]
"""Per name, each expression it is bound to and the file that binds it, whose names the expression uses."""


def _definitions(name: str, src: str) -> Definitions:
    """What each name in a file is bound to, as source text: a constant, an arrow helper's body, a
    function's returns. Only what a path can be read from is kept; the rest resolves to nothing."""
    found: Definitions = {}
    for m in re.finditer(r"\b(?:const|let)\s+(\w+)\s*(?::[^=;\n]+)?=(?!=)\s*", src):
        value = _expression(src, m.end())
        arrow = re.match(r"(?:async\s+)?(?:\([^()]*\)|\w+)\s*(?::[^=]+)?=>\s*", value)
        found.setdefault(m.group(1), []).append((value[arrow.end():] if arrow else value, name))
    for m in re.finditer(r"\bfunction\s+(\w+)\s*(?:<[^>]*>)?\([^)]*\)[^{]*\{", src):
        close = src.find("\n}", m.end())
        body_end = close if close >= 0 else len(src)
        for r in re.finditer(r"\breturn\s+", src[m.end():body_end]):
            found.setdefault(m.group(1), []).append((_expression(src, m.end() + r.end()), name))
    return found


def _branches(expression: str) -> list[str] | None:
    """The two outcomes of a top-level ``cond ? a : b``, or None when it is not one."""
    depth, j, question = 0, 0, -1
    while j < len(expression):
        c = expression[j]
        if c in "`\"'":
            _, j = _literal(expression, j)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and c == "?" and expression[j + 1 : j + 2] not in ("?", ".") and expression[j - 1 : j] != "?":
            if question < 0:
                question = j
        elif depth == 0 and c == ":" and question >= 0:
            return [expression[question + 1 : j], expression[j + 1 :]]
        j += 1
    return None


def _resolve(expression: str, where: str, files: dict[str, Definitions], shared: Definitions, depth: int = 0) -> list[str] | None:
    """The templates an expression can stand for, [] when it is plainly not a path, None when the
    scan cannot tell."""
    expression = re.sub(r"^await\s+", "", expression.strip())
    if depth > 6 or not expression:
        return None
    if expression in ("null", "undefined", "false", '""', "''"):
        return []
    branches = _branches(expression)
    if branches is not None:
        resolved: list[str] = []
        known = False
        for branch in branches:
            more = _resolve(branch, where, files, shared, depth + 1)
            if more is not None:
                known = True
                resolved.extend(more)
        return resolved if known else None
    if expression[0] in "`\"'":
        text, end = _literal(expression, 0)
        if end != len(expression):
            return None
        if text.startswith(HOLE):
            inner, _, rest = text[1:].partition(HOLE)
            heads = _resolve(inner, where, files, shared, depth + 1)
            return None if heads is None else [head + rest for head in heads]
        return [text] if text.startswith("/api/") else []
    name = re.fullmatch(r"(\w+)!?(?:\(.*\))?", expression, re.DOTALL)
    if name is None:
        return None
    # A name the file binds itself wins; otherwise it is an import, bound in some other file.
    values = files[where].get(name.group(1)) or shared.get(name.group(1))
    if not values:
        return None
    resolved = []
    for value, home in values:
        more = _resolve(value, home, files, shared, depth + 1)
        if more is not None:
            resolved.extend(more)
    return resolved or None


def app_calls() -> list[Call]:
    files = list(_sources())
    per_file = {name: _definitions(name, src) for name, src in files}
    # An imported name is one some file exports; that file's binding is what the import means.
    shared: Definitions = {}
    for name, src in files:
        for exported in re.finditer(r"\bexport\s+(?:async\s+)?(?:const|let|function)\s+(\w+)", src):
            shared.setdefault(exported.group(1), []).extend(per_file[name].get(exported.group(1), []))
    calls: list[Call] = []
    for name, src in files:
        consumed: set[int] = set()
        for head in HEADS.finditer(src):
            if _in_comment(src, head.start()):
                continue
            opening = _skip_type_arguments(src, head.end())
            if opening >= len(src) or src[opening] != "(":
                continue
            verb = head.group("verb") or head.group("plain")
            first, after = _argument(src, opening + 1)
            method: str | None
            if verb in VERBS:
                method = VERBS[verb]
            elif verb == "useQuery":
                method = "GET"
            elif verb == "upload":
                method = "POST"
            elif verb in ("request", "call"):
                explicit = re.fullmatch(r"\"(GET|POST|PUT|PATCH|DELETE)\"", first)
                if explicit is None:
                    continue
                method = explicit.group(1)
                first, after = _argument(src, after + 1)
            else:
                rest, _ = _argument(src, after + 1) if src[after : after + 1] == "," else ("", after)
                stated = re.search(r"method:\s*\"(\w+)\"", rest)
                method = stated.group(1) if stated else "GET"
            line = src.count("\n", 0, head.start()) + 1
            for branch in _branches(first) or [first]:
                paths = _resolve(branch, name, per_file, shared)
                if paths:
                    calls.append(Call(f"{name}:{line}", method, tuple(paths)))
            start = opening + 1
            while src[start].isspace():
                start += 1
            consumed.add(start)
        # Every other path written down: a cache key, a base, a path kept for a later request. Its
        # method is not known here, so it only has to be the path, or the head of a path, of some route.
        for m in re.finditer(r"[\"'`]/api/|`\$\{\w+(?:\([^`]*?\))?\}/", src):
            if m.start() in consumed or _in_comment(src, m.start()):
                continue
            text, end = _literal(src, m.start())
            line = src.count("\n", 0, m.start()) + 1
            paths = _resolve(src[m.start() : end], name, per_file, shared)
            if paths:
                calls.append(Call(f"{name}:{line}", None, tuple(paths)))
    return calls


def _route_pattern(path: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}]+\})", path)
    return re.compile("".join((".+" if p.endswith(":path}") else "[^/]+") if p.startswith("{") else re.escape(p) for p in parts))


def _shapes(template: str) -> set[str]:
    """The path a template can be: the query string cut off, and a trailing hole glued to a word
    (``/sample${query}``) also tried as absent, since that is how a query or an optional tail is written."""
    path = re.sub(HOLE + r"[^" + HOLE + r"]*" + HOLE, HOLE, template).split("?")[0]
    shapes = {path}
    if path.endswith(HOLE) and not path.endswith("/" + HOLE):
        shapes.add(path.rstrip(HOLE))
    return shapes


def served(routes: list[tuple[str, str]], call: Call) -> bool:
    """Whether some route answers ``call``; a bare path (no method) may also be the head of one."""
    for shape in {shape for path in call.paths for shape in _shapes(path)}:
        if shape.startswith("/api/panel/"):
            # The side panel's door: its own routes and the operator's it forwards, ``/panel`` dropped.
            sample = shape.replace(HOLE, "p")
            door = [(m, p) for m, p in OWN] + [(m, p) for m, p in FORWARDED]
            if any(p.fullmatch(sample) and (call.method in (None, m)) for m, p in door):
                return True
            continue
        sample = shape.replace(HOLE, "p")
        loose = re.compile("".join("[^?]*" if c == HOLE else re.escape(c) for c in shape))
        for method, path in routes:
            if call.method not in (None, method):
                continue
            example = re.sub(r"\{[^}]+\}", "p", path)
            if _route_pattern(path).fullmatch(sample) or loose.fullmatch(example):
                return True
            if call.method is None and (example.startswith(sample) or loose.match(example)):
                return True
    return False


UNREAD_QUERY = {
    # The review's head is part of the cache key, so a new attempt's change is fetched afresh instead
    # of answering from the previous head's entry; the host reads the head itself.
    ("/api/board/{task_id}/review/since-previous", "head"),
}


def _query_names(dependant: Any) -> tuple[set[str], bool]:
    """The query parameters a route declares, its dependencies' included, and whether it reads the
    raw request (and so may read any query parameter itself)."""
    names = {p.alias for p in dependant.query_params} | {p.name for p in dependant.query_params}
    raw = bool(dependant.request_param_name)
    for sub in dependant.dependencies:
        more, reads = _query_names(sub)
        names |= more
        raw = raw or reads
    return names, raw


async def route_table(settings: Settings, db: Database) -> list[tuple[str, str]]:
    return [(method, route.path) for method, route in await _routes(settings, db)]


async def _routes(settings: Settings, db: Database) -> list[tuple[str, Any]]:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None,
                              extensions={}, guard=None, create_session=manager.create_session)
        api = build_app(app, "tok")  # type: ignore[arg-type]
        return [(method, route) for route in api.routes for method in (getattr(route, "methods", None) or {"GET"})]
    finally:
        await manager.close()


def test_the_scan_reads_the_app() -> None:
    # A scan that silently found nothing would pass every check below.
    calls = app_calls()
    assert len([c for c in calls if c.method]) > 600
    assert any(c.method == "POST" and f"/api/recurring/{HOLE}encodeURIComponent(s.id){HOLE}/run" in c.paths for c in calls)
    assert any(c.method == "GET" and any(p.startswith("/api/projects/") and p.endswith("/watches") for p in c.paths) for c in calls)


async def test_every_request_the_app_sends_is_a_route_the_host_serves(settings: Settings, db: Database) -> None:
    routes = await route_table(settings, db)
    # The comparison itself can tell a route from its absence and a method from the wrong one.
    assert served(routes, Call("probe", "POST", (f"/api/recurring/{HOLE}id{HOLE}/run",)))
    assert not served(routes, Call("probe", "DELETE", (f"/api/recurring/{HOLE}id{HOLE}/run",)))
    assert not served(routes, Call("probe", "POST", (f"/api/schedules/{HOLE}id{HOLE}/run",)))
    assert served(routes, Call("probe", None, ("/api/schedules",)))
    missing = sorted({f"{c.where}  {c.method or 'ANY'} {' | '.join(c.paths).replace(HOLE, '{}')}"
                      for c in app_calls() if not served(routes, c)})
    assert not missing, "the app calls routes the host does not serve:\n" + "\n".join(missing)


async def test_every_route_the_panel_door_forwards_exists(settings: Settings, db: Database) -> None:
    routes = await route_table(settings, db)
    for method, pattern in FORWARDED:
        operator = re.sub(r"\(\?P<sid>[^)]*\)", "p", pattern.pattern.replace("/api/panel", "/api", 1))
        sample = re.sub(r"\[[^\]]*\]\{[^}]*\}", "p", operator).replace("\\", "")
        assert any(m == method and _route_pattern(p).fullmatch(sample) for m, p in routes), f"{method} {pattern.pattern}"


async def test_every_query_parameter_the_app_sends_is_one_the_route_reads(settings: Settings, db: Database) -> None:
    # FastAPI ignores a query parameter no handler declares, so a renamed one fails silently: the
    # filter or the page the app asked for is simply not applied.
    routes = [(m, r) for m, r in await _routes(settings, db) if hasattr(r, "dependant")]
    unread = set()
    for call in app_calls():
        if call.method is None:
            continue
        for template in call.paths:
            path, _, query = template.partition("?")
            sent = set(re.findall(r"(?:^|&|" + HOLE + r")(\w+)=", query))
            sample = re.sub(HOLE + "[^" + HOLE + "]*" + HOLE, "p", path)
            route = next((r for m, r in routes if m == call.method and _route_pattern(r.path).fullmatch(sample)), None)
            if route is None or not sent:
                continue
            declared, _ = _query_names(route.dependant)
            unread |= {f"{call.where}  {call.method} {route.path} ?{name}=" for name in sent - declared
                       if (route.path, name) not in UNREAD_QUERY}
    assert not unread, "the app sends query parameters no route reads:\n" + "\n".join(sorted(unread))
