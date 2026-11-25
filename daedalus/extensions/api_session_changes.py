"""A chat's own working-tree changes, for its Changes tab: the files changed in the folder it works
in, one file's diff, discarding changes and committing them.

- ``GET /api/sessions/{id}/changes``: the changed files against the last commit, untracked ones
  included, the branch and how far it is from its upstream; ``repository: false`` (not an error) for
  a folder outside any repository.
- ``GET /api/sessions/{id}/changes/diff?path=``: one file's unified diff, cut at a size.
- ``POST /api/sessions/{id}/changes/revert`` with ``{"confirm": true, "paths": [...]}`` or
  ``{"confirm": true, "all": true}``: discards those changes; a new file is deleted, and the answer
  says which.
- ``POST /api/sessions/{id}/commit`` with ``{"message", "paths"?}``: commits them, as the
  repository's own identity, and never pushes.

git runs where the session's tools run, through the terminal daemon of that environment, as the
staff view's changes and the Terminals side panel already do (:mod:`daedalus.terminals.changes`):
in the container for a chat working there, on the host for one working in a host folder. A remote
conversation's tools run on the operator's own machine through ``daedalus-cli``, which no daemon
here reaches, so its folder is reported as out of reach rather than read somewhere it is not.

A revert or a commit holds the session still (:meth:`SessionManager.held_quiet`) and is refused
while a run of it, or of another session working in the same folder, is in progress: the agent
would go on from files that are no longer what it wrote.
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from daedalus.host.session_runner import SessionManager
from daedalus.terminals import changes
from daedalus.terminals.changes import FILES_MAX, GIT_ENV, ChangesBlocked, ChangesRefused
from daedalus.terminals.model import TerminalError, is_device_env

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.host.session_runner import SessionState

logger = logging.getLogger(__name__)

READ_TIMEOUT_S = 20.0
WRITE_TIMEOUT_S = 60.0
"""A commit of many files, or a restore of large ones, takes longer than a read; past this the daemon
ends git and the request answers instead of hanging."""
COUNTED_NEW_FILES = 50
"""New files whose lines the list counts, one daemon call each; the rest show without a count."""
MESSAGE_MAX = 20_000

PathText = Annotated[str, Field(min_length=1, max_length=4096)]


class RevertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
    """Required to be true: a revert deletes new files and discards edits, and nothing brings them back."""
    paths: list[PathText] | None = Field(default=None, max_length=FILES_MAX)
    all: bool = False


class CommitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=MESSAGE_MAX)
    paths: list[PathText] | None = Field(default=None, min_length=1, max_length=FILES_MAX)


class OutOfReach(Exception):
    """The session's folder is somewhere no terminal daemon of this host runs git."""


def overlaps(one: str, other: str) -> bool:
    a, b = posixpath.normpath(one), posixpath.normpath(other)
    return a == b or b.startswith(a.rstrip("/") + "/") or a.startswith(b.rstrip("/") + "/")


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    manager = app.manager
    assert manager is not None

    def place(state: SessionState) -> tuple[str, str]:
        """The environment the session's tools run in and its working directory there.

        A chat in a host folder seen from the container runs from a directory of its own here that
        holds only its inbox; its work is the folder on the host, and that path is the one read
        whether or not a run has built its tools yet (``work_dir`` knows it only once they are)."""
        sid = state.session.id
        env = manager.env_of(sid, state.metadata, state.project)
        if is_device_env(env):
            raise OutOfReach("the conversation's tools run on the operator's own machine through daedalus-cli, which this host cannot run git on")
        if env != manager.projects.local_env:
            try:
                return env, str(manager.folder_workspace(sid, state.metadata, state.project))
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from None
        return env, str(manager.work_dir(state))

    async def session_of(session_id: str) -> SessionState:
        state = await manager.get_state(session_id)
        if state is None:
            raise HTTPException(404, "no such session")
        if state.project is None:
            raise HTTPException(409, "the session has no project, so no folder to compare")
        return state

    def runner(env: str, *, write: bool) -> changes.Runner:
        """git in ``env`` through its terminal daemon, with the daemon's own timeout. A read is not
        audited: the panel reads again on every change, and one row per reload would bury the rows
        a person or an agent caused; a revert or a commit is."""
        terminals: Any = app.extensions.get("terminals")
        if terminals is None:
            raise HTTPException(503, "the terminals service is not running")
        if not terminals.configured(env):
            raise HTTPException(503, f"this installation has no terminal daemon for {env}")

        async def run(argv: list[str], max_output: int) -> Any:
            return await terminals.exec_run(env, argv, env_vars=dict(GIT_ENV), timeout=WRITE_TIMEOUT_S if write else READ_TIMEOUT_S,
                                            max_output=max_output, actor="operator", audit=write)

        return run

    def busy_beside(state: SessionState, env: str, directory: str) -> str:
        """Another session whose run is in progress in this folder, or one above or below it, by
        title; ``""`` when there is none. A staff member's own worktree below the folder is a
        repository of its own, which nothing here touches, so its runs do not count."""
        for sid in manager.busy_sessions():
            other = manager.live_state(sid)
            if other is None or sid == state.session.id or other.project is None:
                continue
            try:
                their_env, theirs = place(other)
            except (OutOfReach, HTTPException):
                continue
            if their_env != env or not overlaps(directory, theirs):
                continue
            if other.metadata.get("worktree_cwd") and theirs != directory and theirs.startswith(directory.rstrip("/") + "/"):
                continue
            return other.session.title or sid
        return ""

    def refused(exc: ChangesRefused | ChangesBlocked | TerminalError) -> HTTPException:
        if isinstance(exc, ChangesRefused):
            return HTTPException(400, str(exc))
        if isinstance(exc, ChangesBlocked):
            return HTTPException(409, str(exc))
        return HTTPException(503, f"git could not be run there: {exc.message}")

    async def told(state: SessionState, payload: dict[str, Any]) -> None:
        """The panel reloads on this; the files changed whether or not the bus could say so."""
        try:
            await manager.bus.publish("session.changes", payload, session_id=state.session.id,
                                      project_id=state.project.id if state.project is not None else None,
                                      staff_id=str(state.metadata.get("staff_id") or "") or None)
        except Exception:  # noqa: BLE001 — the revert or commit is done; the event is a courtesy to open panels
            logger.warning("could not publish session.changes for %s", state.session.id, exc_info=True)

    @api.get("/api/sessions/{session_id}/changes")
    async def session_changes(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The files changed in the session's folder against the last commit. ``busy`` names why a
        revert or a commit would be refused now, so the tab can say it before the press."""
        state = await session_of(session_id)
        try:
            env, directory = place(state)
        except OutOfReach as exc:
            return {"repository": False, "env": "", "directory": "", "files": [], "added": 0, "removed": 0, "truncated": False,
                    "reason": "out_of_reach", "detail": str(exc), "busy": ""}
        try:
            found = await changes.listing(runner(env, write=False), directory, count_new=COUNTED_NEW_FILES)
        except TerminalError as exc:
            raise refused(exc) from None
        busy = ""
        if state.running or state.pending is not None:
            busy = "the chat's run is in progress"
        elif beside := busy_beside(state, env, directory):
            busy = f"a run of {beside} is in progress in this folder"
        return {"env": env, "directory": directory, **found, "busy": busy}

    @api.get("/api/sessions/{session_id}/changes/diff")
    async def session_change_diff(session_id: str, path: str = Query(min_length=1, max_length=4096), _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        state = await session_of(session_id)
        try:
            env, directory = place(state)
        except OutOfReach as exc:
            raise HTTPException(409, str(exc)) from None
        try:
            return await changes.file_diff(runner(env, write=False), directory, path, scoped=True)
        except (ChangesRefused, TerminalError) as exc:
            raise refused(exc) from None

    async def changing(state: SessionState, what: str) -> tuple[str, str]:
        """Where a revert or a commit runs, after the refusals that hold before it may."""
        if SessionManager.lives_in_window(state):
            raise HTTPException(409, "this session only reads its chat's folder; change the files from the chat")
        try:
            env, directory = place(state)
        except OutOfReach as exc:
            raise HTTPException(409, str(exc)) from None
        if beside := busy_beside(state, env, directory):
            raise HTTPException(409, f"a run of {beside} is in progress in this folder; {what} when it is over")
        return env, directory

    @api.post("/api/sessions/{session_id}/changes/revert")
    async def session_changes_revert(session_id: str, body: RevertBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Discard the changes of the files named, or of every changed file with ``all``. ``deleted``
        is every file whose content is gone for good; ``restored`` every one put back as committed."""
        if not body.confirm:
            raise HTTPException(400, "a revert discards work that cannot be brought back; send confirm: true")
        if body.all == bool(body.paths):
            raise HTTPException(400, "name the files to revert in paths, or send all: true, not both")
        state = await session_of(session_id)
        env, directory = await changing(state, "revert")
        try:
            async with manager.held_quiet(state, what="the chat"):
                done = await changes.revert(runner(env, write=True), directory, None if body.all else list(body.paths or []))
        except (ChangesRefused, ChangesBlocked, TerminalError) as exc:
            raise refused(exc) from None
        except RuntimeError as exc:
            # The session's own refusal: a run in flight or a question outstanding.
            raise HTTPException(409, str(exc)) from None
        await told(state, {"change": "reverted", "files": len(done["reverted"])})
        return {"env": env, "directory": directory, **done}

    @api.post("/api/sessions/{session_id}/commit")
    async def session_commit(session_id: str, body: CommitBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Commit the files named, or every changed file of the folder, with the operator's message."""
        state = await session_of(session_id)
        env, directory = await changing(state, "commit")
        try:
            async with manager.held_quiet(state, what="the chat"):
                done = await changes.commit(runner(env, write=True), directory, body.message, list(body.paths) if body.paths is not None else None)
        except (ChangesRefused, ChangesBlocked, TerminalError) as exc:
            raise refused(exc) from None
        except RuntimeError as exc:
            # The session's own refusal: a run in flight or a question outstanding.
            raise HTTPException(409, str(exc)) from None
        await told(state, {"change": "committed", "files": len(done["files"]), "commit": done["commit"]})
        return {"env": env, "directory": directory, **done}


__all__ = ["register"]
