"""Recorded skills: a recording of the operator's steps, edited by them into a skill with a replay
script, and ``BrowserReplay``, which follows that script in the agent's browser.

The operator records a task while they drive (``daedalus.browser.workflows``), then, in the app's step
editor, deletes the steps that were noise, turns what they typed into named parameters (a typed date
becomes ``{month}``) and says how each sign-in is done: by them when the replay reaches it, with the
sign-in they saved for the site, or with operator secrets, one ``«secret:name»`` per field. What they
save is a skill draft (``daedalus.host.skills.save_skill_draft``, the same writer as ``SkillDraft``)
holding ``SKILL.md``, the steps in words, and ``replay.json``, the steps as the replay runs them.

A secret is never in either file: a sign-in's field holds the placeholder of an operator's secret, which
the browser agent puts in at the last moment as it does for any typed placeholder, and nothing else is
accepted there. A replay grants nothing either: each step is the agent's own action through the same
wall, classifier and questions, on an element found again by the daemon (``page.locate``) by the
recorded selector, else its role and name, else its words. A step found by none of them stops the
replay, and the model is told which step it was and what was left, to go on with its own tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from daedalus.browser.model import BrowserError, InvalidRequest, NotFound, Unsupported
from daedalus.host.skills import DRAFTS_DIR, draft_slug, save_skill_draft
from daedalus.security.operator_secrets import PLACEHOLDER_RE

if TYPE_CHECKING:
    from daedalus.browser.agent import BrowserAgent, Caller

logger = logging.getLogger(__name__)

SCRIPT_FILE = "replay.json"
SCRIPT_VERSION = 1
SIGN_INS = ("operator", "saved", "secrets")
"""How a recorded sign-in is done on replay: handed to the operator, with the sign-in they saved for the
site (``BrowserLogin``), or typed from the operator secrets bound to its fields."""
ELEMENT_ACTIONS = ("click", "double_click", "right_click", "check", "uncheck", "type", "select")
KEPT_ACTIONS = (*ELEMENT_ACTIONS, "navigate", "arrive", "press", "scroll", "handoff", "dialog", "download", "expect")
"""The recorded steps a script keeps. A switch of tabs is left out: a tab's id is the recording's own and
means nothing on the next run, and the steps after it say where they act."""
TEXT_MAX = 2000
STEPS_MAX = 200
PARAM = re.compile(r"\{([a-z][a-z0-9_]{0,39})\}")
LOCATE_TRIES = 4
LOCATE_PAUSE = 0.75
"""A page that is still drawing gets about three seconds to show the element before the step is called
lost: the recording's pauses are the person's, the replay's waits are for the page."""
ARRIVE_TIMEOUT_MS = 15000


class ReplayLost(BrowserError):
    """A recorded element the page no longer has by any of its descriptions."""

    status, code = 404, "replay_lost"


def _clip(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _element(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    return {k: _clip(raw.get(k), 500 if k == "selector" else 200) for k in ("role", "name", "place", "selector", "text") if raw.get(k)}


def params_of(steps: list[dict[str, Any]]) -> list[str]:
    """The parameters a script takes, in the order its steps first use them."""
    seen: dict[str, None] = {}
    for step in steps:
        for key in ("text", "option", "to"):
            for name in PARAM.findall(str(step.get(key) or "")):
                seen.setdefault(name, None)
    return list(seen)


def build_script(workflow: dict[str, Any], edits: list[dict[str, Any]], *, name: str, description: str) -> dict[str, Any]:
    """The replay script of a stopped recording as the operator edited it.

    ``edits`` are the steps kept, each by its recorded ``n`` with what the operator changed: ``text`` of a
    typing (a value, or a ``{param}``), ``option`` of a choice, ``to`` of an address, and of a sign-in its
    ``signin`` and, for ``secrets``, one placeholder per recorded field. A step the edits leave out is
    deleted; nothing outside the recording can be added, so a script goes only where the operator went.
    """
    recorded = {int(s.get("n") or 0): s for s in workflow.get("steps") or [] if isinstance(s, dict)}
    if len(edits) > STEPS_MAX:
        raise InvalidRequest(f"a recorded skill holds at most {STEPS_MAX} steps")
    steps: list[dict[str, Any]] = []
    seen: set[int] = set()
    for edit in edits:
        n = int(edit.get("n") or 0) if isinstance(edit, dict) else 0
        source = recorded.get(n)
        if source is None or n in seen:
            raise InvalidRequest(f"step {n} is not a step of this recording")
        seen.add(n)
        action = str(source.get("action") or "")
        if action not in KEPT_ACTIONS:
            continue
        step: dict[str, Any] = {"n": n, "action": action}
        element = _element(source.get("element"))
        if element:
            step["element"] = element
        if action == "type":
            text = edit.get("text")
            if text is None:
                text = source.get("value") if source.get("value") is not None else "{" + str(source.get("slot") or "text") + "}"
            if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
                raise InvalidRequest(f"step {n}: what is typed is 1–{TEXT_MAX} characters (a value, or a {{parameter}})")
            step["text"] = text
            if source.get("submit"):
                step["submit"] = True
        elif action == "select":
            option = edit.get("option") if isinstance(edit.get("option"), str) else source.get("option") or "{" + str(source.get("slot") or "option") + "}"
            step["option"] = _clip(option, 300)
        elif action in ("navigate", "arrive"):
            if source.get("go"):
                step["go"] = str(source["go"])
            else:
                to = edit.get("to") if isinstance(edit.get("to"), str) and edit.get("to") else source.get("to")
                if not _web(str(to or "")):
                    raise InvalidRequest(f"step {n}: an address is http or https")
                if _host(str(to)) not in {_host(str(source.get("to") or ""))}:
                    raise InvalidRequest(f"step {n}: the address stays on {_host(str(source.get('to') or ''))}, the site the recording went to")
                step["to"] = _clip(to, 2000)
            if action == "arrive" and source.get("title"):
                step["title"] = _clip(source["title"], 200)
        elif action in ("press", "scroll"):
            step.update({k: source[k] for k in ("keys", "direction") if source.get(k)})
            step["count"] = max(1, min(int(source.get("count") or 1), 20))
        elif action == "handoff":
            step.update(_signin(n, source, edit))
        elif action == "dialog":
            step["accept"] = bool(source.get("accept"))
            step["text"] = _clip(source.get("text"), 300)
        elif action in ("download", "expect"):
            step.update({k: _clip(source[k], 300) for k in ("text", "title") if source.get(k)})
        steps.append(step)
    if not any(s["action"] in (*ELEMENT_ACTIONS, "press", "handoff") for s in steps):
        raise InvalidRequest("keep at least one step that does something: a click, a typing, a choice, a key or a sign-in")
    start = str(workflow.get("start_url") or "")
    return {
        "version": SCRIPT_VERSION, "name": name, "description": description, "recording": str(workflow.get("id") or ""),
        "start_url": start if _web(start) else "", "params": params_of(steps), "steps": steps,
    }


def _signin(n: int, source: dict[str, Any], edit: dict[str, Any]) -> dict[str, Any]:
    reason = str(source.get("reason") or "login")
    mode = str(edit.get("signin") or "operator")
    if mode not in SIGN_INS:
        raise InvalidRequest(f"step {n}: a sign-in is done by the operator, with a saved sign-in, or from operator secrets")
    out: dict[str, Any] = {"reason": reason, "signin": mode}
    if mode == "saved" and reason != "login":
        raise InvalidRequest(f"step {n}: a saved sign-in fills a username and a password, not a {reason.replace('_', ' ')}")
    if mode == "secrets":
        fields = [f for f in source.get("fields") or [] if isinstance(f, dict)]
        given = edit.get("secrets") if isinstance(edit.get("secrets"), list) else []
        if not fields or len(given) != len(fields):
            raise InvalidRequest(f"step {n}: give one operator secret for each of the sign-in's {len(fields)} fields")
        bound = []
        for field, secret in zip(fields, given, strict=True):
            # Only a placeholder: whatever else is written here would be a secret kept in a file.
            if not isinstance(secret, str) or not PLACEHOLDER_RE.fullmatch(secret.strip()):
                raise InvalidRequest(f"step {n}: a sign-in's field takes an operator secret as «secret:name», never a value")
            bound.append({**(_element(field) or {}), "secret": _clip(field.get("secret"), 40), "use": secret.strip()})
        out["fields"] = bound
    return out


def _web(url: str) -> bool:
    return urlsplit(url).scheme.lower() in ("http", "https") and bool(urlsplit(url).hostname)


def _host(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def _q(text: Any) -> str:
    return json.dumps(str(text or ""), ensure_ascii=False)


def _what(step: dict[str, Any]) -> str:
    el = step.get("element") or {}
    role = str(el.get("role") or "element")
    return f"the {role} {_q(el.get('name'))}" if el.get("name") else f"the {role}"


SIGN_IN_WORDS = {"operator": "the operator signs in", "saved": "the sign-in the operator saved for the site is used", "secrets": "the operator's secrets are typed into the sign-in"}


def describe(step: dict[str, Any]) -> str:
    """A script's step in words, for the skill's text and the replay's report."""
    action = step["action"]
    times = f" {step['count']} times" if int(step.get("count") or 1) > 1 else ""
    if action == "navigate":
        return f"Go {step['go']}." if step.get("go") else f"Open {step.get('to')}."
    if action == "arrive":
        return f"Wait for the page {step.get('to') or ''}" + (f" ({_q(step['title'])})." if step.get("title") else ".")
    if action in ("click", "double_click", "right_click", "check", "uncheck"):
        verb = {"click": "Click", "double_click": "Double-click", "right_click": "Right-click", "check": "Tick", "uncheck": "Untick"}[action]
        return f"{verb} {_what(step)}."
    if action == "type":
        return f"Type {_q(step.get('text'))} into {_what(step)}" + (", then Enter." if step.get("submit") else ".")
    if action == "select":
        return f"Choose {_q(step.get('option'))} in {_what(step)}."
    if action == "press":
        return f"Press {step.get('keys')}{times}."
    if action == "scroll":
        return f"Scroll {step.get('direction')}{times}."
    if action == "handoff":
        return f"Sign-in ({step.get('reason')}): {SIGN_IN_WORDS.get(str(step.get('signin')), SIGN_IN_WORDS['operator'])}."
    if action == "dialog":
        return f"{'Accept' if step.get('accept') else 'Dismiss'} the page's question {_q(step.get('text'))}."
    if action == "download":
        return f"A file downloads ({_q(step.get('text'))})."
    if action == "expect":
        return f"Check that the page shows {_q(step.get('text') or step.get('title'))}."
    return action


def skill_markdown_body(script: dict[str, Any]) -> str:
    """The skill's text: when to use it, the steps in words, how to run it, and how to tell it worked."""
    params = script["params"]
    call = f"BrowserReplay(skill={_q(script['name'])}" + (", params={" + ", ".join(f"{_q(p)}: …" for p in params) + "}" if params else "") + ")"
    lines = [
        "## When to use",
        "",
        script["description"],
        "",
        "## Procedure",
        "",
        f"Run {call}. It replays the steps the operator recorded in your browser; when a step's element cannot be found, "
        "it stops and says which step, and you go on from there with your own browser tools.",
        "",
        *(["Parameters: " + ", ".join("{" + p + "}" for p in params) + ".", ""] if params else []),
        *(["Start at " + script["start_url"] + ".", ""] if script.get("start_url") else []),
        *[f"{i}. {describe(step)}" for i, step in enumerate(script["steps"], start=1)],
        "",
        "## Checks",
        "",
    ]
    ends = [describe(s) for s in script["steps"] if s["action"] == "expect"]
    lines.extend(ends or ["The replay's result says each step was done; read the page to confirm the outcome."])
    return "\n".join(lines)


@dataclass(slots=True)
class RecordedSkills:
    """Where recorded skills are written and read: drafts under the state directory, and the same
    layout in the skills directory for one the operator copied into the repository."""

    state_dir: Path
    skills_dir: Path
    redact: Any = None

    def save(self, workflow: dict[str, Any], edits: list[dict[str, Any]], *, name: str, description: str) -> dict[str, Any]:
        """Write the recording as the operator edited it into a skill draft; what was written."""
        slug = draft_slug(name)
        if not slug:
            raise InvalidRequest("the name is 1–48 lowercase letters, digits and hyphens")
        if workflow.get("state") != "stopped":
            raise InvalidRequest("stop the recording first; a skill is made from a finished one")
        description = " ".join(description.split())
        script = build_script(workflow, edits, name=slug, description=description)
        redact = self.redact or (lambda text: text)
        try:
            directory, slug, verdict = save_skill_draft(
                self.state_dir, redact, slug, description, skill_markdown_body(script),
                params=", ".join(script["params"]) or None, front={"requires": "browser", "tools": "BrowserReplay"},
                files={SCRIPT_FILE: json.dumps(script, ensure_ascii=False, indent=1)},
            )
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from None
        return {"skill": slug, "path": str(directory), "params": script["params"], "steps": len(script["steps"]), "valid": verdict["valid"], "errors": verdict["errors"]}

    def _dirs(self) -> list[Path]:
        return [self.skills_dir, self.state_dir / DRAFTS_DIR]

    def load(self, name: str) -> dict[str, Any]:
        slug = draft_slug(name)
        for base in self._dirs():
            path = base / slug / SCRIPT_FILE if slug else None
            if path is not None and path.is_file():
                try:
                    script = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise InvalidRequest(f"the replay script of {slug} cannot be read: {exc}") from None
                if not isinstance(script, dict) or script.get("version") != SCRIPT_VERSION or not isinstance(script.get("steps"), list):
                    raise InvalidRequest(f"the replay script of {slug} is not one this host runs")
                return script
        raise NotFound(f"no recorded skill {name!r}; BrowserReplay without a skill lists them")

    def listing(self) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for base in self._dirs():
            if not base.is_dir():
                continue
            for path in sorted(base.glob(f"*/{SCRIPT_FILE}")):
                try:
                    script = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(script, dict):
                    found.setdefault(path.parent.name, {"skill": path.parent.name, "description": str(script.get("description") or ""), "params": list(script.get("params") or [])})
        return list(found.values())


def _fill(text: str, params: dict[str, str]) -> str:
    return PARAM.sub(lambda m: params.get(m.group(1), m.group(0)), text)


async def replay(agent: BrowserAgent, caller: Caller, script: dict[str, Any], params: dict[str, Any], *, from_step: int = 1) -> tuple[str, bool]:
    """Follow a recorded skill's script in the caller's browser; ``(report, failed)``.

    It stops at a sign-in the operator does (``paused``, not failed: the report says how to go on), and at
    the first step that cannot be done, which it names with the steps left, for the model to finish."""
    from daedalus.browser.agent import explain  # the agent module imports this one

    name = str(script.get("name") or "")
    given = {str(k): str(v) for k, v in (params or {}).items()}
    missing = [p for p in script.get("params") or [] if p not in given]
    if missing:
        raise InvalidRequest(f"{name} takes {', '.join(script.get('params') or [])}; give {', '.join(missing)} in params")
    steps = [s for s in script["steps"] if isinstance(s, dict) and s.get("action") in KEPT_ACTIONS]
    order = list(enumerate(steps, start=1))
    if from_step > 1:
        order = [(i, s) for i, s in order if i >= from_step]
        if not order:
            raise InvalidRequest(f"{name} has {len(steps)} steps; from_step is 1–{len(steps)}")
    why = f"replaying the recorded skill {name}"
    said: list[str] = []
    try:
        group = await agent._group(caller)
    except NotFound:
        group = None
    start = _fill(str(script.get("start_url") or ""), given)
    if group is None or (from_step == 1 and start):
        if group is None:
            await agent.open(caller, url=start or None, why=why)
        else:
            await agent.navigate(caller, url=start, why=why)
        group = await agent._group(caller)
        if start:
            said.append(f"Opened {start}.")
    total = len(steps)

    def rest(index: int) -> str:
        left = [f"{i}. {describe(s)}" for i, s in order if i > index]
        return ("\nWhat was left:\n" + "\n".join(left)) if left else ""

    def again(index: int) -> str:
        args = f"skill={_q(name)}" + (f", params={json.dumps(given, ensure_ascii=False)}" if given else "")
        return f"BrowserReplay({args}, from_step={index + 1})"

    for index, step in order:
        try:
            line = await _do(agent, caller, group, step, given, why)
        except _Paused as paused:
            said.append(f"{index}. {paused.said}")
            more = f" When it is done, {again(index)} goes on from the next step." if index < total else ""
            return _report(name, said, index - 1, total) + f"\nPaused at step {index}.{more}{rest(index)}", False
        except ReplayLost as exc:
            said.append(f"{index}. {describe(step)} — not done: {exc.message}")
            return _report(name, said, index - 1, total) + (
                f"\nStopped at step {index}: the page has no element matching what was recorded. Take a BrowserSnapshot, "
                f"do step {index} with BrowserAct, then the rest by hand (or {again(index)} once the step is done)." + rest(index)
            ), True
        except Unsupported as exc:
            return _report(name, said, index - 1, total) + f"\nStopped at step {index}: {exc.message}. The browser service needs rebuilding for replays.", True
        except BrowserError as exc:
            said.append(f"{index}. {describe(step)} — not done: {explain(exc)}")
            return _report(name, said, index - 1, total) + f"\nStopped at step {index}. Finish the task with your own browser tools from here.{rest(index)}", True
        if line:
            said.append(f"{index}. {line}")
    return _report(name, said, total, total), False


def _report(name: str, said: list[str], done: int, total: int) -> str:
    return "\n".join([f"Replayed the recorded skill {name}: {done} of {total} steps done.", *said])


class _Paused(Exception):
    def __init__(self, said: str) -> None:
        super().__init__(said)
        self.said = said


async def _locate(agent: BrowserAgent, caller: Caller, group: dict[str, Any], tab: dict[str, Any], element: dict[str, Any]) -> tuple[str, str]:
    """``(ref, how it was found)`` for a recorded element, waiting a moment for a page still drawing."""
    params: dict[str, Any] = {"tab_id": tab["id"], "origin": agent._origin(caller)}
    for key in ("selector", "role", "name", "text"):
        if element.get(key):
            params[key] = str(element[key])
    if not any(params.get(k) for k in ("selector", "name", "text")):
        raise ReplayLost("the recording could not name what was pressed there")
    for attempt in range(LOCATE_TRIES):
        try:
            found = await agent.service.call(group["id"], "page.locate", params, what="finding a recorded element")
        except NotFound:
            if attempt == LOCATE_TRIES - 1:
                raise ReplayLost("no element matches its selector, its role and name, or its words") from None
            await asyncio.sleep(LOCATE_PAUSE)
            continue
        return str(found.get("ref") or ""), str(found.get("by") or "")
    raise ReplayLost("no element matches its selector, its role and name, or its words")


FOUND_BY = {
    "role": " (found by its role and name: the recorded selector no longer matches)",
    "text": " (found by its words: neither the recorded selector nor its role and name match any more)",
}


async def _do(agent: BrowserAgent, caller: Caller, group: dict[str, Any], step: dict[str, Any], params: dict[str, str], why: str) -> str:
    from daedalus.browser.agent import Step  # the agent module imports this one

    action = step["action"]
    if action == "navigate":
        if step.get("go"):
            await agent.navigate(caller, go=str(step["go"]), why=why)
            return f"Went {step['go']}."
        url = _fill(str(step.get("to") or ""), params)
        if PARAM.search(url):
            raise InvalidRequest(f"the address {url} still has a blank")
        await agent.navigate(caller, url=url, why=why)
        return f"Opened {url}."
    tab = await agent._tab(group, None)
    if action == "arrive":
        result = await agent.service.call(group["id"], "page.wait", {"tab_id": tab["id"], "for": "load", "timeout_ms": ARRIVE_TIMEOUT_MS, "origin": agent._origin(caller)},
                                          what="waiting for the page", timeout=ARRIVE_TIMEOUT_MS / 1000 + 25)
        return f"The page is {result.get('url') or tab.get('url')}."
    if action in ELEMENT_ACTIONS:
        ref, by = await _locate(agent, caller, group, tab, step.get("element") or {})
        text = _fill(str(step["text"]), params) if action == "type" else None
        option = _fill(str(step.get("option") or ""), params) if action == "select" else None
        if (text is not None and PARAM.search(text)) or (option and PARAM.search(option)):
            raise InvalidRequest(f"{describe(step)} still has a blank")
        act = Step(action=action, element=_what(step), ref=ref, text=text, option=option or None, submit=bool(step.get("submit")))
        act.check()
        result, name = await agent._act_one(caller, group, tab, act, label="replay")
        lines = agent._act_lines(action, tab, result, name, diff=False)
        return " ".join(lines[:3]) + FOUND_BY.get(by, "")
    if action in ("press", "scroll"):
        count = max(1, min(int(step.get("count") or 1), 20))
        act = Step(action=action, element="the page", keys=step.get("keys"), direction=step.get("direction"))
        act.check()
        for _ in range(count):
            await agent._act_one(caller, group, tab, act, label="replay")
        return describe(step).replace("Press", "Pressed").replace("Scroll", "Scrolled")
    if action == "handoff":
        return await _sign_in(agent, caller, group, tab, step, why)
    if action == "dialog":
        return await agent.dialog(caller, accept=bool(step.get("accept")))
    if action == "download":
        return f"A file downloads here ({_q(step.get('text'))}); BrowserDownload(name) keeps it."
    if action == "expect":
        value = str(step.get("text") or step.get("title") or "")
        result = await agent.service.call(group["id"], "page.wait", {"tab_id": tab["id"], "for": "text", "value": value, "timeout_ms": 8000, "origin": agent._origin(caller)},
                                          what="checking the page", timeout=40)
        if result.get("matched") == "timeout":
            raise BrowserError(f"the page does not show {_q(value)}, which the recording ended with")
        return f"The page shows {_q(value)}, as the recording ended."
    return ""


async def _sign_in(agent: BrowserAgent, caller: Caller, group: dict[str, Any], tab: dict[str, Any], step: dict[str, Any], why: str) -> str:
    from daedalus.browser.agent import Step  # the agent module imports this one

    mode = str(step.get("signin") or "operator")
    reason = str(step.get("reason") or "login")
    url = str(tab.get("url") or "")
    if mode == "secrets":
        fields = [f for f in step.get("fields") or [] if isinstance(f, dict)]
        for i, field in enumerate(fields):
            ref, _ = await _locate(agent, caller, group, tab, field)
            # The placeholder, as the model would write it: the agent puts the value in at the last moment,
            # and only the placeholder is what the audit and the policy see.
            act = Step(action="type", element=_what({"element": field}), ref=ref, text=str(field.get("use") or ""), submit=i == len(fields) - 1)
            act.check()
            await agent._act_one(caller, group, tab, act, label="replay")
        return f"Signed in with the operator's secrets ({', '.join(str(f.get('use')) for f in fields)})."
    if mode == "saved":
        said = await agent.login(caller, url_or_domain=url or _host(url), reason=why)
        # A sign-in the operator saved and allowed for this session is done at once and says so; any
        # other answer is a question now with the operator, which the replay waits for.
        if said.startswith("Signed in"):
            return said
        raise _Paused(said)
    what = {"login": "sign in", "two_factor": "enter the one-time code", "payment": "enter the card details"}.get(reason, "sign in")
    said = await agent.handoff(caller, reason=reason if reason in ("login", "two_factor", "payment") else "other", what=f"{what} on {_host(url) or 'the page'} ({why})")
    raise _Paused(said)


__all__ = ["SCRIPT_FILE", "RecordedSkills", "ReplayLost", "build_script", "describe", "params_of", "replay", "skill_markdown_body"]
