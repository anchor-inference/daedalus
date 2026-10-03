"""Inbound events: a loopback inbox, authenticated webhooks, and standing intents.

Anything on this box can hand the agent a message (``POST /api/inbound``); GitHub, CI or
any service with a shared secret can post events (``POST /webhooks/<provider>``); and the
agent can register standing intents — "when an inbound event mentions X, do Y" — that
fire as runs with a cooldown, a fire budget and an expiry. Every inbound event is echoed
in the chat and recorded in the inbox, so no channel into the agent is invisible.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import multiprocessing
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiosqlite

from daedalus.extensions.notifications import Draft, Tone

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

PAYLOAD_MAX_CHARS = 2000
PAYLOAD_MAX_KEYS = 400
DELIVERIES_KEEP_DAYS = 14
PATTERN_MAX_CHARS = 500
ACTION_MAX_CHARS = 4000
INTENT_MATCH_SECONDS = 2.0
"""A pattern that has not matched by then is a pattern that never will: the intent is disabled."""
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")
KV_SESSION_PREFIX = "inbound_session:"


def flatten_payload(value: Any, prefix: str = "", *, limit: int = PAYLOAD_MAX_CHARS) -> str:
    """``key.path: value`` lines from a JSON payload, capped so a webhook cannot flood the prompt."""
    lines: list[str] = []
    total = 0
    visited = 0

    def walk(v: Any, path: str) -> None:
        nonlocal total, visited
        visited += 1
        if total > limit or visited > PAYLOAD_MAX_KEYS:
            return
        if isinstance(v, dict):
            for k, item in list(v.items())[:PAYLOAD_MAX_KEYS]:
                walk(item, f"{path}.{k}" if path else str(k))
        elif isinstance(v, list):
            for i, item in enumerate(v[:20]):
                walk(item, f"{path}[{i}]")
        else:
            text = str(v).replace("\n", " ")
            if text.strip():
                lines.append(f"{path}: {text[:300]}")
                total += len(lines[-1]) + 1

    walk(value, prefix)
    out = "\n".join(lines)
    return out[:limit] + ("\n…" if len(out) > limit else "")


def _dig(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def webhook_facts(event: str, payload: Any) -> dict[str, str]:
    """What a project's watches match on, read from a GitHub-style payload: the repository, the
    action, a pull request's or a CI run's outcome, the branch and a title. Every value is a short
    string; what a payload does not say is left out.

    A pull request closed with ``merged`` is reported as ``merged``, since that is the outcome anyone
    waits for. A CI run's conclusion is empty while it runs, so only a finished run has one.
    """
    if not isinstance(payload, dict):
        return {}
    facts: dict[str, Any] = {
        "repo": _dig(payload, "repository", "full_name") or _dig(payload, "project", "path_with_namespace"),
        "repository_id": _dig(payload, "repository", "id"),
        "action": payload.get("action"),
    }
    if event == "pull_request":
        pull = payload.get("pull_request") or {}
        merged = bool(_dig(pull, "merged"))
        facts["conclusion"] = "merged" if payload.get("action") == "closed" and merged else payload.get("action")
        facts["branch"] = _dig(pull, "head", "ref")
        facts["head_sha"] = _dig(pull, "head", "sha")
        facts["title"] = _dig(pull, "title")
    elif event in ("check_suite", "check_run", "workflow_run"):
        run = payload.get(event) or {}
        facts["conclusion"] = _dig(run, "conclusion")
        facts["branch"] = _dig(run, "head_branch") or _dig(run, "check_suite", "head_branch")
        facts["head_sha"] = _dig(run, "head_sha") or _dig(run, "check_suite", "head_sha")
        facts["run_id"] = _dig(run, "id")
        facts["check_name"] = _dig(run, "name") or _dig(run, "display_title")
        facts["title"] = _dig(run, "name") or _dig(run, "display_title")
    elif event == "status":
        state = payload.get("state")
        facts["conclusion"] = state if state != "pending" else None
        branches = payload.get("branches") or []
        facts["branch"] = _dig(branches[0], "name") if branches and isinstance(branches[0], dict) else None
        facts["head_sha"] = payload.get("sha")
        facts["run_id"] = payload.get("id")
        facts["check_name"] = payload.get("context")
        facts["title"] = payload.get("context")
    return {key: str(value)[:200] for key, value in facts.items() if isinstance(value, (str, int)) and str(value)}


def verify_signature(scheme: str, secret: str, body: bytes, headers: dict[str, str]) -> bool:
    if not secret:
        return False
    if scheme == "github":
        signature = headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.encode("utf-8", "replace"), expected.encode())
    auth = headers.get("authorization", "")
    scheme_token, _, credential = auth.partition(" ")
    return scheme_token.lower() == "bearer" and hmac.compare_digest(credential.strip().encode("utf-8", "replace"), secret.encode())


def _regex_search(pattern: str, text: str, out: Any) -> None:
    try:
        out.value = 1 if re.search(pattern, text, re.IGNORECASE) else 0
    except re.error:
        out.value = -1


def search_bounded(pattern: str, text: str, seconds: float = INTENT_MATCH_SECONDS) -> bool | None:
    """``re.search`` with a wall-clock bound, in a throwaway process so a backtracking blowup cannot stall the bot.

    Returns True/False, or ``None`` when the bound was hit (or the pattern does not compile).
    """
    ctx = multiprocessing.get_context("fork")
    flag = ctx.Value("i", -2)
    proc = ctx.Process(target=_regex_search, args=(pattern, text, flag), daemon=True)
    proc.start()
    proc.join(seconds)
    if proc.is_alive():
        proc.kill()
        proc.join(1.0)
        return None
    return None if flag.value < 0 else bool(flag.value)


class Inbound:
    def __init__(self, app: Application) -> None:
        self.app = app

    # -- delivery ---------------------------------------------------------------------

    async def resolve_session(self, ref: str | None, *, default_title: str) -> Any:
        """A session by id or exact title; otherwise the standing session with ``default_title`` (created once)."""
        manager = self.app.manager
        assert manager is not None
        if ref:
            state = await manager.get_state(ref)
            if state is not None:
                return state
            for s in await manager.list_sessions(limit=500):
                if s["title"] == ref:
                    return await manager.get_state(s["id"])
        remembered = await self.app.db.kv_get(KV_SESSION_PREFIX + default_title, None)
        if remembered:
            state = await manager.get_state(str(remembered))
            if state is not None:
                return state
        for s in await manager.list_sessions(limit=500):
            if s["title"] == default_title and s.get("metadata", {}).get("inbound"):
                await self.app.db.kv_set(KV_SESSION_PREFIX + default_title, s["id"])
                return await manager.get_state(s["id"])
        state = await self.app.create_session(default_title, metadata={"unattended": True, "inbound": True})
        await self.app.db.kv_set(KV_SESSION_PREFIX + default_title, state.session.id)
        return state

    async def deliver(self, *, source: str, text: str, session_ref: str | None, default_title: str, prompt: str = "") -> dict[str, Any]:
        manager = self.app.manager
        assert manager is not None
        state = await self.resolve_session(session_ref, default_title=default_title)
        front = self.app.front
        if front is not None:
            outbox = await front.outbox_for_session(state.session.id)
            if outbox is not None:
                try:
                    await outbox.send_text(f"📨 inbound from {source}\n\n{text[:1500]}", markdown=False)
                except Exception:  # noqa: BLE001
                    logger.warning("inbound echo failed", exc_info=True)
        # Intents first: one that lives in the receiving session rides along with the event instead of queueing behind it.
        folded = await self.match_intents(source, text, fold_into=state.session.id)
        body = (prompt.strip() + "\n\n" if prompt.strip() else "") + f"[inbound event from {source}]\n{text}"
        if folded:
            body += "\n\n" + "\n\n".join(folded)
        run_id = await manager.submit(state.session.id, body, as_answer=False, origin=f"inbound:{source}")
        await self._post("inbound", f"Inbound from {source}", text[:2000], quiet=True, session_id=state.session.id, run_id=run_id or None)
        return {"session_id": state.session.id, "run_id": run_id}

    async def record_delivery(self, provider: str, delivery_id: str) -> bool:
        """True when the delivery is new; an INSERT OR IGNORE is the atomic dedupe."""
        async with self.app.db.transaction() as conn:
            return await self.record_delivery_in(conn, provider, delivery_id)

    async def record_delivery_in(self, conn: aiosqlite.Connection, provider: str, delivery_id: str) -> bool:
        """Keep dedupe in the caller's transaction with the accepted event and CI observation."""
        cursor = await conn.execute(
            "INSERT OR IGNORE INTO webhook_deliveries(provider, delivery_id, at) VALUES (?, ?, ?)",
            (provider, delivery_id, datetime.now(UTC).isoformat()),
        )
        fresh = bool(cursor.rowcount)
        cutoff = (datetime.now(UTC) - timedelta(days=DELIVERIES_KEEP_DAYS)).isoformat()
        await conn.execute("DELETE FROM webhook_deliveries WHERE at < ?", (cutoff,))
        return fresh

    async def forget_delivery(self, provider: str, delivery_id: str) -> None:
        """A delivery that could not be handled is not a delivery: the sender's retry must get through."""
        await self.app.db.execute("DELETE FROM webhook_deliveries WHERE provider = ? AND delivery_id = ?", (provider, delivery_id))

    # -- intents ------------------------------------------------------------------------

    async def create_intent(self, *, pattern: str, action: str, session_id: str | None, cooldown_minutes: int, max_fires: int, expires_in_hours: int | None, created_by: str | None) -> dict[str, Any]:
        if len(pattern) > PATTERN_MAX_CHARS or len(action) > ACTION_MAX_CHARS:
            raise ValueError(f"pattern is limited to {PATTERN_MAX_CHARS} characters and action to {ACTION_MAX_CHARS}")
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"pattern is not a valid regular expression: {exc}") from exc
        if _NESTED_QUANTIFIER.search(pattern):
            raise ValueError("pattern nests a quantifier inside a quantified group (e.g. (a+)+), which can take forever to match; simplify it")
        intent_id = uuid.uuid4().hex[:8]
        expires = (datetime.now(UTC) + timedelta(hours=expires_in_hours)).isoformat() if expires_in_hours else None
        await self.app.db.execute(
            "INSERT INTO intents(id, pattern, action, session_id, cooldown_minutes, max_fires, expires_at, created_by_session, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (intent_id, pattern, action, session_id, max(1, cooldown_minutes), max(1, max_fires), expires, created_by, datetime.now(UTC).isoformat()),
        )
        return {"id": intent_id, "pattern": pattern, "expires_at": expires}

    async def list_intents(self) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.app.db.fetchall("SELECT * FROM intents ORDER BY created_at DESC")]

    async def delete_intent(self, intent_id: str) -> bool:
        row = await self.app.db.fetchone("SELECT id FROM intents WHERE id = ?", (intent_id,))
        if row is None:
            return False
        await self.app.db.execute("DELETE FROM intents WHERE id = ?", (intent_id,))
        return True

    async def match_intents(self, source: str, text: str, *, fold_into: str | None = None) -> list[str]:
        """Fire every enabled intent whose pattern matches the event text, within cooldown, budget and expiry.

        Returns the prompts of matched intents whose session is ``fold_into`` (the caller adds them
        to the event it is about to submit there); every other match is submitted here. Without
        ``fold_into`` the return value is the list of fired intent ids.
        """
        now = datetime.now(UTC)
        fired: list[str] = []
        folded: list[str] = []
        manager = self.app.manager
        if manager is None:
            return fired
        for row in await self.app.db.fetchall("SELECT * FROM intents WHERE enabled = 1"):
            if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < now:
                await self.app.db.execute("UPDATE intents SET enabled = 0 WHERE id = ?", (row["id"],))
                continue
            if int(row["fired_count"]) >= int(row["max_fires"]):
                await self.app.db.execute("UPDATE intents SET enabled = 0 WHERE id = ?", (row["id"],))
                await self._post("intent_exhausted", f"Standing intent '{row['pattern']}' used its fire budget", row["action"][:500])
                continue
            if row["last_fired_at"] and now - datetime.fromisoformat(row["last_fired_at"]) < timedelta(minutes=int(row["cooldown_minutes"])):
                continue
            matched = await asyncio.to_thread(search_bounded, row["pattern"], text)
            if matched is None:
                await self.app.db.execute("UPDATE intents SET enabled = 0 WHERE id = ?", (row["id"],))
                await self._post("intent_disabled", f"Standing intent '{row['pattern'][:80]}' disabled", f"its pattern did not finish matching within {INTENT_MATCH_SECONDS:.0f} s (or no longer compiles); rewrite it with IntentCreate", tone="warning")
                continue
            if not matched:
                continue
            session = row["session_id"] or row["created_by_session"]
            prompt = f"[standing intent {row['id']} matched an inbound event from {source}: pattern {row['pattern']!r}]\n\n{row['action']}\n\nThe event:\n{text[:PAYLOAD_MAX_CHARS]}"
            if fold_into and session == fold_into:
                folded.append(f"[standing intent {row['id']} matched this event: pattern {row['pattern']!r}]\n{row['action']}")
                await self.app.db.execute("UPDATE intents SET fired_count = fired_count + 1, last_fired_at = ? WHERE id = ?", (now.isoformat(), row["id"]))
                fired.append(row["id"])
                continue
            state = await manager.get_state(session) if session else None
            try:
                if state is not None and not state.running and state.pending is None:
                    await manager.submit(state.session.id, prompt, as_answer=False, origin="intent")
                else:
                    scheduler = self.app.extensions.get("scheduler")
                    if scheduler is None:
                        await self._post("intent_deferred", f"Standing intent '{row['pattern'][:80]}' matched but could not run", "its session is busy or gone and no task session could be started; the intent stays armed")
                        continue
                    await scheduler.run_task_session(f"[intent] {row['pattern'][:30]}", prompt, self.app.settings.workspaces_dir / f"intent-{row['id']}", {"intent_id": row["id"], "unattended": True}, origin="intent")  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                await self._post("intent_failed", f"Standing intent '{row['pattern']}' could not run", f"{type(exc).__name__}: {exc}", tone="warning")
                continue
            await self.app.db.execute("UPDATE intents SET fired_count = fired_count + 1, last_fired_at = ? WHERE id = ?", (now.isoformat(), row["id"]))
            fired.append(row["id"])
            await self._post("intent_fired", f"Standing intent fired: {row['pattern']}", row["action"][:500], session_id=session)
        return folded if fold_into else fired

    async def _post(self, kind: str, title: str, body: str = "", *, tone: Tone = "info", quiet: bool = False, session_id: str | None = None, run_id: str | None = None) -> None:
        if self.app.notifications is not None:
            await self.app.notifications.post(Draft("system", title, body, kind=kind, tone=tone, level="quiet" if quiet else None, session_id=session_id, run_id=run_id, source="inbound"))

    async def service(self, op: str, **kwargs: Any) -> Any:
        if op == "create":
            return await self.create_intent(**kwargs)
        if op == "list":
            return await self.list_intents()
        if op == "delete":
            return await self.delete_intent(kwargs["intent_id"])
        raise ValueError(op)


async def install(app: Application) -> list[asyncio.Task[None]]:
    inbound = Inbound(app)
    app.extensions["inbound"] = inbound
    assert app.manager is not None
    app.manager.service_hooks["intents"] = inbound.service
    front = app.front
    if front is not None:

        async def cmd_intents(message, command) -> None:  # type: ignore[no-untyped-def]
            parts = (command.args or "").split()
            if len(parts) == 2 and parts[0] == "delete":
                await message.answer("deleted" if await inbound.delete_intent(parts[1]) else "no such intent")
                return
            items = await inbound.list_intents()
            if not items:
                await message.answer("No standing intents. The agent creates them with IntentCreate ('when an inbound event mentions X, do Y').")
                return
            lines = [f"{'✓' if i['enabled'] else '✗'} {i['id']} /{i['pattern']}/ → {i['action'][:60]} · fired {i['fired_count']}/{i['max_fires']}" for i in items]
            await message.answer("\n".join(lines) + "\n\n/intents delete <id>")

        front.command_hooks["intents"] = cmd_intents
    return []


__all__ = ["Inbound", "flatten_payload", "install", "search_bounded", "verify_signature", "webhook_facts"]
