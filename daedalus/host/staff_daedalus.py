"""A staff member run by Daedalus itself: a Daedalus session in the project, briefed as the member.

It reuses what subagents taught — a session made for one task in the project's folder, the model
chosen per member, the last reply as the result — but a staff session is not a subagent: it carries no
``subagent_of``. The tool restrictions of a session are the union of its ancestors' along that chain,
and an orchestrator allows only its control tools, so a member parented on it could not run a command.
The member is instead marked ``staff_id`` / ``staff_session_id`` and restricted by the host's staff
rule, and what it reports goes to the event bus rather than into anyone's transcript.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from protocore.contracts.types import MessageRole, TextBlock, ToolUseBlock

from daedalus.host.gitrun import GitError, run_git
from daedalus.host.prompts import split_headline
from daedalus.host.services import SessionServices
from daedalus.staff_runtime import (
    AskRef,
    Availability,
    Decision,
    LiveSession,
    OutgoingMessage,
    ReadPage,
    ReadRequest,
    Receipt,
    Started,
    StartRequest,
    UsageSnapshot,
)
from daedalus.stores.staff import daedalus_reaches

if TYPE_CHECKING:
    from daedalus.host.session_runner import SessionManager

logger = logging.getLogger(__name__)

STOP_WAIT_SECONDS = 30.0
"""How long an interrupting message waits for the stopped turn to settle before it is sent anyway."""
DIFF_TIMEOUT = 30.0


def isolation_of(req: StartRequest) -> str:
    """The isolation the session actually gets: a worktree only when one was made for it."""
    if req.worktree is not None:
        return "worktree"
    return "readonly" if req.staff.isolation == "readonly" else "shared"


class DaedalusStaffRuntime:
    kind = "daedalus"

    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager

    async def available(self, env: str) -> Availability:
        local = self.manager.projects.local_env
        if not daedalus_reaches(env, local):
            return Availability(False, f"a Daedalus staff member works where Daedalus runs ({local}) and cannot reach a {env} folder; give this to a command-line member, which works in a {env} terminal")
        if env != local:
            # A host folder seen from the container: every command of the member runs through the
            # host terminal daemon, so a member started while it is down could do nothing at all.
            problem = self.manager.host_unreachable()
            if problem:
                return Availability(False, problem)
        if not self.manager.config.has_model:
            return Availability(False, "no model is configured")
        return Availability(True)

    async def start(self, req: StartRequest) -> Started:
        isolation = isolation_of(req)
        metadata: dict[str, Any] = {
            "staff_id": req.staff.id,
            "staff_session_id": req.staff_session_id,
            "folder_id": req.folder.id,
            "brief": req.brief_text,
            "telegram_detached": True,
            "staff_isolation": isolation,
        }
        if req.task is not None:
            metadata["task_id"] = req.task.id
        if req.worktree is not None:
            metadata["worktree"] = str(req.worktree.path)
            metadata["worktree_cwd"] = str(req.worktree.cwd)
        title = f"{req.staff.name} · {req.task.title}" if req.task is not None else req.staff.name
        state = await self.manager.create_session(title[:120], metadata=metadata, project_id=req.project.id, folder_id=req.folder.id)
        session_id = state.session.id
        try:
            if req.model:
                await self.manager.set_model(session_id, preset=req.model)
            if req.effort and req.effort != "off":
                await self.manager.set_model(session_id, reasoning_effort=req.effort)
            elif req.effort == "off":
                await self.manager.set_model(session_id, thinking=False)
            await self.manager.submit(session_id, req.first_message, as_answer=False, origin=req.origin, client_message_id=req.first_message_id or None)
        except Exception:
            # A session that could not be given its task is not left behind as an empty chat.
            try:
                await self.manager.delete_session(session_id, delete_workspace=False)
            except Exception:  # noqa: BLE001
                logger.exception("could not remove the staff session %s that failed to start", session_id)
            raise
        return Started(terminal_id=None, cli_session_id=None, transcript_ref=session_id, session_id=session_id)

    def _session(self, live: LiveSession) -> str:
        if not live.session_id:
            raise RuntimeError(f"{live.staff.name}'s session has no Daedalus session")
        return live.session_id

    async def send(self, live: LiveSession, msg: OutgoingMessage) -> Receipt:
        session_id = self._session(live)
        text = f"[message from the {msg.origin}]\n{msg.text}"
        if msg.mode == "interrupt":
            await self.interrupt(live)
            await self._settle(session_id)
        try:
            await self.manager.submit(
                session_id, text, as_answer=False, steer=msg.mode == "now", follow_up=msg.mode == "after_turn", origin=msg.origin, client_message_id=msg.id
            )
        except Exception as exc:  # noqa: BLE001 — a refused delivery is a receipt, not a crash of the caller
            return Receipt("failed", str(exc)[:500])
        receipt = await self.manager.live.receipt(session_id, msg.id)
        # Consumed means the model has it in front of it: a new run started from it, or the running
        # one placed it. Anything else waits in the session's queue, which the host holds.
        return Receipt("acknowledged" if receipt is not None and receipt["status"] == "consumed" else "submitted")

    async def _settle(self, session_id: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STOP_WAIT_SECONDS
        while loop.time() < deadline:
            state = self.manager.live_state(session_id)
            if state is None or not state.running:
                return
            await asyncio.sleep(0.05)

    async def interrupt(self, live: LiveSession) -> None:
        await self.manager.stop(self._session(live))

    async def answer(self, live: LiveSession, ask: AskRef, decision: Decision) -> None:
        session_id = self._session(live)
        via = "orchestrator" if decision.by == "orchestrator" else "app"
        if ask.kind == "question":
            answer: dict[str, Any] = {"selected": list(decision.selected)}
            if decision.text and decision.text.strip():
                answer["custom"] = decision.text.strip()
            if not answer["selected"] and "custom" not in answer:
                raise ValueError("an answer to a question selects an option or says something")
            await self.manager.answer(session_id, [answer], via=via, source=decision.by, claimed=True)
            return
        key = ask.request_ref
        reason = f": {decision.text.strip()}" if decision.text and decision.text.strip() else ""
        if decision.allow:
            await self.manager.grant(session_id, key, via=via)
            note = f"[the {decision.by} granted request {key}{reason}] Retry the call now; the same call passes once."
        else:
            await self.manager.refuse(session_id, key, via=via)
            note = f"[the {decision.by} refused request {key}{reason}] Do not retry it; do the task another way, or report what it leaves undone."
        # Only the reason is the operator's; the rest of the note is the host's instruction.
        words = [decision.text.strip()] if decision.by == "operator" and decision.text and decision.text.strip() else []
        await self.manager.submit(session_id, note, as_answer=False, follow_up=True, origin=decision.by, operator_words=words)

    async def read(self, live: LiveSession, req: ReadRequest) -> ReadPage:
        session_id = self._session(live)
        if req.what == "screen":
            return ReadPage("a Daedalus staff member has no screen; read its turns instead", None, False)
        if req.what == "diff":
            return await self._diff(live, req)
        messages = await self.manager.sessions.list_transcript(session_id)
        after = int(req.cursor) if req.cursor and req.cursor.isdigit() else 0
        messages = [m for m in messages if int(m.metadata.get("daedalus.seq") or 0) > after]
        next_cursor = str(max((int(m.metadata.get("daedalus.seq") or 0) for m in messages), default=after)) if messages else req.cursor
        if req.what == "last":
            for message in reversed(messages):
                if message.role is MessageRole.assistant:
                    text = "".join(b.text for b in message.content_blocks if isinstance(b, TextBlock)).strip()
                    if text:
                        return _clip(split_headline(text)[0], req.max_chars, next_cursor)
            return ReadPage("(no reply yet)", next_cursor, False)
        return _clip(_turns(messages, max(1, req.turns)), req.max_chars, next_cursor)

    async def _diff(self, live: LiveSession, req: ReadRequest) -> ReadPage:
        path = live.session.worktree_path
        base = live.session.base_ref
        if not path:
            state = await self.manager.get_state(self._session(live))
            if state is None:
                return ReadPage("the session is gone", None, False)
            path, base = str(state.workspace), "HEAD"
        loaded = await self.manager.get_state(self._session(live))
        services = loaded.services if loaded is not None else None
        if services is not None and services.exec_backend is not None:
            return await self._remote_diff(services, path if live.session.worktree_path else str(services.workspace_dir), base or "HEAD", req)
        cwd = Path(path)
        try:
            stat = await run_git(["diff", "--stat", base or "HEAD"], cwd=cwd, timeout=DIFF_TIMEOUT)
            patch = await run_git(["diff", base or "HEAD"], cwd=cwd, timeout=DIFF_TIMEOUT)
            untracked = await run_git(["ls-files", "--others", "--exclude-standard"], cwd=cwd, timeout=DIFF_TIMEOUT)
        except GitError as exc:
            return ReadPage(f"no diff: {exc}", None, False)
        text = (stat.strip() or "(no changes against the base)") + (f"\n\nnew files not yet added:\n{untracked.strip()}" if untracked.strip() else "") + (f"\n\n{patch}" if patch.strip() else "")
        return _clip(text, req.max_chars, None, keep="head")

    async def _remote_diff(self, services: SessionServices, path: str, base: str, req: ReadRequest) -> ReadPage:
        """The diff of a member working in a host folder, taken there by the commands it runs with:
        the session here only runs from a directory of its own, which holds none of its changes."""
        assert services.exec_backend is not None
        quoted = shlex.quote(base)
        outcomes = [
            await services.exec_backend.run(f"git -c core.quotepath=off {args}", cwd=path, env={"GIT_TERMINAL_PROMPT": "0"}, timeout=DIFF_TIMEOUT)
            for args in (f"diff --stat {quoted}", f"diff {quoted}", "ls-files --others --exclude-standard")
        ]
        failed = next((o for o in outcomes if o.exit_code != 0), None)
        if failed is not None:
            return ReadPage(f"no diff: {failed.output.strip()[:500]}", None, False)
        stat, patch, untracked = (o.output for o in outcomes)
        text = (stat.strip() or "(no changes against the base)") + (f"\n\nnew files not yet added:\n{untracked.strip()}" if untracked.strip() else "") + (f"\n\n{patch}" if patch.strip() else "")
        return _clip(text, req.max_chars, None, keep="head")

    async def stop(self, live: LiveSession) -> None:
        """Stop the turn and archive the session: its history and spend stay, only the session list hides it."""
        session_id = live.session_id
        if not session_id:
            return
        await self.manager.stop(session_id)
        state = await self.manager.get_state(session_id)
        if state is not None:
            state.session.metadata["archived"] = True
            state.metadata["archived"] = True
            await self.manager.sessions.update_metadata(session_id, state.session.metadata)

    async def resume(self, req: StartRequest, prior: LiveSession) -> Started:
        # A Daedalus session is its transcript; a replacement is a new session told what ended.
        return await self.start(req)

    async def usage(self, live: LiveSession) -> UsageSnapshot | None:
        session_id = live.session_id
        if not session_id:
            return None
        rows = await self.manager.db.fetchall("SELECT id, metadata FROM sessions WHERE metadata LIKE ?", (f'%"subagent_of": "{session_id}"%',))
        ids = [session_id, *(str(r["id"]) for r in rows if json.loads(r["metadata"] or "{}").get("subagent_of") == session_id)]
        marks = ",".join("?" for _ in ids)
        row = await self.manager.db.fetchone(
            f"SELECT coalesce(sum(input_tokens), 0) i, coalesce(sum(output_tokens), 0) o, sum(cost_usd) usd FROM usage_events WHERE session_id IN ({marks})",  # noqa: S608 — placeholders only
            tuple(ids),
        )
        if row is None:
            return None
        return UsageSnapshot(int(row["i"]), int(row["o"]), float(row["usd"]) if row["usd"] is not None else None, None, "metered")


def _turns(messages: list[Any], turns: int) -> str:
    """The last ``turns`` turns: what started each, the replies in full, and every tool call as one line."""
    starts = [i for i, m in enumerate(messages) if m.role is MessageRole.user]
    begin = starts[-turns] if len(starts) >= turns else 0
    lines: list[str] = []
    for message in messages[begin:]:
        if message.role is MessageRole.user:
            text = " ".join("".join(b.text for b in message.content_blocks if isinstance(b, TextBlock)).split())
            if text:
                lines.append(f"» {text[:300]}")
        elif message.role is MessageRole.assistant:
            for block in message.content_blocks:
                if isinstance(block, TextBlock) and block.text.strip():
                    lines.append(split_headline(block.text.strip())[0])
                elif isinstance(block, ToolUseBlock):
                    lines.append(f"· {block.name} {' '.join(block.arguments_json.split())[:160]}")
    return "\n".join(lines) or "(nothing yet)"


def _clip(text: str, limit: int, cursor: str | None, *, keep: str = "tail") -> ReadPage:
    if len(text) <= limit:
        return ReadPage(text, cursor, False)
    kept = text[:limit] if keep == "head" else text[-limit:]
    return ReadPage(kept, cursor, True)


__all__ = ["DaedalusStaffRuntime", "isolation_of"]
