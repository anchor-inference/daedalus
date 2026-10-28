"""Persist agent schedule requests, and activate them under the operator's standing choice.

Every schedule the agent asks for is first saved as a proposal: its files are pinned by digest and the
request is hashed, whatever happens next. With ``scheduler.require_approval`` on, the proposal waits
for the operator's Accept. With it off (the default) the host accepts it at once on the operator's
behalf, through the very same acceptance, so the file checks, the grant and the receipts are those of
an approval the operator pressed — only the press is replaced by the setting.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from croniter import croniter

from daedalus.extensions.recurring import _contract
from daedalus.extensions.schedule_files import read_project_file
from daedalus.stores.control import (
    ControlConflict,
    ControlDenied,
    ControlStore,
    Entity,
    Principal,
    Scope,
    canonical,
    digest,
    now,
    one,
)
from daedalus.stores.files import FileRefused

if TYPE_CHECKING:
    from daedalus.app import Application


def _due(cron: str | None, run_at: str | None) -> str:
    if bool(cron) == bool(run_at):
        raise ValueError("choose exactly one time rule")
    if cron:
        if not croniter.is_valid(cron):
            raise ValueError("invalid cron expression")
        return croniter(cron, datetime.now(UTC)).get_next(datetime).astimezone(UTC).isoformat()
    due = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
    if due.tzinfo is None:
        raise ValueError("a timezone-aware run_at is required")
    due = due.astimezone(UTC)
    if due <= datetime.now(UTC):
        raise ValueError("the requested time has already passed")
    return due.isoformat()


def _legacy_current(row: Any, request: dict[str, Any]) -> bool:
    keys = ("name", "prompt", "cron", "run_at", "model", "kind", "run_in", "target_session",
            "next_run_at", "workspace")
    return all(row[key] == request.get(key) for key in keys) and row["files"] == request.get("legacy_files", request.get("files"))


def _legacy_file_paths(request: dict[str, Any]) -> list[str]:
    raw = request["files"]
    if not isinstance(raw, str):
        return []
    paths = json.loads(raw)
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ControlDenied("the original attachment list cannot be reviewed")
    return paths


STANDING_ISSUER = Principal("operator:standing-schedules", "operator")
"""Who a schedule the agent set directly is recorded as approved by: the operator, through
``scheduler.require_approval`` being off. A distinct identity rather than the operator's own, so a
schedule the agent made is told apart from one the operator made or accepted by hand — which is what
lets the agent remove its own and never the operator's."""

STANDING_RECURRING_DAYS = 3650
"""How long the standing approval of a recurring schedule lasts. The operator's own Accept grants a
month and asks again; the standing choice is the operator having already said yes, so a recurring task
the agent set must not quietly stop firing after thirty days."""

HAND_APPROVAL_DAYS = 30
"""How long the operator's own Accept of a recurring schedule lasts while ``scheduler.require_approval``
is on: the operator asked to review the agent's schedules, so the review comes round again."""


def approval_days(config: Any) -> int:
    """How far ahead an Accept pressed in the app approves a recurring schedule.

    With approval off the operator has already said yes to the agent's schedules, so a hand-pressed
    Accept, a Resume or an edit gets the standing horizon too. A thirty-day grant in that mode only
    meant a schedule that silently stopped a month later, with the app nagging about a renewal nobody
    had asked for.
    """
    return HAND_APPROVAL_DAYS if config.scheduler.require_approval else STANDING_RECURRING_DAYS


class ScheduleProposals:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.db = app.db
        self.control = ControlStore(app.db)

    async def _file_snapshot(self, paths: list[str], project_id: str, actor: str) -> list[dict[str, Any]]:
        if len(paths) > 10:
            raise ValueError("at most ten files can accompany a schedule proposal")
        roots = await self.db.fetchall("SELECT path FROM project_folders WHERE project_id=?", (project_id,))
        if paths and not roots:
            raise ControlDenied("the project has no folder for schedule attachments")
        result: list[dict[str, Any]] = []
        for name in paths:
            path, contents = read_project_file(name, [row["path"] for row in roots])
            try:
                stored = await self.app.manager.files.add(
                    contents, name=Path(path).name, origin="schedule", origin_ref=path,
                    scope=project_id, actor=actor,
                )
            except FileRefused as exc:
                raise ControlDenied("the attachment could not be kept for review") from exc
            result.append({"path": path, "name": stored.name, "handle": stored.handle,
                           "size": stored.size, "digest": stored.sha256})
        return result

    async def create(self, *, source_session_id: str, source_run_id: str | None,
                     source_command_id: str, name: str, prompt: str, cron: str | None,
                     run_at: str | None, files: list[str], model: str | None, kind: str,
                     run_in: str, target_session: str | None = None,
                     requested_time: dict[str, Any] | None = None) -> dict[str, Any]:
        """A host-identified tool call can save a suggestion, never a runnable schedule."""
        if not source_session_id or not source_command_id or len(source_command_id) > 160:
            raise ControlDenied("the host must identify the proposing session and command")
        if kind not in ("agent", "message", "lazy", "wake") or run_in not in ("new", "self"):
            raise ValueError("unsupported schedule action or run mode")
        if files and kind != "agent":
            raise ValueError("only an agent schedule can consume attached files")
        if run_in == "self" and kind != "agent":
            raise ValueError("only an agent schedule can reuse its session")
        if run_in == "self" and target_session not in (None, source_session_id):
            raise ControlDenied("a self schedule can target only its source session")
        if not name.strip() or len(name) > 160 or not prompt.strip() or len(prompt) > 4000:
            raise ValueError("a bounded name and prompt are required")
        source_actor_id = f"session:{source_session_id}"
        source_request_digest = digest({
            "name": name.strip(), "prompt": prompt.strip(), "cron": cron,
            "requested_time": requested_time if requested_time is not None else run_at,
            "files": files, "model": model, "kind": kind, "run_in": run_in,
            "target_session": target_session or source_session_id,
        })
        source_before = await self.db.fetchone("SELECT project_id FROM sessions WHERE id=?", (source_session_id,))
        if source_before is None:
            raise ControlDenied("the proposing session no longer exists")
        existing_before = await self.db.fetchone("SELECT * FROM schedule_proposals WHERE source_actor_id=?"
                                                " AND source_command_id=?", (source_actor_id, source_command_id))
        if existing_before is not None:
            if (existing_before["source_project_id"] != source_before["project_id"]
                    or existing_before["source_request_digest"] != source_request_digest
                    or existing_before["source_run_id"] != source_run_id):
                raise ControlConflict("the command identity was reused with different contents")
            saved = json.loads(existing_before["request_json"])
            return {"id": existing_before["id"], "proposal_revision": existing_before["proposal_revision"],
                    "status": existing_before["status"], "next_run_at": saved["next_run_at"],
                    "kind": kind, "run_in": run_in, "request_digest": existing_before["request_digest"]}
        pinned = await self._file_snapshot(files, source_before["project_id"], source_actor_id)
        async with self.db.transaction() as conn:
            source = await one(conn, "SELECT project_id FROM sessions WHERE id = ?", (source_session_id,))
            if source is None:
                raise ControlDenied("the proposing session no longer exists")
            if source_run_id:
                run = await one(conn, "SELECT session_id FROM runs WHERE id = ?", (source_run_id,))
                if run is None or run["session_id"] != source_session_id:
                    raise ControlDenied("the host run does not belong to this session")
            existing = await one(conn, "SELECT * FROM schedule_proposals WHERE source_actor_id = ?"
                                 " AND source_command_id = ?", (source_actor_id, source_command_id))
            if existing is not None:
                if (existing["source_project_id"] != source["project_id"]
                        or existing["source_request_digest"] != source_request_digest
                        or existing["source_run_id"] != source_run_id):
                    raise ControlConflict("the command identity was reused with different contents")
                stored = json.loads(existing["request_json"])
                return {"id": existing["id"], "proposal_revision": existing["proposal_revision"],
                        "status": existing["status"], "next_run_at": stored["next_run_at"],
                        "kind": kind, "run_in": run_in, "request_digest": existing["request_digest"]}
            due = _due(cron, run_at)
            request = {"name": name.strip(), "prompt": prompt.strip(), "cron": cron, "run_at": run_at,
                       "files": pinned, "model": model, "kind": kind,
                       "run_in": run_in, "target_session": target_session or source_session_id,
                       "next_run_at": due}
            request_digest = digest(request)
            proposal_id = "p" + uuid.uuid5(uuid.NAMESPACE_URL,
                                            canonical((source_actor_id, source_command_id))).hex[:15]
            await conn.execute(
                "INSERT INTO schedule_proposals(id,source_session_id,source_run_id,source_command_id,"
                "source_project_id,source_actor_id,request_json,source_request_digest,request_digest,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, source_session_id, source_run_id, source_command_id,
                 source["project_id"], source_actor_id, canonical(request), source_request_digest,
                 request_digest, now(), now()),
            )
            return {"id": proposal_id, "proposal_revision": 1, "status": "pending",
                    "next_run_at": due, "kind": kind, "run_in": run_in, "request_digest": request_digest}

    async def create_direct(self, **request: Any) -> dict[str, Any]:
        """Save the proposal and accept it at once under the standing approval.

        A replay of the same tool call finds its proposal already accepted and answers with the same
        schedule. An acceptance that is refused retires the proposal: a pending one left behind by a
        call that reported failure is exactly the forgotten request this path exists to avoid.

        Going through the proposal is not ceremony. Its pinned digests are what every occurrence is
        checked against when it fires (``verify_accepted_files``), so an attached file swapped in the
        file store after the schedule was set is refused rather than fed to an unattended run.
        """
        proposal = await self.create(**request)
        try:
            return await self._accept_standing(proposal["id"])
        except Exception:
            await self.db.execute("UPDATE schedule_proposals SET status='withdrawn',"
                                  "proposal_revision=proposal_revision+1,updated_at=?"
                                  " WHERE id=? AND status='pending'", (now(), proposal["id"]))
            raise

    async def _accept_standing(self, proposal_id: str) -> dict[str, Any]:
        for _ in range(3):
            row = await self.db.fetchone("SELECT * FROM schedule_proposals WHERE id=?", (proposal_id,))
            if row is None:
                raise KeyError(proposal_id)
            request = json.loads(row["request_json"])
            done = {"proposal_id": proposal_id, "kind": request["kind"], "run_in": request["run_in"]}
            if row["status"] == "accepted":
                schedule = await self.db.fetchone("SELECT next_run_at FROM schedules WHERE id=?",
                                                  (row["accepted_schedule_id"],))
                return {**done, "id": row["accepted_schedule_id"], "status": "accepted",
                        "next_run_at": schedule["next_run_at"] if schedule else request["next_run_at"]}
            if row["status"] != "pending":
                raise ControlDenied("this schedule request was withdrawn")
            scope = (("project", row["source_project_id"]) if row["source_project_id"] else ("global", "global"))
            revision = await self.db.fetchone("SELECT revision FROM domain_collection_revisions"
                                              " WHERE scope_kind=? AND scope_id=?", scope)
            if revision is None:
                raise KeyError(scope[1])
            # A one-shot needs its approval to outlive the occurrence, with a day to spare for a
            # restart that delays the tick; a recurring one gets the standing horizon.
            horizon = (timedelta(days=STANDING_RECURRING_DAYS) if request["cron"] else
                       datetime.fromisoformat(request["next_run_at"]) - datetime.now(UTC) + timedelta(days=1))
            try:
                accepted = await self.accept(
                    STANDING_ISSUER, proposal_id, request_digest=row["request_digest"],
                    expected_proposal_revision=row["proposal_revision"],
                    expires_at=(datetime.now(UTC) + horizon).isoformat(),
                    expected_collection_revision=int(revision["revision"]),
                    client_operation_id=f"standing:{proposal_id}",
                )
            except ControlConflict:
                # Another command moved the collection between the read and the write; nothing
                # was committed, so the same identity is tried again on the new revision.
                continue
            return {**done, "id": accepted["id"], "status": "accepted", "next_run_at": accepted["next_run_at"]}
        raise ControlConflict("the schedules kept changing; try again")

    async def remove_agent(self, *, source_session_id: str, source_run_id: str | None,
                           source_command_id: str, schedule_id: str) -> dict[str, Any]:
        """The session that set a schedule under the standing approval may remove it again.

        Only that session and only that kind of schedule: one the operator created, or accepted by
        hand, carries the operator's own identity and stays theirs to remove. The removal goes through
        the operator's removal command, so it withdraws the grant and any queued delivery the same way.
        """
        if not source_session_id or not source_command_id or len(source_command_id) > 160:
            raise ControlDenied("the host must identify this removal")
        earlier = await self.db.fetchone(
            "SELECT response_json FROM operation_receipts WHERE actor_id=? AND operation_kind='schedule.remove'"
            " AND client_operation_id=?", (STANDING_ISSUER.actor_id, source_command_id))
        if earlier is not None:
            response = json.loads(earlier["response_json"])
            if response.get("id") != schedule_id:
                raise ControlConflict("the command identity was reused with different contents")
            return response
        if source_run_id:
            run = await self.db.fetchone("SELECT session_id FROM runs WHERE id=?", (source_run_id,))
            if run is None or run["session_id"] != source_session_id:
                raise ControlDenied("the host run does not belong to this session")
        recurring = self.app.extensions.get("recurring")
        if recurring is None:
            raise ControlDenied("durable scheduling is unavailable")
        for _ in range(3):
            schedule = await self.db.fetchone("SELECT * FROM schedules WHERE id=?", (schedule_id,))
            if schedule is None or schedule["deleted_at"]:
                raise KeyError(schedule_id)
            origin = await self.db.fetchone("SELECT source_session_id FROM schedule_proposals"
                                            " WHERE accepted_schedule_id=? AND status='accepted'"
                                            " ORDER BY updated_at DESC,id DESC LIMIT 1", (schedule_id,))
            if (schedule["actor_id"] != STANDING_ISSUER.actor_id or origin is None
                    or origin["source_session_id"] != source_session_id):
                raise ControlDenied("only a schedule this conversation set itself can be removed here;"
                                    " the operator removes the others in Schedules")
            scope = ("project", schedule["project_id"]) if schedule["project_id"] else ("global", "global")
            revision = await self.db.fetchone("SELECT revision FROM domain_collection_revisions"
                                              " WHERE scope_kind=? AND scope_id=?", scope)
            if revision is None:
                raise KeyError(scope[1])
            try:
                return await recurring.remove(
                    STANDING_ISSUER, schedule_id, expected_collection_revision=int(revision["revision"]),
                    expected_schedule_revision=int(schedule["schedule_revision"]),
                    client_operation_id=source_command_id,
                )
            except ControlConflict:
                continue
        raise ControlConflict("the schedules kept changing; try again")

    async def card(self, item_id: str) -> dict[str, Any]:
        """What the chat card of a ScheduleCreate call shows: the schedule, its proposal, or both.

        A proposal's card needs the revision of its collection to answer Accept or Decline with the
        exact request the operator saw; an accepted one is shown as the schedule it became.
        """
        proposal: dict[str, Any] | None = None
        schedule_id = item_id
        if item_id.startswith("p"):
            row = await self.db.fetchone("SELECT * FROM schedule_proposals WHERE id=?", (item_id,))
            if row is None:
                raise KeyError(item_id)
            proposal = self._view(dict(row))
            schedule_id = row["accepted_schedule_id"] or ""
        found = await self.db.fetchone("SELECT * FROM schedules WHERE id=?", (schedule_id,)) if schedule_id else None
        if proposal is None and found is None:
            raise KeyError(item_id)
        schedule = None
        if found is not None:
            schedule = {key: found[key] for key in ("id", "name", "cron", "run_at", "next_run_at", "kind",
                                                    "run_in", "enabled", "authority_state")}
            schedule["deleted"] = bool(found["deleted_at"])
            schedule["set_by_agent"] = found["actor_id"] == STANDING_ISSUER.actor_id
        collection_revision = None
        if proposal is not None:
            scope = (("project", proposal["source_project_id"]) if proposal["source_project_id"]
                     else ("global", "global"))
            revision = await self.db.fetchone("SELECT revision FROM domain_collection_revisions"
                                              " WHERE scope_kind=? AND scope_id=?", scope)
            collection_revision = int(revision["revision"]) if revision is not None else None
        return {"schedule": schedule, "proposal": proposal, "collection_revision": collection_revision,
                "require_approval": bool(self.app.config.scheduler.require_approval),
                "approval_days": approval_days(self.app.config)}

    async def list(self, *, limit: int = 100, source_session_id: str | None = None) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM schedule_proposals WHERE (? IS NULL OR source_session_id=?)"
            " ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END,created_at DESC LIMIT ?",
            (source_session_id, source_session_id, min(max(limit, 1), 100)),
        )
        return [self._view(dict(row)) for row in rows]

    async def withdraw_agent(self, *, source_session_id: str, source_run_id: str | None,
                             source_command_id: str, proposal_id: str) -> dict[str, Any]:
        """A source session may withdraw its own still-inert suggestion once."""
        if not source_session_id or not source_command_id or len(source_command_id) > 160:
            raise ControlDenied("the host must identify this withdrawal")
        actor_id = f"session:{source_session_id}"
        payload_digest = digest({"proposal_id": proposal_id})
        async with self.db.transaction() as conn:
            source = await one(conn, "SELECT project_id FROM sessions WHERE id=?", (source_session_id,))
            if source is None:
                raise ControlDenied("the source session no longer exists")
            if source_run_id:
                run = await one(conn, "SELECT session_id FROM runs WHERE id=?", (source_run_id,))
                if run is None or run["session_id"] != source_session_id:
                    raise ControlDenied("the host run does not belong to this session")
            receipt = await one(conn, "SELECT * FROM schedule_proposal_receipts WHERE actor_id=?"
                                " AND operation_kind='withdraw' AND client_operation_id=?",
                                (actor_id, source_command_id))
            if receipt is not None:
                if receipt["request_digest"] != payload_digest or receipt["proposal_id"] != proposal_id:
                    raise ControlConflict("the command identity was reused with different contents")
                return json.loads(receipt["response_json"])
            row = await one(conn, "SELECT * FROM schedule_proposals WHERE id=?", (proposal_id,))
            if (row is None or row["source_session_id"] != source_session_id
                    or row["source_project_id"] != source["project_id"] or row["status"] != "pending"):
                raise ControlDenied("only the source may withdraw a pending proposal")
            if row["source_actor_id"].startswith("legacy:"):
                raise ControlDenied("a legacy proposal requires operator review")
            await conn.execute("UPDATE schedule_proposals SET status='withdrawn',"
                               "proposal_revision=proposal_revision+1,updated_at=? WHERE id=?", (now(), proposal_id))
            receipt_id = uuid.uuid5(uuid.NAMESPACE_URL,
                                    canonical((actor_id, "withdraw", source_command_id))).hex
            response = {"proposal_id": proposal_id, "status": "withdrawn",
                        "proposal_revision": int(row["proposal_revision"]) + 1, "receipt_id": receipt_id}
            await conn.execute("INSERT INTO schedule_proposal_receipts"
                               "(id,proposal_id,actor_id,operation_kind,client_operation_id,"
                               "request_digest,response_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                               (receipt_id, proposal_id, actor_id, "withdraw", source_command_id,
                                payload_digest, canonical(response), now()))
            return response

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        request = json.loads(row["request_json"])
        legacy_paths = _legacy_file_paths(request)
        files = request["files"] if isinstance(request["files"], list) else []
        return {"id": row["id"], "name": request["name"], "prompt": request["prompt"],
                "cron": request["cron"], "run_at": request["run_at"], "kind": request["kind"],
                "run_in": request["run_in"], "next_run_at": request["next_run_at"],
                "file_count": len(files) if files else len(legacy_paths),
                "files": [{"name": file.get("name") or Path(file["path"]).name,
                           "size": file["size"], "digest": file["digest"],
                           "pinned": bool(file.get("handle"))} for file in files],
                "legacy_file_review_required": bool(legacy_paths)
                or any(not file.get("handle") for file in files),
                "source_session_id": row["source_session_id"], "source_project_id": row["source_project_id"],
                "proposal_revision": row["proposal_revision"], "request_digest": row["request_digest"],
                "status": row["status"], "accepted_schedule_id": row["accepted_schedule_id"],
                "legacy_schedule_id": row["legacy_schedule_id"], "created_at": row["created_at"]}

    async def verify_accepted_files(self, schedule: dict[str, Any]) -> None:
        """A path's bytes can change after approval, so compare the launch copy to its pinned digest."""
        row = await self.db.fetchone("SELECT request_json FROM schedule_proposals"
                                     " WHERE accepted_schedule_id=? AND status='accepted'"
                                     " ORDER BY updated_at DESC,id DESC LIMIT 1", (schedule["id"],))
        if row is None:
            return
        expected = json.loads(row["request_json"])["files"]
        if not isinstance(expected, list):
            if _legacy_file_paths(json.loads(row["request_json"])):
                raise ControlDenied("unreviewed attachments cannot run")
            return
        actual = json.loads(schedule["files"] or "[]")
        if len(actual) != len(expected):
            raise ControlConflict("approved attachments changed")
        for handle_text, pinned in zip(actual, expected, strict=True):
            if (not isinstance(pinned, dict) or not pinned.get("handle")
                    or handle_text != pinned["handle"]):
                raise ControlConflict("approved attachments changed")
            try:
                stored = await self.app.manager.files.in_scope(handle_text, schedule["project_id"])
                if stored.size != pinned["size"] or stored.sha256 != pinned["digest"]:
                    raise ControlConflict("approved attachments changed")
                await self.app.manager.files.read(stored)
            except FileRefused as exc:
                raise ControlConflict("approved attachments are unavailable") from exc

    async def review_legacy_files(self, principal: Principal, proposal_id: str, *, request_digest: str,
                                  expected_proposal_revision: int, expected_collection_revision: int,
                                  client_operation_id: str) -> dict[str, Any]:
        """Pin today's exact bytes in a new proposal revision before old files can be approved."""
        if principal.origin_class != "operator":
            raise ControlDenied("only an operator can review legacy attachments")
        initial = await self.db.fetchone("SELECT * FROM schedule_proposals WHERE id=?", (proposal_id,))
        if initial is None:
            raise KeyError(proposal_id)
        scope = Scope("project", initial["source_project_id"]) if initial["source_project_id"] else Scope("global", "global")
        payload = {"proposal_id": proposal_id, "request_digest": request_digest,
                   "expected_proposal_revision": expected_proposal_revision}
        original = json.loads(initial["request_json"])
        paths = _legacy_file_paths(original) or [file["path"] for file in original["files"]
                                                    if isinstance(file, dict) and not file.get("handle")]
        pinned = (await self._file_snapshot(paths, initial["source_project_id"], principal.actor_id)
                  if paths and initial["proposal_revision"] == expected_proposal_revision
                  and initial["request_digest"] == request_digest else [])

        async def effect(conn: Any, _: Any) -> dict[str, Any]:
            row = await one(conn, "SELECT * FROM schedule_proposals WHERE id=?", (proposal_id,))
            if (row is None or row["status"] != "pending"
                    or row["source_project_id"] != initial["source_project_id"]
                    or row["proposal_revision"] != expected_proposal_revision
                    or row["request_digest"] != request_digest):
                raise ControlConflict("the proposal changed")
            request = json.loads(row["request_json"])
            if not pinned:
                raise ControlDenied("there are no unreviewed attachments")
            if row["legacy_schedule_id"]:
                legacy = await one(conn, "SELECT * FROM schedules WHERE id=?", (row["legacy_schedule_id"],))
                if legacy is None or not _legacy_current(legacy, request):
                    raise ControlConflict("the original pending schedule changed")
                request["legacy_files"] = request["files"]
            request["files"] = pinned
            revised_digest = digest(request)
            await conn.execute("UPDATE schedule_proposals SET request_json=?,request_digest=?,"
                               "proposal_revision=proposal_revision+1,updated_at=? WHERE id=?",
                               (canonical(request), revised_digest, now(), proposal_id))
            return {"proposal_id": proposal_id, "proposal_revision": expected_proposal_revision + 1,
                    "request_digest": revised_digest, "status": "pending"}

        async with self.db.authority_effect_lock(scope.kind, scope.id):
            return await self.control.mutate(principal, scope, "schedule.proposal.review_files",
                                             client_operation_id, expected_collection_revision,
                                             Entity("collection", scope.id), payload, effect)

    async def accept(self, principal: Principal, proposal_id: str, *, request_digest: str,
                     expected_proposal_revision: int, expires_at: str,
                     expected_collection_revision: int, client_operation_id: str) -> dict[str, Any]:
        if principal.origin_class != "operator":
            raise ControlDenied("only an operator can activate a proposal")
        initial = await self.db.fetchone("SELECT source_project_id FROM schedule_proposals WHERE id = ?",
                                         (proposal_id,))
        if initial is None:
            raise KeyError(proposal_id)
        scope = Scope("project", initial["source_project_id"]) if initial["source_project_id"] else Scope("global", "global")
        payload = {"proposal_id": proposal_id, "request_digest": request_digest,
                   "expected_proposal_revision": expected_proposal_revision, "expires_at": expires_at}
        key = (scope.kind, scope.id, principal.actor_id, "schedule.proposal.accept", client_operation_id)
        schedule_id = "s" + uuid.uuid5(uuid.NAMESPACE_URL, canonical(key)).hex[:15]
        workspace = self.app.settings.workspaces_dir / f"sched-{schedule_id}"
        made_workspace = False

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            nonlocal made_workspace
            row = await one(conn, "SELECT * FROM schedule_proposals WHERE id = ?", (proposal_id,))
            if row is None or row["source_project_id"] != initial["source_project_id"]:
                raise ControlConflict("the proposal changed scope")
            if (row["status"] != "pending" or row["proposal_revision"] != expected_proposal_revision
                    or row["request_digest"] != request_digest):
                raise ControlConflict("the proposal changed", current_revision=row["proposal_revision"])
            source = await one(conn, "SELECT project_id,metadata FROM sessions WHERE id = ?",
                               (row["source_session_id"],))
            if source is None or source["project_id"] != row["source_project_id"]:
                raise ControlDenied("the proposal source is no longer in its original scope")
            request = json.loads(row["request_json"])
            has_files = bool(_legacy_file_paths(request)) or bool(request["files"] if isinstance(request["files"], list) else [])
            if has_files and request["kind"] != "agent":
                raise ControlDenied("only an agent schedule can consume attached files")
            target_id = request["target_session"]
            target = await one(conn, "SELECT project_id,metadata FROM sessions WHERE id = ?", (target_id,))
            if target is None or target["project_id"] != row["source_project_id"]:
                raise ControlDenied("the schedule target is outside the source scope")
            if request["kind"] == "wake":
                project = await one(conn, "SELECT settings FROM projects WHERE id = ?", (scope.id,))
                office = json.loads(project["settings"]).get("orchestrator", {}) if project else {}
                if (not office.get("enabled") or office.get("session_id") != target_id
                        or json.loads(target["metadata"] or "{}").get("orchestrator_of") != scope.id):
                    raise ControlDenied("the wake target is no longer the current coordinator")
            due = _due(request["cron"], request["run_at"])
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                raise ValueError("a timezone-aware approval expiry is required")
            if not request["cron"] and expires <= datetime.fromisoformat(due):
                raise ValueError("the approval must cover the first scheduled occurrence")
            copied: list[str] = []
            if _legacy_file_paths(request):
                raise ControlDenied("review the original attachments before activation")
            for file in request["files"] if isinstance(request["files"], list) else []:
                if not file.get("handle"):
                    raise ControlDenied("review the original attachments before activation")
                roots = await conn.execute("SELECT path FROM project_folders WHERE project_id=?",
                                           (row["source_project_id"],))
                path, contents = read_project_file(file["path"], [item["path"] for item in await roots.fetchall()])
                try:
                    stored = await self.app.manager.files.in_scope_in(conn, file["handle"], row["source_project_id"])
                    await self.app.manager.files.read(stored)
                except FileRefused as exc:
                    raise ControlConflict("an attached file is unavailable") from exc
                if (path != file["path"] or len(contents) != file["size"]
                        or hashlib.sha256(contents).hexdigest() != file["digest"]
                        or stored.size != file["size"] or stored.sha256 != file["digest"]):
                    raise ControlConflict("an attached file changed after it was proposed")
                copied.append(stored.handle)
            legacy_id = row["legacy_schedule_id"]
            if legacy_id:
                legacy = await one(conn, "SELECT * FROM schedules WHERE id = ?", (legacy_id,))
                if (legacy is None or not _legacy_current(legacy, request) or legacy["grant_id"]
                        or legacy["authority_state"] != "needs_approval"):
                    raise ControlConflict("the original pending schedule changed")
                if legacy["active_session_id"] or legacy["active_run_id"]:
                    raise ControlDenied("the previous scheduled run has not finished")
                cycle = await one(conn, "SELECT c.id FROM recurring_cycles c LEFT JOIN effect_outbox e"
                                  " ON e.id=c.effect_id WHERE c.schedule_id=?"
                                  " AND (c.action_state IN ('pending','dispatching','unknown')"
                                  " OR e.state IN ('pending','claimed','unknown')) LIMIT 1", (legacy_id,))
                if cycle is not None:
                    raise ControlDenied("settle the prior occurrence before renewed approval")
                actual_id = legacy_id
            else:
                actual_id = "s" + mutation.object_id[:15]
                if actual_id != schedule_id:
                    raise ControlConflict("schedule identity changed during creation")
                if request["kind"] == "agent" and request["run_in"] == "new" and not row["source_project_id"]:
                    inbox = workspace / "inbox"
                    if not inbox.exists():
                        inbox.mkdir(parents=True)
                        made_workspace = True
            grant = await self.control.issue_grant_in(
                conn, principal, Principal(f"schedule:{actual_id}", "system"), scope,
                operations=["schedule.fire"], effects=[f"schedule.{request['kind']}"], expires_at=expires_at,
            )
            contract = _contract(request)
            if legacy_id:
                await conn.execute("UPDATE schedules SET enabled=1,authority_state='current',actor_id=?,"
                                   "grant_id=?,grant_generation=?,files=?,schedule_revision=schedule_revision+1,"
                                   "output_contract_json=?,updated_at=? WHERE id=?",
                                   (principal.actor_id, grant["grant_id"], grant["generation"],
                                    canonical(copied), canonical(contract), now(), actual_id))
            else:
                if request["kind"] == "agent" and row["source_project_id"]:
                    folder = await one(conn, "SELECT path FROM project_folders WHERE project_id=?"
                                       " ORDER BY position,id LIMIT 1", (row["source_project_id"],))
                    if folder is None:
                        raise ControlDenied("the project no longer has a runnable folder")
                    use_workspace = folder["path"]
                elif request["kind"] == "agent" and request["run_in"] == "new":
                    use_workspace = str(workspace)
                else:
                    use_workspace = ""
                await conn.execute(
                    "INSERT INTO schedules(id,name,cron,run_at,prompt,files,model,recurring,enabled,workspace,"
                    "next_run_at,created_by_session,created_at,kind,target_session,run_in,schedule_revision,project_id,"
                    "actor_id,grant_id,grant_generation,authority_state,output_contract_json,timezone,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,1,?,?,?,?, 'current',?,'UTC',?)",
                    (actual_id, request["name"], request["cron"], request["run_at"], request["prompt"],
                     canonical(copied), request["model"], int(bool(request["cron"])), use_workspace,
                     due, row["source_session_id"], now(), request["kind"], target_id, request["run_in"],
                     row["source_project_id"], principal.actor_id, grant["grant_id"], grant["generation"],
                     canonical(contract), now()),
                )
            await conn.execute("UPDATE schedule_proposals SET status='accepted',accepted_schedule_id=?,"
                               "proposal_revision=proposal_revision+1,updated_at=? WHERE id=?",
                               (actual_id, now(), proposal_id))
            return {"proposal_id": proposal_id, "id": actual_id,
                    "schedule_revision": int(legacy["schedule_revision"]) + 1 if legacy_id else 1,
                    "proposal_revision": expected_proposal_revision + 1, "authority_state": "current",
                    "next_run_at": due, "grant_expires_at": grant["expires_at"]}

        try:
            async with self.db.authority_effect_lock(scope.kind, scope.id):
                return await self.control.mutate(principal, scope, "schedule.proposal.accept",
                                                 client_operation_id, expected_collection_revision,
                                                 Entity("collection", scope.id), payload, effect)
        except Exception:
            if made_workspace:
                shutil.rmtree(workspace)
            raise

    async def withdraw(self, principal: Principal, proposal_id: str, *, request_digest: str,
                       expected_proposal_revision: int, expected_collection_revision: int,
                       client_operation_id: str) -> dict[str, Any]:
        if principal.origin_class != "operator":
            raise ControlDenied("only an operator can withdraw a pending proposal")
        initial = await self.db.fetchone("SELECT source_project_id FROM schedule_proposals WHERE id = ?",
                                         (proposal_id,))
        if initial is None:
            raise KeyError(proposal_id)
        scope = Scope("project", initial["source_project_id"]) if initial["source_project_id"] else Scope("global", "global")
        payload = {"proposal_id": proposal_id, "request_digest": request_digest,
                   "expected_proposal_revision": expected_proposal_revision}

        async def effect(conn: Any, _: Any) -> dict[str, Any]:
            row = await one(conn, "SELECT * FROM schedule_proposals WHERE id = ?", (proposal_id,))
            if (row is None or row["status"] != "pending" or row["request_digest"] != request_digest
                    or row["proposal_revision"] != expected_proposal_revision
                    or row["source_project_id"] != initial["source_project_id"]):
                raise ControlConflict("the proposal changed", current_revision=row["proposal_revision"] if row else None)
            await conn.execute("UPDATE schedule_proposals SET status='withdrawn',"
                               "proposal_revision=proposal_revision+1,updated_at=? WHERE id=?",
                               (now(), proposal_id))
            return {"proposal_id": proposal_id, "proposal_revision": expected_proposal_revision + 1,
                    "status": "withdrawn"}

        return await self.control.mutate(principal, scope, "schedule.proposal.withdraw",
                                         client_operation_id, expected_collection_revision,
                                         Entity("collection", scope.id), payload, effect)
