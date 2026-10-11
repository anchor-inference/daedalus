"""The page tools: the agent builds the operator's custom pages, the screens of their own inside the app.

A page is a folder of the state volume (``daedalus.host.pages``), which a session's commands cannot
reach; every tool here goes through the one page store the API also uses, so a page an agent publishes
reaches every open app at once and the per-page locks are shared with the operator's own edits. The
store is reached through ``manager.service_hooks["pages"]``, the desk the pages extension installs.

Nothing here limits what a page is. The templates are starting points, the kit and the bridge are
conveniences, and the agent may build a page with any toolchain in its workspace and import the
output. The only refusals are the store's own (a path out of the page, a slug that is not one path
segment) and two of this module's: a service page's port must be in the services range, the one
range a check and the operator's browser can reach without opening the installation's own doors,
and removing a published page needs the operator's yes, because nothing of it can be brought back.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.browser.agent import LOOK_INSTRUCTION
from daedalus.host.filesystem import ShellFS
from daedalus.host.pages import KINDS, PageError, PageStore, clean_path, title_text
from daedalus.host.services import SessionServices
from daedalus.stores.files import human_size
from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import FETCH_MAX_BYTES, error, ok, output_limit, refuse_protected, services_for
from daedalus.tools.vision import VisionUnavailable
from daedalus.tools.vision import look as vision_look

CHUNK = 1 << 20
"""How much of a file is read at a time on its way into a page: a build's 200 MB model streams
through without the whole of it in memory."""
IMPORT_MAX_FILES = 100_000
"""How many files one import lists on a machine this process does not share; the store's own caps
decide what a page may hold."""
LIST_SHOWN = 400
TEXT_SUFFIXES = frozenset({".html", ".htm", ".css", ".js", ".mjs", ".json", ".md", ".svg", ".txt", ".webmanifest", ".xml"})
"""The template files placeholders are filled in: text a person reads, never a binary asset."""
CHECK_FOLDER = ".page-check"
VISION_QUESTION = (
    "This is a web page shown inside an app, at {width}x{height} ({view}, {scheme} colour scheme). List the concrete "
    "visual problems a user would notice: content cut off or overlapping, text hard to read against its background, "
    "controls too small to tap, broken images or empty areas where content should be, anything that looks unfinished "
    "or out of place. Then say in one sentence what the page shows. If it looks right, say so briefly."
)


def _desk(context: ToolContext) -> Any:
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("pages") if manager is not None else None


def _unavailable(context: ToolContext) -> ToolResult:
    return error(context, "custom pages are not available in this installation")


def _refused(context: ToolContext, exc: PageError) -> ToolResult:
    return error(context, str(exc))


def _plain_title(title: Any) -> str:
    try:
        return title_text(title)
    except (AttributeError, StopIteration, TypeError):
        return str(title)


def _addresses(slug: str) -> str:
    return f"/app/p/{slug} in the app (the page's own address is /pages/{slug}/, its draft /pages/{slug}/?draft=1)"


def _templates(desk: Any) -> list[str]:
    root: Path = desk.templates_dir
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")) if root.is_dir() else []


def _template_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file() and not p.is_symlink() and p.name != "README.md")


def _filled(data: bytes, suffix: str, *, title: str, slug: str) -> bytes:
    """A template file with its placeholders filled: ``{{title}}`` (escaped for HTML, since every
    template names it in markup) and ``{{slug}}``."""
    if suffix.lower() not in TEXT_SUFFIXES:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return text.replace("{{title}}", html.escape(title)).replace("{{slug}}", slug).encode("utf-8")


def _service_target(desk: Any, target: Any) -> int | str:
    """A service page's target: a port of the services range, or the name of a service."""
    lo, hi = desk.services_range()
    if isinstance(target, bool) or target in (None, ""):
        raise PageError(f"a service page needs a target: a port of the services range {lo}-{hi}, or the name of a service started with ServiceStart")
    if isinstance(target, int) or (isinstance(target, str) and target.strip().isdigit()):
        port = int(target)
        if not lo <= port <= hi:
            raise PageError(f"a service page's port is one of the services range {lo}-{hi} (ServiceStart picks one), not {port}")
        return port
    if isinstance(target, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", target.strip()):
        return target.strip()
    raise PageError(f"a service page's target is a port of {lo}-{hi} or a service's name, not {target!r}")


async def _chunks(path: Path) -> AsyncIterator[bytes]:
    handle = await asyncio.to_thread(path.open, "rb")
    try:
        while True:
            block = await asyncio.to_thread(handle.read, CHUNK)
            if not block:
                return
            yield block
    finally:
        await asyncio.to_thread(handle.close)


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _local_tree(root: Path) -> tuple[list[tuple[str, Path]], int]:
    """Every file under ``root`` with its path relative to it, links left out (a page is its own
    files, never a pointer elsewhere); and how many links were left out."""
    found: list[tuple[str, Path]] = []
    skipped = 0
    for directory, folders, names in os.walk(root, followlinks=False):
        base = Path(directory)
        kept = []
        for folder in folders:
            if (base / folder).is_symlink():
                skipped += 1
            else:
                kept.append(folder)
        folders[:] = kept
        for name in names:
            path = base / name
            if path.is_symlink() or not path.is_file():
                skipped += 1
                continue
            found.append((path.relative_to(root).as_posix(), path))
    return sorted(found), skipped


def _head(path: Path, size: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(size)


def _join(dest: str, rel: str) -> str:
    return clean_path(f"{dest.rstrip('/')}/{rel}" if dest else rel)


# -- creating -----------------------------------------------------------------------------------


@tool_group("pages")
@search_hint(
    "custom page screen inside the app dashboard tool viewer form embed service web app rail new page create scaffold template "
    "страница своя страница экран в приложении дашборд панель инструмент вьювер форма встроить сервис создать страницу шаблон"
)
@tool(
    name="PageCreate",
    description=(
        "Create a custom page: a screen of the operator's own inside the Daedalus app, at /app/p/<slug>, which can sit "
        "in the rail and the phone drawer. Anything goes inside it — any framework, CDN, WebAssembly, WebSockets, the "
        "whole /api — and nothing restarts. slug: 1-63 lower-case letters, digits, hyphens. title: a string or one per "
        "language ({\"en\": …, \"ru\": …}). kind: 'static' (files you write or import), 'service' (a web app you run, "
        "proxied; target = a port of the services range or the name of a service from ServiceStart), 'link' (another "
        "site in a frame; target = its https URL). template (static only) is a starting point in the draft: blank, "
        "dashboard, split (a tool with a panel for the page's agent), viewer (a three.js 3D viewer), embed-service (a "
        "toolbar around a framed app), form. icon: an app icon name (grid, board, chart, globe, file, …); icon_path: "
        "one SVG path d on a 24-unit grid. Load the daedalus-pages skill first for the kit, the bridge and the phone "
        "rules. Next: PageWrite / PageImport, PageCheck, PagePublish."
    ),
)
async def page_create(context: ToolContext, slug: str, title: str | dict[str, str], kind: str = "static", template: str = "blank",
                      icon: str | None = None, icon_path: str | None = None, target: int | str | None = None) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    if kind not in KINDS:
        return error(context, f"kind is one of {', '.join(KINDS)}")
    store: PageStore = desk.store
    folder: Path | None = None
    notes: list[str] = []
    try:
        if kind == "service":
            target = _service_target(desk, target)
            if isinstance(target, str):
                names = {r.get("name") for r in await desk.service_rows(context.session_id)}
                if target not in names:
                    notes.append(f"No service named {target!r} runs in this session yet: start it with ServiceStart(name={target!r}, …) before the page is opened.")
        elif kind == "link":
            target = str(target or "").strip()
        else:
            target = None
            available = _templates(desk)
            if template not in available:
                return error(context, f"no template {template!r}; the templates are {', '.join(available) or '(none installed)'}")
            folder = desk.templates_dir / template
        manifest = await store.create(slug, title=title, kind=kind, target=target, icon=icon, icon_path=icon_path, session_id=context.session_id)
    except PageError as exc:
        return _refused(context, exc)
    written: list[str] = []
    if folder is not None:
        plain = _plain_title(manifest["title"])
        try:
            for path in _template_files(folder):
                rel = path.relative_to(folder).as_posix()
                data = _filled(await asyncio.to_thread(path.read_bytes), path.suffix, title=plain, slug=slug)
                await store.write_file(slug, rel, data)
                written.append(rel)
        except PageError as exc:
            return error(context, f"the page {slug!r} was created but its template could not be copied: {exc}")
    elif kind == "service":
        notes.append("A service page serves whatever the service answers; its draft files are not used while the kind is 'service'.")
    else:
        notes.append("A link page frames the site; a site that refuses to be framed is offered as a link to open in a tab of its own.")
    what = f"from the template {template!r} ({', '.join(written)})" if written else f"pointing at {target!r}"
    lines = [f"Created the page {slug!r} ({kind}, a draft) {what}.", f"It will be at {_addresses(slug)}.", *notes,
             "This session is the page's agent: the operator talks to it beside the page, and the page's agent.ask reaches it "
             "(PageBind hands the page to another session).",
             "Next: change the draft with PageWrite or PageImport (build anything in your workspace and import the output), "
             "look at it with PageCheck, then PagePublish."]
    return ok(context, "\n".join(lines), slug=slug, kind=kind)


# -- the draft's files --------------------------------------------------------------------------


@tool_group("pages")
@search_hint(
    "write page file html css javascript save into page draft edit page "
    "записать файл страницы сохранить в страницу html css js черновик страницы правка страницы"
)
@tool(
    name="PageWrite",
    description=(
        "Write one text file into a custom page's draft (path relative to the page, e.g. index.html, app.js, "
        "assets/style.css), replacing it whole. For binary files or a whole build folder use PageImport. The live page "
        "changes only at PagePublish."
    ),
)
async def page_write(context: ToolContext, slug: str, path: str, content: str) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    try:
        written = await desk.store.write_file(slug, path, content.encode("utf-8"))
    except PageError as exc:
        return _refused(context, exc)
    return ok(context, f"Wrote {written['path']} ({human_size(written['bytes'])}) into the draft of {slug!r}.", path=written["path"], bytes=written["bytes"])


@tool_group("pages")
@search_hint(
    "import build output dist folder into page copy files from workspace vite npm build assets models upload into page "
    "импортировать сборку папку dist в страницу скопировать файлы из рабочей папки ассеты модели залить в страницу"
)
@tool(
    name="PageImport",
    description=(
        "Copy a file or a whole folder from your workspace into a custom page's draft, at dest (a folder inside the "
        "page; empty = the page's root). This is how a page built with any toolchain gets in: build it in the workspace "
        "(npm, vite, a static site generator, anything; set its base to /pages/<slug>/ or use relative paths) and "
        "import the output folder. Files of any type and size are streamed; links are skipped. replace=true first "
        "empties dest (the whole draft when dest is empty), so files of an earlier build do not linger."
    ),
)
async def page_import(context: ToolContext, slug: str, source_path: str, dest: str = "", replace: bool = False) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    store: PageStore = desk.store
    services = services_for(context)
    try:
        source = services.resolve(source_path)
    except PermissionError as exc:
        return error(context, str(exc))
    if refusal := refuse_protected(context, services, source, "read"):
        return refusal
    dest = dest.strip().strip("/")
    try:
        if dest:
            dest = clean_path(dest)
        await store.require(slug)
        if services.fs.remote:
            files, size, skipped = await _import_remote(services, store, slug, source, dest, replace)
        else:
            outcome = await _import_local(context, services, store, slug, source, dest, replace)
            if isinstance(outcome, ToolResult):
                return outcome
            files, size, skipped = outcome
    except PageError as exc:
        return _refused(context, exc)
    except FileNotFoundError:
        return error(context, f"no such file or folder: {source}")
    where = f"/{dest}/" if dest else "the root"
    said = f"Imported {files} file{'s' if files != 1 else ''} ({human_size(size)}) into the draft of {slug!r} at {where}."
    if skipped:
        said += f" {skipped} link{'s were' if skipped != 1 else ' was'} left out: a page holds its own files."
    return ok(context, said + " PageCheck to look at it, PagePublish to make it live.", files=files, bytes=size)


async def _empty(store: PageStore, slug: str, dest: str) -> None:
    """``dest`` emptied before an import with ``replace``: the folder, or every file of the draft."""
    if dest:
        await store.delete_file(slug, dest)
        return
    for row in await store.list_files(slug):
        await store.delete_file(slug, row["path"])


async def _import_local(context: ToolContext, services: SessionServices, store: PageStore, slug: str, source: Path, dest: str, replace: bool) -> tuple[int, int, int] | ToolResult:
    if source.is_file():
        if replace:
            await _empty(store, slug, dest)
        written = await store.write_stream(slug, _join(dest, source.name), _chunks(source))
        return 1, int(written["bytes"]), 0
    if not source.is_dir():
        raise FileNotFoundError(source)
    tree, skipped = await asyncio.to_thread(_local_tree, source)
    # A folder that holds a protected file (the state volume, a key) is refused whole, before anything
    # is written: an import must not be a way to carry what Read would refuse into a page.
    for _, path in tree:
        if refusal := refuse_protected(context, services, path, "read"):
            return refusal
    if replace and not dest:
        copied = await store.replace_draft(slug, source)
        return int(copied["files"]), int(copied["bytes"]), skipped
    if replace:
        await _empty(store, slug, dest)
    size = 0
    for rel, path in tree:
        written = await store.write_stream(slug, _join(dest, rel), _chunks(path))
        size += int(written["bytes"])
    return len(tree), size, skipped


async def _import_remote(services: SessionServices, store: PageStore, slug: str, source: Path, dest: str, replace: bool) -> tuple[int, int, int]:
    """The same import from the machine a session works on, which this process reaches only through its shell."""
    fs = services.fs
    if await fs.is_file(source):
        data = await fs.read_bytes(source, limit=FETCH_MAX_BYTES)
        if replace:
            await _empty(store, slug, dest)
        await store.write_stream(slug, _join(dest, source.name), _once(data))
        return 1, len(data), 0
    if not await fs.is_dir(source):
        raise FileNotFoundError(source)
    names = await fs.find(source, "*", IMPORT_MAX_FILES)
    if replace:
        await _empty(store, slug, dest)
    size = 0
    for rel in names:
        data = await fs.read_bytes(source / rel, limit=FETCH_MAX_BYTES)
        await store.write_stream(slug, _join(dest, PurePosixPath(rel).as_posix()), _once(data))
        size += len(data)
    return len(names), size, 0


@tool_group("pages")
@search_hint(
    "read page file show page source html of the page version "
    "прочитать файл страницы показать исходник страницы версия страницы"
)
@tool(
    name="PageRead",
    description="Read one text file of a custom page: from the draft, or from a published version (version=N, from PageFiles or PageList).",
)
async def page_read(context: ToolContext, slug: str, path: str, version: int | None = None) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    store: PageStore = desk.store
    try:
        manifest = await store.require(slug)
        if version is not None and not any(v["n"] == version for v in manifest["versions"]):
            raise PageError(f"the page {slug!r} has no version {version}", 404)
        target = store.draft_file(slug, path) if version is None else store.version_file(slug, int(version), path)
    except PageError as exc:
        return _refused(context, exc)
    side = "the draft" if version is None else f"version {version}"
    if not target.is_file():
        return error(context, f"{side} of {slug!r} has no {path!r}; PageFiles lists what it has")
    size = target.stat().st_size
    limit = max(1000, output_limit(context) - 400)
    data = await asyncio.to_thread(_head, target, limit * 4)
    # A NUL near the start is what marks a binary file; a cut through a multi-byte character at the
    # end of the window is not, so the text is decoded with replacement rather than refused.
    if b"\x00" in data[:8192]:
        return ok(context, f"{path} in {side} of {slug!r} is a binary file of {human_size(size)}.", bytes=size)
    text = data.decode("utf-8", errors="replace")
    head = f"{path} in {side} of {slug!r} ({human_size(size)}):\n"
    if len(text) > limit:
        text = text[:limit] + f"\n[... the rest of the file is not shown; it is {human_size(size)} in all]"
    return ok(context, head + text, bytes=size)


@tool_group("pages")
@search_hint(
    "page files list what is in the page draft version contents "
    "файлы страницы список что лежит в странице черновик версия содержимое"
)
@tool(name="PageFiles", description="The files of a custom page's draft, or of a published version (version=N), with their sizes.")
async def page_files(context: ToolContext, slug: str, version: int | None = None) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    try:
        rows = await desk.store.list_files(slug, version)
    except PageError as exc:
        return _refused(context, exc)
    side = "the draft" if version is None else f"version {version}"
    if not rows:
        return ok(context, f"{side} of {slug!r} has no files.", count=0)
    total = sum(int(r["bytes"]) for r in rows)
    lines = [f"- {r['path']} ({human_size(int(r['bytes']))})" for r in rows[:LIST_SHOWN]]
    if len(rows) > LIST_SHOWN:
        lines.append(f"… and {len(rows) - LIST_SHOWN} more")
    return ok(context, f"{side} of {slug!r}: {len(rows)} files, {human_size(total)}\n" + "\n".join(lines), count=len(rows))


@tool_group("pages")
@search_hint(
    "delete page file remove from page draft folder "
    "удалить удали файл страницы убрать убери из черновика папку стереть лишний"
)
@tool(name="PageDelete", description="Delete one file, or a folder with everything in it, from a custom page's draft.")
async def page_delete(context: ToolContext, slug: str, path: str) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    try:
        removed = await desk.store.delete_file(slug, path)
    except PageError as exc:
        return _refused(context, exc)
    if not removed:
        return error(context, f"the draft of {slug!r} has no {path!r}")
    return ok(context, f"Deleted {path} from the draft of {slug!r}.")


# -- looking at it ------------------------------------------------------------------------------


@tool_group("pages")
@search_hint(
    "check page screenshot phone desktop how does the page look console errors overflow test page before publish "
    "проверить страницу скриншот телефон десктоп как выглядит страница ошибки консоли переполнение перед публикацией"
)
@tool(
    name="PageCheck",
    description=(
        "Look at a custom page the way the operator will: it is loaded in a browser at 390x844 (phone) and 1440x900 "
        "(desktop), in the app's dark theme and in the light scheme, inside a frame that answers the bridge as the app "
        "does. Returns what a vision model saw on each screenshot (saved in your workspace under .page-check/), console "
        "errors and warnings, failed requests and files the page asked for that it does not have, horizontal overflow "
        "and small tap targets at phone width, and whether the bridge shook hands. The check runs without the "
        "operator's sign-in, so /api requests are answered 401 there and work in the app. Advice, not a gate: publish "
        "when you judge the page ready. draft=false checks the live version; look=false skips the vision model."
    ),
)
async def page_check(context: ToolContext, slug: str, draft: bool = True, look: bool = True) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    try:
        result = await desk.check(slug, draft=bool(draft), session_id=context.session_id)
    except PageError as exc:
        return _refused(context, exc)
    except Exception as exc:  # noqa: BLE001 — the browser service's refusals carry their own sentence
        message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
        return error(context, f"the page could not be looked at: {message}")
    services = services_for(context)
    saved = await _save_shots(services, slug, result.shots)
    seen: dict[str, str] = {}
    vision_note = ""
    if look and result.shots:
        seen, vision_note = await _look_at(context, services, result.shots)
    text = check_text(result, saved, seen, vision_note)
    return ok(context, text, slug=slug, screenshots=[saved.get(s.name) for s in result.shots if saved.get(s.name)])


async def _save_shots(services: SessionServices, slug: str, shots: list[Any]) -> dict[str, str]:
    """Each screenshot in the workspace, where ImageView can look closer and SendFile can hand it on."""
    saved: dict[str, str] = {}
    for shot in shots:
        rel = f"{CHECK_FOLDER}/{slug}/{shot.name}.{'png' if shot.mime == 'image/png' else 'jpg'}"
        try:
            target = services.resolve(rel, write=True)
            fs = services.fs
            if isinstance(fs, ShellFS):
                await fs.write_bytes(target, shot.data)
            else:
                await asyncio.to_thread(_write, target, shot.data)
        except (OSError, PermissionError, RuntimeError):
            continue
        saved[shot.name] = rel
    return saved


def _write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


async def _look_at(context: ToolContext, services: SessionServices, shots: list[Any]) -> tuple[dict[str, str], str]:
    manager = services.extra.get("manager")

    async def one(shot: Any) -> tuple[str, str | None, str]:
        question = VISION_QUESTION.format(width=shot.width, height=shot.height, view=shot.view, scheme=shot.scheme)
        try:
            answer, _ = await vision_look(services.extra.get("vision"), manager, shot.data, shot.mime, question, instruction=LOOK_INSTRUCTION, session_id=context.session_id)
        except VisionUnavailable as exc:
            return shot.name, None, str(exc)
        return shot.name, answer, ""

    answers = await asyncio.gather(*(one(s) for s in shots))
    seen = {name: answer for name, answer, _ in answers if answer}
    failure = next((why for _, answer, why in answers if answer is None and why), "")
    return seen, (f"No vision model looked at the screenshots: {failure}" if failure else "")


def _bridge_line(runs: list[dict[str, Any]]) -> str:
    reports = [r.get("report") or {} for r in runs]
    shaken = next((r["handshake"] for r in reports if r.get("handshake")), None)
    loaded = next(((r.get("measure") or {}).get("bridge") for r in reports if (r.get("measure") or {}).get("bridge")), None)
    if shaken:
        version = shaken.get("version") or loaded or "unknown version"
        return f"Bridge: shook hands (daedalus.js {version}); the page is handed the app's theme and follows it."
    if loaded:
        return f"Bridge: daedalus.js {loaded} is loaded but never asked the app for the theme (Daedalus.ready() was not reached)."
    return "Bridge: not used. The page keeps its own colours whatever theme the operator picks; load /pages/_kit/daedalus.js if it should follow the app."


def check_text(result: Any, saved: dict[str, str], seen: dict[str, str], vision_note: str) -> str:
    """The check in words, most telling first: what is broken, then how it looks."""
    side = "draft" if result.draft else "live version"
    lines = [f"Checked the {side} of {result.slug!r} ({result.kind}) at 390x844 and 1440x900, in the dark theme and the light scheme. "
             "This is advice: publish when you judge the page ready."]
    runs = result.runs
    unfinished = [r["name"] for r in runs if not r.get("report")]
    if unfinished:
        lines.append(f"Did not finish loading within the check's wait: {', '.join(unfinished)} (a slow CDN, a script that never ends, or a page that waits for something).")
    for r in runs:
        if r.get("navigation_error"):
            lines.append(f"{r['name']}: the browser could not open it: {r['navigation_error']}")
    lines.append(_bridge_line(runs))
    phone = next(((r.get("report") or {}).get("measure") or {} for r in runs if r["view"] == "phone" and r.get("report")), {})
    if phone.get("reachable") is False:
        lines.append("Phone width: the frame's document could not be read, so overflow and tap targets were not measured.")
    elif phone:
        width, scrolled = int(phone.get("width") or 0), int(phone.get("scroll_width") or 0)
        if scrolled > width + 1:
            wide = ", ".join(f"{w['el']} ({w['width']} px wide, to {w['right']} px)" for w in phone.get("wide") or [])
            lines.append(f"Phone width: OVERFLOWS sideways — the content is {scrolled} px wide on a {width} px screen." + (f" Where it starts: {wide}." if wide else ""))
        elif phone.get("wide"):
            wide = ", ".join(f"{w['el']} (to {w['right']} px)" for w in phone["wide"])
            lines.append(f"Phone width: no sideways scroll, but these reach past the {width} px edge and are cut off: {wide}.")
        else:
            lines.append(f"Phone width: fits ({width} px, no horizontal overflow).")
        if not phone.get("viewport_meta"):
            lines.append('No <meta name="viewport" content="width=device-width, initial-scale=1">: a phone draws the page at desktop width and shrinks it.')
        if phone.get("small"):
            lines.append(f"Small tap targets at phone width (under 24 px): {phone['small']}, e.g. {', '.join(phone.get('small_examples') or [])}.")
        if not phone.get("text_chars") and int(phone.get("elements") or 0) < 8:
            lines.append("The page looks empty at phone width: almost no elements and no text.")
    console = result.console
    if console is None:
        pass
    elif console:
        shown: dict[str, int] = {}
        for entry in console:
            key = f"{entry.get('level')}: {str(entry.get('text') or '').strip()[:300]}" + (f" ({str(entry.get('url') or '')[-120:]}:{entry.get('line')})" if entry.get("url") else "")
            shown[key] = shown.get(key, 0) + max(1, int(entry.get("count") or 1))
        lines.append("Console (all four looks):\n" + "\n".join(f"- {k}" + (f" ×{n}" if n > 1 else "") for k, n in list(shown.items())[:20]))
    else:
        lines.append("Console: no errors or warnings.")
    misses = sorted({(e.method, e.path, e.status) for e in result.exchanges if e.status >= 400 and not e.path.startswith("/api/") and e.path != "/favicon.ico"})
    if misses:
        lines.append("The page asked its own address for what it does not have:\n" + "\n".join(f"- {m} {p} → {s}" for m, p, s in misses[:20]))
    api = sorted({(e.method, e.path) for e in result.exchanges if e.path.startswith("/api/")})
    if api:
        lines.append(f"{len(api)} request{'s' if len(api) != 1 else ''} to /api were answered 401 because the check carries no sign-in (in the app they carry the operator's session): "
                     + ", ".join(f"{m} {p}" for m, p in api[:6]) + ("…" if len(api) > 6 else ""))
    failed = [r for r in result.failed or [] if not str(r.get("url") or "").split("?")[0].endswith("/__report")]
    if failed:
        lines.append("Failed requests:\n" + "\n".join(f"- {r.get('method') or 'GET'} {str(r.get('url') or '')[:300]}: {r.get('failed') or r.get('blocked') or r.get('status')}" for r in failed[:20]))
    lines.extend(result.notes)
    if result.shots:
        lines.append("Screenshots" + (" (ImageView looks closer, SendFile hands one to the operator):" if saved else ":"))
        for shot in result.shots:
            where = saved.get(shot.name, "(not saved: the workspace is not writable)")
            said = seen.get(shot.name)
            lines.append(f"- {shot.name} {shot.width}x{shot.height}: {where}" + (f"\n  {said.strip()}" if said else ""))
        if vision_note:
            lines.append(vision_note)
    else:
        lines.append("No screenshot came back from the browser.")
    return "\n".join(lines)


# -- publishing ---------------------------------------------------------------------------------


@tool_group("pages")
@search_hint(
    "publish page make live release page version deploy page to the app rail "
    "опубликовать страницу выложить сделать живой версия страницы в приложение на рейку"
)
@tool(
    name="PagePublish",
    description=(
        "Make a custom page's draft its live version (a new version number; earlier ones stay for PageRollback). "
        "Every open app shows it at once; nothing restarts. note: one line on what changed."
    ),
)
async def page_publish(context: ToolContext, slug: str, note: str = "") -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    try:
        manifest = await desk.store.publish(slug, note)
    except PageError as exc:
        return _refused(context, exc)
    version = manifest["version"]
    title = _plain_title(manifest["title"])
    lines = [
        f"Published {slug!r} as version {version}. It is live at {_addresses(slug)} and in the app's list of pages.",
        f"To put it on the rail: SettingsGet('navigation'), then SettingsChange({{\"navigation\": {{\"rail\": [<the current items>, \"page:{slug}\"]}}}}) — "
        "keep the items already there; 'drawer' is the phone's list, the same way.",
        f"Show it to the operator with PageShow({slug!r}), or link it in your answer as [{title}](/app/p/{slug}).",
    ]
    return ok(context, "\n".join(lines), slug=slug, version=version)


@tool_group("pages")
@search_hint(
    "rollback page previous version undo publish restore "
    "вернуть верни страницу предыдущая прошлая версия откатить откати публикацию"
)
@tool(name="PageRollback", description="Make an earlier (or later) published version of a custom page the live one; nothing is deleted, the draft is untouched.")
async def page_rollback(context: ToolContext, slug: str, version: int) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    try:
        await desk.store.rollback(slug, int(version))
    except PageError as exc:
        return _refused(context, exc)
    return ok(context, f"Version {version} of {slug!r} is live again; every open app shows it now. The draft is as it was.")


async def _draft_changed(store: PageStore, manifest: dict[str, Any], live: dict[str, Any]) -> bool:
    """Whether the draft differs from the live version. The times are to the second, so a write in the
    same second as the publish is told apart by the files themselves (their paths and sizes)."""
    edited, published = str(manifest["draft"].get("updated_at") or ""), str(live.get("at") or "")
    if edited != published:
        return edited > published
    slug = manifest["slug"]
    sides = await asyncio.gather(asyncio.to_thread(store.files, slug), asyncio.to_thread(store.files, slug, live["n"]))
    return [(f["path"], f["bytes"]) for f in sides[0]] != [(f["path"], f["bytes"]) for f in sides[1]]


@tool_group("pages")
@search_hint(
    "list pages custom pages which pages exist my screens "
    "список страниц какие есть страницы свои мои экраны покажи перечень"
)
@tool(name="PageList", description="The custom pages: slug, title, kind, the live version and whether the draft has changed since, and the page's agent (its bound session).")
async def page_list(context: ToolContext) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    pages = await desk.store.list()
    if not pages:
        return ok(context, "There are no custom pages yet. PageCreate makes one.", count=0)
    lines = []
    for m in pages:
        live = next((v for v in m["versions"] if v["n"] == m.get("version")), None)
        kind = (live or m["draft"]).get("kind")
        state = f"live v{m['version']}" if live else "not published"
        if live and await _draft_changed(desk.store, m, live):
            state += ", the draft has changed since"
        kept = ", ".join(str(v["n"]) for v in m["versions"])
        owner = (" · agent: this session" if m["session_id"] == context.session_id else f" · agent: session {m['session_id']}") if m.get("session_id") else ""
        lines.append(f"- {m['slug']} \"{_plain_title(m['title'])}\" · {kind} · {state}" + (f" (versions {kept})" if kept else "") + f" · /app/p/{m['slug']}{owner}")
    return ok(context, "\n".join(lines), count=len(pages))


@tool_group("pages")
@search_hint(
    "remove page delete custom page drop page "
    "удалить удали страницу убрать убери снести снеси выбросить целиком"
)
@tool(
    name="PageRemove",
    description=(
        "Remove a custom page with every version and everything it stored; this cannot be undone. A page that was ever "
        "published is removed only with confirm=true, and only once the operator said so: ask with AskUser first "
        "unless the operator asked for the removal in so many words."
    ),
)
async def page_remove(context: ToolContext, slug: str, confirm: bool = False) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    store: PageStore = desk.store
    try:
        manifest = await store.require(slug)
    except PageError as exc:
        return _refused(context, exc)
    if manifest["versions"] and not confirm:
        count = len(manifest["versions"])
        return error(context, (
            f"{slug!r} has {count} published version{'s' if count != 1 else ''}, and removing it deletes them, the draft and the page's stored "
            "values for good. Ask the operator with AskUser (unless they asked for this removal themselves), then call PageRemove again "
            "with confirm=true. To take it off the rail without deleting anything, change the navigation instead."
        ))
    try:
        await store.remove(slug)
    except PageError as exc:
        return _refused(context, exc)
    return ok(context, f"Removed {slug!r}: its versions, draft and stored values are gone. A rail item 'page:{slug}' is ignored from now on; drop it from the navigation when you next change it.")


@tool_group("pages")
@search_hint(
    "show page to operator open page card link in chat look at my page "
    "показать страницу оператору открыть страницу карточка ссылка в чате посмотри мою страницу"
)
@tool(
    name="PageShow",
    description=(
        "Show a custom page to the operator: a card with an Open button appears in this chat and opens it in the app. "
        "draft=true opens the draft instead of the live version, for the operator to look before you publish."
    ),
)
async def page_show(context: ToolContext, slug: str, draft: bool = False) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    try:
        manifest = await desk.store.require(slug)
    except PageError as exc:
        return _refused(context, exc)
    title = _plain_title(manifest["title"])
    path = f"/app/p/{slug}" + ("?draft=1" if draft else "")
    if manifest.get("version") is None and not draft:
        side = "It has nothing published, so the app shows its draft."
    else:
        side = "The card opens the draft." if draft else f"The card opens version {manifest['version']}."
    return ok(context, f"Shown {title!r} to the operator: a card with Open is in this chat ({path}). {side} If they are not watching this chat, "
                       f"Notify(title, link='{path}') reaches them elsewhere.", slug=slug, path=path, title=title)


# -- the page's agent ---------------------------------------------------------------------------


@tool_group("pages")
@search_hint(
    "page agent bind session to page own agent of the page hand page to another session unbind "
    "агент страницы привязать сессию к странице свой агент передать страницу другой сессии отвязать"
)
@tool(
    name="PageBind",
    description=(
        "Make a session the custom page's own agent: the app shows its conversation beside the page, and the page's "
        "Daedalus.agent.ask(…) reaches it. session_id omitted binds this session; unbind=true leaves the page without an "
        "agent. A page has one agent at a time; PageCreate already binds the session that made the page."
    ),
)
async def page_bind(context: ToolContext, slug: str, session_id: str | None = None, unbind: bool = False) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    store: PageStore = desk.store
    try:
        manifest = await store.require(slug)
        if unbind:
            if not manifest.get("session_id"):
                return ok(context, f"{slug!r} has no agent; nothing changed.")
            await store.update(slug, session_id=None)
            return ok(context, f"{slug!r} has no agent now; the app no longer offers a conversation beside it, and agent.ask from the page is refused until one is bound.")
        target = (session_id or "").strip() or context.session_id
        title = await desk.session_title(target)
        if title is None:
            return error(context, f"there is no session {target!r}")
        await store.update(slug, session_id=target)
    except PageError as exc:
        return _refused(context, exc)
    who = "this session" if target == context.session_id else f"the session {title!r} ({target})"
    before = manifest.get("session_id")
    was = f" (it was {before})" if before and before != target else ""
    return ok(context, f"{who[0].upper()}{who[1:]} is the agent of {slug!r} now{was}: its conversation opens beside the page, and messages the "
                       "page sends arrive there marked as the page's.", slug=slug, session_id=target)


@tool_group("pages")
@search_hint(
    "push data to the open page notify page reload model update page live event send to page "
    "отправить данные на страницу обновить открытую страницу событие странице перезагрузить модель"
)
@tool(
    name="PageEmit",
    description=(
        "Push data to every open instance of a custom page: the page hears it with Daedalus.on(event, fn) — say a scene "
        "was rebuilt (PageEmit('cad', 'scene.updated', {\"file\": \"model.glb\"})) and the page reloads it without the "
        "operator lifting a finger. event: 1-64 letters, digits, . _ : - ('theme' and 'agent.*' are the bridge's own). "
        "data: any JSON up to 64 KB. Nothing is kept: a page opened later never sees it, so keep lasting state in "
        "PageStorage or a file of the page and emit only that it changed."
    ),
)
async def page_emit(context: ToolContext, slug: str, event: str, data: Any = None) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    try:
        await desk.store.emit(slug, event, data)
    except PageError as exc:
        return _refused(context, exc)
    return ok(context, f"Sent {event!r} to every open instance of {slug!r}; one opened later will not see it.", slug=slug, event=event)


STORAGE_OPS = ("get", "set", "delete", "list")


@tool_group("pages")
@search_hint(
    "page storage read write page state shared state with page key value page data settings saved by page "
    "хранилище страницы прочитать записать состояние страницы общие данные со страницей ключ значение"
)
@tool(
    name="PageStorage",
    description=(
        "Read or write a custom page's own storage, the durable values the page keeps with Daedalus.storage: share state "
        "with the page (a scene, settings, the operator's entries in a form, results for it to show). op: 'get' (key), "
        "'set' (key, value: any JSON), 'delete' (key), 'list' (key = a prefix, or empty for all). After a 'set' the open "
        "page sees the new value only when it reads again: PageEmit tells it to."
    ),
)
async def page_storage(context: ToolContext, slug: str, op: str, key: str = "", value: Any = None) -> ToolResult:
    desk = _desk(context)
    if desk is None:
        return _unavailable(context)
    if op not in STORAGE_OPS:
        return error(context, f"op is one of {', '.join(STORAGE_OPS)}")
    store: PageStore = desk.store
    try:
        if op == "list":
            rows = await store.storage_list(slug, key)
            if not rows:
                return ok(context, f"{slug!r} keeps nothing" + (f" under {key!r}." if key else "."), count=0)
            lines = [f"- {row['key']} ({human_size(row['bytes'])}, {row['updated_at']})" for row in rows[:LIST_SHOWN]]
            if len(rows) > LIST_SHOWN:
                lines.append(f"… and {len(rows) - LIST_SHOWN} more; narrow with a prefix")
            return ok(context, f"{len(rows)} keys in {slug!r}:\n" + "\n".join(lines), count=len(rows))
        if not key:
            return error(context, f"op {op!r} needs a key")
        if op == "get":
            found = await store.storage_get(slug, key)
            if found is None:
                return ok(context, f"Nothing is stored under {key!r} in {slug!r}.", found=False)
            text = json.dumps(found["value"], ensure_ascii=False, indent=2)
            return ok(context, f"{key!r} in {slug!r} (written {found['updated_at']}):\n{text}", found=True)
        if op == "set":
            written = await store.storage_set(slug, key, value)
            return ok(context, f"Stored {key!r} in {slug!r} ({written['updated_at']}). PageEmit tells an open page to read it again.", key=key)
        removed = await store.storage_delete(slug, key)
        return ok(context, f"Deleted {key!r} from {slug!r}." if removed else f"Nothing was stored under {key!r} in {slug!r}.", deleted=removed)
    except (PageError, ValueError) as exc:
        return error(context, str(exc))


TOOLS = [page_create, page_write, page_import, page_read, page_files, page_delete, page_check, page_publish, page_rollback, page_list, page_remove, page_show,
         page_bind, page_emit, page_storage]

__all__ = ["TOOLS", "check_text"]
