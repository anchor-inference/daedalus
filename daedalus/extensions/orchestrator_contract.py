"""What the orchestrator's contract tools do: requirements, exact-result review and decisions.

The limits are the host's, not the prompt's. A requirement the operator stated is not replaced or
withdrawn on the orchestrator's word: the fallback "draw the screen if you cannot record it" once
went to a member as a message, against the operator's demand for real recordings, and the operator
found drawn screens an hour and a half later. A reviewed result is pinned to its immutable report,
current contract and evidence: a silent video was once closed as done against a bar of two reference
videos with sound, because "done" was all a member's report took. The operator decides final acceptance.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from daedalus.extensions.notifications import Draft
from daedalus.extensions.orchestrator_domain import DomainConflict, OrchestratorDomain, record_verdict, return_result
from daedalus.extensions.orchestrator_loops import DECIDED
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.extensions.task_contract import REQUIREMENT_KINDS, Contracts, Requirement
from daedalus.host import prompts
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope
from daedalus.stores.files import FileRefused, StoredFile
from daedalus.stores.projects import Project
from daedalus.stores.staff import StaffError

if TYPE_CHECKING:
    from daedalus.extensions.orchestrator import Orchestrators

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
    return source == "operator" or source.startswith(("operator:", "answer:", "rule:"))


async def bound(orch: Orchestrators, session_id: str, source: str) -> str:
    """``operator`` bound to the message of theirs it came from, when there is one: the operator's chat
    shows each message's fate — the requirement it became, who got it, whether they confirmed it."""
    if source != "operator":
        return source
    seq = await orch.operator_message_seq(session_id)
    return f"operator:{seq}" if seq else source


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
    origin = await bound(orch, session_id, await source_of(orch, project, source))
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


async def review_result(
    orch: Orchestrators, project: Project, session_id: str, *, task_id: str,
    op: str = "inspect", result_id: str | None = None, verdict_id: str | None = None,
    expected_entity_revision: int | None = None, client_operation_id: str = "",
    verification: str = "unverified", accepted: bool = False,
    evidence_ids: list[str] | None = None, head: str | None = None, base: str | None = None,
    environment_digest: str | None = None, reason: str = "",
    contract_revision: int | None = None,
) -> str:
    """Review a pinned report with an office grant supplied by the host, never by tool arguments."""
    task = await card(orch, project, session_id, task_id)
    if task.get("project_id") != project.id:
        raise Refused("the result is outside this orchestrator's project")
    domain = OrchestratorDomain(orch.manager.db)
    if op == "inspect":
        results = await domain.results(task_id)
        if result_id:
            results = [item for item in results if item["result_id"] == result_id]
        contract = await domain.contract(task_id)
        return json.dumps({"task_id": task_id, "contract": contract, "results": results},
                          ensure_ascii=False, sort_keys=True)
    if op not in ("verdict", "return"):
        raise Refused("op is inspect, verdict or return")
    if not result_id or not client_operation_id or not isinstance(expected_entity_revision, int) or isinstance(expected_entity_revision, bool):
        raise Refused("mutation needs result_id, host call id and expected_entity_revision")
    if op == "return" and (not verdict_id or contract_revision is None):
        raise Refused("return needs the exact verdict_id and contract_revision")
    if op == "verdict" and verification not in ("verified", "failed", "stale"):
        raise Refused("verification must be verified, failed or stale")
    await orch.current(session_id)
    authority = orch.app.extensions.get("orchestrator_review_authority")
    if authority is None:
        raise Refused("review authority is unavailable")
    principal = await authority(session_id=session_id, project_id=project.id, task_id=task_id,
                                operation="review.verdict" if op == "verdict" else "review.return")
    if not isinstance(principal, Principal) or principal.origin_class != "agent" or not principal.grant_id:
        raise Refused("the host did not attest a scoped reviewer")
    control = ControlStore(orch.manager.db)
    payload = {"task_id": task_id, "result_id": result_id, "verdict_id": verdict_id,
               "verification": verification, "accepted": accepted, "evidence_ids": evidence_ids or [],
               "head": head, "base": base, "environment_digest": environment_digest,
               "reason": reason, "contract_revision": contract_revision}

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        if op == "verdict":
            return await record_verdict(
                conn, verdict_id=mutation.object_id, result_id=result_id,
                reviewer_actor_id=principal.actor_id, verification=verification, accepted=accepted,
                head=head, base=base, environment_digest=environment_digest,
                evidence_ids=evidence_ids or [], reason=reason)
        assert verdict_id is not None and contract_revision is not None
        return await return_result(
            conn, return_id=mutation.object_id, task_id=task_id, result_id=result_id,
            verdict_id=verdict_id, contract_revision=contract_revision,
            actor_id=principal.actor_id, reason=reason)

    try:
        response = await control.mutate(principal, Scope("project", project.id),
                                        "review.verdict" if op == "verdict" else "review.return",
                                        client_operation_id, expected_entity_revision,
                                        Entity("task", task_id), payload, effect)
    except (ControlConflict, ControlDenied, DomainConflict, ValueError, KeyError) as exc:
        raise Refused(str(exc)) from exc
    return json.dumps(response, ensure_ascii=False, sort_keys=True)


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
    "review_result": review_result,
    "decide": decide,
}

__all__ = ["OPS", "card", "deliver", "narrowed", "narrowing_refusal", "source_of"]
