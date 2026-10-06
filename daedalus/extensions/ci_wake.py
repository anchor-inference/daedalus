"""Tell a task's worker, or else its coordinator, when required CI fails on the branch head."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING, Any

from daedalus.extensions.ci_observations import ci_readiness
from daedalus.stores.staff import StaffError

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

CLOSED = ("done", "archived", "dropped")


def ci_link(event: str, payload: Any) -> str:
    """The page a person opens to read the failure: GitHub names it differently per event."""
    if not isinstance(payload, dict):
        return ""
    run = payload if event == "status" else payload.get(event)
    if not isinstance(run, dict):
        return ""
    for key in ("html_url", "details_url", "target_url"):
        value = run.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value[:500]
    return ""


def failure_key(task_id: str, head: str, check: str, run_id: str, attempt: int) -> str:
    """One notice per task, head, check and run attempt; 32 hex characters, the host message identity."""
    return hashlib.sha256(f"{task_id}\n{head}\n{check}\n{run_id}\n{attempt}".encode()).hexdigest()[:32]


class CiFailures:
    """Wakes whoever can act on a failed required check of a task's current branch head.

    The webhook already deduplicates a delivery; GitHub still sends one failure several times under
    different delivery identities (a check run, then its suite, then a redelivery the operator asks
    for), so the notice is deduplicated again on what it is about. The record of that is the notice
    itself: the staff message under a derived identity, or the coordinator's event carrying the key.
    """

    def __init__(self, app: Application) -> None:
        self.app = app
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    def spawn(self, observation: dict[str, Any], *, link: str = "") -> None:
        """Run :meth:`observed` behind the webhook's answer: git and a worker's runtime can be slower
        than the ten seconds GitHub waits before it counts the delivery as failed and repeats it."""
        task = asyncio.create_task(self._guarded(observation, link))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, observation: dict[str, Any], link: str) -> None:
        try:
            await self.observed(observation, link=link)
        except Exception:  # noqa: BLE001 — the observation is stored; a lost notice must not crash the host
            logger.warning("could not pass on a CI failure", exc_info=True)

    async def observed(self, observation: dict[str, Any], *, link: str = "") -> list[dict[str, Any]]:
        """Notify for each open task that requires this check and whose branch head is this head."""
        if observation.get("state") != "final" or observation.get("conclusion") != "failed":
            return []
        rows = await self.app.db.fetchall(
            "SELECT t.id,t.title,t.project_id,t.contract_revision FROM ci_required_checks r"
            " JOIN board_tasks t ON t.id = r.task_id AND t.contract_revision = r.contract_revision"
            " WHERE r.provider = ? AND r.repository_id = ? AND r.check_name = ?"
            f" AND t.status NOT IN ({','.join('?' * len(CLOSED))})",
            (observation["provider"], observation["repository_id"], observation["check_name"], *CLOSED),
        )
        out = []
        for task in rows:
            sent = await self._one(dict(task), observation, link)
            if sent is not None:
                out.append(sent)
        return out

    async def _head(self, task_id: str) -> str | None:
        review = getattr(self.app.extensions.get("staff"), "review", None)
        if review is None:
            return None
        try:
            return await review.branch_head(task_id)
        except Exception:  # noqa: BLE001 — no branch, no folder or git refused: not this head, then
            return None

    async def _one(self, task: dict[str, Any], observation: dict[str, Any], link: str) -> dict[str, Any] | None:
        head = observation["head_sha"]
        if await self._head(task["id"]) != head:
            # A failure on an older head is history: the worker has already moved past it.
            return None
        async with self.app.db.transaction() as conn:
            readiness = await ci_readiness(conn, task["id"], task["contract_revision"], head)
        failed = [check["check_name"] for check in readiness["checks"] if check["state"] == "failed"]
        if observation["check_name"] not in failed:
            # A late failure of an older run, after a newer run of the same check passed.
            return None
        key = failure_key(task["id"], head, observation["check_name"], observation["run_id"],
                          int(observation["run_attempt"]))
        async with self._lock:
            if await self._noticed(key):
                return None
            text = self._text(task, observation, failed, link)
            told = await self._tell_worker(task["id"], key, text)
            if told is not None:
                return {"task_id": task["id"], "to": "worker", "key": key, **told}
            await self._wake_coordinator(task, observation, failed, link, key)
            return {"task_id": task["id"], "to": "coordinator", "key": key}

    async def _noticed(self, key: str) -> bool:
        message = await self.app.db.fetchone("SELECT 1 FROM staff_messages WHERE id = ?", (f"sm-{key}",))
        if message is not None:
            return True
        event = await self.app.db.fetchone(
            "SELECT 1 FROM app_events WHERE type = 'task.ci_failed'"
            " AND json_extract(payload_json,'$.ci_key') = ? LIMIT 1", (key,))
        return event is not None

    @staticmethod
    def _text(task: dict[str, Any], observation: dict[str, Any], failed: list[str], link: str) -> str:
        attempt = int(observation["run_attempt"])
        lines = [f"Required CI failed on task {task['id']} at branch head {observation['head_sha'][:12]}: "
                 f"{observation['check_name']} (run {observation['run_id']}"
                 + (f", attempt {attempt}" if attempt > 1 else "") + ")."]
        others = [name for name in failed if name != observation["check_name"]]
        if others:
            lines.append(f"Also failed at this head: {', '.join(others)}.")
        if link:
            lines.append(f"Details: {link}")
        lines.append("Read the failure, fix it on the same branch and commit; the new head is checked again.")
        return "\n".join(lines)

    async def _tell_worker(self, task_id: str, key: str, text: str) -> dict[str, Any] | None:
        team = self.app.extensions.get("staff")
        if team is None:
            return None
        row = await self.app.db.fetchone("SELECT id FROM staff_sessions WHERE task_id = ? AND ended_at IS NULL"
                                         " ORDER BY started_at DESC LIMIT 1", (task_id,))
        live = await team.live(row["id"]) if row is not None else None
        if live is None:
            return None
        try:
            # The message travels as the coordinator's: only the operator's words are framed as a
            # person's, and this notice is the board speaking, which is the coordinator's side.
            receipt = await team.tell(live.staff, text, when="now", by="orchestrator", message_id=f"sm-{key}")
        except StaffError:
            logger.info("CI failure of task %s could not reach its worker", task_id, exc_info=True)
            return None
        if receipt.get("state") == "failed":
            return None
        return {"staff_id": live.staff.id, "message_id": receipt["message_id"], "state": receipt["state"]}

    async def _wake_coordinator(self, task: dict[str, Any], observation: dict[str, Any], failed: list[str],
                                link: str, key: str) -> None:
        await self.app.manager.bus.publish(
            "task.ci_failed",
            {"task_id": task["id"], "title": task["title"] or "", "actor": "system",
             "contract_revision": int(task["contract_revision"]),
             "head_sha": observation["head_sha"], "check_name": observation["check_name"],
             "failed_checks": failed, "run_id": observation["run_id"],
             "run_attempt": int(observation["run_attempt"]), "link": link, "ci_key": key},
            project_id=task["project_id"],
        )


__all__ = ["CiFailures", "ci_link", "failure_key"]
