"""Heartbeat: a periodic unattended check whose instructions are one editable file.

``HEARTBEAT.md`` on the state volume is the prompt. When it is empty (or the feature is
off) nothing runs and nothing is spent. Otherwise, inside the active hours and at the
configured interval, the agent runs it in the standing "[heartbeat]" session and either
reports something worth the operator's attention or calls ``StaySilent`` — in which case
only the inbox records that the check happened. Its identity, last run and daily count
are persisted so a restart neither resets the cap nor opens a second topic.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.extensions.notifications import Draft

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

FILE_NAME = "HEARTBEAT.md"
TICK_SECONDS = 60
KV_KEY = "heartbeat_state"
MAX_TEXT_CHARS = 20_000
TEMPLATE = """# Heartbeat

This file is the prompt for the periodic unattended check. Leave it empty to switch the
heartbeat off. Write what to look at, and what counts as worth the operator's attention:

- check the mail inbox for anything urgent
- look at the status page of X; report only outages
- if the deploy from last night failed, say so

Say nothing when everything is routine: call StaySilent.
"""


def in_active_hours(now: datetime, window: str) -> bool:
    """``HH:MM-HH:MM`` in UTC; a window that wraps midnight (22:00-06:00) is allowed."""
    try:
        start_s, end_s = window.split("-")
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except ValueError:
        return True
    minutes = now.hour * 60 + now.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return True
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


class Heartbeat:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.last_run: datetime | None = None
        self.runs_today: tuple[str, int] = ("", 0)
        self.session_id: str | None = None
        self.active_run: str | None = None

    @property
    def path(self) -> Path:
        return self.app.settings.state_dir / FILE_NAME

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".md.tmp")
        tmp.write_text(text[:MAX_TEXT_CHARS], encoding="utf-8")
        tmp.replace(self.path)

    # -- persistence ----------------------------------------------------------------

    async def load(self) -> None:
        data = await self.app.db.kv_get(KV_KEY, {}) or {}
        self.session_id = data.get("session_id") or None
        self.last_run = datetime.fromisoformat(data["last_run"]) if data.get("last_run") else None
        today = data.get("runs_today") or ["", 0]
        self.runs_today = (str(today[0]), int(today[1]))
        manager = self.app.manager
        if self.session_id and manager is not None:
            state = await manager.get_state(self.session_id)
            if state is None:
                self.session_id = None
            elif state.running or state.pending is not None:
                self.active_run = state.run_id  # a run resumed from a snapshot is still ours

    async def save(self) -> None:
        await self.app.db.kv_set(
            KV_KEY,
            {"session_id": self.session_id, "last_run": self.last_run.isoformat() if self.last_run else None, "runs_today": list(self.runs_today)},
        )

    def status(self) -> dict[str, Any]:
        cfg = self.app.config.heartbeat
        text = self.read()
        return {
            "enabled": cfg.enabled,
            "armed": cfg.enabled and bool(text.strip()),
            "interval_minutes": cfg.interval_minutes,
            "active_hours": cfg.active_hours,
            "preset": cfg.preset,
            "max_runs_per_day": cfg.max_runs_per_day,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "runs_today": self.runs_today[1] if self.runs_today[0] == datetime.now(UTC).strftime("%Y-%m-%d") else 0,
            "session_id": self.session_id,
            "running": self.active_run is not None,
            "file": str(self.path),
            "text": text,
        }

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("heartbeat tick failed")
            await asyncio.sleep(TICK_SECONDS)

    def due(self, now: datetime | None = None) -> bool:
        cfg = self.app.config.heartbeat
        now = now or datetime.now(UTC)
        if not cfg.enabled or not self.read().strip() or self.active_run is not None:
            return False
        if not in_active_hours(now, cfg.active_hours):
            return False
        today = now.strftime("%Y-%m-%d")
        if self.runs_today[0] == today and self.runs_today[1] >= cfg.max_runs_per_day:
            return False
        if self.last_run is not None and now - self.last_run < timedelta(minutes=cfg.interval_minutes):
            return False
        manager = self.app.manager
        if manager is None or manager.budget_exceeded() is None:
            return manager is not None
        try:
            _preset_id, preset = self.app.config.preset(cfg.preset or None)
        except Exception:  # noqa: BLE001 — submit reports an unusable preset in its normal path
            return False
        # The key proxy's daily flag protects paid upstreams. A heartbeat on the operator's own
        # llama.cpp server, a free model or a subscription login cannot add to that bill and
        # remains useful when priced calls are paused.
        return manager.provider_costs_nothing(preset.provider, preset.model)

    async def tick(self) -> None:
        if self.due():
            await self.fire()

    async def fire(self, *, manual: bool = False) -> str:
        """Run the heartbeat now (the loop calls this when due; ``/heartbeat run`` forces it)."""
        manager = self.app.manager
        scheduler = self.app.extensions.get("scheduler")
        assert manager is not None
        text = self.read().strip()
        if not text:
            raise RuntimeError("HEARTBEAT.md is empty")
        now = datetime.now(UTC)
        prompt = (
            f"Heartbeat check at {now.strftime('%Y-%m-%d %H:%M UTC')}" + (" (started by the operator)" if manual else "") + ".\n\n"
            f"{text}\n\n"
            "This run is unattended. Do the checks above with the tools. Then either report what needs the "
            "operator's attention in a short final reply, or — when everything is routine — call StaySilent "
            "with a one-line note of what you checked and end. Never announce that there is nothing new."
        )
        preset = self.app.config.heartbeat.preset or None
        state = await manager.get_state(self.session_id) if self.session_id else None
        if state is not None and (state.running or state.pending is not None):
            raise RuntimeError("the previous heartbeat run is still active")
        if state is None:
            workspace = self.app.settings.workspaces_dir / "heartbeat"
            metadata: dict[str, Any] = {"heartbeat": True, "unattended": True}
            if scheduler is not None:
                state = await scheduler.run_task_session("[heartbeat]", prompt, workspace, metadata, preset=preset)  # type: ignore[attr-defined]
            else:
                state = await manager.create_session("[heartbeat]", workspace=workspace, metadata=metadata)
                if preset:
                    await manager.set_model(state.session.id, preset=preset)
                await manager.submit(state.session.id, prompt, [], as_answer=False, origin="heartbeat")
            self.session_id = state.session.id
        else:
            if preset:
                await manager.set_model(state.session.id, preset=preset)  # a preset changed in the settings applies to the next run
            await manager.submit(state.session.id, prompt, [], as_answer=False, origin="heartbeat")
        # The slot counts only once the run exists.
        today = now.strftime("%Y-%m-%d")
        self.runs_today = (today, (self.runs_today[1] if self.runs_today[0] == today else 0) + 1)
        self.last_run = now
        self.active_run = state.run_id
        await self.save()
        return state.session.id

    async def on_run_finished(self, session_id: str, run_id: str, status: str) -> None:
        if session_id != self.session_id or status == "awaiting":
            return  # an awaiting run stays active; the scheduler's timeout sweep answers it and the run ends later
        self.active_run = None
        notifications = self.app.notifications
        manager = self.app.manager
        services = manager.locator_services(session_id) if manager else None
        quiet = services is not None and services.extra.get("silent_run") == run_id
        if notifications is None:
            return
        if quiet:
            await notifications.post(Draft("reminder", "Heartbeat: quiet", str(services.extra.get("silent_note") or ""), kind="heartbeat", level="quiet", session_id=session_id, run_id=run_id, source="heartbeat"))  # type: ignore[union-attr]
        elif status == "completed":
            # The reply itself went to the heartbeat's topic when there is a chat to have one in.
            handled = frozenset({"telegram"}) if self.app.front is not None else frozenset()
            await notifications.post(Draft("reminder", "Heartbeat reported something", "The reply is in the heartbeat topic.", kind="heartbeat", session_id=session_id, run_id=run_id, source="heartbeat", handled=handled))
        else:
            await notifications.post(Draft("reminder", f"Heartbeat run {status}", "", kind="heartbeat", tone="warning", session_id=session_id, run_id=run_id, source="heartbeat"))


async def install(app: Application) -> list[asyncio.Task[None]]:
    heartbeat = Heartbeat(app)
    app.extensions["heartbeat"] = heartbeat
    assert app.manager is not None
    await heartbeat.load()
    app.manager.on_finished(heartbeat.on_run_finished)
    if not heartbeat.path.exists():
        try:
            heartbeat.write("")
        except OSError:
            logger.warning("could not create %s", heartbeat.path, exc_info=True)
    front = app.front
    if front is not None:

        async def cmd_heartbeat(message, command) -> None:  # type: ignore[no-untyped-def]
            arg = (command.args or "").strip().lower()
            cfg = app.config.heartbeat
            if arg in ("on", "off"):
                cfg.enabled = arg == "on"
                await app.save_config(app.config)
                await message.answer(f"heartbeat {'on' if cfg.enabled else 'off'}" + ("" if heartbeat.read().strip() else " (HEARTBEAT.md is empty: edit it in the Mini App → Settings → Heartbeat)"))
                return
            if arg == "run":
                try:
                    sid = await heartbeat.fire(manual=True)
                except RuntimeError as exc:
                    await message.answer(f"cannot run: {exc}")
                    return
                await message.answer(f"heartbeat started in session {sid}")
                return
            st = heartbeat.status()
            await message.answer(
                f"heartbeat: {'on' if st['enabled'] else 'off'}{'' if st['armed'] or not st['enabled'] else ' (file empty → idle)'} · every {st['interval_minutes']} min · "
                f"active {st['active_hours']} UTC · today {st['runs_today']}/{st['max_runs_per_day']} · last {st['last_run'] or 'never'}\n"
                "usage: /heartbeat on|off|run — the prompt is HEARTBEAT.md (Mini App → Settings → Heartbeat)"
            )

        front.command_hooks["heartbeat"] = cmd_heartbeat
    return [asyncio.create_task(heartbeat.loop(), name="heartbeat")]


__all__ = ["FILE_NAME", "TEMPLATE", "Heartbeat", "in_active_hours", "install"]
