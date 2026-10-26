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

from daedalus.browser.agent import LOOK_INSTRUCTION, SWITCH_ASK, BrowserAgent, Caller, SensitiveAsk
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
    "BrowserLogs", "BrowserNetwork", "BrowserInspect", "BrowserCookies", "BrowserLogin", "BrowserReplay", "BrowserAskOperatorChrome",
    "BrowserEval", "BrowserScripts", "BrowserExtensions",
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

    async def read_folder(self, path: str, *, max_files: int, max_bytes: int) -> tuple[str, dict[str, bytes]] | None:
        """The files of a folder of the session's (an extension it builds), or ``None`` when ``path`` is
        not a folder. Read through the session's filesystem, so a session working on the host hands over
        the folder there; the folders every project carries and an extension never needs (.git,
        node_modules, .venv) are left out, as the file search leaves them."""
        if parse_handle(path) is not None:
            return None
        try:
            target = self.services.resolve(path)
        except PermissionError as exc:
            raise Forbidden(str(exc)) from None
        fs = self.services.fs
        if not await fs.is_dir(target):
            return None
        if self.services.is_protected(target):
            raise Forbidden(f"{target} is protected and cannot be handed to the browser")
        names = await fs.find(target, "*", max_files + 1)
        if len(names) > max_files:
            raise Forbidden(f"{target} holds more than {max_files} files: name the extension's own folder (the one with manifest.json), or its build output")
        files: dict[str, bytes] = {}
        total = 0
        for rel in names:
            entry = target / rel
            if self.services.is_protected(entry):
                continue
            try:
                data = await fs.read_bytes(entry, limit=max_bytes - total)
            except FileNotFoundError:
                continue
            except ValueError:
                raise Forbidden(f"{target} holds more than {max_bytes >> 20} MiB: name the extension's own folder, or its build output") from None
            except OSError as exc:
                raise Forbidden(f"could not read {entry}: {exc}") from None
            total += len(data)
            files[rel.replace(os.sep, "/")] = data
        return target.name, files

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

    async def rules(ask: SensitiveAsk) -> SensitiveAsk:
        action_rules = getattr(manager, "action_rules", None)
        return await action_rules.browser(context.session_id, ask) if action_rules is not None else ask

    async def looked(data: bytes, mime: str, question: str) -> str:
        try:
            answer, _ = await look(services.extra.get("vision"), manager, data, mime, question, instruction=LOOK_INSTRUCTION, session_id=context.session_id)
        except VisionUnavailable as exc:
            raise EnvUnavailable(str(exc)) from None
        return answer

    async def ask_operator(key: str, text: str, card: dict[str, Any], *, on_answer: str = SWITCH_ASK, extra: dict[str, Any] | None = None) -> bool:
        tool = "BrowserAskOperatorChrome" if on_answer == SWITCH_ASK else "BrowserOpen"
        return bool(manager.ask_operator(context.session_id, key=key, tool=tool, text=text, on_answer=on_answer, card=card, extra=extra))

    budget = max(4000, output_limit(context) - FRAME_CHARS - 600)
    return Caller(
        owner=owner, actor=f"agent:{context.session_id}", gate=gate, files=SessionFiles(services, manager, owner), look=looked, budget=budget, note_scope=lasting, rules=rules,
        ask_operator=ask_operator,
    )


async def _run(context: ToolContext, name: str, arguments: dict[str, Any]) -> ToolResult:
    agent = _agent(context)
    caller = _caller(context) if agent is not None else None
    if agent is None or caller is None:
        return error(context, "the browser is not available in this installation")
    text, failed = await agent.run(name, {k: v for k, v in arguments.items() if v is not None}, caller)
    return error(context, text) if failed else ok(context, text)


@tool_group("browser")
@search_hint(
    "launch start chromium chrome real browser watch live login profile incognito throwaway webgl 3d gpu three.js canvas "
    "браузер браузера запустить запусти открыть открой хром инкогнито профиль логины поднять подними вебгл видеокарта"
)
@tool(
    name="BrowserOpen",
    description=(
        "Start your browser, or come back to it: a real Chromium the operator can watch live and take over. It keeps "
        "your project's logins between sessions. url opens a page at once; fresh=true gives a throwaway browser whose "
        "cookies are wiped when it closes. Returns the tabs. Next: BrowserSnapshot to see the page. It opens the operator's "
        "own Chrome only when they switched this conversation there; when they ask for their browser and have not, call "
        "BrowserAskOperatorChrome instead. " + NETWORK_ASK + " Your browser has no WebGL unless turned on: webgl='software' "
        "(drawn on the CPU, works everywhere) or 'gpu' (the machine's GPU, where one is passed in) asks the operator — say "
        "in why what needs 3D — unless they allowed it already; on their yes the browser restarts in that mode with its "
        "tabs. webgl='off' turns it off again without asking."
    ),
)
async def browser_open(context: ToolContext, url: str | None = None, fresh: bool = False, why: str | None = None, webgl: str | None = None) -> ToolResult:
    return await _run(context, "BrowserOpen", {"url": url, "fresh": fresh, "why": why, "webgl": webgl})


@tool_group("browser")
@search_hint(
    "my browser my chrome use my chrome operator's chrome signed in as me switch to my browser "
    "мой браузер моём браузере моем браузере моего браузера моём хроме мой хром моём chrome моем chrome мой профиль"
)
@tool(
    name="BrowserAskOperatorChrome",
    description=(
        "Ask the operator to let you work in their own Chrome, signed in as them. Use it when they ask for their browser "
        "(\"my browser\", \"in my Chrome\", \"в моём браузере\") and this conversation still uses yours: do not open your own "
        "instead. reason is one sentence they read on the card: what you need their Chrome for. Only their yes switches "
        "the conversation; end your turn after asking — a message brings the answer. When their Chrome is not connected "
        "it says so and asks nothing."
    ),
)
async def browser_ask_operator_chrome(context: ToolContext, reason: str) -> ToolResult:
    return await _run(context, "BrowserAskOperatorChrome", {"reason": reason})


@tool_group("browser")
@search_hint(
    "go to address url visit website homepage back forward reload refresh open page "
    "перейти перейди зайти зайди сайт адрес ссылка главная страница назад вперед обновить обнови перезагрузить перезагрузи"
)
@tool(
    name="BrowserNavigate",
    description=(
        "Go to a URL in the current tab (http and https only), or go='back', 'forward' or 'reload'. tab picks another "
        "tab by id. Every ref of the tab is stale afterwards: do not send BrowserAct for it in the same batch. Next: "
        "BrowserSnapshot to see where you are. " + NETWORK_ASK
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
        "(regex=true for a regular expression) and returns each match with the ref of the control it is in or beside; "
        "that control may be a whole menu or list, so for an item inside it take BrowserSnapshot(scope=<ref>) first. "
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
        "accepted, an upload, or a form posted in the operator's Chrome while they keep it read-only is held for the "
        "operator's approval when you try it (do not ask first); a password, code or payment field refuses you (use "
        "BrowserLogin to sign in, BrowserHandoff for the rest). Returns what changed."
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
    description=(
        "Your browser's tabs: action='list' (the default), 'new' (url opens in it), 'select' or 'close' (tab = its id). "
        "Prefer BrowserNavigate in the current tab; 'new' is for a second page you need at the same time, and an "
        "address already open in a tab switches to that tab instead. Close the tabs you no longer need."
    ),
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
    description=(
        "Close one tab (tab = its id), or the whole browser (all=true, or no tab). The profile and its logins stay for "
        "the next BrowserOpen. In the operator's own Chrome no tab closes every tab you opened; a tab the operator gave you "
        "is handed back to them, open, never closed."
    ),
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
        "Copy a file the browser downloaded into your workspace: name as the action's result gave it (a PDF a click or "
        "an address opens in the operator's Chrome arrives as a download). to is the path "
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
        "keeps. extension='<name or id>' reads the console of an extension of yours (BrowserExtensions) instead: its "
        "background service worker's logs and errors; its content scripts log into the page's console."
    ),
)
async def browser_logs(context: ToolContext, tab: str | None = None, level: str | None = None, all: bool = False, extension: str | None = None) -> ToolResult:  # noqa: A002 — the tool's argument name
    return await _run(context, "BrowserLogs", {"tab": tab, "level": level, "all": all or None, "extension": extension})


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


@tool_group("browser")
@search_hint(
    "cookies import load cookie file cookies.txt netscape json export cookie editor storage state session login sign in "
    "куки куки-файл импортировать импортируй загрузить загрузи кукисы сессия залогинить авторизация вход"
)
@tool(
    name="BrowserCookies",
    description=(
        "Your browser's cookies. action='import' (the default) loads a cookie file the operator gave you into it, so "
        "you are signed in where they are: path is the file (a cookies.txt from a browser extension, a Cookie-Editor, "
        "EditThisCookie or Playwright storageState JSON); or text is a copied Cookie header with domain naming its "
        "site. domain also narrows a file to one site. They go into your profile and stay with it. action='list' shows "
        "the sites, names and expiry (domain= narrows it); action='clear' removes one site's (domain required). "
        "Values are never shown. For scraping with curl the file itself may serve better."
    ),
)
async def browser_cookies(context: ToolContext, action: str = "import", path: str | None = None, text: str | None = None, domain: str | None = None) -> ToolResult:
    return await _run(context, "BrowserCookies", {"action": action, "path": path, "text": text, "domain": domain})


@tool_group("browser")
@search_hint(
    "sign in log in login password account credentials private form operator types username two factor code "
    "войти вход залогиниться авторизоваться авторизация пароль логин учетная запись аккаунт код подтверждения"
)
@tool(
    name="BrowserLogin",
    description=(
        "Get your browser signed in to a site without seeing the credentials: the operator types the username, password "
        "and any one-time code into a private form of the app, and the browser fills the page's sign-in form and sends it. "
        "url_or_domain is the sign-in page's address or the site (github.com); reason says what it is for (the operator "
        "reads it). selectors ({username, password, otp, submit} as CSS) name fields the guess misses. End your turn "
        "afterwards: a message says where the page is and whether the sign-in took (yes, no or unknown). A sign-in the "
        "operator saved and allowed for this session is done at once and the result says so."
    ),
)
async def browser_login(context: ToolContext, url_or_domain: str, reason: str, selectors: dict[str, str] | None = None) -> ToolResult:
    return await _run(context, "BrowserLogin", {"url_or_domain": url_or_domain, "reason": reason, "selectors": selectors})


@tool_group("browser")
@search_hint(
    "replay recorded skill steps operator demonstrated macro repeat workflow automation "
    "повторить повтори запись записанный навык макрос шаги оператора воспроизвести"
)
@tool(
    name="BrowserReplay",
    description=(
        "Run a skill the operator recorded in the browser: the steps they did, replayed in your browser with the "
        "parameters you give (params={name: value}). Without skill it lists the recorded skills and their parameters. "
        "Each step is your own action through the usual checks. When a step's element is no longer on the page the "
        "replay stops and names the step and what was left: finish from there with BrowserSnapshot and BrowserAct. "
        "At a sign-in the operator does, it pauses; from_step goes on after it."
    ),
)
async def browser_replay(context: ToolContext, skill: str | None = None, params: dict[str, str] | None = None, from_step: int | None = None) -> ToolResult:
    return await _run(context, "BrowserReplay", {"skill": skill, "params": params, "from_step": from_step})


@tool_group("browser")
@search_hint(
    "run javascript evaluate js in page console execute script devtools console change page test tweak dom style "
    "выполнить джаваскрипт js в странице консоль выполни скрипт поменять страницу протестировать изменить стиль дом"
)
@tool(
    name="BrowserEval",
    description=(
        "Run your own JavaScript in the page now, in the page's own world (as a developer's console runs it), and get "
        "the result back: for testing and development — read a value, try a change to the page's look or behaviour "
        "before building it. code is read as the console reads it: the last statement's value is the answer, a "
        "top-level await waits. Elements, objects and errors come back as readable JSON (max_chars bounds it), with "
        "what the console logged meanwhile; timeout_s (default 10, at most 60) stops a script that runs on. The change "
        "lasts until the page loads again; to run code on every load use BrowserScripts. Your own browser only."
    ),
)
async def browser_eval(context: ToolContext, code: str, tab: str | None = None, max_chars: int | None = None, timeout_s: float | None = None) -> ToolResult:
    return await _run(context, "BrowserEval", {"code": code, "tab": tab, "max_chars": max_chars, "timeout_s": timeout_s})


@tool_group("browser")
@search_hint(
    "userscript user script inject javascript css on page load document start tampermonkey greasemonkey override site "
    "style test change site behaviour inject "
    "юзерскрипт внедрить скрипт стили при загрузке страницы инжект подменить поведение сайта тампермонки изменить вид сайта"
)
@tool(
    name="BrowserScripts",
    description=(
        "Page scripts: your own JavaScript and CSS run at the start of every matching page in your browser, before "
        "the page's own scripts, as a userscript manager does — for testing a change to a site (its look, a function "
        "replaced) while you develop it or an extension. action='add' keeps one by name (the same name replaces it): js "
        "or js_path (a file in your workspace), css or css_path, matches (address globs like 'https://example.com/*' or "
        "'*://localhost:*/*', or a bare host like 'example.com' for it and its subdomains; none = every page), "
        "all_frames to run in frames too. It reaches every tab on its next load (BrowserNavigate go='reload') and stays "
        "in your browser's profile until action='remove' (name, or '*' for all). action='list' (the default) shows "
        "them and which run on the current page. Your own browser only."
    ),
)
async def browser_scripts(
    context: ToolContext,
    action: str = "list",
    name: str | None = None,
    js: str | None = None,
    js_path: str | None = None,
    css: str | None = None,
    css_path: str | None = None,
    matches: list[str] | None = None,
    all_frames: bool = False,
) -> ToolResult:
    return await _run(context, "BrowserScripts", {
        "action": action, "name": name, "js": js, "js_path": js_path, "css": css, "css_path": css_path, "matches": matches, "all_frames": all_frames or None,
    })


@tool_group("browser")
@search_hint(
    "chrome extension install unpacked load extension manifest.json reload extension develop extension test addon "
    "content script service worker crx "
    "расширение хрома установить расширение загрузить распакованное перезагрузить расширение разработка расширения аддон плагин браузера"
)
@tool(
    name="BrowserExtensions",
    description=(
        "Chrome extensions in your own browser, for developing and testing one: action='install' loads the extension "
        "at path — its folder in your workspace (the one holding manifest.json, unpacked), or a .zip or .crx of it; "
        "action='reload' after you edit its files (extension = its name or id, or path) reads them again; "
        "action='remove' uninstalls it; action='list' (the default) shows id, name, version and whether it runs. "
        "Installed extensions stay in your browser's profile and load whenever it starts. Content scripts reach pages "
        "loaded after an install or reload (BrowserNavigate go='reload'); BrowserLogs(extension=…) shows its background "
        "worker's console. Not in a throwaway browser, not in the operator's Chrome."
    ),
)
async def browser_extensions(context: ToolContext, action: str = "list", path: str | None = None, extension: str | None = None) -> ToolResult:
    return await _run(context, "BrowserExtensions", {"action": action, "path": path, "extension": extension})


TOOLS = [
    browser_open, browser_navigate, browser_snapshot, browser_text, browser_look, browser_act, browser_tabs, browser_wait, browser_dialog, browser_handoff, browser_close,
    browser_download, browser_note, browser_logs, browser_network, browser_inspect, browser_cookies, browser_login, browser_replay, browser_ask_operator_chrome,
    browser_eval, browser_scripts, browser_extensions,
]

__all__ = ["BROWSER_TOOLS", "READ_ONLY_TOOLS", "TOOLS", "SessionFiles"]
