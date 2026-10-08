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

from daedalus.extensions.contract_changes import apply_change_in, link_stop_in, stage_change_in
from daedalus.extensions.notifications import Draft
from daedalus.extensions.orchestrator_domain import (
    DomainConflict,
    OrchestratorDomain,
    accept_report,
    invalidate_moved_verdicts,
    record_verdict,
    return_result,
)
from daedalus.extensions.orchestrator_loops import DECIDED
from daedalus.extensions.orchestrator_ops import Refused, board_principal
from daedalus.extensions.task_contract import REQUIREMENT_KINDS, Contracts, Requirement
from daedalus.extensions.task_controls import queue_stop
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope
from daedalus.stores.files import FileRefused, StoredFile
from daedalus.stores.projects import Project

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
    op: str = "stage",
    intent_id: str | None = None,
    client_operation_id: str = "",
    expected_entity_revision: int | None = None,
) -> str:
    task = await card(orch, project, session_id, task_id)
    if task.get("project_id") != project.id:
        raise Refused("the task is outside this coordinator's project")
    if op == "inspect":
        rows = await orch.manager.db.fetchall(
            "SELECT id,state,base_contract_revision,base_attempt_id,stop_receipt_id,stop_effect_id,"
            " apply_receipt_id,created_at,applied_at FROM contract_change_intents"
            " WHERE task_id = ? ORDER BY created_at,id", (task["id"],))
        return json.dumps({"task_id": task["id"], "intents": [dict(row) for row in rows]}, ensure_ascii=False)
    if op not in ("stage", "stop", "apply") or not client_operation_id or not isinstance(expected_entity_revision, int) or isinstance(expected_entity_revision, bool):
        raise Refused("mutation needs op=stage, stop or apply, client_operation_id and expected_entity_revision")
    if op == "stop":
        if not intent_id:
            raise Refused("stop needs the exact staged intent_id")
        row = await orch.manager.db.fetchone("SELECT actor_id,state,stop_effect_id FROM contract_change_intents"
                                             " WHERE id = ? AND task_id = ?", (intent_id, task["id"]))
        principal = await board_principal(orch, session_id, project.id, "task.stop", task["id"])
        if row is None or row["actor_id"] != principal.actor_id or row["state"] != "pending_stop":
            raise Refused("no pending contract stop belongs to this actor and task")
        if row["stop_effect_id"]:
            previous = await orch.manager.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?",
                                                       (row["stop_effect_id"],))
            if previous is None or previous["state"] not in ("cancelled", "failed"):
                raise Refused("the preceding stop must be reconciled before another is issued")
        try:
            stop = await queue_stop(orch.app, task["id"], principal,
                                    client_operation_id=client_operation_id,
                                    expected_entity_revision=expected_entity_revision,
                                    reason="the task contract has a pending semantic change")
            async with orch.manager.db.transaction() as conn:
                await link_stop_in(conn, intent_id=intent_id, actor_id=principal.actor_id,
                                   stop_receipt_id=stop["receipt_id"], stop_effect_id=stop.get("effect_id"))
        except (ControlConflict, ControlDenied, DomainConflict) as exc:
            raise Refused(str(exc)) from exc
        return json.dumps({"intent_id": intent_id, "state": "pending_physical_exit" if stop.get("effect_id") else "ready_to_apply",
                           "stop_receipt_id": stop["receipt_id"], "stop_effect_id": stop.get("effect_id"),
                           "entity_revision": stop["entity_revision"]}, ensure_ascii=False, sort_keys=True)
    if op == "apply":
        if not intent_id:
            raise Refused("apply needs the exact staged intent_id")
        row = await orch.manager.db.fetchone("SELECT actor_id,state,request_json,base_contract_revision FROM contract_change_intents"
                                             " WHERE id = ? AND task_id = ?", (intent_id, task["id"]))
        if row is None:
            raise Refused("the contract intent is not on this task")
        staged = json.loads(row["request_json"])
        principal = await board_principal(orch, session_id, project.id, "contract.apply", task["id"])
        if row["actor_id"] != principal.actor_id:
            raise Refused("the contract intent belongs to another actor")
        async def apply_effect(conn: Any, mutation: Any) -> dict[str, Any]:
            return await apply_change_in(conn, intent_id=intent_id, principal=principal,
                                         receipt_id=mutation.receipt_id)
        try:
            result = await ControlStore(orch.manager.db).mutate(
                principal, Scope("project", project.id), "contract.apply", client_operation_id,
                expected_entity_revision, Entity("task", task["id"]),
                {"intent_id": intent_id, "source_literal": staged["source_literal"]}, apply_effect)
        except (ControlConflict, ControlDenied, DomainConflict) as exc:
            raise Refused(str(exc)) from exc
        if (row["state"] != "applied" and staged["mode"] != "withdraw" and staged["kind"] == "constraint"
                and staged["source"] == "orchestrator"):
            condition = await _contracts(orch).find(task["id"], result["requirement_id"])
            grant = next((item for item in await _contracts(orch).requirements(task["id"])
                          if item.kind == "scope" and item.from_operator), None)
            if condition is not None and grant is not None:
                await narrowed(orch, project, task, condition, grant, staged.get("why", ""))
        await orch._changed(project.id, "board", "orchestrator")
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    if intent_id:
        raise Refused("stage creates its own stable intent_id")
    if withdraw and (replaces or text.strip() or file):
        raise Refused("withdraw names only its current requirement and source")
    if task["status"] in ("review", "done", "dropped"):
        raise Refused("return or reopen the exact result before changing this task's contract")
    contracts = _contracts(orch)
    literal_source = await source_of(orch, project, source)
    origin = await bound(orch, session_id, literal_source)
    mode = "withdraw" if withdraw else "replace" if replaces else "add"
    target: Requirement | None = None
    if withdraw:
        target = await contracts.find(task["id"], withdraw)
        if target is None or target.state != "active":
            raise Refused(f"task {task['id']} has no requirement {withdraw} in force; Tasks(op='get') lists them")
        if target.from_operator and not _operator_backed(origin):
            raise Refused(
                f"{target.label} is the operator's (\"{_clip(target.text)}\"): dropping it is their decision. AskOperator, and withdraw it "
                "with their answer as source='answer:<request id>'"
            )
    elif replaces:
        target = await contracts.find(task["id"], replaces)
        if target is None or target.state != "active":
            raise Refused(f"task {task['id']} has no requirement {replaces} in force; Tasks(op='get') lists them")
        if target.from_operator and not _operator_backed(origin):
            raise Refused(
                f"{target.label} is the operator's (\"{_clip(target.text)}\"): changing it — or letting the work fall short of it — is "
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
    if not withdraw and kind == "input" and stored is None:
        raise Refused("an input names its file: file='att:…' (or a path in the project's folders)")
    body = (text or "").strip() or (f"{stored.name}: the work starts from it" if stored is not None else "")
    if not withdraw and (not body or len(body) > 1000):
        raise Refused("a requirement needs text of at most 1000 characters")
    if mode == "add":
        same = next((item for item in await contracts.requirements(task["id"])
                     if item.text.casefold() == body.casefold()
                     and item.file_id == (stored.id if stored else None)), None)
        if same is not None:
            return json.dumps({"task_id": task["id"], "state": "unchanged",
                               "requirement_id": same.id, "label": same.label}, ensure_ascii=False)
    grants = [r for r in await contracts.requirements(task["id"]) if r.kind == "scope" and r.from_operator]
    if grants and kind == "constraint" and not _operator_backed(origin) and len(" ".join((why or "").split())) < WHY_MIN:
        raise Refused(narrowing_refusal(grants[0].text))
    principal = await board_principal(orch, session_id, project.id,
                                      "contract.withdraw" if withdraw else "contract.require", task["id"])
    request = {"mode": mode, "text": body, "kind": kind, "source": origin,
               "source_literal": literal_source, "target_id": target.id if target else None,
               "file_id": stored.id if stored else None, "why": why}
    async def stage_effect(conn: Any, mutation: Any) -> dict[str, Any]:
        return await stage_change_in(conn, intent_id=mutation.object_id, receipt_id=mutation.receipt_id,
                                     principal=principal, project_id=project.id, task_id=task["id"],
                                     client_operation_id=client_operation_id, request=request)
    try:
        result = await ControlStore(orch.manager.db).mutate(
            principal, Scope("project", project.id), "contract.withdraw" if withdraw else "contract.require",
            client_operation_id, expected_entity_revision, Entity("task", task["id"]), request, stage_effect)
        current_intent = await orch.manager.db.fetchone(
            "SELECT state,apply_receipt_id FROM contract_change_intents WHERE id = ?", (result["intent_id"],))
        if current_intent is None:
            raise Refused("the contract intent is missing after its receipt")
        if current_intent["state"] == "applied":
            result["state"] = "applied"
            result["apply_receipt_id"] = current_intent["apply_receipt_id"]
        elif result["state"] == "pending_stop":
            try:
                stop_principal = await board_principal(orch, session_id, project.id, "task.stop", task["id"])
                stop = await queue_stop(orch.app, task["id"], stop_principal,
                                        client_operation_id=f"{client_operation_id}:stop",
                                        expected_entity_revision=result["entity_revision"],
                                        reason="the task contract has a pending semantic change")
            except (ControlConflict, ControlDenied, Refused) as exc:
                result["state"] = "stop_unavailable"
                result["blocker"] = str(exc)
            else:
                async with orch.manager.db.transaction() as conn:
                    await link_stop_in(conn, intent_id=result["intent_id"], actor_id=principal.actor_id,
                                       stop_receipt_id=stop["receipt_id"], stop_effect_id=stop.get("effect_id"))
                result["stop_receipt_id"] = stop["receipt_id"]
                result["stop_effect_id"] = stop.get("effect_id")
                result["state"] = "pending_physical_exit" if stop.get("effect_id") else "ready_to_apply"
        else:
            result["state"] = "ready_to_apply"
    except (ControlConflict, ControlDenied, DomainConflict) as exc:
        raise Refused(str(exc)) from exc
    await orch._changed(project.id, "board", "orchestrator")
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


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
        if task.get("branch") and any(row["verdict_id"] for row in results):
            review = getattr(orch.team, "review", None) if orch.team is not None else None
            try:
                binding = await review.review(task_id) if review is not None else None
            except (KeyError, ValueError, RuntimeError, OSError):
                binding = None
            invalidate_moved_verdicts(results, binding)
        if result_id:
            results = [item for item in results if item["result_id"] == result_id]
        contract = await domain.contract(task_id)
        return json.dumps({"task_id": task_id, "contract": contract, "results": results},
                          ensure_ascii=False, sort_keys=True)
    if op not in ("verdict", "return", "accept"):
        raise Refused("op is inspect, verdict, accept or return")
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
                                operation="review.return" if op == "return" else "review.verdict")
    if not isinstance(principal, Principal) or principal.origin_class != "agent" or not principal.grant_id:
        raise Refused("the host did not attest a scoped reviewer")
    control = ControlStore(orch.manager.db)
    payload = {"task_id": task_id, "result_id": result_id, "verdict_id": verdict_id,
               "verification": verification, "accepted": accepted, "evidence_ids": evidence_ids or [],
               "head": head, "base": base, "environment_digest": environment_digest,
               "reason": reason, "contract_revision": contract_revision}

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        if op == "accept":
            return await accept_report(
                conn, verdict_id=mutation.object_id, task_id=task_id, result_id=result_id,
                reviewer_actor_id=principal.actor_id, reason=reason)
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
                                        "review.return" if op == "return" else "review.verdict",
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

__all__ = ["OPS", "card", "narrowed", "narrowing_refusal", "source_of"]
