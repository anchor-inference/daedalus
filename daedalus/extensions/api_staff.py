"""The staff view's routes: one member's live session as its runtime sees it, the operator's message
from its composer, its transcript, its events and its changes. (The messages with their receipts are
``GET /api/staff/{id}/messages`` in ``api.py``.)

The team's own routes (assign, tell, interrupt, pause, release, the requests) are in ``api.py``;
these are what the staff view reads, and they ask the member's runtime rather than the rows alone,
because a command-line member's transcript and channels live in its CLI and its terminal daemon.
Like ``api.py`` this module may import the HTTP framework; the runtimes behind it may not.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.staff import Team
from daedalus.extensions.staff_kept import how_kept
from daedalus.harness.capabilities import CAPABILITIES
from daedalus.stores.harness import HarnessStore
from daedalus.stores.staff import Staff, StaffBusy, StaffError

if TYPE_CHECKING:
    from daedalus.app import Application

CHANGES_TIMEOUT_S = 30.0
TRANSCRIPT_TURNS_MAX = 500


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=64_000)
    when: Literal["now", "after_turn", "interrupt"] = "now"


def _numstat(text: str) -> dict[str, Any]:
    """``git diff --numstat`` as the files it names and the lines added and removed; a binary file
    counts as changed with no lines."""
    files: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, path = parts
        files.append({"path": path, "added": int(added) if added.isdigit() else None, "removed": int(removed) if removed.isdigit() else None})
    return {
        "files": files,
        "added": sum(f["added"] or 0 for f in files),
        "removed": sum(f["removed"] or 0 for f in files),
    }


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    manager = app.manager
    assert manager is not None

    def team() -> Any:
        found = app.extensions.get("staff")
        if found is None:
            raise HTTPException(503, "the staff runtime is not running")
        return found

    async def member_of(staff_id: str) -> Staff:
        member = await manager.staff.get(staff_id)
        if member is None:
            raise HTTPException(404, "no such staff member")
        return member

    async def live_of(member: Staff) -> Any:
        live = await team().live_of(member)
        if live is None:
            raise HTTPException(409, f"{member.name} has no live session")
        return live

    async def kept_of(member: Staff) -> list[dict[str, Any]]:
        """How the member's settings are kept, for its next launch: live session or not, since the
        operator reads it while deciding how to hire or change the member."""
        project = await manager.projects.get(member.project_id)
        if project is None:
            return []
        try:
            folder = Team.folder_for(project, member, None)
        except StaffError:
            return []
        return how_kept(member, folder, local_env=manager.projects.local_env, permission_level=Team.permission_level(project))

    @api.get("/api/staff/{staff_id}/session")
    async def staff_session(staff_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The member's live session with what its runtime knows: the status and what it waits for,
        the terminal, the launch (CLI, version, model, mode, worktree, branch, task), what the CLI
        can do, what each channel last said (``channel``) and the verdict on them (``health``), the
        requests open, the spend. ``rules`` are the operator's standing grants to the member and
        ``kept`` how each of its settings is kept, live session or not."""
        member = await member_of(staff_id)
        session = await manager.staff.live(staff_id)
        out: dict[str, Any] = {"staff": member.view(), "session": session.view() if session is not None else None, "rules": await manager.staff.allow_rules(staff_id)}
        out["kept"] = await kept_of(member)
        if session is None:
            return out
        caps = CAPABILITIES.get(member.harness)
        out["capabilities"] = asdict(caps) if caps is not None else None
        launch = await HarnessStore(manager.db).open_launch_for(session.id) if caps is not None else None
        runtime = team().runtimes.get(member.harness)
        live = await team().live(session.id)
        out["launch"] = {
            "harness": member.harness,
            "model": member.model,
            "effort": member.effort,
            "agent": member.agent,
            "permission_mode": member.permission_mode,
            "env": launch.env if launch is not None else None,
            "version": launch.harness_version if launch is not None else None,
            "launch_id": launch.launch_id if launch is not None else None,
            "companion_terminal_id": launch.companion_terminal_id if launch is not None else None,
            "worktree": session.worktree_path,
            "branch": session.branch,
            "task_id": session.task_id,
        }
        channel = getattr(runtime, "channel", None)
        out["channel"] = channel(live) if channel is not None and live is not None else {}
        out["health"] = (await team().health(live)).view() if live is not None else None
        out["requests"] = [a.view() for a in await manager.asks.open_for(member.project_id) if a.staff_session_id == session.id]
        out["usage"] = session.usage or None
        return out

    @api.delete("/api/staff/{staff_id}/rules/{rule}")
    async def revoke_staff_rule(staff_id: str, rule: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Take back a standing grant. The member's next launch goes without it; a session running now
        keeps it until it ends, since the CLI takes no rule back from outside."""
        member = await member_of(staff_id)
        if not await manager.staff.revoke_rule(member.id, rule):
            raise HTTPException(404, f"{member.name} has no standing grant {rule!r}")
        return {"rules": await manager.staff.allow_rules(member.id)}

    @api.post("/api/staff/{staff_id}/messages")
    async def post_staff_message(staff_id: str, body: MessageBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The operator's message from the staff view: delivered unprefixed, and without waiting for
        the operator to stop typing — it is the operator."""
        member = await member_of(staff_id)
        try:
            return await team().tell(member, body.text, when=body.when, by="operator")  # type: ignore[no-any-return]
        except StaffBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except StaffError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/api/staff/{staff_id}/seen")
    async def staff_seen(staff_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        member = await member_of(staff_id)
        live = await live_of(member)
        await team().seen(live)
        return {"ok": True}

    @api.get("/api/staff/{staff_id}/transcript")
    async def staff_transcript(staff_id: str, since: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=TRANSCRIPT_TURNS_MAX), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """A command-line member's turns, read from its CLI's own transcript: the prompts, the
        replies, one line per tool, the spend of each reply."""
        member = await member_of(staff_id)
        live = await live_of(member)
        runtime = team().runtimes.get(member.harness)
        turns_of = getattr(runtime, "turns", None)
        if turns_of is None:
            raise HTTPException(409, f"{member.name}'s session is shown in its own conversation view")
        turns = [t for t in await turns_of(live) if t.index >= since]
        return {"turns": [asdict(t) for t in turns[:limit]], "more": len(turns) > limit}

    @api.get("/api/staff/{staff_id}/events")
    async def staff_events(staff_id: str, limit: int = Query(100, ge=1, le=1000), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The member's recent events, newest first: statuses, reports, messages, requests."""
        member = await member_of(staff_id)
        rows = await manager.db.fetchall(
            "SELECT seq, at, type, payload_json FROM app_events WHERE project_id = ? AND staff_id = ? ORDER BY seq DESC LIMIT ?",
            (member.project_id, staff_id, limit),
        )
        return {"events": [{"seq": r["seq"], "at": r["at"], "type": r["type"], "payload": json.loads(r["payload_json"] or "{}")} for r in rows]}

    @api.get("/api/staff/{staff_id}/changes")
    async def staff_changes(staff_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What the member's session changed against the base it started from: files, lines added
        and removed, and the files not yet added to git."""
        await member_of(staff_id)
        session = await manager.staff.live(staff_id) or next(iter(await manager.staff.sessions(staff_id, limit=1)), None)
        if session is None or not session.worktree_path:
            return {"files": [], "added": 0, "removed": 0, "untracked": [], "detail": "no worktree of its own"}
        terminals: Any = app.extensions.get("terminals")
        if terminals is None:
            raise HTTPException(503, "the terminals service is not running")
        env = manager.projects.local_env
        if session.folder_id:
            row = await manager.db.fetchone("SELECT env FROM project_folders WHERE id = ?", (session.folder_id,))
            env = str(row["env"]) if row is not None else env
        base = session.base_ref or "HEAD"
        try:
            numstat, untracked = await asyncio.gather(
                terminals.exec_run(env, ["git", "-C", session.worktree_path, "diff", "--numstat", base], timeout=CHANGES_TIMEOUT_S, actor="operator"),
                terminals.exec_run(env, ["git", "-C", session.worktree_path, "ls-files", "--others", "--exclude-standard"], timeout=CHANGES_TIMEOUT_S, actor="operator"),
            )
        except Exception as exc:  # noqa: BLE001 — the daemon's refusal is the answer the view shows
            raise HTTPException(503, f"the changes could not be read: {getattr(exc, 'message', exc)}") from exc
        if numstat.exit_code != 0:
            raise HTTPException(409, f"git could not compare the worktree: {(numstat.stderr or numstat.stdout).strip()[:300]}")
        return {**_numstat(numstat.stdout), "untracked": [line for line in untracked.stdout.splitlines() if line.strip()], "base": base}


__all__ = ["register"]
