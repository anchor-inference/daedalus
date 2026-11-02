"""The ``ComputerUI`` calls: a granted window read as its accessibility tree and driven element by element.

The daemon reads the tree (``ax.snapshot``, ``ax.find``) and acts on an element by the reference a read
handed out (``ax.act``); it also enforces the grant, so nothing here widens what the operator allowed.
What is the host's is the same as for screenshots: the lease is checked first, an action that would
send, buy, delete, accept or hand over credentials is asked about before it is sent — judged by the
element's own role and name, which is far more precise than the window title a click is judged by —
and everything read off the screen is fenced as screen content.

The point of the tree is the loop it allows. After a screenshot action the model is shown a whole new
screenshot, some fifteen hundred tokens however small the change; after an element action it is shown
that element read again (the daemon's ``post_state``), a line or a few. A screenshot comes with the
answer only when the tree cannot say what happened: the daemon could not confirm the action, the
element has no way to do it without real input, or the window has no usable tree (a canvas, a game, a
remote desktop). The model goes on with ``Computer`` from that picture.

A desktop that cannot read trees — a daemon older than them, or one shipped without its helper — is
said to be so once per conversation, and later calls answer with one short line: the model is expected
to carry on with screenshots, not to be told off on every call.

A secure field's value is never shown, whatever the daemon sends, and no tree text reaches a log line:
what is on the operator's screen is theirs.
"""

from __future__ import annotations

import hashlib
import logging
import weakref
from dataclasses import dataclass, field, replace
from typing import Any

from daedalus.computer import refusals, sensitive
from daedalus.computer.agent import Caller, ComputerAgent, ComputerAsk, Outcome, Refused, fenced, foreground
from daedalus.computer.wire import ACT_SLACK, CALL_TIMEOUT, DeskError, DeskUnavailable
from daedalus.host.policy import ALLOW, ASK, DENY, HANDOFF, Decision, approval_key

logger = logging.getLogger(__name__)

READS = ("tree", "find", "get")
ACTS = ("press", "set_value", "select", "toggle", "check", "uncheck", "expand", "collapse", "scroll_to", "focus")
ACTIONS = READS + ACTS
AX_ACTIONS = {
    "press": "click", "set_value": "set_value", "select": "select", "toggle": "toggle", "check": "check", "uncheck": "uncheck",
    "expand": "expand", "collapse": "collapse", "scroll_to": "scroll_to", "focus": "focus",
}
"""The tool's action names to the daemon's. ``press`` is the tool's word because the element is pressed
through its accessibility interface, which is not a click: nothing moves the pointer. An empty
``set_value`` is the daemon's ``clear``, since its ``set_value`` takes at least one character."""
DONE_WORDS = {
    "press": "Pressed", "set_value": "Set the value of", "select": "Selected an option in", "toggle": "Toggled", "check": "Checked",
    "uncheck": "Unchecked", "expand": "Expanded", "collapse": "Collapsed", "scroll_to": "Scrolled into view", "focus": "Focused",
}
REREAD_CHILDREN = ("expand", "select")
"""Actions whose effect is in the element's children (a branch opened, a list's choice), so the
element's subtree is read again; for the rest the daemon's ``post_state`` of the element says it."""
DEPTHS = ("full", "skeleton")
SURFACES = ("menu", "sheet", "popover", "alert")
MAX_LINES = 250
"""The most elements one read shows. A dense window's whole tree costs more than its screenshot; past
this the model is told to drill into a part or to find by role and name."""
MAX_NAME = 80
READ_TIMEOUT = CALL_TIMEOUT + ACT_SLACK
"""A cold Electron app can take many seconds to expose its tree the first time it is read; the daemon
kills its child five seconds after the child's own deadline, so this outlasts both."""
QUIET_STATES = frozenset({"enabled", "visible", "showing", "sensitive", "focusable", "selectable", "single line", "horizontal", "vertical", "opaque"})
"""States every ordinary element has; naming them on every line would only cost tokens."""
SECURE_VALUE = "(hidden)"

ABSENT = (
    "The accessibility tree is not available on this desktop ({why}), so ComputerUI cannot be used here. "
    "Carry on with Computer: take a screenshot and act by points. You will not be told again."
)
ABSENT_AGAIN = "ComputerUI is not available on this desktop: use Computer."
ABSENT_WHY = {
    "off": "the desktop service runs without its accessibility helper",
    "missing": "the desktop application was installed without its accessibility helper",
    "checksum": "the accessibility helper failed its integrity check, so it is not run",
    "version": "the accessibility helper is a version the desktop service does not accept",
    "failed": "the accessibility helper does not run on this system",
    "unsupported": "this desktop does not serve it",
}
"""The daemon's ``1209`` reasons that hold for the whole desktop, said for the model. ``no_window`` (one
window without a tree) and ``crashed`` (one call's helper died; the next tries again) are not here:
they are answered for the call, and the next call may work."""
OLD_DAEMON = "the desktop service is older than accessibility trees; updating the Daedalus desktop application brings them"
NO_TREE = (
    "This window exposes no accessibility tree (it is probably drawn on a canvas, a game or a remote desktop). "
    "Use Computer for it: the attached screenshot is the screen now."
)
CRASHED = "The accessibility helper stopped during that call; nothing is known to have happened. Try once more, or use Computer."
NEEDS_POINTER = (
    "This element offers no way to do that without real input. Use Computer on it instead: the attached screenshot "
    "is the screen now."
)
PARTIAL = "The tree was cut short by its deadline: if what you need is not here, read a part with tree and ref, or take a screenshot with Computer."
PICK_WINDOW = "Name the window to read: window=<id> from ComputerApps list (the grant covers more than one window)."


@dataclass(slots=True)
class Element:
    """What the host knows of an element a read handed out, enough to judge an action on it."""

    ref: str
    role: str
    name: str
    states: tuple[str, ...] = ()
    window: str = ""


@dataclass(slots=True)
class _SessionTree:
    elements: dict[str, Element] = field(default_factory=dict)
    told_absent: str = ""
    """The daemon instance this session was told has no trees, so the long sentence is said once."""
    lease: str = ""
    """The grant the elements were read under. A new grant forgets them: a reference belongs to a
    snapshot of the old grant, and a role and name remembered across it would judge the wrong thing."""


def _clean(text: Any, limit: int = MAX_NAME) -> str:
    # A name or value is screen text: one line, quotes made typographic so a name cannot close the
    # quoting it is shown in, and clipped so one long label cannot fill the read.
    flat = " ".join(str(text or "").split()).replace('"', "”")
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _states(node: dict[str, Any]) -> tuple[str, ...]:
    raw = node.get("states")
    if isinstance(raw, dict):
        raw = [k for k, on in raw.items() if on]
    states = tuple(str(s) for s in raw or () if isinstance(s, str))
    # The daemon marks a secure field apart from its states; it is kept as one, so the element's
    # judging finds it whichever way it was said.
    return (*states, "secure") if node.get("secure") and "secure" not in states else states


def _secure(node: dict[str, Any]) -> bool:
    return sensitive.is_secure(str(node.get("role") or ""), _states(node))


def _line(node: dict[str, Any], depth: int) -> str:
    """One element as the model reads it: ``[ref] role "name" = "value" (states) @x,y w×h``."""
    indent = "  " * depth
    parts = [f"{indent}[{node.get('ref')}]" if node.get("ref") else f"{indent}-", _clean(node.get("role")) or "element"]
    name = _clean(node.get("name"))
    if name:
        parts.append(f'"{name}"')
    if _secure(node):
        parts.append(f"= {SECURE_VALUE}")
    elif node.get("value") not in (None, ""):
        parts.append(f'= "{_clean(node.get("value"))}"')
    shown = [s for s in _states(node) if s.lower() not in QUIET_STATES]
    if shown:
        parts.append(f"({', '.join(_clean(s, 24) for s in shown)})")
    bounds = node.get("bounds")
    if isinstance(bounds, dict) and all(isinstance(bounds.get(k), (int, float)) for k in ("x", "y", "w", "h")):
        parts.append(f"@{round(bounds['x'])},{round(bounds['y'])} {round(bounds['w'])}×{round(bounds['h'])}")
    count = node.get("children_count")
    if isinstance(count, int) and count > 0 and not node.get("children"):
        parts.append(f"+{count} inside")
    elif node.get("truncated"):
        parts.append("+more inside")
    return " ".join(parts)


def _walk(nodes: Any, depth: int = 0) -> list[tuple[dict[str, Any], int]]:
    out: list[tuple[dict[str, Any], int]] = []
    for node in nodes if isinstance(nodes, list) else [nodes] if isinstance(nodes, dict) else ():
        if not isinstance(node, dict):
            continue
        out.append((node, depth))
        out.extend(_walk(node.get("children"), depth + 1))
    return out


def _window_id(value: Any) -> str:
    return str(value.get("id") or "") if isinstance(value, dict) else str(value or "")


def _app_name(app: dict[str, Any]) -> str:
    return str(app.get("name") or app.get("bundle_id") or app.get("id") or "an app")


def _as_node(element: Element, post: dict[str, Any] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"ref": element.ref, "role": element.role, "name": element.name, "states": list(element.states)}
    if post:
        node.update({k: post[k] for k in ("role", "states", "value") if k in post})
        if element.states and "secure" in element.states:
            node["secure"] = True
    return node


class TreeTools:
    """``ComputerUI`` over one :class:`ComputerAgent`: its connection, its lease check and its screenshots."""

    def __init__(self, agent: ComputerAgent) -> None:
        self.agent = agent
        self.sessions: dict[str, _SessionTree] = {}
        self.absent: dict[str, str] = {}
        """Daemon instance → why it reads no trees, learnt from its ``info`` or its first refusal."""

    @property
    def client(self) -> Any:
        return self.agent.client

    def _session(self, caller: Caller) -> _SessionTree:
        return self.sessions.setdefault(caller.session_id, _SessionTree())

    async def call(self, caller: Caller, arguments: dict[str, Any]) -> Outcome:
        action = str(arguments.get("action") or "")
        try:
            if action not in ACTIONS:
                raise Refused(f"unknown action {action!r}; one of: {', '.join(ACTIONS)}")
            self._check_arguments(action, arguments)
            held = await self.agent._leased(caller)
            tree = self._session(caller)
            if tree.lease != str(held.get("id") or ""):
                tree.elements.clear()
                tree.lease = str(held.get("id") or "")
            absent = self._absent_outcome(caller)
            if absent is not None:
                return absent
            if action == "tree":
                return await self._tree(caller, held, arguments)
            if action == "find":
                return await self._find(caller, held, arguments)
            if action == "get":
                return await self._get(caller, str(arguments["ref"]).strip())
            return await self._act(caller, action, arguments)
        except Refused as exc:
            return Outcome(exc.text, image=exc.image, failed=True)
        except DeskError as exc:
            return await self._refusal(caller, action, exc)
        except DeskUnavailable as exc:
            return Outcome(f"The desktop service is not available: {exc.detail}", failed=True)

    async def _refusal(self, caller: Caller, action: str, exc: DeskError) -> Outcome:
        data = exc.data if isinstance(exc.data, dict) else {}
        reason = str(data.get("reason") or "")
        if exc.code == refusals.METHOD_NOT_FOUND or exc.code == refusals.AX_UNAVAILABLE and reason in ABSENT_WHY:
            self._learn_absent(exc)
            return self._absent_outcome(caller) or Outcome(ABSENT_AGAIN)
        if exc.code == refusals.AX_UNAVAILABLE and reason == "crashed":
            return Outcome(CRASHED, failed=True, metadata={"code": exc.code})
        if exc.code == refusals.AX_UNAVAILABLE or exc.code == refusals.UNSUPPORTED and action in ACTS:
            # One window without a tree, or one element without a semantic path: the screenshot is the
            # way on, and taking it here saves the model the call.
            words = NO_TREE if exc.code == refusals.AX_UNAVAILABLE else NEEDS_POINTER
            try:
                shot = await self.agent._capture(caller)
            except (DeskError, DeskUnavailable):
                return Outcome(words.split(":")[0] + ".", failed=True, metadata={"code": exc.code})
            return Outcome(self.agent._with_shot(caller, words, shot), image=shot, failed=exc.code != refusals.AX_UNAVAILABLE, metadata={"code": exc.code, "ax": "empty"})
        if exc.code == refusals.NO_LEASE:
            self.sessions.pop(caller.session_id, None)
        return await self.agent._refusal(caller, exc)

    # -- availability ---------------------------------------------------------------------------

    def _instance(self) -> str:
        return str(self.client.hello.get("instance") or "")

    def _absent_reason(self) -> str:
        known = self.absent.get(self._instance())
        if known:
            return known
        info = self.client.info if isinstance(self.client.info, dict) else {}
        ax = info.get("ax")
        if isinstance(ax, dict) and ax.get("available") is False:
            return ABSENT_WHY.get(str(ax.get("reason") or ""), "the desktop service says so")
        # A daemon that says nothing of trees in its info is older than them: no call is spent to learn
        # it. An empty info (a connection that has not read it) proves nothing and is left to the call.
        if info and "ax" not in info:
            return OLD_DAEMON
        return ""

    def _learn_absent(self, exc: DeskError) -> None:
        if exc.code == refusals.METHOD_NOT_FOUND:
            why = OLD_DAEMON
        else:
            data = exc.data if isinstance(exc.data, dict) else {}
            why = ABSENT_WHY.get(str(data.get("reason") or ""), "the desktop service says so")
        self.absent[self._instance()] = why
        logger.info("accessibility trees unavailable on this desktop: %s", why)

    def _absent_outcome(self, caller: Caller) -> Outcome | None:
        """The answer of a desktop without trees: the reason once a conversation, one short line after."""
        why = self._absent_reason()
        if not why:
            return None
        tree = self._session(caller)
        instance = self._instance() or "-"
        if tree.told_absent == instance:
            return Outcome(ABSENT_AGAIN, metadata={"ax": "unavailable"})
        tree.told_absent = instance
        return Outcome(ABSENT.format(why=why), metadata={"ax": "unavailable"})

    # -- reads ------------------------------------------------------------------------------------

    def _check_arguments(self, action: str, arguments: dict[str, Any]) -> None:
        depth = arguments.get("depth")
        if depth is not None and depth not in DEPTHS:
            raise Refused("depth is full (every element) or skeleton (three levels, with how many are inside where it stops)")
        surface = arguments.get("surface")
        if surface is not None and surface not in SURFACES:
            raise Refused(f"surface is one of {', '.join(SURFACES)}: an overlay the window has open")
        if surface is not None and arguments.get("ref"):
            raise Refused("tree takes ref (a part of what was read) or surface (an open overlay), not both")
        if (action in ACTS or action == "get") and not str(arguments.get("ref") or "").strip():
            raise Refused(f"{action} needs ref, an element's reference from a tree or find read (the [..] at the start of its line)")
        if action == "find" and not any(str(arguments.get(k) or "").strip() for k in ("role", "name", "text")):
            raise Refused("find needs role, name or text (a button named Send: role=button, name=Send; text finds an element holding that text)")
        if action == "set_value":
            text = arguments.get("text")
            if not isinstance(text, str):
                raise Refused("set_value needs text, the field's whole new value (an empty text clears it)")
            limit = int(self.client.limits().get("max_type_chars") or 2000)
            if len(text) > limit:
                raise Refused(f"set_value takes at most {limit} characters and this text has {len(text)}")
        if action == "select" and not str(arguments.get("text") or "").strip():
            raise Refused("select needs text, the option's name as shown")

    async def _listed(self, caller: Caller) -> dict[str, Any]:
        listed = await self.client.call("apps.list", {"session": caller.session_id})
        return listed if isinstance(listed, dict) else {}

    @staticmethod
    def _names(listed: dict[str, Any], window: str) -> tuple[str, str, dict[str, Any]]:
        """The app's name and the window's title for the fence, and the app as ``apps.list`` describes it."""
        for app in listed.get("apps") or ():
            for candidate in app.get("windows") or ():
                if window and str(candidate.get("id")) == window:
                    return _app_name(app), str(candidate.get("title") or ""), app
        front = foreground(listed)
        if front is not None and not window:
            return _app_name(front[1]), str(front[0].get("title") or ""), front[1]
        return "an app", "", {}

    @staticmethod
    def _target(held: dict[str, Any], listed: dict[str, Any]) -> str:
        """The window a read without one is of. The daemon reads the lease's own window by itself; for
        a wider grant it wants one named, and the window with the keyboard inside the grant is the one
        the operator is looking at, else the frontmost in scope."""
        if str((held.get("scope") or {}).get("kind") or "") == "window":
            return ""
        front = foreground(listed)
        if front is not None and front[0].get("in_scope") is not False and front[1].get("in_scope") is not False:
            return str(front[0].get("id") or "")
        candidates = [
            (int(w.get("order") or 0), str(w.get("id") or ""))
            for app in listed.get("apps") or () for w in app.get("windows") or ()
            if w.get("in_scope") and not w.get("minimized")
        ]
        if not candidates:
            raise Refused(PICK_WINDOW)
        return min(candidates)[1]

    def _remember(self, caller: Caller, nodes: list[tuple[dict[str, Any], int]], window: str) -> None:
        elements = self._session(caller).elements
        for node, _ in nodes:
            ref = str(node.get("ref") or "")
            if ref:
                elements[ref] = Element(ref, str(node.get("role") or ""), str(node.get("name") or ""), _states(node), window)

    @staticmethod
    def _body(nodes: list[tuple[dict[str, Any], int]]) -> tuple[str, int]:
        lines = [_line(node, depth) for node, depth in nodes[:MAX_LINES]]
        return "\n".join(lines), max(0, len(nodes) - MAX_LINES)

    async def _tree(self, caller: Caller, held: dict[str, Any], arguments: dict[str, Any]) -> Outcome:
        params: dict[str, Any] = {"session": caller.session_id, "depth": str(arguments.get("depth") or "full")}
        listed = await self._listed(caller)
        root = str(arguments.get("ref") or "").strip()
        if root:
            params["root"] = root
        else:
            window = str(arguments.get("window") or "").strip() or self._target(held, listed)
            if window:
                params["window"] = window
            if arguments.get("surface"):
                params["surface"] = str(arguments["surface"])
        answer = await self.client.call("ax.snapshot", params, timeout=READ_TIMEOUT)
        answer = answer if isinstance(answer, dict) else {}
        window = _window_id(answer.get("window")) or str(params.get("window") or "")
        nodes = _walk(answer.get("tree"))
        self._remember(caller, nodes, window)
        app, title, _ = self._names(listed, window)
        if not any(node.get("ref") for node, _ in nodes):
            shot = await self.agent._capture(caller)
            text = fenced(app, title, "no element one can act on") + "\n" + NO_TREE
            return Outcome(self.agent._with_shot(caller, text, shot), image=shot, metadata={"ax": "empty"})
        body, more = self._body(nodes)
        head = f"The accessibility tree of window {window}" if window else "The accessibility tree of the window"
        if root:
            head += f", from element {root}"
        lines = [head + ": [ref] role \"name\" = value (states). Act on an element by its ref.", fenced(app, title, body)]
        if more:
            lines.append(f"{more} more element{'s' if more != 1 else ''} not shown: read a part with tree and ref, or use find.")
        if answer.get("complete") is False:
            lines.append(PARTIAL)
        return Outcome("\n".join(lines), metadata={"ax": "tree", "elements": len(nodes)})

    async def _find(self, caller: Caller, held: dict[str, Any], arguments: dict[str, Any]) -> Outcome:
        params: dict[str, Any] = {"session": caller.session_id}
        listed = await self._listed(caller)
        root = str(arguments.get("ref") or "").strip()
        if root:
            params["root"] = root
        else:
            window = str(arguments.get("window") or "").strip() or self._target(held, listed)
            if window:
                params["window"] = window
        for key in ("role", "name", "text"):
            if str(arguments.get(key) or "").strip():
                params[key] = str(arguments[key]).strip()
        if arguments.get("exact") is not None:
            params["exact"] = bool(arguments["exact"])
        answer = await self.client.call("ax.find", params, timeout=READ_TIMEOUT)
        answer = answer if isinstance(answer, dict) else {}
        window = _window_id(answer.get("window")) or str(params.get("window") or "")
        nodes = [(node, 0) for node in answer.get("matches") or () if isinstance(node, dict)]
        self._remember(caller, nodes, window)
        app, title, _ = self._names(listed, window)
        wanted = " ".join(filter(None, (
            str(params.get("role") or ""),
            f'"{_clean(params.get("name"))}"' if params.get("name") else "",
            f'holding "{_clean(params.get("text"))}"' if params.get("text") else "",
        )))
        if not nodes:
            roles = [str(r) for r in answer.get("roles_present") or () if isinstance(r, str)]
            hint = fenced(app, title, "roles in this window: " + ", ".join(_clean(r, 32) for r in roles[:40])) + "\n" if roles else ""
            return Outcome(
                f"No element matches {wanted}.\n{hint}Read the window with tree, or take a screenshot with Computer if its tree is poor.",
                metadata={"ax": "find", "elements": 0},
            )
        body, more = self._body(nodes)
        lines = [f"{len(nodes)} element{'s' if len(nodes) != 1 else ''} match {wanted} (a line without a ref can be read, not acted on):", fenced(app, title, body)]
        if more:
            lines.append(f"{more} more not shown: narrow the search.")
        return Outcome("\n".join(lines), metadata={"ax": "find", "elements": len(nodes)})

    async def _reread(self, caller: Caller, ref: str) -> tuple[list[tuple[dict[str, Any], int]], str]:
        """The element and its children read now, and its window; raises the daemon's refusal."""
        answer = await self.client.call("ax.snapshot", {"session": caller.session_id, "root": ref, "depth": "skeleton"}, timeout=READ_TIMEOUT)
        answer = answer if isinstance(answer, dict) else {}
        nodes = _walk(answer.get("tree"))
        window = _window_id(answer.get("window")) or self._window_of(caller, ref)
        self._remember(caller, nodes, window)
        return nodes, window

    async def _get(self, caller: Caller, ref: str) -> Outcome:
        nodes, window = await self._reread(caller, ref)
        app, title, _ = self._names(await self._listed(caller), window)
        body, more = self._body(nodes)
        text = f"Element {ref} now:\n" + fenced(app, title, body or "nothing")
        if more:
            text += f"\n{more} more inside not shown: read it with tree and ref."
        return Outcome(text, metadata={"ax": "get"})

    def _window_of(self, caller: Caller, ref: str) -> str:
        known = self._session(caller).elements.get(ref)
        return known.window if known is not None else ""

    # -- actions ----------------------------------------------------------------------------------

    async def _element(self, caller: Caller, ref: str) -> Element:
        known = self._session(caller).elements.get(ref)
        if known is not None:
            return known
        # A ref the host never saw (a restart in between, or one made up): read it, which both judges
        # it and has the daemon refuse a ref it does not know before anything is sent.
        await self._reread(caller, ref)
        known = self._session(caller).elements.get(ref)
        if known is None:
            raise Refused(f"There is no element {ref}: read the window with tree or find and use a ref from it.")
        return known

    async def _judge(self, caller: Caller, action: str, element: Element, option: str, app: dict[str, Any], title: str) -> None:
        """Ask before an element action that its role and name (or the dialog around a bare OK) make sensitive."""
        kinds = sensitive.element_kinds(action, element.role, element.name, element.states, option=option if action == "select" else "", window_title=title)
        if not kinds:
            return
        name = _app_name(app) if app else "an app"
        origin = f"{name} — {title}" if title else name
        described = f"{_clean(element.role) or 'element'} “{_clean(element.name)}”" if element.name else _clean(element.role) or "an element"
        key = approval_key("ComputerUI", {
            "app": str(app.get("id") or ""), "window": title, "action": action, "kinds": kinds,
            "role": element.role, "name": element.name, "text_sha256": hashlib.sha256(option.encode("utf-8")).hexdigest() if option else "",
        })
        words = " and ".join(sensitive.WORDS_FOR[k] for k in kinds)
        decision = Decision(ASK, f"this would {words} in {origin}", "computer.sensitive", key=key)
        ask = ComputerAsk(decision, "ComputerUI", action, kinds, origin, described, element.name, len(option) if action == "set_value" else 0, name, title)
        if caller.rules is not None:
            ask = await caller.rules(ask)
        decision = ask.decision
        if decision.action == ALLOW:
            return
        if decision.action in (DENY, HANDOFF):
            raise Refused(decision.told or f"Not done: {decision.reason}.")
        if caller.gate is None:
            raise Refused(f"Not done: {decision.reason}, and nobody can approve it here.")
        allowed, why = await caller.gate(replace(ask, decision=decision))
        if not allowed:
            raise Refused(f"Not done: {why}" if why else f"Not done: {decision.reason}; the operator did not allow it.")

    async def _act(self, caller: Caller, action: str, arguments: dict[str, Any]) -> Outcome:
        ref = str(arguments["ref"]).strip()
        element = await self._element(caller, ref)
        listed = await self._listed(caller)
        app_name, title, app = self._names(listed, element.window)
        text = arguments.get("text")
        option = str(text) if action in ("set_value", "select") and isinstance(text, str) else ""
        await self._judge(caller, action, element, option, app, title)
        params: dict[str, Any] = {"session": caller.session_id, "ref": ref, "action": AX_ACTIONS[action]}
        if action == "set_value" and not option:
            params["action"] = "clear"
        elif action in ("set_value", "select"):
            params["text"] = option
        # The value set never reaches a log line, as Computer's typed text never does.
        logger.info("computer ui %s for session %s", params["action"], caller.session_id)
        answer = await self.agent._act(params, CALL_TIMEOUT + ACT_SLACK, method="ax.act")
        answer = answer if isinstance(answer, dict) else {}
        delivery = str(answer.get("delivery") or "")
        verified = answer.get("verified")
        post = answer.get("post_state") if isinstance(answer.get("post_state"), dict) else None
        if delivery == refusals.NOT_DELIVERED:
            status = "already so, nothing was sent"
        elif verified is False:
            status = "sent, not confirmed"
        elif verified:
            status = "done and confirmed"
        else:
            status = "done"
        lines = [f"{DONE_WORDS[action]} {ref} — {status}."]
        window = _window_id(answer.get("window")) or element.window
        if window != element.window:
            app_name, title, _ = self._names(listed, window)
        if post is not None and action not in REREAD_CHILDREN:
            lines.append("The element now:\n" + fenced(app_name, title, _line(_as_node(element, post), 0)))
        else:
            try:
                nodes, _ = await self._reread(caller, ref)
                body, more = self._body(nodes)
                lines.append("The element now:\n" + fenced(app_name, title, body or _line(_as_node(element, post), 0)))
                if more:
                    lines.append(f"{more} more inside not shown: read it with tree and ref.")
            except DeskError as exc:
                if exc.code not in (refusals.STALE_REF, refusals.NOT_FOUND, refusals.AMBIGUOUS_TARGET):
                    raise
                # The element went with the action: a dialog's button closes its dialog, which is
                # often the proof it worked. The window with the keyboard says where things are now.
                self._session(caller).elements.pop(ref, None)
                front_app, front_title, _ = self._names(await self._listed(caller), "")
                lines.append(
                    "The element is no longer there (its dialog or view closed or changed). The window with the keyboard now:\n"
                    + fenced(front_app, front_title, _line(_as_node(element), 0) + " — gone")
                )
        surfaces = [str(s) for s in answer.get("surfaces") or () if str(s) in SURFACES]
        if surfaces:
            lines.append(f"Open now: {', '.join(surfaces)}. Read it with tree and surface={surfaces[0]}.")
        left = answer.get("actions_left")
        if isinstance(left, int):
            lines.append(f"{left} action{'s' if left != 1 else ''} left in this grant of control.")
        if verified is False and delivery != refusals.NOT_DELIVERED:
            # The daemon could not see the change happen: the picture is what the model judges by now.
            shot = await self.agent._after(caller)
            lines.append("The desktop could not confirm the change, so a screenshot is attached: check it before going on.")
            return Outcome(self.agent._with_shot(caller, "\n".join(lines), shot), image=shot, metadata={"ax": action, "delivery": delivery, "verified": False})
        return Outcome("\n".join(lines), metadata={"ax": action, "delivery": delivery, "verified": verified})


_tools: weakref.WeakKeyDictionary[ComputerAgent, TreeTools] = weakref.WeakKeyDictionary()


def tree_tools(agent: ComputerAgent) -> TreeTools:
    """The ``ComputerUI`` half of an agent, made on first use and kept as long as the agent: the
    elements each session has read live as long as its screenshots do."""
    tools = _tools.get(agent)
    if tools is None:
        tools = _tools[agent] = TreeTools(agent)
    return tools


__all__ = ["ACTIONS", "ACTS", "AX_ACTIONS", "READS", "Element", "TreeTools", "tree_tools"]
