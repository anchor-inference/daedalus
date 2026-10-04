"""The operator's list of what waits for them, and the answers they send from it together.

A project's orchestrator asks in batches, and its staff's permissions and escalated questions wait
for the operator beside them. The app shows them as one list in the right panel — a project's own in
its focus mode, every orchestrated project's in the main chat — where the operator answers some,
leaves others, and presses Send once.

Each answer follows the rule every window follows: the first answer to update the request's row
wins (:meth:`daedalus.extensions.staff.Team.answer`), and one that lost says who was first. What
this module adds is the batch. The answers that won are marked with the batch's id on their
resolution, so their own ``ask.answered`` events do not wake the orchestrator one by one, and one
``ask.batch`` event per project then wakes it once with all of them. An orchestrator woken for
every answer of a list of eight would spend eight turns on what one turn reads better.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from daedalus.extensions.staff import AlreadyAnswered
from daedalus.harness.capabilities import CAPABILITIES, Capabilities
from daedalus.harness.contract import mcp_server
from daedalus.stores.staff import Ask, StaffError

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

BATCH_MAX = 50
"""Answers in one send. The list is a screen or two; more than this is not a person pressing Send."""
NOTE_MAX = 4000


class Unanswerable(ValueError):
    """An answer of the wrong shape for its request, said so the operator can fix it."""


def _options(ask: Ask) -> list[str]:
    return [str(o) for o in ask.detail.get("options") or [] if isinstance(o, str)]


def always_server(ask: Ask, caps: Capabilities | None) -> str:
    """The MCP server whose every tool "always" could allow at once for this request, or empty."""
    if ask.kind != "permission" or caps is None or not caps.standing_rules:
        return ""
    return mcp_server(str(ask.detail.get("tool") or ""))


def section_of(ask: Ask) -> str:
    """Where a request goes in the list: what staff wait on (permissions, escalated questions) above the
    orchestrator's own questions, since a member is stopped until it is answered."""
    return "requests" if ask.origin == "staff" else "questions"


async def _view(app: Application, ask: Ask, names: dict[str, str], members: dict[str, Any]) -> dict[str, Any]:
    manager = app.manager
    assert manager is not None
    member = members.get(ask.staff_id or "")
    if ask.staff_id and ask.staff_id not in members:
        member = members[ask.staff_id] = await manager.staff.get(ask.staff_id)
    dispatcher = app.extensions.get("dispatcher")
    host = bool(await dispatcher.host_level(ask)) if dispatcher is not None else False
    options = _options(ask)
    harness = getattr(member, "harness", "") if member is not None else ""
    caps = CAPABILITIES.get(harness)
    return {
        **ask.view(),
        "project_name": names.get(ask.project_id or "", "") or str(ask.detail.get("name") or ""),
        "asker": "main" if ask.origin == "dispatcher" else "orchestrator" if ask.origin == "orchestrator" else (member.name if member is not None else "staff"),
        "section": section_of(ask),
        "options": options,
        "multi": bool(ask.detail.get("multi")) and len(options) > 1,
        "host": host,
        # "Always" exists where the member's CLI has a standing grant to give; a Daedalus member's
        # gate has none.
        "always": ask.kind == "permission" and caps is not None and caps.permissions != "none",
        # And "Always for all its tools" where the tool is an MCP server's and the CLI keeps rules.
        "always_server": always_server(ask, caps),
        "urgent": bool(ask.detail.get("urgent")),
    }


async def waiting(app: Application, project_id: str | None = None) -> list[dict[str, Any]]:
    """What waits for the operator, oldest first: one project's requests, or — with no project — those
    of every project that has an orchestrator, and the main orchestrator's own confirmations."""
    manager = app.manager
    assert manager is not None
    projects = {p.id: p for p in await manager.projects.list()}
    names = {pid: p.name for pid, p in projects.items()}
    if project_id is not None:
        asks = await manager.asks.open_for(project_id, "operator")
    else:
        rows = await manager.db.fetchall("SELECT id FROM asks WHERE resolved_at IS NULL AND routed_to = 'operator' ORDER BY created_at, rowid")
        asks = [a for a in [await manager.asks.get(r["id"]) for r in rows] if a is not None]
        asks = [a for a in asks if a.origin == "dispatcher" or (a.project_id in projects and projects[a.project_id].settings.orchestrator.enabled)]
    members: dict[str, Any] = {}
    return [await _view(app, a, names, members) for a in asks]


def _shape(ask: Ask, item: dict[str, Any]) -> dict[str, Any]:
    """The arguments of :meth:`Team.answer` for one item, or the reason it is not an answer to this request."""
    selected = [str(s) for s in item.get("selected") or []]
    text = str(item.get("text") or "").strip()
    note = str(item.get("note") or "").strip()
    allow = item.get("allow")
    if len(text) > NOTE_MAX or len(note) > NOTE_MAX:
        raise Unanswerable(f"an answer is at most {NOTE_MAX} characters")
    options = _options(ask)
    if ask.kind == "permission":
        if not isinstance(allow, bool) or selected or text:
            raise Unanswerable("a permission is answered with allow or deny, and a reason if you like")
        always = bool(item.get("always")) and allow
        return {"allow": allow, "always": always, "server": always and bool(item.get("server")), "note": note}
    if ask.kind in ("folder", "project"):
        if text or note:
            raise Unanswerable(f"a {ask.kind} request is answered with one of its options, not in words")
        if isinstance(allow, bool) and not selected and not options:
            return {"allow": allow}
        if len(selected) != 1 or selected[0] not in options:
            raise Unanswerable(f"choose one of {', '.join(options) or 'its options'}")
        return {"selected": selected}
    unknown = [s for s in selected if s not in options]
    if unknown:
        raise Unanswerable(f"{unknown[0]!r} is not one of its options")
    if len(selected) > 1 and not (ask.detail.get("multi") and len(options) > 1):
        raise Unanswerable("choose one option")
    if selected:
        if text:
            raise Unanswerable("with an option chosen, words go in the note")
        return {"selected": selected, "note": note}
    if note:
        raise Unanswerable("a note goes beside a chosen option; without one, write the answer itself")
    if not text:
        raise Unanswerable("choose an option or write an answer")
    # A question is always open to the operator's own words, whatever its options: rows stored while
    # an orchestrator could still say "options only" (``allow_free``) are read the same way.
    return {"text": text}


async def answer(app: Application, items: list[dict[str, Any]], *, project_id: str | None, via: str) -> dict[str, Any]:
    """Answer several requests at once. Every item has its own outcome — answered, lost to an answer
    given elsewhere, refused for its shape, or gone — and one never stops the others. The answers
    that won wake each project's orchestrator once, together."""
    manager = app.manager
    assert manager is not None
    team = app.extensions.get("staff")
    if team is None:
        raise RuntimeError("the team is not running on this installation")
    if len(items) > BATCH_MAX:
        raise Unanswerable(f"at most {BATCH_MAX} answers at once")
    batch_id = f"b{uuid.uuid4().hex[:10]}"
    results: list[dict[str, Any]] = []
    won: dict[str, list[str]] = {}
    for item in items:
        ref = str(item.get("ask_id") or "")
        ask = await manager.asks.get(ref) if ref else None
        if ask is None:
            results.append({"ask_id": ref, "state": "missing", "error": "no such request"})
            continue
        base = {"ask_id": ask.id, "short_id": ask.short_id}
        if project_id is not None and ask.project_id != project_id:
            results.append({**base, "state": "refused", "error": "that request is not this project's"})
            continue
        if ask.routed_to != "operator" and ask.open:
            results.append({**base, "state": "refused", "error": "that request is the orchestrator's to answer"})
            continue
        if not ask.open:
            results.append(_lost(base, ask))
            continue
        try:
            shape = _shape(ask, item)
            done = await team.answer(ask.id, by="operator", via=via, extra={"batch": batch_id}, **shape)
        except AlreadyAnswered:
            current = await manager.asks.get(ask.id)
            results.append(_lost(base, current or ask))
            continue
        except (Unanswerable, StaffError, KeyError) as exc:
            results.append({**base, "state": "refused", "error": str(exc)})
            continue
        results.append({**base, "state": "answered", "applied": done.get("applied", True),
                        "delivered": bool(done.get("delivered")), "error": str(done.get("error") or ""),
                        "ask": done.get("ask")})
        if ask.project_id and ask.origin != "dispatcher":
            won.setdefault(ask.project_id, []).append(ask.id)
    for pid, ask_ids in won.items():
        try:
            await manager.bus.publish("ask.batch", {"batch_id": batch_id, "ask_ids": ask_ids, "by": "operator", "via": via}, project_id=pid)
        except Exception:  # noqa: BLE001 — the answers are recorded; the wake-up is the orchestrator's queue's to retry
            logger.warning("could not announce the batch %s of %s", batch_id, pid, exc_info=True)
    return {"batch_id": batch_id, "results": results}


def _lost(base: dict[str, Any], ask: Ask) -> dict[str, Any]:
    """An item that found its request already closed: by whom, and whether it was taken back rather than answered."""
    return {**base, "state": "conflict", "answered_by": ask.resolved_by or "", "withdrawn": ask.resolved_by == "system", "ask": ask.view()}


__all__ = ["BATCH_MAX", "Unanswerable", "answer", "section_of", "waiting"]
