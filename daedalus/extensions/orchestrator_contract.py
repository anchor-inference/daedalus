"""What the orchestrator's contract tools do: a card's requirements (``Require``), the check of a result
(``Accept``) and the decision that nothing further follows one (``Decide``).

The limits are the host's, not the prompt's. A requirement the operator stated is not replaced or
withdrawn on the orchestrator's word: the fallback "draw the screen if you cannot record it" once
went to a member as a message, against the operator's demand for real recordings, and the operator
found drawn screens an hour and a half later. A result is accepted only with a mark for each of its
checks and requirements: a silent video was once closed as done against a bar of two reference videos
with sound, because "done" was all a member's report took. Neither limit asks the operator anything
on its own — the orchestrator decides what goes to them.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from daedalus.extensions.notifications import Draft
from daedalus.extensions.orchestrator_loops import DECIDED
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.extensions.task_contract import REQUIREMENT_KINDS, RETURNED, Contracts, Requirement
from daedalus.host import prompts
from daedalus.stores.files import FileRefused, StoredFile
from daedalus.stores.projects import Project
from daedalus.stores.staff import StaffError

if TYPE_CHECKING:
    from daedalus.extensions.orchestrator import Orchestrators

VERDICTS = ("accepted", "returned")
WHY_MIN = 8
"""The least a reason may be: a clause, so "ok" is not a decision anyone can read later."""
OPERATOR_SOURCES = ("operator",)


def _team(orch: Orchestrators) -> Any:
    team = orch.team
    if team is None:
        raise Refused("the staff runtime is not running on this installation")
    return team


def _contracts(orch: Orchestrators) -> Contracts:
    return _team(orch).contracts  # type: ignore[no-any-return]


async def card(orch: Orchestrators, project: Project, session_id: str, task_id: str | None) -> dict[str, Any]:
    if orch.board is None:
        raise Refused("the board is not available on this installation")
    if not (task_id or "").strip():
        raise Refused("name the card: task_id")
    try:
        found: dict[str, Any] = await orch.board.get(task_id.strip(), actor=session_id)  # type: ignore[union-attr]
    except KeyError as exc:
        raise Refused(f"no task {task_id} on {project.name}'s board; Tasks() lists them") from exc
    return found


def _clip(text: str, limit: int = 60) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


async def source_of(orch: Orchestrators, project: Project, source: str | None) -> str:
    """Where a requirement comes from, checked where the host can check it: an answer must be one the
    operator gave in this project, a rule one in force. "operator" is the orchestrator relaying their
    words from the chat, which the host has no way to verify."""
    text = (source or "orchestrator").strip()
    lowered = text.lower()
    if lowered in ("operator", "orchestrator"):
        return lowered
    kind, _, ref = text.partition(":")
    kind, ref = kind.strip().lower(), ref.strip().strip("[]")
    if kind == "answer" and ref:
        ask = await orch.manager.asks.get(ref)
        if ask is None:
            row = await orch.manager.db.fetchone("SELECT id FROM asks WHERE short_id = ? AND project_id = ? ORDER BY created_at DESC LIMIT 1", (ref.lower(), project.id))
            ask = await orch.manager.asks.get(row["id"]) if row is not None else None
        if ask is None or ask.project_id != project.id:
            raise Refused(f"source {text!r}: {project.name} has no request {ref!r}")
        if ask.resolved_by != "operator":
            raise Refused(f"source {text!r}: request [{ask.short_id}] was not answered by the operator" + (" yet" if ask.open else f" but by the {ask.resolved_by}"))
        return f"answer:{ask.short_id}"
    if kind == "rule" and ref.isdigit():
        if int(ref) not in {r.id for r in await orch.manager.projects.rules(project.id)}:
            raise Refused(f"source {text!r}: no rule #{ref} is in force")
        return f"rule:{ref}"
    raise Refused("source is 'operator' (their words in this chat), 'orchestrator', 'answer:<request id>' or 'rule:<id>'")


def _operator_backed(source: str) -> bool:
    return source == "operator" or source.startswith(("answer:", "rule:"))


async def deliver(orch: Orchestrators, project: Project, task: dict[str, Any], requirement: Requirement, text: str, *, files: list[StoredFile] | None = None) -> str:
    """Give a requirement to the member at work on its card, into the turn it is in; what happened.
    Nobody at work on it: it goes with the card's next brief, as every requirement does."""
    team = _team(orch)
    member = await orch.manager.staff.get(task.get("assignee_staff_id") or "") if task.get("assignee_staff_id") else None
    live = await team.live_of(member) if member is not None and member.active else None
    if member is None or live is None or live.session.task_id != task["id"] or task["status"] != "doing":
        return "nobody is at work on the card now; it goes with the card's brief to whoever works it next"
    try:
        receipt = await team.tell(member, text, when="now", by="orchestrator", files=files or None)
    except StaffError as exc:
        return f"it could not be sent to {member.name} ({exc}); it goes with the card's next brief"
    path = (receipt.get("files") or [""])[0] if files else ""
    await team.contracts.delivered(requirement, staff_session_id=live.id, staff_id=member.id, via="message", message_id=str(receipt["message_id"]), path=path)
    state = receipt["state"] + (f" — {receipt['error']}" if receipt.get("error") else "")
    return f"sent to {member.name} into the turn they are in (receipt: {state}); their confirmation shows in the state block until it comes"


async def require(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    task_id: str,
    text: str = "",
    kind: str = "quality",
    source: str | None = None,
    replaces: str | None = None,
    withdraw: str | None = None,
    file: str | None = None,
    why: str = "",
) -> str:
    task = await card(orch, project, session_id, task_id)
    if task["status"] == "dropped":
        raise Refused(f"task {task['id']} is dropped")
    contracts = _contracts(orch)
    origin = await source_of(orch, project, source)
    if withdraw:
        gone = await contracts.find(task["id"], withdraw)
        if gone is None or gone.state != "active":
            raise Refused(f"task {task['id']} has no requirement {withdraw} in force; Tasks(op='get') lists them")
        if gone.from_operator and not _operator_backed(origin):
            raise Refused(
                f"{gone.label} is the operator's (\"{_clip(gone.text)}\"): dropping it is their decision. AskOperator, and withdraw it "
                "with their answer as source='answer:<request id>'"
            )
        await contracts.withdraw(gone)
        said = await deliver(orch, project, task, gone, prompts.STAFF_REQUIREMENT_WITHDRAWN.format(label=gone.label, task_id=task["id"], origin=_origin_words(origin), text=gone.text))
        await _journal(orch, project, f"Withdrew {gone.label} of {task['id']} (\"{_clip(gone.text)}\"), on {_origin_words(origin)}'s word.", task["id"])
        return f"{gone.label} of {task['id']} is withdrawn; {said}"
    replaced: Requirement | None = None
    if replaces:
        replaced = await contracts.find(task["id"], replaces)
        if replaced is None or replaced.state != "active":
            raise Refused(f"task {task['id']} has no requirement {replaces} in force; Tasks(op='get') lists them")
        if replaced.from_operator and not _operator_backed(origin):
            raise Refused(
                f"{replaced.label} is the operator's (\"{_clip(replaced.text)}\"): changing it — or letting the work fall short of it — is "
                "their decision. AskOperator with the options, and once they answer, Require(replaces=…, source='answer:<request id>')"
            )
    stored: StoredFile | None = None
    if file:
        try:
            [stored] = await _team(orch).handoff.resolve([file], project=project, actor="orchestrator")
        except FileRefused as exc:
            raise Refused(str(exc)) from exc
        except ValueError as exc:
            raise Refused(f"file names one file: {exc}") from exc
        kind = "input"
    kind = (kind or "quality").strip().lower()
    if kind not in REQUIREMENT_KINDS:
        raise Refused(f"kind is one of {', '.join(REQUIREMENT_KINDS)}")
    if kind == "input" and stored is None:
        raise Refused("an input names its file: file='att:…' (or a path in the project's folders)")
    body = (text or "").strip() or (f"{stored.name}: the work starts from it" if stored is not None else "")
    grants = [r for r in await contracts.requirements(task["id"]) if r.kind == "scope" and r.from_operator]
    if grants and kind == "constraint" and not _operator_backed(origin) and len(" ".join((why or "").split())) < WHY_MIN:
        raise Refused(narrowing_refusal(grants[0].text))
    try:
        requirement = await contracts.add(task["id"], project.id, body, kind, origin, replaces=replaced, file_id=stored.id if stored is not None else None)
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    if stored is not None:
        await orch.manager.files.attach_to_task(task["id"], [stored], actor="orchestrator")
    told = ""
    if grants and kind == "constraint" and not _operator_backed(origin):
        await narrowed(orch, project, task, requirement, grants[0], why)
        told = "; the operator is told of the condition you added to what they allowed"
    message = prompts.STAFF_REQUIREMENT.format(
        label=requirement.label, task_id=task["id"], origin=requirement.origin(), replaces=f", replaces {replaced.label}" if replaced is not None else "", text=requirement.text,
    )
    said = await deliver(orch, project, task, requirement, message, files=[stored] if stored is not None else None)
    if kind == "scope" and _operator_backed(origin) and task.get("assignee_staff_id"):
        holder = await orch.manager.staff.get(task["assignee_staff_id"])
        from daedalus.extensions.orchestrator_team import restricted  # Lazy: the team's module imports this one

        limit = restricted(holder) if holder is not None else ""
        if limit:
            said += (
                f". {holder.name} runs {limit}, so cannot do what this allows: StaffEdit(staff='{holder.name}', permission_mode=…) — it takes effect "  # type: ignore[union-attr]
                "from the next session, so Release and Assign(task_id) again — rather than asking the operator to allow it again"
            )
    await orch._changed(project.id, "board", "orchestrator")
    swap = f" in place of {replaced.label}" if replaced is not None else ""
    return f"{requirement.label} is on {task['id']} ({requirement.kind}, from {requirement.origin()}){swap}; {said}{told}"


def narrowing_refusal(grant: str) -> str:
    return (
        f"the operator allowed this work \"{_clip(grant, 80)}\"; a condition of yours that narrows it takes its reason (why=…), "
        "and the operator is told of it. What they allowed is not narrowed quietly"
    )


async def narrowed(orch: Orchestrators, project: Project, task: dict[str, Any], condition: Requirement, grant: Requirement, why: str) -> None:
    """The orchestrator put a condition of its own on work the operator gave a free hand: the journal
    keeps it, and the operator hears of it where they look. An operator's "if something needs fixing,
    fix it" once became "read-only, no configuration changes, nothing until a report and a
    permission" in the brief, and the operator learnt it only from the result."""
    reason = " ".join((why or "").split())
    await _journal(orch, project, f"Added a condition to what the operator allowed on {task['id']} ({grant.label} \"{_clip(grant.text, 80)}\"): {condition.label} {condition.text} — why: {reason}", task["id"], kind="narrowing")
    notifications = orch.app.notifications
    if notifications is None:
        return
    await notifications.post(Draft(
        "orchestrator_report",
        f"{project.name}: a condition on what you allowed",
        f"On \"{_clip(task['title'])}\" you allowed: {_clip(grant.text, 160)}. The orchestrator added: {_clip(condition.text, 200)} — {reason}",
        kind="project_narrowing",
        project_id=project.id,
        link=f"/app/project/{project.id}",
        dedupe_key=f"project-narrowing:{condition.id}",
        source="orchestrator",
    ))


def _origin_words(source: str) -> str:
    if source.startswith("answer:"):
        return f"the operator's answer [{source.split(':', 1)[1]}]"
    if source.startswith("rule:"):
        return f"the operator's rule #{source.split(':', 1)[1]}"
    return "the operator" if source == "operator" else "the orchestrator"


async def _journal(orch: Orchestrators, project: Project, text: str, task_id: str, kind: str = "decision") -> None:
    await orch.manager.projects.record(project.id, "orchestrator", kind, text, {"task_id": task_id})
    await orch._changed(project.id, "journal", "orchestrator")


def _marks(raw: list[Any] | None) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    for entry in raw or []:
        if isinstance(entry, dict):
            item = str(entry.get("item") or entry.get("check") or "").strip()
            ok = entry.get("ok")
            if isinstance(ok, str):
                ok = ok.strip().lower() in ("true", "yes", "ok", "met", "pass", "passed")
            out.append((item, bool(ok), str(entry.get("note") or "").strip()[:500]))
    return out


async def accept(
    orch: Orchestrators,
    project: Project,
    session_id: str,
    *,
    task_id: str,
    verdict: str = "accepted",
    checks: list[Any] | None = None,
    note: str = "",
    ask_operator: bool = False,
    staff: str | None = None,
    reason: str = "",
) -> str:
    task = await card(orch, project, session_id, task_id)
    verdict = (verdict or "accepted").strip().lower()
    if verdict not in VERDICTS:
        raise Refused(f"verdict is one of {', '.join(VERDICTS)}")
    if task["status"] not in ("done", "review"):
        raise Refused(f"task {task['id']} is {task['status']}: nothing was handed in to check yet")
    contracts = _contracts(orch)
    items = await contracts.checks(task["id"])
    requirements = await contracts.requirements(task["id"])
    marked: dict[str, tuple[bool, str]] = {}
    unknown: list[str] = []
    given = _marks(checks)
    if not items and given:
        # A card made before it had checks, or with none: the checks marked now become its checks.
        # Refusing them as matching nothing sent an orchestrator round in circles on a finished card.
        items = [{"text": item[:200], "done": False} for item, _, _ in given if item and contracts.match(item, [], requirements) is None]
    for item, ok, why in given:
        target = contracts.match(item, items, requirements)
        if target is None:
            unknown.append(item or "(unnamed)")
            continue
        label = f"C{int(target[1]) + 1}" if target[0] == "C" else target[1].label  # type: ignore[union-attr]
        marked[label] = (ok, why)
    if unknown:
        raise Refused(f"{', '.join(unknown)} matches no check (C…) or requirement (R…) of task {task['id']}; Tasks(op='get', task_id='{task['id']}') lists them")
    # An input is checked by the host: the member could not hand in without opening it.
    holder = task.get("assignee_staff_id") or ""
    member = await orch.manager.staff.get(holder) if holder else None
    unopened = {r.id for r, _ in await contracts.unmet_inputs(task["id"], holder, cli=member is not None and member.harness != "daedalus")} if holder else set()
    for requirement in requirements:
        if requirement.kind == "input" and requirement.label not in marked and requirement.id not in unopened:
            marked[requirement.label] = (True, "opened by the member")
    wanted = [(f"C{i}", item["text"]) for i, item in enumerate(items, start=1)] + [(r.label, r.text) for r in requirements]
    missing = [f"{label} ({_clip(text, 40)})" for label, text in wanted if label not in marked]
    failed = [label for label, (ok, _) in marked.items() if not ok]
    if verdict == "accepted":
        if missing:
            raise Refused(
                f"not marked: {', '.join(missing)}. Accepting takes a mark for each: checks=[{{item, ok, note}}]. A check you did not see pass "
                "is ok=false — then the work is returned, or the operator judges it (ask_operator=true)"
            )
        if failed and not ask_operator:
            raise Refused(f"{', '.join(failed)} {'is' if len(failed) == 1 else 'are'} marked not met: accepted needs every item met — return the work (verdict='returned') or let the operator judge it (ask_operator=true)")
    elif not failed and not note.strip():
        raise Refused("say what is wrong: a mark with ok=false for what failed, or a note")
    await _keep_marks(contracts, task["id"], items, requirements, marked)
    summary = "; ".join(f"{label} {'met' if ok else 'not met'}" + (f" ({why})" if why else "") for label, (ok, why) in marked.items())
    loops = orch.loops
    if verdict == "returned":
        if task.get("branch"):
            raise Refused(f"task {task['id']} has work on branch {task['branch']}, which the operator reviews: they send it back from review; for more work on it, create a new task")
        what = "; ".join(filter(None, [", ".join(f"{label} not met" + (f": {marked[label][1]}" if marked[label][1] else "") for label in failed), note.strip()]))
        await contracts.set_acceptance(task["id"], "returned")
        await _team(orch).note_on_card(task["id"], RETURNED + what)
        from daedalus.extensions.orchestrator_team import assign  # Lazy: the team's module imports this one

        said = await assign(orch, project, session_id, staff=staff, task_id=task["id"], reason=reason)
        await _journal(orch, project, f"Returned {task['id']} \"{_clip(task['title'])}\": {what}", task["id"])
        return f"returned {task['id']}: {what}. {said}"
    if ask_operator:
        await contracts.set_acceptance(task["id"], "accepted")
        if task["status"] != "review":
            await orch.board.update(task["id"], status="review", note=f"checked by the orchestrator, for the operator to accept: {summary or note.strip() or 'no checks'}", actor=session_id)  # type: ignore[union-attr]
        await loops.close(project.id, task_id=task["id"], by="orchestrator", decision="given to the operator to accept")
        await _ask_operator_to_accept(orch, project, task, summary, note)
        return f"{task['id']} is in the operator's review column with your marks ({summary or 'no checks'}); their acceptance arrives as an event"
    await contracts.set_acceptance(task["id"], "accepted")
    await _team(orch).note_on_card(task["id"], f"accepted by the orchestrator: {summary or note.strip() or 'no checks to mark'}")
    await loops.close(project.id, task_id=task["id"], by="orchestrator", decision="accepted")
    await orch._changed(project.id, "board", "orchestrator")
    merge = "; the branch still waits for the operator's merge" if task["status"] == "review" and task.get("branch") else ""
    return f"{task['id']} \"{_clip(task['title'])}\" is accepted" + (f": {summary}" if summary else "") + merge


async def _keep_marks(contracts: Contracts, task_id: str, items: list[dict[str, Any]], requirements: list[Requirement], marked: dict[str, tuple[bool, str]]) -> None:
    for index, item in enumerate(items, start=1):
        if f"C{index}" in marked:
            ok, why = marked[f"C{index}"]
            item["mark"] = {"ok": ok, "note": why, "by": "orchestrator"}
            item["done"] = ok
    if items:
        await contracts.set_checks(task_id, items)
    for requirement in requirements:
        if requirement.label in marked:
            ok, why = marked[requirement.label]
            await contracts.set_mark(requirement, {"ok": ok, "note": why, "by": "orchestrator"})


async def _ask_operator_to_accept(orch: Orchestrators, project: Project, task: dict[str, Any], summary: str, note: str) -> None:
    notifications = orch.app.notifications
    if notifications is None:
        return
    body = "; ".join(filter(None, [f"The orchestrator's check: {summary}" if summary else "", note.strip()])) or "The orchestrator checked what it could; the rest is yours to see."
    await notifications.post(Draft(
        "orchestrator_report",
        f"{project.name}: \"{_clip(task['title'])}\" waits for your acceptance",
        body,
        kind="project_accept",
        project_id=project.id,
        link=f"/app/project/{project.id}",
        dedupe_key=f"project-accept:{task['id']}",
        source="orchestrator",
    ))


async def decide(orch: Orchestrators, project: Project, session_id: str, *, why: str, task_id: str | None = None, loop: str | None = None) -> str:
    """Nothing further on a result or a card, and why: the result leaves the list, the journal keeps
    the reason. A card decided on this way is not raised again as nobody's."""
    reason = " ".join((why or "").split())
    if len(reason) < WHY_MIN:
        raise Refused("say why nothing further follows, in a sentence: the journal keeps it")
    target_task, target_staff = (task_id or "").strip() or None, None
    if loop:
        number = re.sub(r"^[Ll]", "", str(loop).strip())
        row = await orch.manager.db.fetchone("SELECT task_id, staff_id FROM open_loops WHERE id = ? AND project_id = ? AND closed_at IS NULL", (int(number) if number.isdigit() else -1, project.id))
        if row is None:
            raise Refused(f"{project.name} has no open result {loop}; the state block lists them")
        target_task, target_staff = row["task_id"], (None if row["task_id"] else row["staff_id"])
    if not target_task and not target_staff:
        raise Refused("name what the decision is about: task_id, or loop (L… from the state block)")
    closed = await orch.loops.close(project.id, task_id=target_task, staff_id=target_staff, by="orchestrator", decision=DECIDED + reason)
    if not closed:
        raise Refused(f"no result is waiting for a decision on {target_task or 'that'}; the state block lists those that are")
    about = f"task {target_task}" if target_task else "a member's report"
    await _journal(orch, project, f"Nothing further on {about}: {reason}", target_task or "")
    return f"decided: nothing further on {about} ({', '.join(f'L{i}' for i in closed)} closed); the journal keeps why"


OPS: dict[str, Callable[..., Awaitable[str]]] = {
    "require": require,
    "accept": accept,
    "decide": decide,
}

__all__ = ["OPS", "card", "deliver", "narrowed", "narrowing_refusal", "source_of"]
