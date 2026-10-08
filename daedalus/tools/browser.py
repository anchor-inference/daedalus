"""The browser tools: a real browser the agent drives by an outline of the page, and the operator watches.

Each tool is a thin call into ``daedalus.browser.agent``, the one place their operations live, which
command-line staff reach through their launch's MCP entry as well. What is particular to a Daedalus
session is here: who the owner is (the session, in its project), where its files are (its walls),
how a sensitive action is asked about (the session's own grants), and how a screenshot is looked at
(the vision model, so pixels never enter the agent's context).

The tools are registered only on an installation that has a browser daemon (``capabilities``).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.browser.agent import LOOK_INSTRUCTION, BrowserAgent, Caller, SensitiveAsk
from daedalus.browser.model import EnvUnavailable, Forbidden, NotFound, Owner
from daedalus.host.filesystem import ShellFS
from daedalus.host.services import SessionServices
from daedalus.stores.files import FileRefused, human_size, parse_handle, safe_name
from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import FETCH_MAX_BYTES, FRAME_CHARS, error, ok, output_limit, services_for
from daedalus.tools.vision import VisionUnavailable, look

BROWSER_TOOLS = (
    "BrowserOpen", "BrowserNavigate", "BrowserSnapshot", "BrowserText", "BrowserLook", "BrowserAct",
    "BrowserTabs", "BrowserWait", "BrowserDialog", "BrowserHandoff", "BrowserClose", "BrowserDownload", "BrowserNote",
    "BrowserLogs", "BrowserNetwork", "BrowserInspect",
)
READ_ONLY_TOOLS = ("BrowserSnapshot", "BrowserText", "BrowserLook", "BrowserTabs", "BrowserWait", "BrowserLogs", "BrowserNetwork", "BrowserInspect")
"""The tools that only read the page or wait on it: a command-line agent's own permission rules may
let these through without asking; every other one changes something."""
DOWNLOADS_DIR = "downloads"
NETWORK_ASK = (
    "A local or LAN address (a router's page, a device, a dev server on another port) is asked about before it opens: "
    "say in why what the task needs there; a staff member's request goes to its orchestrator, everyone else's to the operator."
)
"""How the two tools that load an address say that some addresses wait for a yes."""


def _agent(context: ToolContext) -> BrowserAgent | None:
    manager = services_for(context).extra.get("manager")
    agent = manager.service_hooks.get("browser") if manager is not None else None
    return agent if isinstance(agent, BrowserAgent) else None


class SessionFiles:
    """A Daedalus session's files as the browser sees them: read and written under its walls."""

    def __init__(self, services: SessionServices, manager: Any, owner: Owner) -> None:
        self.services = services
        self.manager = manager
        self.owner = owner

    async def read(self, path: str) -> tuple[str, bytes]:
        store = getattr(self.manager, "files", None)
        if parse_handle(path) is not None:
            if store is None or not self.owner.project_id:
                raise Forbidden(f"{path} is a file handle, and this session is not in a project that keeps files")
            try:
                stored = await store.in_scope(path, self.owner.project_id)
            except FileRefused as exc:
                raise Forbidden(str(exc)) from None
            return stored.name, await store.read(stored)
        try:
            target = self.services.resolve(path)
        except PermissionError as exc:
            raise Forbidden(str(exc)) from None
        if self.services.is_protected(target):
            raise Forbidden(f"{target} is protected and cannot be uploaded by tools")
        fs = self.services.fs
        if fs.remote:
            # A session working on the host names a file there, which this process cannot open.
            try:
                return target.name, await fs.read_bytes(target, limit=FETCH_MAX_BYTES)
            except FileNotFoundError:
                raise NotFound(f"no such file: {target}") from None
            except (OSError, ValueError) as exc:
                raise Forbidden(str(exc)) from None
        if not target.is_file():
            raise NotFound(f"no such file: {target}")
        return target.name, await asyncio.to_thread(target.read_bytes)

    async def save(self, name: str, data: bytes, to: str | None) -> str:
        clean = safe_name(name) or "download"
        wanted = to or f"{DOWNLOADS_DIR}/{clean}"
        try:
            target = self.services.resolve(wanted, write=True)
        except PermissionError as exc:
            raise Forbidden(str(exc)) from None
        if self.services.is_protected(target):
            raise Forbidden(f"{target} is protected and cannot be written by tools")
        fs = self.services.fs
        if isinstance(fs, ShellFS):
            # The download belongs where the session works: on the host for a session working there.
            target = await _write_new_remote(fs, target, data, keep=to is not None)
        else:
            target = await asyncio.to_thread(_write_new, target, data, keep=to is not None)
        said = f"Saved {clean} ({human_size(len(data))}) to {target}."
        store = getattr(self.manager, "files", None)
        if store is not None and self.owner.project_id:
            try:
                stored = await store.add(data, name=clean, origin="browser", origin_ref=f"{self.owner.kind}:{self.owner.id}", scope=self.owner.project_id, actor=f"agent:{self.owner.id}")
                said += f" It is also kept as {stored.handle}, which passes to the team by handle."
            except FileRefused:
                pass  # past the store's size: the copy in the workspace is what there is
        return said


async def _write_new_remote(fs: ShellFS, target: Path, data: bytes, *, keep: bool) -> Path:
    """:func:`_write_new` on the machine the session's shell reaches."""
    final = target
    if not keep:
        n = 1
        while await fs.exists(final):
            final = target.with_name(f"{target.stem}-{n}{target.suffix}")
            n += 1
    try:
        await fs.write_bytes(final, data)
    except OSError as exc:
        raise Forbidden(f"could not save {final.name}: {exc}") from None
    return final


def _write_new(target: Path, data: bytes, *, keep: bool) -> Path:
    """Write ``data`` at ``target`` through a temporary file; a download never overwrites a file the
    agent did not name, so an existing name gets a number."""
    target.parent.mkdir(parents=True, exist_ok=True)
    final = target
    if not keep:
        n = 1
        while final.exists():
            final = target.with_name(f"{target.stem}-{n}{target.suffix}")
            n += 1
    temporary = final.with_name(f".{final.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)
    return final


def _caller(context: ToolContext) -> Caller | None:
    services = services_for(context)
    manager = services.extra.get("manager")
    if manager is None:
        return None
    state = manager.live_state(context.session_id)
    metadata = state.metadata if state is not None else {}
    project = state.project.id if state is not None and state.project is not None else None
    # A chat's own throwaway project goes with its last session; a site note learnt there is kept for
    # every agent rather than filed under a project nobody will open again.
    lasting = project if state is not None and state.project is not None and not state.project.settings.ephemeral else ""
    owner = Owner("session", context.session_id, project_id=project, session_id=context.session_id, staff_id=str(metadata.get("staff_id") or "") or None)

    async def gate(ask: SensitiveAsk) -> tuple[bool, str]:
        allowed, why = manager.browser_gate(context.session_id, ask)
        return bool(allowed), str(why)

    async def looked(data: bytes, mime: str, question: str) -> str:
        try:
            answer, _ = await look(services.extra.get("vision"), manager, data, mime, question, instruction=LOOK_INSTRUCTION)
        except VisionUnavailable as exc:
            raise EnvUnavailable(str(exc)) from None
        return answer

    budget = max(4000, output_limit(context) - FRAME_CHARS - 600)
    return Caller(owner=owner, actor=f"agent:{context.session_id}", gate=gate, files=SessionFiles(services, manager, owner), look=looked, budget=budget, note_scope=lasting)


async def _run(context: ToolContext, name: str, arguments: dict[str, Any]) -> ToolResult:
    agent = _agent(context)
    caller = _caller(context) if agent is not None else None
    if agent is None or caller is None:
        return error(context, "the browser is not available in this installation")
    text, failed = await agent.run(name, {k: v for k, v in arguments.items() if v is not None}, caller)
    return error(context, text) if failed else ok(context, text)


@tool_group("browser")
@search_hint(
    "launch start chromium chrome real browser watch live login profile incognito throwaway "
    "браузер браузера запустить запусти открыть открой хром инкогнито профиль логины поднять подними"
)
@tool(
    name="BrowserOpen",
    description=(
        "Start your browser, or come back to it: a real Chromium the operator can watch live and take over. It keeps "
        "your project's logins between sessions. url opens a page at once; fresh=true gives a throwaway browser whose "
        "cookies are wiped when it closes. Returns the tabs. Next: BrowserSnapshot to see the page. " + NETWORK_ASK
    ),
)
async def browser_open(context: ToolContext, url: str | None = None, fresh: bool = False, why: str | None = None) -> ToolResult:
    return await _run(context, "BrowserOpen", {"url": url, "fresh": fresh, "why": why})


@tool_group("browser")
@search_hint(
    "go to address url visit website homepage back forward reload refresh open page "
    "перейти перейди зайти зайди сайт адрес ссылка главная страница назад вперед обновить обнови перезагрузить перезагрузи"
)
@tool(
    name="BrowserNavigate",
    description=(
        "Go to a URL in the current tab (http and https only), or go='back', 'forward' or 'reload'. tab picks another "
        "tab by id. Next: BrowserSnapshot to see where you are. " + NETWORK_ASK
    ),
)
async def browser_navigate(context: ToolContext, url: str | None = None, go: str | None = None, tab: str | None = None, why: str | None = None) -> ToolResult:
    return await _run(context, "BrowserNavigate", {"url": url, "go": go, "tab": tab, "why": why})


@tool_group("browser")
@search_hint(
    "page structure elements outline buttons fields links refs dom accessibility tree interactive references "
    "структура страницы элементы кнопки поля ссылки разметка дерево интерактивные осмотреть осмотри снимок"
)
@tool(
    name="BrowserSnapshot",
    description=(
        "See the page: an outline of its landmarks, headings, links, fields and buttons, each actionable one with a "
        "ref (e14, f2e4 inside a frame). Password and payment fields show as [secret], never their value. scope=<ref> "
        "reads only that part, whole. view='viewport' reads only what is on screen and half a screen around it. Next: "
        "BrowserAct with a ref from here."
    ),
)
async def browser_snapshot(context: ToolContext, tab: str | None = None, scope: str | None = None, view: str | None = None) -> ToolResult:
    return await _run(context, "BrowserSnapshot", {"tab": tab, "scope": scope, "view": view})


@tool_group("browser")
@search_hint(
    "reader view page text article clean plain text without markup extract readable "
    "текст страницы статья чистый текст без разметки выписать выпиши вытащить вытащи прочитать прочитай сайт"
)
@tool(
    name="BrowserText",
    description=(
        "Read the page's main text as a reader view gives it — an article, documentation, a result list — without "
        "the outline. ref reads one element's text; max_chars bounds it. find searches the visible text like Ctrl+F "
        "(regex=true for a regular expression) and returns each match with the ref of the control it is in or beside. "
        "query pulls out only what you ask ('every product with its price') through a smaller model reading the page "
        "part by part; schema (a JSON schema of one item) shapes the items; the same query on the next page adds to "
        "what was collected. For acting on the page use BrowserSnapshot."
    ),
)
async def browser_text(
    context: ToolContext,
    tab: str | None = None,
    ref: str | None = None,
    max_chars: int | None = None,
    find: str | None = None,
    regex: bool = False,
    query: str | None = None,
    schema: dict[str, Any] | None = None,
) -> ToolResult:
    return await _run(context, "BrowserText", {"tab": tab, "ref": ref, "max_chars": max_chars, "find": find, "regex": regex or None, "query": query, "schema": schema})


@tool_group("browser")
@search_hint(
    "vision look at the page chart canvas map layout visual appearance how it looks "
    "как выглядит визуально график карта верстка макет картинка страницы глянуть глянь рассмотреть рассмотри"
)
@tool(
    name="BrowserLook",
    description=(
        "Ask a vision model about what the page shows — a chart, a canvas, a map, a layout — with your question; the "
        "answer is text, the picture never reaches you. ref pictures one element; full_page the whole length. Secret "
        "fields are blanked first."
    ),
)
async def browser_look(context: ToolContext, question: str, tab: str | None = None, ref: str | None = None, full_page: bool = False) -> ToolResult:
    return await _run(context, "BrowserLook", {"question": question, "tab": tab, "ref": ref, "full_page": full_page})


@tool_group("browser")
@search_hint(
    "click type fill form input press enter hover select option check scroll drag upload submit search bar "
    "кликнуть кликни нажать нажми ткнуть ткни ввести введи вбить вбей заполнить заполни форма кнопка поле прокрутить прокрути "
    "скролл"
)
@tool(
    name="BrowserAct",
    description=(
        "Act on the page by ref from BrowserSnapshot. action: click, double_click, right_click, hover, type (text; "
        "submit=true presses Enter after), press (keys: 'Enter', 'Tab', 'Escape', 'Ctrl+A'), select (option by label), "
        "check, uncheck, scroll (a ref into view; direction 'up', 'down', 'left' or 'right' scrolls the page, or with a "
        "ref the pane it is in; text scrolls to the first place that says it), drag (to to_ref), upload (paths of files "
        "in your workspace, or file handles). element is required: say in words what you act on ('the Add to cart "
        "button'). x and y (CSS pixels of the viewport) click or hover at a point instead of a ref, where the operator "
        "allows it. steps=[{action, ref, element, …}, …] does up to 5 actions planned from one snapshot, stopping at a "
        "navigation, a new tab, a dialog, a failure or a question. A purchase, a message sent, a deletion, terms "
        "accepted or an upload is held for the operator's approval when you try it (do not ask first); a password, code or payment field refuses you (use "
        "BrowserHandoff). Returns what changed."
    ),
)
async def browser_act(
    context: ToolContext,
    action: str | None = None,
    element: str | None = None,
    ref: str | None = None,
    text: str | None = None,
    keys: str | None = None,
    option: str | None = None,
    submit: bool = False,
    to_ref: str | None = None,
    direction: str | None = None,
    paths: list[str] | None = None,
    x: float | None = None,
    y: float | None = None,
    steps: list[dict[str, Any]] | None = None,
    tab: str | None = None,
) -> ToolResult:
    return await _run(context, "BrowserAct", {
        "action": action, "element": element, "ref": ref, "text": text, "keys": keys, "option": option, "submit": submit or None, "to_ref": to_ref, "direction": direction,
        "paths": paths, "x": x, "y": y, "steps": steps, "tab": tab,
    })


@tool_group("browser")
@search_hint(
    "tabs new tab switch tab close tab list open tabs browser windows "
    "вкладка вкладки вкладку новая вкладка переключить переключи табы таб список вкладок открытые вкладки браузера"
)
@tool(
    name="BrowserTabs",
    description="Your browser's tabs: action='list' (the default), 'new' (url opens in it), 'select' or 'close' (tab = its id).",
)
async def browser_tabs(context: ToolContext, action: str = "list", tab: str | None = None, url: str | None = None) -> ToolResult:
    return await _run(context, "BrowserTabs", {"action": action, "tab": tab, "url": url})


@tool_group("browser")
@search_hint(
    "wait until loaded appears disappears network idle rendered timeout spinner "
    "подождать подожди дождаться дождись загрузится появится исчезнет прогрузится отрисуется ожидание загрузки"
)
@tool(
    name="BrowserWait",
    description=(
        "Wait for the page, at most timeout_s (up to 60): until='load', 'idle' (the network quiet), 'text' (value "
        "appears), 'gone' (a ref or a text disappears) or 'url' (the address contains value). Never wait for time alone."
    ),
)
async def browser_wait(context: ToolContext, until: str = "load", value: str | None = None, timeout_s: float = 10.0, tab: str | None = None) -> ToolResult:
    return await _run(context, "BrowserWait", {"until": until, "value": value, "timeout_s": timeout_s, "tab": tab})


@tool_group("browser")
@search_hint(
    "alert confirm prompt dialog popup modal accept dismiss "
    "алерт диалог всплывающее окно попап модалка модальное подтвердить подтверди отклонить отклони принять прими"
)
@tool(
    name="BrowserDialog",
    description=(
        "Answer the page's confirm or prompt: accept=true or false, text for a prompt. Every other action waits while "
        "one is open. An alert, and the question a page asks before you leave it, are accepted for you and said in the "
        "result."
    ),
)
async def browser_dialog(context: ToolContext, accept: bool, text: str | None = None, tab: str | None = None) -> ToolResult:
    return await _run(context, "BrowserDialog", {"accept": accept, "text": text, "tab": tab})


@tool_group("browser")
@search_hint(
    "hand over to operator login sign in captcha two factor 2fa payment take over manually "
    "передать передай оператору войти капча двухфакторка 2фа оплата залогиниться залогинься вручную"
)
@tool(
    name="BrowserHandoff",
    description=(
        "Hand the browser to the operator for what only they may do: reason 'login', 'captcha', 'two_factor', "
        "'payment', 'confirm' or 'other'; what says what to do ('sign in to github.com'). They are notified; end your "
        "turn afterwards — a message comes when they give the browser back."
    ),
)
async def browser_handoff(context: ToolContext, reason: str, what: str) -> ToolResult:
    return await _run(context, "BrowserHandoff", {"reason": reason, "what": what})


@tool_group("browser")
@search_hint(
    "close browser quit exit close tab shut down browser end browsing "
    "закрыть закрой браузер вкладку выйти выйди выключить выключи погасить погаси хром"
)
@tool(
    name="BrowserClose",
    description="Close one tab (tab = its id), or the whole browser (all=true, or no tab). The profile and its logins stay for the next BrowserOpen.",
)
async def browser_close(context: ToolContext, tab: str | None = None, all: bool = False) -> ToolResult:  # noqa: A002 — the tool's argument name
    return await _run(context, "BrowserClose", {"tab": tab, "all": all})


@tool_group("browser")
@search_hint(
    "downloaded file save download into workspace copy download fetch browser download "
    "скачать скачай скачанный загрузки сохранить сохрани загруженный файл браузера забрать забери"
)
@tool(
    name="BrowserDownload",
    description=(
        "Copy a file the browser downloaded into your workspace: name as the action's result gave it. to is the path "
        "(default downloads/<name>). In a project the file is also kept by handle for the team."
    ),
)
async def browser_download(context: ToolContext, name: str, to: str | None = None) -> ToolResult:
    return await _run(context, "BrowserDownload", {"name": name, "to": to})


@tool_group("browser")
@search_hint(
    "remember site note what worked on this website tip next time propose note learned recorded procedure read steps "
    "заметка о сайте запомнить что сработало на сайте совет на будущее предложить заметку записанная процедура шаги"
)
@tool(
    name="BrowserNote",
    description=(
        "After a hard-won success on a site, propose a short note for the next agent there: what worked that was not "
        "obvious ('search answers only to Enter, not the button'). host defaults to the current tab's. The operator "
        "reads it first; only an approved note is shown, on that site. Never put secrets or personal data in it. "
        "read=<id> instead reads a procedure the operator recorded on a site, whole; its id comes with the site's notes."
    ),
)
async def browser_note(context: ToolContext, note: str | None = None, host: str | None = None, read: str | None = None) -> ToolResult:
    return await _run(context, "BrowserNote", {"note": note, "host": host, "read": read})


@tool_group("browser")
@search_hint(
    "console log errors exceptions uncaught devtools javascript error debug site broken failed to load warnings "
    "консоль лог логи ошибки исключения отладка девтулз джаваскрипт сайт сломан не работает не загрузилось предупреждения"
)
@tool(
    name="BrowserLogs",
    description=(
        "The page's console since your last BrowserLogs on that tab: what it logged, the errors it threw and did not "
        "catch (with where in the code), and what the browser said about it (a script or picture that failed to load, "
        "404s). For a site you build or one that misbehaves, where the page alone does not say what went wrong. "
        "level ('error', 'warning', 'info', 'debug') is the least severe to show; all=true reads everything the tab "
        "keeps."
    ),
)
async def browser_logs(context: ToolContext, tab: str | None = None, level: str | None = None, all: bool = False) -> ToolResult:  # noqa: A002 — the tool's argument name
    return await _run(context, "BrowserLogs", {"tab": tab, "level": level, "all": all or None})


@tool_group("browser")
@search_hint(
    "network requests xhr fetch api endpoint json response status headers devtools network tab find api 500 error "
    "сеть запросы апи эндпоинт джейсон ответ статус заголовки вкладка сеть найти апи ошибка сервера"
)
@tool(
    name="BrowserNetwork",
    description=(
        "The requests the page made since your last BrowserNetwork on that tab: id, method, status, type, size, time "
        "and address — to find the JSON API a page is built from, or why a request fails. type narrows it ('api' for "
        "xhr, fetch, websocket and eventsource; or 'document', 'script', 'image' …, comma-separated); host, contains, "
        "method and failed=true too; all=true lists everything the tab keeps. id='r12' shows one request with its "
        "headers; body=true adds its response body (JSON and text, at most max_chars). Read only: it never sends a "
        "request, and credentials (cookies, Authorization, tokens, keys) are cut before you see anything."
    ),
)
async def browser_network(
    context: ToolContext,
    tab: str | None = None,
    id: str | None = None,  # noqa: A002 — the tool's argument name
    body: bool = False,
    type: str | None = None,  # noqa: A002 — the tool's argument name
    host: str | None = None,
    contains: str | None = None,
    method: str | None = None,
    failed: bool = False,
    all: bool = False,  # noqa: A002 — the tool's argument name
    max_chars: int | None = None,
) -> ToolResult:
    return await _run(context, "BrowserNetwork", {
        "tab": tab, "id": id, "body": body or None, "type": type, "host": host, "contains": contains, "method": method, "failed": failed or None, "all": all or None,
        "max_chars": max_chars,
    })


@tool_group("browser")
@search_hint(
    "inspect element why not visible hidden covered not clickable css computed style box html markup devtools elements "
    "осмотреть элемент почему не видно скрыт перекрыт не кликается стили разметка хтмл размер позиция"
)
@tool(
    name="BrowserInspect",
    description=(
        "One element as a developer's tools show it: whether it is visible and can be clicked, and if not why "
        "(display none on which ancestor, opacity, no size, off the page, cut off by a pane, covered by what, "
        "disabled, pointer-events), its box, key computed styles, state (value, invalid and why) and its markup "
        "without scripts or secret values. ref from BrowserSnapshot; or selector (CSS) for an element the outline does "
        "not show, such as a hidden one. html=false leaves the markup out; max_chars bounds it."
    ),
)
async def browser_inspect(context: ToolContext, ref: str | None = None, selector: str | None = None, tab: str | None = None, html: bool = True, max_chars: int | None = None) -> ToolResult:
    return await _run(context, "BrowserInspect", {"ref": ref, "selector": selector, "tab": tab, "html": html, "max_chars": max_chars})


TOOLS = [
    browser_open, browser_navigate, browser_snapshot, browser_text, browser_look, browser_act, browser_tabs, browser_wait, browser_dialog, browser_handoff, browser_close,
    browser_download, browser_note, browser_logs, browser_network, browser_inspect,
]

__all__ = ["BROWSER_TOOLS", "READ_ONLY_TOOLS", "TOOLS", "SessionFiles"]
