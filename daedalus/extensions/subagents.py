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
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from protocore.contracts.types import MessageRole, TextBlock

from daedalus.host.prompts import split_headline

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

POLL_SECONDS = 3.0
NO_ANSWER = "(the subagent's run ended without a final reply; see its session)"


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
    "while one of yours is still running, your turn ending is not your report."
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
    "leader needs — it is delivered to the leader verbatim."
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


class Subagents:
    def __init__(self, app: Application) -> None:
        self.app = app
        self._waited: set[str] = set()
        self._removals: set[asyncio.Task[None]] = set()

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
            raise RuntimeError(f"{len(active)} subagents are already running for this session (limit {self.app.config.subagents.max_active}); wait for their reports")
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
            "brief": (IDLE_BRIEF if idle else BRIEF).format(leader=leader_id)
            + (WITHHELD.format(names=", ".join(withheld)) if withheld else "")
            + (f"\n\n[persona: {persona}]\n{persona_text}" if idle and persona_text else ""),
            "unattended": True,
        }
        if withheld:
            metadata["tools_off"] = withheld
        for key in ("mode", "mcp"):
            if leader.metadata.get(key) is not None:
                metadata[key] = leader.metadata[key]
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
            if overrides.get("preset") or (overrides.get("provider") and overrides.get("model_name")):
                await manager.live.set_model(
                    cid,
                    model_name=overrides.get("model_name"),
                    provider=overrides.get("provider"),
                    preset=overrides.get("preset"),
                    thinking_enabled=overrides.get("thinking_enabled"),
                    reasoning_effort=overrides.get("reasoning_effort"),
                )
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
        result: dict[str, Any] = {"session_id": cid, "run_id": run_id, "name": label, "model": model or "(leader's model)", "answer": None, "kept": bool(keep)}
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
                    result["answer"] = contract_verdict(state, await self.answer(cid) or no_answer(state))
                    if not state.metadata.get("subagent_keep"):
                        self._remove_later(cid)
                    return result
            result["note"] = "the subagent did not finish in time; its report will arrive as a message when it does"
        finally:
            self._waited.discard(cid)  # on a timeout the async report takes over
        return result

    async def send(self, *, leader_id: str, name: str, text: str, wait: bool = False, timeout_minutes: int | None = None) -> dict[str, Any]:
        """A message to one of the leader's subagents: a steer while it works, a new task once it has finished."""
        manager = self.app.manager
        assert manager is not None
        text = text.strip()
        if not text:
            raise ValueError("the message is empty")
        children = await self.children(leader_id)
        match = next((c for c in children if c["name"] == name.strip() or c["session_id"] == name.strip()), None)
        if match is None:
            known = ", ".join(str(c["name"]) for c in children) or "none"
            raise ValueError(f"no subagent named {name!r}; yours: {known}. A subagent started without keep=true is removed when it finishes.")
        cid = str(match["session_id"])
        state = await manager.get_state(cid)
        if state is None:
            raise ValueError(f"subagent {name!r} no longer exists")
        if state.pending is not None:
            raise RuntimeError(f"subagent {name!r} is waiting on a question; it cannot take a message now")
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

    async def children(self, leader_id: str) -> list[dict[str, Any]]:
        manager = self.app.manager
        assert manager is not None
        out = []
        for row in await manager.list_sessions(limit=500):
            if row.get("metadata", {}).get("subagent_of") != leader_id:
                continue
            state = await manager.get_state(row["id"])
            out.append({"session_id": row["id"], "name": row["metadata"].get("subagent_name"), "running": bool(state and state.running), "status": row["status"], "kept": bool(row["metadata"].get("subagent_keep"))})
        return out

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
        """Deliver a finished subagent's final reply to its leader."""
        manager = self.app.manager
        assert manager is not None
        state = await manager.get_state(session_id)
        if state is None or not state.metadata.get("subagent_of") or status == "awaiting":
            return
        if session_id in self._waited:
            return  # the leader's tool call collects the answer itself
        if status == "completed" and unfinished_jobs(manager, session_id):
            # Not done: the job's end starts the subagent's next run, and that run's end reports.
            return
        leader_id = str(state.metadata["subagent_of"])
        leader = await manager.get_state(leader_id)
        if leader is None:
            logger.warning("subagent %s finished but its leader %s is gone", session_id, leader_id)
            return
        name = str(state.metadata.get("subagent_name") or session_id)
        answer = await self.answer(session_id) if status == "completed" else None
        body = contract_verdict(state, answer or no_answer(state))
        kept = bool(state.metadata.get("subagent_keep"))
        fate = f"It stays for follow-ups: SubAgentSend({name!r}, …)." if kept else "It has been removed; its files are in your workspace."
        report = f"[subagent {name!r} finished: {status}. {fate}]\n\n{body}"
        try:
            await manager.submit(leader_id, report, as_answer=False, origin=f"subagent:{name}")
        except Exception:  # noqa: BLE001
            logger.exception("could not deliver subagent %s report to %s", session_id, leader_id)
        if not kept:
            self._remove_later(session_id)

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
        raise ValueError(op)


async def install(app: Application) -> list[asyncio.Task[None]]:
    subagents = Subagents(app)
    app.extensions["subagents"] = subagents
    assert app.manager is not None
    app.manager.service_hooks["subagents"] = subagents.service
    app.manager.on_finished(subagents.on_run_finished)
    return []
