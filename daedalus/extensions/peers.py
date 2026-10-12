"""Named peers: sessions other sessions can talk to.

The operator names a topic (``/peer here reviewer``); a session of the same project can then
AskPeer it — the question runs as a turn in the peer's session and the peer's final reply comes
back as the tool result. A depth counter stops a peer from asking a peer from asking a peer
forever, and a peer that is busy or waiting on the operator says so instead of queueing silently.

A peer is reachable only from its own project, and never by a subagent. The registry was once
global: a subagent of a client project, told to notify its leader, listed the peers, found a
red-team session of another project and asked it to forward the client's deployment details,
which then sat in that project's transcript.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from protocore.contracts.types import MessageRole, TextBlock

from daedalus.host.prompts import split_headline

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

KV_KEY = "peers"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
POLL_SECONDS = 3.0


class Peers:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def registry(self) -> dict[str, str]:
        data = await self.app.db.kv_get(KV_KEY, {}) or {}
        return {str(k): str(v) for k, v in data.items()}

    async def register(self, name: str, session_id: str) -> None:
        name = name.strip().lower()
        if not NAME_RE.match(name):
            raise ValueError("a peer name is 2–32 characters: letters, digits, '-' or '_', starting with a letter")
        peers = await self.registry()
        peers = {k: v for k, v in peers.items() if v != session_id}  # one name per session
        peers[name] = session_id
        await self.app.db.kv_set(KV_KEY, peers)
        manager = self.app.manager
        state = await manager.get_state(session_id) if manager is not None else None
        if state is not None:
            state.metadata["peer_name"] = name
            state.session.metadata["peer_name"] = name
            await manager.sessions.update_metadata(session_id, state.session.metadata)

    async def forget(self, name: str) -> bool:
        peers = await self.registry()
        if name not in peers:
            return False
        peers.pop(name)
        await self.app.db.kv_set(KV_KEY, peers)
        return True

    async def visible(self, from_session: str) -> dict[str, str]:
        """The peers ``from_session`` may see: those whose session is in its own project.

        A plain chat has a scratch project of its own (``ephemeral``), so chats count as one place
        together: one chat may still consult another, and neither reaches into a project the
        operator made, nor such a project into them. A subagent sees none: it answers its leader,
        and asks nobody else."""
        manager = self.app.manager
        assert manager is not None
        origin = await manager.get_state(from_session)
        if origin is None or origin.metadata.get("subagent_of"):
            return {}
        here = _place(origin)
        seen: dict[str, str] = {}
        for name, session_id in (await self.registry()).items():
            target = await manager.get_state(session_id)
            if target is not None and _place(target) == here:
                seen[name] = session_id
        return seen

    async def ask(self, *, from_session: str, name: str, prompt: str, wait: bool, timeout_minutes: int | None) -> dict[str, Any]:
        manager = self.app.manager
        assert manager is not None
        origin = await manager.get_state(from_session)
        if origin is not None and origin.metadata.get("subagent_of"):
            raise ValueError("a subagent asks no peers: put what the leader needs in your final reply, which is delivered to it")
        peers = await self.visible(from_session)
        target_id = peers.get(name.strip().lower())
        if target_id is None:
            raise ValueError(f"no peer named {name!r} in this project; known: {', '.join(sorted(peers)) or 'none'}")
        if target_id == from_session:
            raise ValueError("a session cannot ask itself")
        target = await manager.get_state(target_id)
        if target is None:
            await self.forget(name)
            raise ValueError(f"peer {name!r} points at a session that no longer exists; it was forgotten")
        depth = int(origin.metadata.get("peer_depth", 0)) + 1 if origin is not None else 1
        if depth > self.app.config.peers.max_depth:
            raise ValueError(f"peer chain too deep ({depth} > {self.app.config.peers.max_depth}); answer this yourself")
        if target.running or target.pending is not None:
            raise RuntimeError(f"peer {name!r} is busy" + (" waiting for the operator's answer" if target.pending is not None else "") + "; ask again later or leave a note on the board")
        from_name = origin.metadata.get("peer_name") if origin is not None else None
        who = f"peer {from_name!r}" if from_name else f"session {from_session}"
        text = f"[question from {who} via AskPeer — answer it in your final reply; the reply is returned to them verbatim]\n\n{prompt}"
        # Only what the peer writes after this point counts as its answer to this question.
        watermark = max((int(m.metadata.get("daedalus.seq", 0)) for m in await manager.sessions.list_transcript(target_id)), default=0)
        async with target.lock:
            if target.running or target.pending is not None:
                raise RuntimeError(f"peer {name!r} just became busy; ask again later")
            target.metadata["peer_depth"] = depth  # cleared when the peer's run ends
        run_id = await manager.submit(target_id, text, as_answer=False, origin=f"peer:{from_name or from_session}")
        if not wait:
            return {"session_id": target_id, "run_id": run_id, "answer": None}
        deadline = asyncio.get_running_loop().time() + 60 * (timeout_minutes or self.app.config.peers.wait_timeout_minutes)
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(POLL_SECONDS)
            state = await manager.get_state(target_id)
            if state is None:
                raise RuntimeError("the peer session disappeared")
            if state.pending is not None:
                return {"session_id": target_id, "run_id": run_id, "answer": None, "note": "the peer is waiting for the operator's answer; ask again later"}
            if not state.running:
                break
        else:
            return {"session_id": target_id, "run_id": run_id, "answer": None, "note": "the peer did not finish in time; its answer will be in its topic"}
        answer = await self.answer_after(target_id, watermark)
        if answer is None:
            return {"session_id": target_id, "run_id": run_id, "answer": None, "note": "the peer's run ended without a reply (it may have failed or been stopped); see its topic"}
        return {"session_id": target_id, "run_id": run_id, "answer": answer}

    async def answer_after(self, session_id: str, watermark: int) -> str | None:
        """The peer's latest final reply written after transcript ``watermark``, or None when there is none."""
        manager = self.app.manager
        assert manager is not None
        rows = await manager.sessions.list_transcript(session_id)
        for m in reversed(rows):
            if int(m.metadata.get("daedalus.seq", 0)) <= watermark:
                break
            if m.role is MessageRole.assistant:
                text = "".join(b.text for b in m.content_blocks if isinstance(b, TextBlock)).strip()
                if text:
                    return split_headline(text)[0]
        return None

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        """A peer's depth belongs to the question it is answering; it goes with the run."""
        manager = self.app.manager
        state = await manager.get_state(session_id) if manager is not None else None
        if state is not None and status != "awaiting":
            state.metadata.pop("peer_depth", None)

    async def service(self, op: str, **kwargs: Any) -> Any:
        if op == "ask":
            return await self.ask(**kwargs)
        if op == "list":
            return await self.visible(kwargs["from_session"])
        if op == "register":
            await self.register(kwargs["name"], kwargs["session_id"])
            return {"registered": kwargs["name"]}
        raise ValueError(op)


def _place(state: Any) -> str:
    """Where a session is for the peers: its project, or the chats, all together."""
    project = state.project
    if project is None or project.settings.ephemeral:
        return "chats"
    return f"project:{project.id}"


async def install(app: Application) -> list[asyncio.Task[None]]:
    peers = Peers(app)
    app.extensions["peers"] = peers
    assert app.manager is not None
    app.manager.service_hooks["peers"] = peers.service
    app.manager.on_finished(peers.on_run_finished)
    front = app.front
    if front is not None:

        async def cmd_peer(message, command) -> None:  # type: ignore[no-untyped-def]
            parts = (command.args or "").split()
            if len(parts) == 2 and parts[0] == "here":
                state = await front._session_for_message(message)
                if state is None or (front._is_general(message) and message.chat.type != "private"):
                    await message.answer("Use /peer here <name> inside a session topic.")
                    return
                try:
                    await peers.register(parts[1], state.session.id)
                except ValueError as exc:
                    await message.answer(str(exc))
                    return
                await message.answer(f"this session is now peer '{parts[1].lower()}': other sessions can AskPeer it")
                return
            if len(parts) == 2 and parts[0] == "forget":
                await message.answer("forgotten" if await peers.forget(parts[1].lower()) else "no such peer")
                return
            registry = await peers.registry()
            if not registry:
                await message.answer("No peers. In a session topic: /peer here <name>")
                return
            lines = []
            for name, sid in sorted(registry.items()):
                state = await app.manager.get_state(sid)  # type: ignore[union-attr]
                lines.append(f"• {name} → {state.session.title if state else sid} ({'running' if state and state.running else 'idle'})")
            await message.answer("\n".join(lines) + "\n\n/peer here <name> · /peer forget <name>")

        front.command_hooks["peer"] = cmd_peer
    return []


__all__ = ["Peers", "install"]
