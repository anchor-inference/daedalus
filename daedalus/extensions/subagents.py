"""Subagents: helper sessions a leader spawns into its own workspace.

A subagent is a session created for one task. It shares the leader's workspace (files it
writes are the leader's files), runs on the leader's model unless another preset is named,
and reports back when its run ends: the report is delivered to the leader as a message
from ``subagent:<name>`` — queued as a follow-up while the leader is still running, or
starting a new leader turn when the leader has already finished. A leader may therefore
hand off work and end its turn; the result arrives on its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from protocore.contracts.types import Message, MessageRole, TextBlock, ToolUseBlock

from daedalus.config import REASONING_EFFORTS
from daedalus.host.prompts import split_headline
from daedalus.host.session_runner import BUDGET_WAY_OUT, PROVIDER_OUTAGE_KINDS
from daedalus.tools._common import idle_words

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

POLL_SECONDS = 3.0
SETTLE_WAIT_SECONDS = 60
"""How long a reminder waits for its leader's finished run to be written down before it goes anyway."""
TASK_KEPT_CHARS = 400
TITLE_CHARS = 160
REMINDER_ANSWER_CHARS = 200
NO_ANSWER = "(the subagent's run ended without a final reply; see its session)"
BUDGET_POLL_SECONDS = 30.0
"""How often the budget watch looks whether a held report can go and a parked helper can go on.
The supervisor itself rereads the day's spend once a minute, so a shorter wait would find nothing new."""
PROGRESS_TEXT_CHARS = 1500
PROGRESS_FILES = 8
WRITING_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})

CAP_KINDS = frozenset({"spending_cap", "run_cap", "run_tokens", "run_minutes", "session_cap", "provider_cap", "total_cap"})
"""The error kinds a host cap ends a run with: the work did not fail, a limit stopped it."""

PARKED_KEY = "subagent_parked"
"""On a helper the daily cap stopped: why and when, until it is driven on. The panel draws it as parked."""
HELD_KEY = "subagent_reports_held"
"""On a helper whose report its leader could not take: the reports, in order, until each is delivered."""
NOTICE_KEY = "daedalus.subagent_notice"
"""On the line in the leader's chat that says a helper is parked or its report waits, for the app to draw."""

CONTINUE_NOTE = (
    "[the daily spending cap that stopped your previous run is open again: continue the task from where it "
    "stopped — your files and history are as you left them — and end with your complete final reply]"
)


EFFORTS = ("off", *REASONING_EFFORTS)
"""What a leader may give a subagent: ``off`` turns thinking off, the rest are the session efforts."""


def check_effort(effort: str | None) -> str | None:
    """The effort a leader asked for, normalised; ``None`` keeps what the subagent would have had."""
    if effort is None or not effort.strip():
        return None
    effort = effort.strip().lower()
    if effort not in EFFORTS:
        raise ValueError(f"unknown effort {effort!r}; choose one of: {', '.join(EFFORTS)}")
    return effort


async def set_effort(manager: Any, session_id: str, effort: str) -> None:
    """Set a subagent's reasoning effort on top of its model, whichever way that model was chosen.
    An effort without thinking means nothing to the model, so any effort turns thinking back on."""
    if effort == "off":
        await manager.set_model(session_id, thinking=False)
    else:
        await manager.set_model(session_id, thinking=True, reasoning_effort=effort)


def unfinished_jobs(manager: Any, session_id: str) -> int:
    """Background jobs of the session still running, or ended and not yet told to it.

    A subagent that ends its turn on "the suite is running" has not finished: the job's end wakes it
    with a new run. Reporting and removing it at that turn's end delivered a half-done report and,
    because deleting a session ends its jobs, killed the very job it was waiting for. A service is
    left running on purpose and never ends, so it does not hold the report back.
    """
    jobs = getattr(manager, "jobs", {}).get(session_id, {})
    return sum(
        1
        for job in jobs.values()
        if getattr(job, "kind", "job") != "service" and (getattr(job, "running", False) or not getattr(job, "reported", True))
    )


def no_answer(state: Any) -> str:
    """The missing reply, with the error that ended the run when the host recorded one."""
    kind = str(getattr(state, "last_error_kind", "") or "")
    return f"(the subagent's run ended without a final reply: {kind}; see its session)" if kind else NO_ANSWER


def stop_of(state: Any, status: str, *, retry_limit: int) -> tuple[str, str]:
    """Why a helper's run ended, as ``(cause, detail)``.

    ``cause`` is ``completed``; ``budget`` (the daily cap: it is parked and goes on by itself when the
    cap reopens); ``outage`` (the provider failed and the host drives the run again after a wait);
    ``cap`` (another cap, which someone has to raise); ``cancelled``; or ``failed``. A cap is read off
    the error kind before the status, because the per-run caps stop the loop cooperatively and the run
    then ends as cancelled, which alone would read as somebody pressing Stop.
    """
    kind = str(getattr(state, "last_error_kind", "") or "")
    detail = " ".join(str(getattr(state, "last_error_message", "") or "").split())[:400]
    if kind == "spending_cap" and "daily" in detail.lower():
        return "budget", detail
    if kind in CAP_KINDS:
        return "cap", detail
    if status == "completed":
        return "completed", ""
    if status == "cancelled":
        return "cancelled", detail
    if kind in PROVIDER_OUTAGE_KINDS and not getattr(state, "last_error_permanent", False) and int(getattr(state, "outage_streak", 0) or 0) < retry_limit:
        return "outage", detail
    return "failed", detail or kind


def stop_header(name: str, cause: str, detail: str) -> str:
    """The first line of a report from a helper that ended without its own: what happened and what comes next."""
    keep = f"It is kept: SubAgentSend({name!r}, …) continues it, SubAgentDelete({name!r}) drops it."
    if cause == "budget":
        return (
            f"[subagent {name!r} paused by the daily spending cap; it is parked, not removed. It goes on by itself once "
            f"{BUDGET_WAY_OUT}, and its final report comes then, once. Meanwhile you can wait for it, carry on without it "
            f"(SubAgentDelete({name!r}) drops it), or tell the operator.]"
        )
    if cause == "outage":
        return f"[subagent {name!r} stopped: the model provider failed ({detail or 'no detail'}). The host drives it again after a wait, and its final report comes when it finishes. SubAgentDelete({name!r}) drops it.]"
    if cause == "cap":
        return f"[subagent {name!r} stopped by a cap before its report: {detail or 'a spending cap'}. {keep}]"
    if cause == "cancelled":
        return f"[subagent {name!r} was stopped before it finished; it wrote no report. {keep}]"
    return f"[subagent {name!r} failed before its report: {detail or 'no detail recorded'}. {keep}]"

TASK_HEADER = (
    "[task from your leader session {leader} via SubAgent — you work in the leader's workspace; "
    "do the task, then put the complete result in your final reply: it is delivered to the leader "
    "verbatim, and the leader sees nothing else you wrote]\n\n"
)

BRIEF = (
    "You are a subagent of session {leader}: a helper started for one task. Work in the shared workspace "
    "(the leader reads the files you leave there), do not ask the operator questions, do not start "
    "subagents of your own, and end with a final reply that contains everything the leader needs. "
    "A background job you start is part of the task: wait for it (JobWait) before your final reply — "
    "while one of yours is still running, your turn ending is not your report. "
    "When you are done, blocked, or have a finding the leader should act on before you finish, say so with "
    "ReportToLeader(text) — it reaches the leader at once; your final reply still carries the full report."
)

WITHHELD = (
    "\n\nYour leader withheld these tools from this session: {names}. They are not missing by accident and "
    "there is no way around them. If the task cannot be finished without one, say so in your report, name the "
    "tool and say the leader withheld it — do not improvise a substitute."
)

IDLE_BRIEF = (
    "You are a subagent of session {leader}: a standing helper. You were started without a task and wait "
    "for the leader's messages; each one arrives via SubAgentSend and is a job to do. Work in the shared "
    "workspace (the leader reads the files you leave there), do not ask the operator questions, do not "
    "start subagents of your own, and end every run with a final reply that contains everything the "
    "leader needs — it is delivered to the leader verbatim. When you are blocked, or have a finding the "
    "leader should act on before you finish, say so with ReportToLeader(text) — it reaches the leader at "
    "once; your final reply still carries the full report."
)

REPORT_HEADER = "[message from subagent {name!r} via ReportToLeader — it is still working; its final report comes when it finishes]\n\n"

REMINDER_TAIL = (
    "Delete the ones you no longer need with SubAgentDelete(name). Any you leave are taken as kept on purpose: "
    "you will not be asked about them again. Answer in one short line."
)

FOLLOW_UP_HEADER = "[message from your leader session {leader} via SubAgentSend — act on it; your final reply is delivered to the leader verbatim]\n\n"


def contract_text(expects: str | None, deliverable: str | None) -> str:
    """What the leader asked for, spelled out to the subagent under its task."""
    lines = []
    if expects and expects.strip():
        lines.append(f"Your final reply must contain: {expects.strip()}")
    if deliverable and deliverable.strip():
        lines.append(f"The file {deliverable.strip()} (relative to the workspace) must exist when you finish; the host checks it.")
    return ("\n\n[contract]\n" + "\n".join(lines)) if lines else ""


def contract_verdict(state: Any, answer: str) -> str:
    """The report with the host's own check of the contract on top: a missing deliverable is a fact, not a claim."""
    deliverable = str(state.metadata.get("subagent_deliverable") or "")
    expects = str(state.metadata.get("subagent_expects") or "")
    notes = []
    if deliverable:
        root = Path(state.workspace).resolve()
        target = (root / deliverable).resolve()
        if not target.is_relative_to(root):
            notes.append(f"deliverable {deliverable}: refused (outside the workspace)")
        else:
            notes.append(f"deliverable {deliverable}: present ({target.stat().st_size} bytes)" if target.is_file() else f"deliverable {deliverable}: MISSING")
    if expects:
        wanted = [part.strip() for part in re.split(r"[;,\n]", expects) if part.strip()]
        lowered = answer.lower()
        missing = [w for w in wanted if len(w.split()) <= 6 and w.lower() not in lowered]
        checkable = [w for w in wanted if len(w.split()) <= 6]
        if checkable:
            notes.append(f"expected items named in the report: {len(checkable) - len(missing)}/{len(checkable)}" + (f"; not found: {', '.join(missing)}" if missing else ""))
        else:
            notes.append(f"expected in the report (not machine-checked): {expects}")
    return ("[contract] " + "; ".join(notes) + "\n\n" + answer) if notes else answer



def _timestamp(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def status_of(state: Any) -> str:
    """The session's state in the word the session detail uses, compaction first.

    Compaction between runs runs on a task of its own, and a helper reported as running while it
    summarises reads as a run nobody started — the same order ``get_session`` keeps.
    """
    if state.compacting:
        return "compacting"
    if state.running:
        return "running"
    if state.pending is not None:
        return "waiting"
    return "failed" if state.last_error_kind else "idle"


def run_started_at(state: Any) -> str | None:
    """When the current run began driving the model, as wall-clock ISO time; ``None`` between runs.

    The session keeps only a monotonic reading (the time cap counts from it), so it is carried over to
    the wall clock here. A run resumed after a question counts from the resume, as the cap does.
    """
    if not state.run_active_since or not (state.running or state.pending is not None):
        return None
    began = time.time() - max(0.0, time.monotonic() - state.run_active_since)
    return datetime.fromtimestamp(began, UTC).isoformat()


class Subagents:
    def __init__(self, app: Application) -> None:
        self.app = app
        self._waited: set[str] = set()
        self._removals: set[asyncio.Task[None]] = set()
        self._removing: set[str] = set()
        """Subagents a removal was scheduled for: finished and on their way out, so never "lingering"."""
        self._reminders: set[asyncio.Task[None]] = set()
        self._watching = True
        """Whether some helper may hold a report or be parked, so the budget watch has to look. True at
        start, because both are stored on the helper and outlive a restart; set again whenever one is."""

    async def models(self) -> list[str]:
        """Preset ids a subagent can run on: those whose provider is configured."""
        manager = self.app.manager
        assert manager is not None
        available = set(manager.providers.available())
        return [pid for pid, preset in self.app.config.presets.items() if preset.provider in available and preset.model]

    async def spawn(
        self,
        *,
        leader_id: str,
        task: str | None = None,
        model: str | None = None,
        name: str | None = None,
        wait: bool = False,
        timeout_minutes: int | None = None,
        keep: bool = False,
        expects: str | None = None,
        deliverable: str | None = None,
        persona: str | None = None,
        tools_off: list[str] | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        manager = self.app.manager
        assert manager is not None
        leader = await manager.get_state(leader_id)
        if leader is None:
            raise ValueError("unknown leader session")
        task = (task or "").strip()
        idle = not task
        if idle and not (name or "").strip():
            raise ValueError("a subagent started without a task needs a name, so you can address it with SubAgentSend")
        persona_text = ""
        if persona:
            persona_text = self.persona(persona)
            if persona_text is None:
                raise ValueError(f"unknown persona {persona!r}; available: {', '.join(self.personas()) or 'none'}")
        if leader.metadata.get("subagent_of"):
            # SubAgent is withheld from subagents (SUBAGENT_BLOCKED_TOOLS); this refuses any other way in.
            raise ValueError("a subagent cannot start subagents of its own; do this yourself or say in your report that it needs another agent")
        active = [s for s in await self.children(leader_id) if s["running"]]
        if len(active) >= self.app.config.subagents.max_active:
            # Where the limit lives, so the agent can tell the operator what to change instead of
            # guessing: the operator once looked for it in Settings and could not find it.
            raise RuntimeError(
                f"{len(active)} subagents are already running for this session (limit {self.app.config.subagents.max_active}, "
                "set in Settings → Limits → Subagents at once, config subagents.max_active); wait for their reports"
            )
        effort = check_effort(effort)
        models = await self.models()
        if model is not None:
            model = model.strip()
            if model not in models:
                raise ValueError(f"unknown model {model!r}; choose one of: {', '.join(models)}")
        # Withholding runs down the tree, not just one level: a subagent that keeps SubAgent would
        # otherwise hand a grandchild the very tool its leader took away from it, and tools_off would
        # be a request rather than the enforcement it is sold as. Names the leader carries are already
        # known to the registry, so only the new ones are checked.
        withheld = sorted(set(self.withhold(tools_off)) | {str(n) for n in (leader.metadata.get("tools_off") or ())})
        label = (name or task.splitlines()[0])[:48].strip()  # type: ignore[union-attr]
        taken = {c["name"] for c in await self.children(leader_id)}
        if label in taken:
            # A name is how the leader addresses the subagent later; two of one name would be ambiguous.
            n = 2
            while f"{label}-{n}" in taken:
                n += 1
            label = f"{label}-{n}"
        metadata: dict[str, Any] = {
            "subagent_of": leader_id,
            "subagent_name": label,
            "subagent_keep": bool(keep) or idle,
            "subagent_expects": (expects or "").strip(),
            "subagent_deliverable": (deliverable or "").strip(),
            # What the panel titles the helper with; the first part is enough, the task itself is in its transcript.
            "subagent_task": task[:TASK_KEPT_CHARS],
            "subagent_idle": idle,
            "brief": (IDLE_BRIEF if idle else BRIEF).format(leader=leader_id)
            + (WITHHELD.format(names=", ".join(withheld)) if withheld else "")
            + (f"\n\n[persona: {persona}]\n{persona_text}" if idle and persona_text else ""),
            "unattended": True,
        }
        if withheld:
            metadata["tools_off"] = withheld
        if leader.metadata.get("mcp") is not None:
            metadata["mcp"] = leader.metadata["mcp"]
        # A subagent works beside its leader, which in a project means inside the project: the same
        # folder and the same wall. The project is passed rather than the directory, because the
        # directory alone would give the child the leader's files with none of the containment —
        # and moving the project would then move the leader and leave the child behind.
        if leader.project is None:
            raise RuntimeError("the leader has no project")
        child = await manager.create_session(f"[sub] {label}", metadata=metadata, project_id=leader.project.id, folder_id=leader.metadata.get("folder_id") or None)
        cid = child.session.id
        if model is not None:
            await manager.set_model(cid, preset=model)
        else:
            overrides = await manager.live.load(leader_id)
            # The effort the operator picked in the leader's composer is part of "the leader's model":
            # it used to be carried only along with a model override, so a leader on the default model
            # set to xhigh started helpers on the preset's effort.
            if overrides.get("preset") or (overrides.get("provider") and overrides.get("model_name")) or overrides.get("thinking_enabled") is not None or overrides.get("reasoning_effort"):
                await manager.live.set_model(
                    cid,
                    model_name=overrides.get("model_name"),
                    provider=overrides.get("provider"),
                    preset=overrides.get("preset"),
                    thinking_enabled=overrides.get("thinking_enabled"),
                    reasoning_effort=overrides.get("reasoning_effort"),
                )
        if effort is not None:
            await set_effort(manager, cid, effort)
        opening = (f"[persona: {persona}]\n{persona_text}\n\n" if persona_text else "")
        if idle:
            # No run at all: the session stands by with its brief until the leader sends it work.
            on = model or "the leader's model"
            return {
                "session_id": cid,
                "run_id": None,
                "name": label,
                "model": model or "(leader's model)",
                "answer": None,
                "kept": True,
                "idle": True,
                "note": f"subagent {label!r} is up and idle on {on} (session {cid}); give it work with SubAgentSend({label!r}, …)",
            }
        if wait:
            self._waited.add(cid)
        run_id = await manager.submit(cid, TASK_HEADER.format(leader=leader_id) + opening + task + contract_text(expects, deliverable), as_answer=False, origin=f"subagent-task:{leader_id}")
        result: dict[str, Any] = {"session_id": cid, "run_id": run_id, "name": label, "model": model or "(leader's model)", "effort": effort, "answer": None, "kept": bool(keep)}
        if not wait:
            return result
        return await self._collect(cid, result, timeout_minutes)

    def withhold(self, names: list[str] | None) -> list[str]:
        """The tools the leader takes away from this subagent, checked against the registry.

        Negative authorization: a helper inherits the leader's toolbox and uses what it finds, so a
        launch that must not happen is removed rather than discouraged. An unknown name is refused
        instead of ignored — a typo that silently withholds nothing would read as enforcement.
        """
        wanted = sorted({str(n).strip() for n in (names or []) if str(n).strip()})
        if not wanted:
            return []
        manager = self.app.manager
        assert manager is not None
        known = {t.name for t in manager.tools.list_all()}
        unknown = [n for n in wanted if n not in known]
        if unknown:
            raise ValueError(f"no such tool(s) to withhold: {', '.join(unknown)}")
        return wanted

    def personas(self) -> list[str]:
        """The named stances a subagent can take: one markdown file each under ``personas/`` in the host repository."""
        directory = self.app.settings.bot_repo_dir / "personas"
        return sorted(p.stem for p in directory.glob("*.md")) if directory.is_dir() else []

    def persona(self, name: str) -> str | None:
        name = name.strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", name):
            return None
        path = self.app.settings.bot_repo_dir / "personas" / f"{name}.md"
        try:
            return path.read_text(encoding="utf-8").strip() if path.is_file() else None
        except OSError:
            return None

    async def _collect(self, cid: str, result: dict[str, Any], timeout_minutes: int | None) -> dict[str, Any]:
        """Wait for the subagent's run to end and put its reply into ``result``; remove it afterwards unless kept."""
        manager = self.app.manager
        assert manager is not None
        deadline = asyncio.get_running_loop().time() + 60 * (timeout_minutes or self.app.config.subagents.wait_timeout_minutes)
        try:
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(POLL_SECONDS)
                state = await manager.get_state(cid)
                if state is None:
                    raise RuntimeError("the subagent session disappeared")
                if not state.running and state.pending is None and not unfinished_jobs(manager, cid):
                    cause, detail = stop_of(state, "failed" if state.last_error_kind else "completed", retry_limit=self.app.config.ops.provider_retry_max_attempts)
                    if cause != "completed":
                        # A helper the cap or the provider stopped is not done, and removing it lost the
                        # work it had under way: it stays, and the leader is told what it had done.
                        name = str(state.metadata.get("subagent_name") or cid)
                        result["answer"] = stop_header(name, cause, detail) + "\n\n" + contract_verdict(state, await self.progress(cid))
                        if cause == "budget":
                            await self._park(cid, run_id=str(state.run_id or result.get("run_id") or ""), detail=detail, mode="stopped")
                        return result
                    result["answer"] = contract_verdict(state, await self.answer(cid) or no_answer(state))
                    if not state.metadata.get("subagent_keep"):
                        self._remove_later(cid)
                    return result
            result["note"] = "the subagent did not finish in time; its report will arrive as a message when it does"
        finally:
            self._waited.discard(cid)  # on a timeout the async report takes over
        return result

    async def send(self, *, leader_id: str, name: str, text: str, wait: bool = False, timeout_minutes: int | None = None, effort: str | None = None) -> dict[str, Any]:
        """A message to one of the leader's subagents: a steer while it works, a new task once it has finished.
        ``effort`` re-tunes the subagent for this and later runs; a running one picks it up at its next request."""
        manager = self.app.manager
        assert manager is not None
        text = text.strip()
        if not text:
            raise ValueError("the message is empty")
        effort = check_effort(effort)
        match = await self._match(leader_id, name)
        cid = str(match["session_id"])
        state = await manager.get_state(cid)
        if state is None:
            raise ValueError(f"subagent {name!r} no longer exists")
        if state.pending is not None:
            raise RuntimeError(f"subagent {name!r} is waiting on a question; it cannot take a message now")
        if effort is not None:
            await set_effort(manager, cid, effort)
        if state.metadata.get(PARKED_KEY) and not state.running:
            # The leader continues it by hand: the budget watch must not start a second run on top.
            await self._set_meta(cid, {PARKED_KEY: None})
        if state.running:
            await manager.submit(cid, FOLLOW_UP_HEADER.format(leader=leader_id) + text, steer=True, as_answer=False, origin=f"subagent-task:{leader_id}")
            return {"session_id": cid, "name": match["name"], "delivered": "steer", "answer": None}
        if wait:
            self._waited.add(cid)
        run_id = await manager.submit(cid, FOLLOW_UP_HEADER.format(leader=leader_id) + text, as_answer=False, origin=f"subagent-task:{leader_id}")
        result: dict[str, Any] = {"session_id": cid, "name": match["name"], "delivered": "run", "run_id": run_id, "answer": None, "kept": bool(state.metadata.get("subagent_keep"))}
        if not wait:
            return result
        return await self._collect(cid, result, timeout_minutes)

    def _remove_later(self, cid: str) -> None:
        """Delete a finished subagent once its run has fully settled; its files stay, the workspace is the leader's."""
        manager = self.app.manager
        assert manager is not None
        self._removing.add(cid)

        async def _remove() -> None:
            for _ in range(120):
                state = await manager.get_state(cid)
                if state is None:
                    return
                if not state.running:
                    break
                await asyncio.sleep(1)
            try:
                await manager.delete_session(cid, delete_workspace=False)
            except Exception:  # noqa: BLE001
                logger.exception("could not remove finished subagent %s", cid)

        task = asyncio.create_task(_remove(), name=f"subagent-remove:{cid}")
        self._removals.add(task)
        task.add_done_callback(self._removals.discard)
        task.add_done_callback(lambda _: self._removing.discard(cid))

    async def _child_rows(self, leader_id: str) -> list[dict[str, Any]]:
        manager = self.app.manager
        assert manager is not None
        return [row for row in await manager.list_sessions(limit=500) if row.get("metadata", {}).get("subagent_of") == leader_id]

    async def children(self, leader_id: str) -> list[dict[str, Any]]:
        manager = self.app.manager
        assert manager is not None
        out = []
        for row in await self._child_rows(leader_id):
            state = await manager.get_state(row["id"])
            out.append({"session_id": row["id"], "name": row["metadata"].get("subagent_name"), "running": bool(state and state.running), "status": row["status"], "kept": bool(row["metadata"].get("subagent_keep"))})
        return out

    async def views(self, leader_id: str, *, model_label: Callable[[Any], Awaitable[str]] | None = None) -> list[dict[str, Any]]:
        """The leader's subagents as the app's Subagents panel draws them, the active ones first.

        ``model_label`` names the model a session answers on; the API passes the label its session
        detail uses, so the panel and the header never disagree. Without it the model is left empty.
        """
        manager = self.app.manager
        assert manager is not None
        out = []
        for row in await self._child_rows(leader_id):
            meta = row["metadata"]
            state = await manager.get_state(row["id"])
            task = str(meta.get("subagent_task") or "").strip()
            status = status_of(state) if state is not None else row["status"]
            parked = meta.get(PARKED_KEY) if isinstance(meta.get(PARKED_KEY), dict) else None
            if parked and status in ("idle", "failed"):
                # Not a failure and not done: the cap stopped it and the host drives it on when it reopens.
                status = "parked"
            out.append({
                "session_id": row["id"],
                "name": meta.get("subagent_name"),
                "title": (task.splitlines()[0] if task else "")[:TITLE_CHARS],
                "status": status,
                "parked": {"reason": str(parked.get("reason") or "budget"), "at": str(parked.get("at") or "")} if parked else None,
                "reports_held": len(meta.get(HELD_KEY) or ()),
                "running": bool(state and state.running),
                "model": await model_label(state) if model_label is not None and state is not None else "",
                "kept": bool(meta.get("subagent_keep")),
                "idle_start": bool(meta.get("subagent_idle")),
                "created_at": row["created_at"],
                "last_activity_at": row["last_message_at"],
                "run_started_at": run_started_at(state) if state is not None else None,
            })
        out.sort(key=lambda view: view["last_activity_at"] or "", reverse=True)
        out.sort(key=lambda view: view["status"] not in ("running", "waiting", "parked"))
        return out

    async def _match(self, leader_id: str, name: str) -> dict[str, Any]:
        """One of the leader's subagents by name or session id; the error lists the ones it has."""
        children = await self.children(leader_id)
        match = next((c for c in children if c["name"] == name.strip() or c["session_id"] == name.strip()), None)
        if match is None:
            known = ", ".join(str(c["name"]) for c in children) or "none"
            raise ValueError(f"no subagent named {name!r}; yours: {known}. A subagent started without keep=true is removed when it finishes.")
        return match

    async def delete(self, *, leader_id: str, name: str) -> dict[str, Any]:
        """Remove one of the leader's subagents; its files stay, the workspace is the leader's.

        ``delete_session`` stops a run in flight itself, so a working helper is not refused: the leader
        that deletes it has decided it is not needed, and making it wait for the end would only cost.
        """
        manager = self.app.manager
        assert manager is not None
        match = await self._match(leader_id, name)
        cid = str(match["session_id"])
        self._waited.discard(cid)
        self._removing.add(cid)
        try:
            deleted = await manager.delete_session(cid, delete_workspace=False)
        finally:
            self._removing.discard(cid)
        if not deleted:
            raise ValueError(f"subagent {name!r} no longer exists")
        return {"session_id": cid, "name": match["name"], "was_running": bool(match["running"])}

    async def report(self, *, session_id: str, text: str) -> dict[str, Any]:
        """A subagent's message to its leader before its run ends.

        Submitted as a steer: a leader at work sees it before its next step, and an idle one starts a
        run on it, the way a final report wakes it. Only the final reply ends the helper's work, so the
        framing says it is still going — a leader taking this for the report would stop waiting.
        """
        manager = self.app.manager
        assert manager is not None
        text = text.strip()
        if not text:
            raise ValueError("the message is empty")
        state = await manager.get_state(session_id)
        leader_id = str(state.metadata.get("subagent_of") or "") if state is not None else ""
        if not leader_id:
            raise ValueError("this session is not a subagent: it has no leader to report to")
        leader = await manager.get_state(leader_id)
        if leader is None:
            raise RuntimeError("your leader session no longer exists; nobody would read this message")
        name = str(state.metadata.get("subagent_name") or session_id)  # type: ignore[union-attr]
        await manager.submit(leader_id, REPORT_HEADER.format(name=name) + text, steer=True, as_answer=False, origin=f"subagent:{name}")
        return {"leader_id": leader_id, "name": name, "delivered": "steer" if leader.running else "run"}

    async def answer(self, session_id: str) -> str | None:
        manager = self.app.manager
        assert manager is not None
        rows = await manager.sessions.list_transcript(session_id)
        for m in reversed(rows):
            if m.role is MessageRole.assistant:
                text = "".join(b.text for b in m.content_blocks if isinstance(b, TextBlock)).strip()
                if text:
                    return split_headline(text)[0]
        return None

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        """Deliver a finished subagent's final reply to its leader, then look for helpers it left idle."""
        manager = self.app.manager
        assert manager is not None
        state = await manager.get_state(session_id)
        if state is None:
            return
        if state.metadata.get("subagent_of") and status != "awaiting":
            await self._deliver_report(session_id, state, status, run_id)
        if status == "completed":
            try:
                await self._remind_lingering(session_id, state)
            except Exception:  # noqa: BLE001 — a reminder is a courtesy; the run is over either way
                logger.exception("could not check the idle subagents of %s", session_id)

    async def _deliver_report(self, session_id: str, state: Any, status: str, run_id: str) -> None:
        """Tell the leader how the helper's run ended: its reply, or what stopped it and what it had done.

        A helper that ended without its own reply — the daily cap, a provider failure, a crash, a Stop —
        used to be reported as "finished: failed" over "(no final reply)" and then deleted, so the leader
        learned nothing of the work under way and the operator saw the helper vanish. It is kept now, and
        the report says what happened, what it had done, and whether it goes on by itself.
        """
        manager = self.app.manager
        assert manager is not None
        if session_id in self._waited or session_id in self._removing:
            return  # the leader's tool call collects the answer itself, or the leader deleted it
        cause, detail = stop_of(state, status, retry_limit=self.app.config.ops.provider_retry_max_attempts)
        if cause == "completed" and unfinished_jobs(manager, session_id):
            # Not done: the job's end starts the subagent's next run, and that run's end reports.
            return
        leader_id = str(state.metadata["subagent_of"])
        leader = await manager.get_state(leader_id)
        if leader is None:
            logger.warning("subagent %s finished but its leader %s is gone", session_id, leader_id)
            return
        name = str(state.metadata.get("subagent_name") or session_id)
        kept = bool(state.metadata.get("subagent_keep"))
        if cause == "completed":
            body = contract_verdict(state, await self.answer(session_id) or no_answer(state))
            fate = f"It stays for follow-ups: SubAgentSend({name!r}, …)." if kept else "It has been removed; its files are in your workspace."
            report = f"[subagent {name!r} finished: {status}. {fate}]\n\n{body}"
            if state.metadata.get(PARKED_KEY):
                await self._set_meta(session_id, {PARKED_KEY: None})
        else:
            report = stop_header(name, cause, detail) + "\n\n" + contract_verdict(state, await self.progress(session_id))
            if cause == "budget":
                await self._park(session_id, run_id=run_id, detail=detail, mode="stopped", report=False)
        delivered = await self._to_leader(session_id, leader_id, name, report, f"subagent-report:{session_id}:{run_id}")
        if cause == "budget" or not delivered:
            await self._note_parked(leader_id, session_id, name, cause=cause, detail=detail, delivered=delivered)
        if cause == "completed" and not kept and delivered:
            self._remove_later(session_id)

    async def progress(self, session_id: str) -> str:
        """What a helper that ended without its reply had done: its steps, the files it wrote, its last words.

        Read from its transcript, which outlives the run: the leader decides from this whether to wait
        for the helper, take over from the files, or start another."""
        manager = self.app.manager
        assert manager is not None
        steps = 0
        files: list[str] = []
        last = ""
        for message in await manager.sessions.list_transcript(session_id):
            if message.role is not MessageRole.assistant:
                continue
            for block in message.content_blocks:
                if isinstance(block, ToolUseBlock):
                    steps += 1
                    if block.name in WRITING_TOOLS:
                        try:
                            args = json.loads(block.arguments_json or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        path = str(args.get("path") or args.get("file_path") or "") if isinstance(args, dict) else ""
                        if path and path not in files:
                            files.append(path)
                elif isinstance(block, TextBlock) and block.text.strip():
                    last = block.text.strip()
        lines = [f"What it had done: {steps} tool call{'s' if steps != 1 else ''}"]
        if files:
            shown = files[-PROGRESS_FILES:]
            more = f" and {len(files) - len(shown)} more" if len(files) > len(shown) else ""
            lines[0] += "; files it wrote: " + ", ".join(shown) + more
        lines[0] += "."
        if last:
            last = split_headline(last)[0]
            if len(last) > PROGRESS_TEXT_CHARS:
                last = "…" + last[-PROGRESS_TEXT_CHARS:].lstrip()
            lines.append("Its last words:\n" + last)
        else:
            lines.append("It had written nothing yet.")
        return "\n".join(lines)

    async def _set_meta(self, session_id: str, changes: dict[str, Any]) -> None:
        """Change a helper's metadata where the live state and the stored row both see it; ``None`` removes a key."""
        manager = self.app.manager
        assert manager is not None
        state = await manager.get_state(session_id)
        if state is None:
            return
        for metadata in (state.metadata, state.session.metadata):
            for key, value in changes.items():
                if value is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = value
        await manager.sessions.update_metadata(session_id, state.session.metadata)

    async def _park(self, session_id: str, *, run_id: str, detail: str, mode: str, report: bool = True) -> None:
        """Mark a helper as stopped by the daily cap, to be driven on when it reopens.

        ``mode`` says how it goes on: ``stopped`` (its run ended; a new run continues it) or ``snapshot``
        (boot recovery kept its unfinished run, which the host resumes itself). ``report`` tells the
        leader now — the path for a helper found parked at start, which no run end announces."""
        manager = self.app.manager
        assert manager is not None
        self._watching = True
        await self._set_meta(session_id, {PARKED_KEY: {"reason": "budget", "mode": mode, "run_id": run_id, "detail": detail, "at": datetime.now(UTC).isoformat()}})
        if not report:
            return
        state = await manager.get_state(session_id)
        leader_id = str(state.metadata.get("subagent_of") or "") if state is not None else ""
        if state is None or not leader_id or await manager.get_state(leader_id) is None:
            return
        name = str(state.metadata.get("subagent_name") or session_id)
        text = stop_header(name, "budget", detail) + "\n\n" + contract_verdict(state, await self.progress(session_id))
        delivered = await self._to_leader(session_id, leader_id, name, text, f"subagent-parked:{session_id}:{run_id}")
        await self._note_parked(leader_id, session_id, name, cause="budget", detail=detail, delivered=delivered)

    async def _to_leader(self, child_id: str, leader_id: str, name: str, text: str, key: str) -> bool:
        """Hand a report to the leader, or hold it on the helper when the leader cannot take it now.

        A leader on a paid model cannot start a run while the daily cap is closed, and the report a
        free helper finished with was dropped: the refusal was logged, the helper deleted, and the work
        reached nobody. A refused report is now kept on the helper, which therefore stays, and the
        budget watch delivers it as soon as the leader can run. The key is the input receipt, so a
        delivery repeated after a crash or a race is the same message, never a second one. Reports
        go in the order they were written: one held is never overtaken by a later one."""
        manager = self.app.manager
        assert manager is not None
        state = await manager.get_state(child_id)
        held = list(state.metadata.get(HELD_KEY) or ()) if state is not None else []
        if held and not await self._flush_held(child_id):
            held = list((await manager.get_state(child_id)).metadata.get(HELD_KEY) or ())  # type: ignore[union-attr]
            if all(entry.get("key") != key for entry in held):
                held.append({"leader_id": leader_id, "name": name, "text": text, "key": key, "at": datetime.now(UTC).isoformat(), "reason": "an earlier report is still waiting"})
                await self._set_meta(child_id, {HELD_KEY: held})
            return False
        try:
            await manager.submit(leader_id, text, as_answer=False, origin=f"subagent:{name}", client_message_id=key)
            return True
        except Exception as exc:  # noqa: BLE001 — whatever refused it, the report is kept, not lost
            logger.warning("subagent %s report to %s is held: %s", child_id, leader_id, exc)
            self._watching = True
            if state is not None:
                await self._set_meta(child_id, {HELD_KEY: [{"leader_id": leader_id, "name": name, "text": text, "key": key, "at": datetime.now(UTC).isoformat(), "reason": str(exc)[:400]}]})
            return False

    async def _flush_held(self, child_id: str) -> bool:
        """Deliver a helper's held reports in order; whether none is left. Removes a finished one-off helper once they are out."""
        manager = self.app.manager
        assert manager is not None
        state = await manager.get_state(child_id)
        if state is None:
            return True
        held = list(state.metadata.get(HELD_KEY) or ())
        while held:
            entry = held[0]
            if await manager.get_state(str(entry.get("leader_id") or "")) is None:
                held.pop(0)  # the leader is gone: nobody is left to read it
                continue
            try:
                await manager.submit(str(entry["leader_id"]), str(entry["text"]), as_answer=False, origin=f"subagent:{entry.get('name') or child_id}", client_message_id=str(entry["key"]))
            except Exception as exc:  # noqa: BLE001 — still refused; the next look tries again
                logger.debug("held report of subagent %s still refused: %s", child_id, exc)
                break
            held.pop(0)
        await self._set_meta(child_id, {HELD_KEY: held or None})
        if held:
            return False
        state = await manager.get_state(child_id)
        if state is not None and not state.metadata.get("subagent_keep") and not state.metadata.get(PARKED_KEY) and not state.running and state.pending is None and not state.last_error_kind:
            self._remove_later(child_id)
        return True

    async def _note_parked(self, leader_id: str, child_id: str, name: str, *, cause: str, detail: str, delivered: bool) -> None:
        """Say in the leader's chat that a helper is parked by the daily cap, or that its report waits.

        The toast about the cap disappears, and the leader on a paid model cannot say anything itself
        while the cap is closed: without this line the operator watching the leader saw helpers go
        quiet and nothing anywhere said why or what would bring them back. The line is the transcript's
        only; the model's history does not carry it."""
        manager = self.app.manager
        assert manager is not None
        state = "parked" if cause == "budget" else "held"
        exceeded = bool(manager.budget_exceeded())
        if state == "parked":
            text = f"Subagent {name!r} is parked by the daily spending cap. It goes on by itself once {BUDGET_WAY_OUT}, and its report follows."
            if not delivered:
                text += " What it had done so far waits for the agent too, which cannot run before then."
        else:
            ended = "finished" if cause == "completed" else f"stopped ({cause})"
            why = f" the daily spending cap is closed and {BUDGET_WAY_OUT}" if exceeded else " the agent cannot start a run now"
            text = f"Subagent {name!r} {ended}, but its report cannot reach the agent yet:{why}. The report is held and delivered then, once."
        notice = {"state": state, "cause": cause, "name": name, "session_id": child_id, "detail": detail, "budget": exceeded or cause == "budget", "held": not delivered}
        message = Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": f"subagent:{name}", NOTICE_KEY: notice})
        try:
            await manager.note_in_chat(leader_id, message)
        except Exception:  # noqa: BLE001 — the report itself is delivered or held either way
            logger.warning("could not note subagent %s in the chat of %s", child_id, leader_id, exc_info=True)

    async def budget_tick(self) -> None:
        """What the daily cap held back, looked at again: reports go to leaders that can run now, helpers
        parked by the cap go on once it is open, and a helper found parked at start is reported."""
        manager = self.app.manager
        assert manager is not None
        if manager.shutting_down or manager.recovering:
            return
        if not self._watching and not manager.budget_parked:
            return
        exceeded = manager.budget_exceeded()
        if exceeded:
            for run_id, session_id in list(manager.budget_parked.items()):
                state = await manager.get_state(session_id)
                if state is not None and state.metadata.get("subagent_of") and not state.metadata.get(PARKED_KEY):
                    await self._park(session_id, run_id=run_id, detail=f"daily budget exceeded ({exceeded})", mode="snapshot")
        self._watching = False
        for row in await manager.list_sessions(limit=500):
            metadata = row.get("metadata") or {}
            if not metadata.get("subagent_of"):
                continue
            cid = str(row["id"])
            if metadata.get(HELD_KEY) and not await self._flush_held(cid):
                self._watching = True
            if metadata.get(PARKED_KEY):
                if exceeded or not await self._go_on(cid, metadata[PARKED_KEY]):
                    self._watching = True

    async def _go_on(self, child_id: str, parked: dict[str, Any]) -> bool:
        """Drive on a helper the daily cap parked, now that it is open; exactly one run continues it.
        Whether it is on its way again (or gone); ``False`` leaves it for the next look."""
        manager = self.app.manager
        assert manager is not None
        state = await manager.get_state(child_id)
        if state is None:
            return True
        if str(parked.get("run_id") or "") in manager.budget_parked:
            return False  # its saved run is resumed by the host itself first
        if not (state.running or state.pending is not None):
            try:
                await manager.submit(child_id, CONTINUE_NOTE, as_answer=False, origin=f"subagent-task:{state.metadata.get('subagent_of')}", client_message_id=f"subagent-continue:{child_id}:{parked.get('run_id') or ''}")
            except Exception as exc:  # noqa: BLE001 — still refused (the flag lags the ledger); the next look tries again
                logger.debug("parked subagent %s could not go on yet: %s", child_id, exc)
                return False
        await self._set_meta(child_id, {PARKED_KEY: None})
        return True

    async def _remind_lingering(self, leader_id: str, leader: Any) -> None:
        """Ask the leader, once per helper, about subagents it left idle for a long while.

        Kept and standing helpers outlive the work they were raised for, and a session collected them
        for weeks: nothing ever asked. Each one is named in one reminder and never again — whatever the
        leader leaves is taken as kept on purpose. The ids are written down before the note goes, so a
        reminder's own run ending, or a restart, cannot ask a second time.
        """
        manager = self.app.manager
        assert manager is not None
        if manager.shutting_down or leader_id in self._removing:
            return
        reminded = {str(i) for i in (leader.metadata.get("subagents_reminded") or ())}
        limit = 60.0 * self.app.config.subagents.linger_remind_minutes
        now = time.time()
        lingering: list[tuple[str, Any, float]] = []
        for row in await self._child_rows(leader_id):
            cid = row["id"]
            if cid in reminded or cid in self._removing or cid in self._waited:
                continue
            if row["metadata"].get(PARKED_KEY) or row["metadata"].get(HELD_KEY):
                continue  # not forgotten: the cap holds it, and the host moves it on when the cap reopens
            child = await manager.get_state(cid)
            if child is None or child.running or child.pending is not None or child.compacting or unfinished_jobs(manager, cid):
                continue
            idle = now - _timestamp(row["last_message_at"])
            if idle < limit:
                continue  # between jobs, not forgotten
            lingering.append((cid, child, idle))
        if not lingering:
            return
        marked = sorted(reminded | {cid for cid, _, _ in lingering})
        leader.metadata["subagents_reminded"] = marked
        leader.session.metadata["subagents_reminded"] = list(marked)
        await manager.sessions.update_metadata(leader_id, leader.session.metadata)
        lines = []
        for cid, child, idle in lingering:
            name = str(child.metadata.get("subagent_name") or cid)
            word = "failed" if child.last_error_kind else "idle"
            fate = "kept" if child.metadata.get("subagent_keep") else "not kept"
            last = " ".join((await self.answer(cid) or "(no reply yet)").split())
            if len(last) > REMINDER_ANSWER_CHARS:
                last = last[:REMINDER_ANSWER_CHARS].rstrip() + "…"
            lines.append(f"- {name} ({word}, {fate}, idle for {idle_words(idle)}): {last}")
        count = len(lines)
        text = (
            f"[subagents: {count} of your helpers {'has' if count == 1 else 'have'} sat idle for a while]\n\n"
            + "\n".join(lines) + "\n\n" + REMINDER_TAIL
        )
        task = asyncio.create_task(self._deliver_reminder(leader_id, text), name=f"subagent-remind:{leader_id}")
        self._reminders.add(task)
        task.add_done_callback(self._reminders.discard)

    async def _deliver_reminder(self, leader_id: str, text: str) -> None:
        """Hand the reminder over once the leader's finished run is written down.

        The finished callbacks run before the session drains what queued up during the run; a note
        submitted from inside one would race that drain. Waiting for the housekeeping to end puts the
        note after it — as a short run of its own, or as a steer when the operator started one meanwhile.
        """
        manager = self.app.manager
        assert manager is not None
        for _ in range(SETTLE_WAIT_SECONDS):
            state = await manager.get_state(leader_id)
            if state is None:
                return
            if state.housekeeping is None or state.housekeeping.done():
                break
            await asyncio.sleep(1)
        if manager.shutting_down:
            return
        try:
            await manager.submit(leader_id, text, steer=True, as_answer=False, origin="subagents")
        except Exception:  # noqa: BLE001
            logger.exception("could not remind %s about its idle subagents", leader_id)

    async def service(self, op: str, **kwargs: Any) -> Any:
        if op == "spawn":
            return await self.spawn(**kwargs)
        if op == "models":
            return await self.models()
        if op == "personas":
            return self.personas()
        if op == "children":
            return await self.children(kwargs["leader_id"])
        if op == "send":
            return await self.send(**kwargs)
        if op == "views":
            return await self.views(kwargs["leader_id"])
        if op == "delete":
            return await self.delete(**kwargs)
        if op == "report":
            return await self.report(**kwargs)
        raise ValueError(op)


async def install(app: Application) -> list[asyncio.Task[None]]:
    subagents = Subagents(app)
    app.extensions["subagents"] = subagents
    assert app.manager is not None
    app.manager.service_hooks["subagents"] = subagents.service
    app.manager.on_finished(subagents.on_run_finished)
    return []
