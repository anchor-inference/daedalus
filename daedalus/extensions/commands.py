"""Slash commands for the Mini App: the same commands the chat knows, run for a named session.

Every command here runs on the session manager and on the extensions directly — never through
the Telegram front — so an installation with no bot token answers exactly as one with a chat.
The chat handlers in the Telegram transport are a second rendering of the same actions, not the
implementation of them; only the commands whose subject *is* the chat (binding a forum, moving
the private chat's window, closing a topic) belong to the transport alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from daedalus.config import NO_MODEL_MESSAGE, REASONING_EFFORTS
from daedalus.doctor import DoctorContext, render_text, run_checks
from daedalus.extensions.notifications import format_entries
from daedalus.host.prompts import DEFAULT_RULES
from daedalus.security import redact
from daedalus.transport.telegram.front import SESSION_LIST_LIMIT, yagni_argument

if TYPE_CHECKING:
    from daedalus.app import Application


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    args: str
    description: str
    scope: str = "session"
    """``session``: acts on the open session; ``global``: the same everywhere."""
    confirm: bool = False
    """The Mini App asks before running it (destructive or expensive)."""


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("compact", "[focus]", "replace this session's history with a summary", confirm=True),
    CommandSpec("clear", "", "start over with an empty history; project files, brief and settings stay", confirm=True),
    CommandSpec("stop", "", "stop the current run"),
    CommandSpec("model", "[preset | provider/model | default]", "the model for this session (no argument: list)"),
    CommandSpec("thinking", "on|off|low|medium|high|xhigh", "thinking for this session"),
    CommandSpec("mode", "[quick|deep|careful|default]", "limits and rules for this session (no argument: list)"),
    CommandSpec("yagni", "[on|off]", "YAGNI rules for this session: the smallest change that does the job (no argument: switch)"),
    CommandSpec("rename", "<title>", "rename this session and its topic"),
    CommandSpec("cap", "<usd | none>", "spend cap for this session over all of its runs"),
    CommandSpec("allow", "<key>", "let one call the policy refused through, once (the key is in the refusal)", confirm=True),
    CommandSpec("brief", "[text]", "the standing brief in this session's system prompt (no argument: show)"),
    CommandSpec("usage", "", "spend today and in this session"),
    CommandSpec("status", "", "what is running", scope="global"),
    CommandSpec("sessions", "", "every session", scope="global"),
    CommandSpec("new", "<title>", "a new session on the site", scope="global"),
    CommandSpec("delete", "<id>", "delete a session; shared project files stay", scope="global", confirm=True),
    CommandSpec("cleanup", "[confirm]", "delete every session whose topic is closed", scope="global", confirm=True),
    CommandSpec("inbox", "[all|clear]", "the inbox", scope="global"),
    CommandSpec("board", "[all]", "the task board", scope="global"),
    CommandSpec("schedules", "", "scheduled tasks", scope="global"),
    CommandSpec("schedule", "run|on|off|delete <id>", "act on a scheduled task", scope="global"),
    CommandSpec("intents", "[delete <id>]", "standing intents", scope="global"),
    CommandSpec("peer", "here <name> | list | forget <name>", "name this session as a peer", scope="session"),
    CommandSpec("loop", "[10m] <instruction> | status | pause | resume | stop | remove", "this session's loop: a standing task it is woken up for", scope="session"),
    CommandSpec("heartbeat", "[on|off|run]", "the periodic check", scope="global"),
    CommandSpec("balance", "", "provider balances", scope="global"),
    CommandSpec("doctor", "[fix]", "health checks", scope="global"),
    CommandSpec("prompt", "", "the working rules", scope="global"),
    CommandSpec("settings", "", "a summary of the configuration", scope="global"),
    CommandSpec("verbosity", "0|1|2", "how much of a run the chat shows", scope="global"),
    CommandSpec("approval", "manual|auto", "how self-change proposals are approved", scope="global"),
    CommandSpec("rebuild", "", "rebuild and restart from main", scope="global", confirm=True),
    CommandSpec("rollback", "[n]", "roll back to a known-good build", scope="global", confirm=True),
)
BY_NAME = {c.name: c for c in COMMANDS}

TELEGRAM_ONLY = frozenset({"bind", "use", "close"})
"""Commands whose subject is the chat itself. They are never advertised here; naming them keeps
the refusal specific instead of "unknown command"."""

NEEDS_EXTENSION: dict[str, str] = {
    "loop": "loops",
    "inbox": "inbox",
    "board": "board",
    "schedules": "scheduler",
    "schedule": "scheduler",
    "intents": "inbound",
    "peer": "peers",
    "heartbeat": "heartbeat",
    "balance": "balance",
    "rebuild": "selfdev",
    "rollback": "selfdev",
}
"""A command is only as present as the extension that carries it."""


def available(app: Application) -> tuple[CommandSpec, ...]:
    """The commands this installation can actually run, so the palette never offers a refusal."""
    return tuple(c for c in COMMANDS if c.name not in NEEDS_EXTENSION or NEEDS_EXTENSION[c.name] in app.extensions)


def parse(line: str) -> tuple[str, str] | None:
    """``/name args`` → (name, args); None when the line is not a command."""
    text = line.strip()
    if not text.startswith("/") or len(text) < 2:
        return None
    head, _, rest = text[1:].partition(" ")
    name = head.split("@", 1)[0].lower()
    return (name, rest.strip()) if name.isidentifier() else None


def _cost_words(usd: float | None, unmetered: int) -> str:
    """Spend for a summary line: a priced total, and an honest count of calls with no known price."""
    if usd is None:
        return " · cost unknown (no price for these calls)" if unmetered else ""
    text = f" · ${usd:.4f}"
    if unmetered:
        text += f" (+{unmetered} unmetered call{'s' if unmetered != 1 else ''})"
    return text


async def run_command(app: Application, session_id: str, line: str) -> str:  # noqa: C901, PLR0911, PLR0912, PLR0915 — one branch per command reads better than a table of thirty callbacks
    """Run one slash command for ``session_id`` and return what the chat would have shown."""
    parsed = parse(line)
    if parsed is None:
        raise ValueError("not a command")
    name, args = parsed
    if name in TELEGRAM_ONLY:
        raise RuntimeError(f"/{name} acts on the Telegram chat itself and has nothing to act on here")
    spec = BY_NAME.get(name)
    if spec is None:
        raise KeyError(name)
    manager = app.manager
    front = app.front
    assert manager is not None
    state = await manager.get_state(session_id)
    if state is None:
        raise KeyError("no such session")

    # -- the session's history and its run ------------------------------------------
    if name == "compact":
        if state.running:
            return "Stop the run first."
        summary = await manager.compact(session_id, args)
        return "🗜 History compacted. The session continues from this summary.\n\n" + summary
    if name == "clear":
        if state.running:
            return "Stop the run first."
        result = await manager.clear_history(session_id)
        return f"🧹 History cleared: {result['dropped']} message(s) left the working history. The project files, the brief and the session's settings stay; the transcript keeps the old turns."
    if name == "stop":
        return "Stopping…" if await manager.stop(session_id) else "Nothing is running in this session."

    # -- what the session runs with -------------------------------------------------
    if name == "model":
        found = app.config.default_preset()
        if found is None:
            return NO_MODEL_MESSAGE
        default_id, default = found
        if not args:
            lines = [f"  {pid} — {p.display(pid)}{'  (default)' if pid == default_id else ''}" for pid, p in app.config.presets.items()]
            return f"default: {default.display(default_id)}\nmodels:\n" + "\n".join(lines) + "\nusage: /model <preset-id> · /model default · /model provider/model-id"
        if args in app.config.presets:
            await manager.set_model(session_id, preset=args)
            return f"Session model: {app.config.presets[args].display(args)} (from the next model call)"
        if args in ("default", "reset"):
            await manager.set_model(session_id, clear=True)
            return "Session model: back to the global default"
        provider, _, model_name = args.partition("/")
        if not model_name or provider not in manager.providers.available():
            return "usage: /model <preset-id> | provider/model-id | default"
        await manager.set_model(session_id, model_name=model_name, provider=provider)
        return f"Session model: {provider}/{model_name} (from the next model call)"
    if name == "thinking":
        arg = args.lower()
        found = app.config.default_preset()
        if found is None:
            return NO_MODEL_MESSAGE
        default_id, default = found
        if arg in ("on", "off"):
            thinking, effort = arg == "on", None
        elif arg in REASONING_EFFORTS:
            thinking, effort = True, arg
        else:
            return (
                f"{default.display(default_id)}: thinking={default.thinking} effort={default.reasoning_effort}\n"
                f"usage: /thinking on|off|{'|'.join(REASONING_EFFORTS)}"
            )
        await manager.set_model(session_id, thinking=thinking, reasoning_effort=effort)
        return f"Session thinking={thinking} effort={effort or default.reasoning_effort}"
    if name == "mode":
        arg = args.lower()
        if not arg:
            current = state.metadata.get("mode") or "default"
            lines = [
                f"  {mode_name} — {m.description or ''} (iterations {m.max_iterations or app.config.limits.max_iterations}, cap ${m.usd_per_run if m.usd_per_run is not None else app.config.limits.usd_per_run})"
                for mode_name, m in app.config.modes.items()
            ]
            return f"mode: {current}\navailable:\n" + "\n".join(lines) + "\nusage: /mode <name> · /mode default"
        try:
            chosen = await manager.set_mode(session_id, None if arg in ("default", "off", "reset") else arg)
        except ValueError as exc:
            return str(exc)
        return f"mode: {chosen or 'default'} (applies from the next run)"
    if name == "yagni":
        wanted = yagni_argument(args, bool(state.metadata.get("yagni")))
        if wanted is None:
            return "usage: /yagni [on|off]"
        await manager.set_yagni(session_id, wanted)
        return f"YAGNI: {'on' if wanted else 'off'} (the next turn is told)"
    if name == "cap":
        if args.lower() in ("none", "off", "", "-"):
            await manager.set_session_cap(session_id, None)
            return "Session cap removed; the global limits still apply."
        try:
            cap = float(args)
        except ValueError:
            return "usage: /cap <usd> | none"
        await manager.set_session_cap(session_id, cap)
        spent, _ = await manager.spend(session_id=session_id)
        return f"Session cap: ${cap:.2f} (spent so far ${spent:.2f})."
    if name == "allow":
        try:
            result = await manager.grant(session_id, args, via="app")
        except ValueError as exc:
            return f"usage: /allow <key> — {exc}"
        approves = result.get("approves")
        what = f"{approves['tool']}: `{approves['text']}`" if approves else "a call this host has not seen refused yet (the key is taken on trust)"
        return f"Granted {result['key']} for {what}. The same call passes once within {result['expires_in_minutes']} minutes. Open grants: {', '.join(result['grants'])}."
    if name == "brief":
        if not args:
            current = str(state.metadata.get("brief") or "")
            return ("Brief:\n" + current) if current else "No brief. /brief <text> sets one; it lives in this session's system prompt."
        await manager.set_brief(session_id, args)
        return "Brief updated; it applies from the next run."

    # -- the sessions themselves ----------------------------------------------------
    if name == "rename":
        title = args.strip()
        if not title:
            return f"Current title: {state.session.title}\nusage: /rename <new title>"
        if front is not None:
            await front.rename_session(session_id, title)  # renames the topic with it, where there is one
        else:
            await manager.rename_session(session_id, title)
        return f"Renamed to: {title}"
    if name == "new":
        title = args.strip() or datetime.now(UTC).strftime("session %m-%d %H:%M")
        # Typed on the site, not sent as a Telegram command: the session stays on the site.
        # Pointing the private chat at it would deliver the answer somewhere the operator is not looking.
        created = await manager.create_session(title, metadata={"telegram_detached": True})
        return f"New session '{title}' ({created.session.id})."
    if name == "delete":
        target = args.strip()
        if not target:
            return "usage: /delete <session id>  (see /sessions)"
        if front is not None:
            await front.forget_session(target)  # while its topic row is still there to be read
        removed = await manager.delete_session(target)
        return f"Session {target} deleted. Shared project files were kept." if removed else "no such session"
    if name == "cleanup":
        closed = await manager.closed_topic_sessions()
        if not closed:
            return "No sessions with closed topics."
        if args.lower() != "confirm":
            return "Sessions whose topics are closed:\n" + "\n".join(f"- {s['title']} ({s['session_id']})" for s in closed) + "\n\n/cleanup confirm deletes the sessions; shared project files stay."
        removed = 0
        for s in closed:
            removed += int(await manager.delete_session(s["session_id"]))
        return f"Deleted {removed} session(s). Shared project files were kept."
    if name == "sessions":
        sessions = await manager.list_sessions(limit=SESSION_LIST_LIMIT)
        if not sessions:
            return "No sessions yet."
        current = await front.current_session_id() if front is not None and front.private_mode() else ""
        return "\n".join(
            f"{i}. {'▶' if s['status'] == 'running' else '❓' if s['status'] == 'waiting' else '·'} {s['title']} — {s['id']} ({s['status']})"
            + ("  ← the chat is writing here" if s["id"] == current else "")
            for i, s in enumerate(sessions, 1)
        )
    if name == "status":
        active = [s for s in await manager.list_sessions(limit=50) if s["status"] in ("running", "waiting")]
        if not active:
            return "Idle. No active runs."
        return "\n".join(f"{s['status']}: {s['title']} ({s['id']})" for s in active)
    if name == "usage":
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        row = await app.db.fetchone(
            "SELECT count(*) c, sum(input_tokens) i, sum(output_tokens) o, sum(cache_read_tokens) ch, sum(cost_usd) usd,"
            " sum(cost_usd IS NULL) unmetered FROM usage_events WHERE at >= ?",
            (today,),
        )
        text = f"today: {row['c'] or 0} calls · in {row['i'] or 0:,} · out {row['o'] or 0:,} · cached {row['ch'] or 0:,}"
        text += _cost_words(row["usd"], int(row["unmetered"] or 0))
        srow = await app.db.fetchone(
            "SELECT count(*) c, sum(input_tokens) i, sum(output_tokens) o, sum(cost_usd) usd, sum(cost_usd IS NULL) unmetered"
            " FROM usage_events WHERE session_id = ?",
            (session_id,),
        )
        text += f"\nthis session: {srow['c'] or 0} calls · in {srow['i'] or 0:,} · out {srow['o'] or 0:,}"
        text += _cost_words(srow["usd"], int(srow["unmetered"] or 0))
        if app.config.limits.usd_per_run > 0:
            text += f"\nper-run cap: ${app.config.limits.usd_per_run:.2f} (limits.usd_per_run)"
        return text

    # -- what the extensions carry --------------------------------------------------
    if name == "loop":
        loops = app.extensions.get("loops")
        if loops is None:
            return "loops are not installed"
        head = args.split(" ", 1)[0].lower() if args else "status"
        if head in ("", "status", "list"):
            loop = await loops.get(session_id)
            return ("Loop: " + loops.note(loop).lstrip("- ")) if loop else "No loop. /loop 10m <instruction> wakes this session every 10 minutes for it; /loop <instruction> lets the agent pace itself."
        if head in ("pause", "resume", "stop", "remove"):
            try:
                if head == "pause":
                    await loops.pause(session_id, "paused by the operator")
                elif head == "resume":
                    await loops.resume(session_id)
                elif head == "stop":
                    await loops.stop(session_id, "stopped by the operator")
                else:
                    await loops.remove(session_id)
            except ValueError as exc:
                return str(exc)
            return f"Loop {head}d." if head != "remove" else "Loop removed."
        interval = None
        text = args
        match = re.match(r"^(\d+)\s*(s|m|h|d)\s+(.+)$", args, re.S)
        if match:
            interval = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
            text = match.group(3)
        try:
            loop = await loops.create(session_id, instruction=text, mode="interval" if interval else "dynamic", interval_seconds=interval, max_runs=None, start_now=True)
        except ValueError as exc:
            return str(exc)
        return "Loop started: " + loops.note(loop).lstrip("- ")
    if name == "inbox":
        notifications = app.notifications
        if notifications is None:
            return "Notifications are not installed."
        if args.lower() == "clear":
            n = await notifications.mark_seen(everything=True)
            return f"marked {n} entr{'y' if n == 1 else 'ies'} as read"
        entries = (await notifications.list("all" if args.lower() == "all" else "unseen", limit=15))["entries"]
        if not entries:
            return "Inbox: nothing unread." if args.lower() != "all" else "Inbox is empty."
        text = format_entries(entries)
        if args.lower() != "all":
            await notifications.mark_seen([e["id"] for e in entries])
        return text
    if name == "board":
        board = app.extensions.get("board")
        if board is None:
            return "The board is not installed."
        tasks = await board.list(None, include_done=args.lower() == "all")
        return ("📋 Board\n" + board.render(tasks) + "\n\n/board all shows finished tasks too")[:4000]
    if name == "schedules":
        scheduler = app.extensions.get("scheduler")
        if scheduler is None:
            return "The scheduler is not installed."
        items = await scheduler.list()
        if not items:
            return "No scheduled tasks. The agent creates them with ScheduleCreate."
        lines = [
            f"{'✓' if s['enabled'] else '✗'} {s['id']} [{s.get('kind') or 'agent'}] {s['name']} — {('cron ' + s['cron']) if s['cron'] else 'once'}"
            f" · next {s['next_run_at'] or '-'}" + (f" · failures {s['failure_count']}" if s.get("failure_count") else "") + (" · running" if s["id"] in scheduler._active else "")
            for s in items
        ]
        return "\n".join(lines) + "\n\nOpen Schedules in the app to review and change an action."
    if name == "schedule":
        return "Open Schedules in the app to approve, change or run an action with a receipt."
    if name == "intents":
        inbound = app.extensions.get("inbound")
        if inbound is None:
            return "Inbound intents are not installed."
        parts = args.split()
        if len(parts) == 2 and parts[0] == "delete":
            return "deleted" if await inbound.delete_intent(parts[1]) else "no such intent"
        items = await inbound.list_intents()
        if not items:
            return "No standing intents. The agent creates them with IntentCreate ('when an inbound event mentions X, do Y')."
        return "\n".join(f"{'✓' if i['enabled'] else '✗'} {i['id']} /{i['pattern']}/ → {i['action'][:60]} · fired {i['fired_count']}/{i['max_fires']}" for i in items) + "\n\n/intents delete <id>"
    if name == "peer":
        peers = app.extensions.get("peers")
        if peers is None:
            return "Peers are not installed."
        parts = args.split()
        if len(parts) == 2 and parts[0] == "here":
            try:
                await peers.register(parts[1], session_id)
            except ValueError as exc:
                return str(exc)
            return f"this session is now peer '{parts[1].lower()}': other sessions can AskPeer it"
        if len(parts) == 2 and parts[0] == "forget":
            return "forgotten" if await peers.forget(parts[1].lower()) else "no such peer"
        registry = await peers.registry()
        if not registry:
            return "No peers. /peer here <name> names this session as one."
        lines = []
        for peer_name, peer_id in sorted(registry.items()):
            peer_state = await manager.get_state(peer_id)
            lines.append(f"• {peer_name} → {peer_state.session.title if peer_state else peer_id} ({'running' if peer_state is not None and peer_state.running else 'idle'})")
        return "\n".join(lines) + "\n\n/peer here <name> · /peer forget <name>"
    if name == "heartbeat":
        heartbeat = app.extensions.get("heartbeat")
        if heartbeat is None:
            return "The heartbeat is not installed."
        arg = args.lower()
        if arg in ("on", "off"):
            app.config.heartbeat.enabled = arg == "on"
            await app.save_config(app.config)
            return f"heartbeat {'on' if app.config.heartbeat.enabled else 'off'}" + ("" if heartbeat.read().strip() else " (HEARTBEAT.md is empty: fill it in Settings → Heartbeat)")
        if arg == "run":
            try:
                started = await heartbeat.fire(manual=True)
            except RuntimeError as exc:
                return f"cannot run: {exc}"
            return f"heartbeat started in session {started}"
        st = heartbeat.status()
        return (
            f"heartbeat: {'on' if st['enabled'] else 'off'}{'' if st['armed'] or not st['enabled'] else ' (file empty → idle)'} · every {st['interval_minutes']} min · "
            f"active {st['active_hours']} UTC · today {st['runs_today']}/{st['max_runs_per_day']} · last {st['last_run'] or 'never'}\n"
            "usage: /heartbeat on|off|run — the prompt is HEARTBEAT.md (Settings → Heartbeat)"
        )
    if name == "balance":
        monitor = app.extensions.get("balance")
        if monitor is None:
            return "The balance monitor is not installed."
        balances = await monitor.current()
        if not balances:
            return "No provider with a balance endpoint is configured."
        return "\n".join(f"{p}: {f'${b:.2f}' if b is not None else 'unavailable'}" for p, b in balances.items())

    # -- the installation itself ----------------------------------------------------
    if name == "doctor":
        ctx = DoctorContext(settings=app.settings, config=app.config, db=app.db, manager=manager, front=front, extensions=dict(app.extensions), extension_failures=dict(app.extension_failures), guard=app.guard, fix=args.lower() == "fix")
        return redact.redact(render_text(await run_checks(ctx)))
    if name == "prompt":
        rules = app.config.prompt.rules.strip() or DEFAULT_RULES.strip()
        return "Working rules" + (" (default)" if not app.config.prompt.rules.strip() else "") + ":\n" + rules
    if name == "settings":
        c = app.config
        found = c.default_preset()
        model_line = f"{found[1].display(found[0])} thinking={found[1].thinking} effort={found[1].reasoning_effort}" if found else f"none — {NO_MODEL_MESSAGE}"
        if front is None:
            chat = "no Telegram front; the app and the API are the way in"
        elif front.private_mode():
            chat = "one private chat, a window onto one session at a time (/sessions, /use)"
        else:
            chat = f"one topic per session in forum {c.telegram.forum_chat_id or 'not bound'}"
        return (
            f"model: {model_line}\n"
            f"fallback: {', '.join(c.model.chain) or 'none'}\n"
            f"self-change approval: {c.self_change.approval}, auto_rebuild={c.self_change.auto_rebuild}\n"
            f"limits: ${app.settings.usd_per_day}/day (env), {c.limits.max_iterations} iterations, tool timeout {c.limits.tool_timeout_seconds:.0f}s\n"
            f"balance thresholds: {c.balance.thresholds_usd} (every {c.balance.poll_seconds}s)\n"
            f"verbosity: {c.telegram.verbosity}\n"
            f"chat: {chat}"
        )
    if name == "verbosity":
        try:
            app.config.telegram.verbosity = max(0, min(2, int(args.strip())))
        except ValueError:
            return "usage: /verbosity 0|1|2"
        await app.save_config(app.config)
        return f"verbosity={app.config.telegram.verbosity}"
    if name == "approval":
        arg = args.strip()
        if arg not in ("manual", "auto"):
            return f"approval={app.config.self_change.approval}\nusage: /approval manual|auto"
        app.config.self_change.approval = arg
        await app.save_config(app.config)
        return f"approval={arg}"
    if name in ("rebuild", "rollback"):
        selfdev = app.extensions.get("selfdev")
        if selfdev is None:
            return "Self-development is not installed on this host."
        if name == "rebuild":
            return await selfdev.rebuild(args.strip() or "operator request")
        try:
            steps = int(args.strip()) if args.strip() else 0
        except ValueError:
            return "usage: /rollback [steps_back]"
        return await selfdev.rollback(steps)

    raise KeyError(name)  # a spec with no branch: the two lists are kept together above


__all__ = ["BY_NAME", "COMMANDS", "NEEDS_EXTENSION", "TELEGRAM_ONLY", "CommandSpec", "available", "parse", "run_command"]
