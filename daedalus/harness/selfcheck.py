"""The session part of a CLI's self-check: one short session, driven the way a staff member's is.

The harness manager checks the version and the sign-in by asking the CLI; only a session shows that
the parts a staff member depends on still fit together after an update — the overlay files, the
hooks, the readiness gate, the team tools, a message getting through, a clean exit. So the check
launches the CLI in a folder of its own (a stable one, so its trust question is asked once), and
walks these steps, each recorded with what it saw:

``launch`` the terminal started with the launch's files · ``ready`` the CLI said it was ready (a
hook) · ``team`` the team tools loaded (``ptyd team-mcp`` said hello) · with the model turn:
``deliver`` a one-line prompt acknowledged, ``reply`` the turn ended and its ``Report`` came
through the team channel and was answered — the whole round trip, CLI → MCP → listener → host →
reply · ``exit`` the CLI left when asked, with exit code 0.

The one prompt costs a few tokens of the subscription, on the cheapest model; the operator agreed to
that, and ``harness.self_check_model_turn`` switches it off.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any

from daedalus.config import HarnessConfig
from daedalus.harness.contract import (
    DIAL_DIR,
    LAUNCH_DIR,
    CheckResult,
    CheckStep,
    EnvironmentPort,
    EventKind,
    HarnessAdapter,
    HookPost,
    Launch,
    LaunchSpec,
    StaffEvent,
)
from daedalus.harness.env import terminal_environment
from daedalus.harness.runtime import (
    GATE_ANSWERS_MAX,
    GATE_REPEAT_S,
    LAUNCH_COLS,
    LAUNCH_ROWS,
    RuntimeTerminal,
    port_range,
)
from daedalus.terminals.model import LaunchSpec as DaemonLaunch
from daedalus.terminals.model import Owner, TerminalError, TerminalSpec
from daedalus.terminals.service import Terminals

logger = logging.getLogger(__name__)

PROMPT = "This is a self-check. Call the Report tool with kind checkpoint and note self-check, then reply with the single word ready."
TURN_TIMEOUT_S = 180.0
FOLDER = ".cache/daedalus-selfcheck"
ACTOR = "agent:harness-self-check"
# How long a session end may trail the process's exit. Codex and OpenCode report none at all, and the
# check used to wait out the whole stop grace plus five seconds for one after they had already exited.
SESSION_END_AFTER_EXIT_S = 2.0


class _Steps:
    def __init__(self) -> None:
        self.steps: list[CheckStep] = []
        self.at = time.monotonic()

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        now = time.monotonic()
        self.steps.append(CheckStep(name, ok, detail[:500], int((now - self.at) * 1000)))
        self.at = now
        return ok


async def session_check(
    adapter: HarnessAdapter,
    terminals: Terminals,
    config: Callable[[], HarnessConfig],
    env: EnvironmentPort,
    model: str,
    model_turn: bool,
) -> CheckResult:
    """Run the session check in ``env``; see the module's description. Never raises for a failing
    CLI: a step that fails is the result."""
    cfg = config()
    started = time.monotonic()
    steps = _Steps()
    version = ""
    with contextlib.suppress(Exception):
        version = (await adapter.installed(env)).version
    if not env.home:
        steps.add("launch", False, "the environment did not say where its home is")
        return CheckResult(False, tuple(steps.steps), version, 0)
    folder = f"{env.home.rstrip('/')}/{FOLDER}/{adapter.name}"
    launch_id = "c" + secrets.token_hex(8)
    term_id = ""
    companions: list[str] = []
    registered = False
    feeder: asyncio.Task[None] | None = None
    try:
        await terminals.fs_mkdir(env.name, folder, actor=ACTOR)
        spec = LaunchSpec(
            harness=adapter.name, env=env.name, cwd=folder, launch_id=launch_id, first_prompt=None, model=model,
            title="self-check", team_block="This is a self-check of the command-line agent. Do only what the prompt asks.",
            ask_hold_ms=cfg.ask_hold_s * 1000, report_hold_ms=cfg.report_hold_s * 1000, permission_hold_ms=0,
            port_range=port_range(cfg.opencode_port_range),
        )
        plan = adapter.launch_plan(spec)
        daemon = await terminals.register_launch(env.name, DaemonLaunch(launch_id=launch_id, files=dict(plan.files), ports=list(plan.ports), hold_max_ms=cfg.report_hold_s * 1000 + 30_000), actor=ACTOR)
        registered = True

        def placed(value: str) -> str:
            return value.replace(LAUNCH_DIR, daemon.dir).replace(DIAL_DIR, daemon.dial_dir)

        async def create(argv: tuple[str, ...], values: dict[str, str], title: str) -> str:
            environment = terminal_environment({k: placed(v) for k, v in values.items()}, capabilities=adapter.capabilities)
            view = await terminals.create(
                TerminalSpec(
                    env=env.name, owner=Owner("free"), cwd=folder, argv=[placed(a) for a in argv],
                    env_vars=environment.set, strip_env=list(environment.strip), title=title,
                    cols=LAUNCH_COLS, rows=LAUNCH_ROWS, profile=f"harness:{adapter.name}", launch_id=launch_id, created_by="operator",
                ),
                confirm_over_cap=True,
            )
            return str(view["id"])

        # A companion first (Codex's app server), as a staff launch starts it: the CLI talks to it.
        for companion in plan.companions:
            companions.append(await create(companion.argv, dict(companion.env), f"{adapter.capabilities.label} self-check · {companion.role}"))
            if companion.ready_pattern and (await terminals.wait_for(companions[-1], regex=companion.ready_pattern, timeout=cfg.ready_timeout_s)).get("matched") != "regex":
                steps.add("launch", False, f"the {companion.role} did not start within {cfg.ready_timeout_s:g} s")
                return CheckResult(False, tuple(steps.steps), version, _ms(started))
        term_id = await create(plan.argv, dict(plan.env), f"{adapter.capabilities.label} self-check")
        steps.add("launch", True, f"terminal {term_id}")
        hooks: asyncio.Queue[HookPost | None] = asyncio.Queue()
        term = RuntimeTerminal(terminals, term_id, env.name, actor=ACTOR, launch_id=launch_id, hooks=hooks)
        record = Launch(launch_id, "", adapter.name, env.name, term_id, companions[0] if companions else None, daemon.dir, plan.session_ref, version, "")
        seen: dict[str, Any] = {}
        events: asyncio.Queue[StaffEvent] = asyncio.Queue()

        async def feed() -> None:
            async def posts() -> None:
                async for hook in terminals.hook_events(launch_id):
                    post = HookPost(name=hook.name, body=hook.body, at=hook.at, reply_id=hook.reply_id, hold_ms=hook.hold_ms)
                    body = post.body if isinstance(post.body, dict) else {}
                    if post.name != "team":
                        hooks.put_nowait(post)
                    elif body.get("tool") == "hello":
                        seen["hello"] = body.get("stage")
                    else:
                        seen.setdefault("team", []).append(body)
                        if post.reply_id:
                            await term.reply(post.reply_id, {"text": "self-check recorded"})
                hooks.put_nowait(None)

            pump = asyncio.create_task(posts())
            try:
                async for event in adapter.events(term, record):
                    events.put_nowait(event)
            finally:
                pump.cancel()

        feeder = asyncio.create_task(feed(), name=f"self-check-{adapter.name}")

        async def until(kinds: set[EventKind], timeout: float) -> StaffEvent | None:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                try:
                    event = await asyncio.wait_for(events.get(), min(cfg.ready_poll_ms / 1000, max(0.01, deadline - loop.time())))
                except TimeoutError:
                    continue
                if event.kind in kinds:
                    return event
                if event.kind is EventKind.TURN_FAILED:
                    return event
            return None

        ready = await _gate(adapter, term, cfg, until)
        if not steps.add("ready", ready is None, ready or "the CLI said it was ready"):
            return CheckResult(False, tuple(steps.steps), version, _ms(started))
        await adapter.after_spawn(term, record, plan)
        if adapter.capabilities.team_tools != "none":
            loop = asyncio.get_running_loop()
            deadline = loop.time() + cfg.team_hello_s
            while "hello" not in seen and loop.time() < deadline:
                await asyncio.sleep(0.2)
            steps.add("team", "hello" in seen, "the team tools loaded" if "hello" in seen else f"no word from the team tools within {cfg.team_hello_s:g} s")
        if model_turn:
            delivered = await adapter.send(term, "", PROMPT, "after_turn")
            acknowledged = await until({EventKind.PROMPT_ACKNOWLEDGED}, cfg.ack_timeout_s * 2)
            if steps.add("deliver", acknowledged is not None and acknowledged.kind is EventKind.PROMPT_ACKNOWLEDGED, f"{delivered.via}: " + ("acknowledged" if acknowledged is not None else "not acknowledged")):
                done = await until({EventKind.TURN_COMPLETED}, TURN_TIMEOUT_S)
                reports = [r for r in seen.get("team", []) if r.get("tool") == "report"]
                ok = done is not None and done.kind is EventKind.TURN_COMPLETED and bool(reports)
                detail = "the turn ended and its Report came through the team channel" if ok else ("the turn did not end" if done is None or done.kind is not EventKind.TURN_COMPLETED else "the turn ended without a Report through the team channel")
                steps.add("reply", ok, detail)
        await adapter.stop(term)
        ended = await _session_end(terminals, term_id, until, cfg.stop_grace_s + 5)
        code = await _exit_code(terminals, term_id, cfg.stop_grace_s + 5)
        steps.add("exit", code == 0, f"exit code {code}" + ("" if ended is not None else "; no session end reported"))
    except TerminalError as exc:
        steps.add("launch" if not term_id else "session", False, exc.message)
    except Exception as exc:  # noqa: BLE001 — the check's own failure is its result
        logger.exception("the %s self-check failed", adapter.name)
        steps.add("session", False, str(exc))
    finally:
        if feeder is not None:
            feeder.cancel()
        for terminal_id in [term_id, *companions]:
            with contextlib.suppress(Exception):
                if terminal_id and (await terminals.get(terminal_id))["status"] == "running":
                    await terminals.kill(terminal_id, actor=ACTOR)
        with contextlib.suppress(Exception):
            if registered:
                await terminals.unregister_launch(env.name, launch_id, actor=ACTOR)
    ok = bool(steps.steps) and all(s.ok for s in steps.steps if not s.skipped)
    return CheckResult(ok, tuple(steps.steps), version, _ms(started))


async def _gate(adapter: HarnessAdapter, term: RuntimeTerminal, cfg: HarnessConfig, until: Any) -> str | None:
    """The readiness gate of the check: None once the CLI is ready, else why not."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.ready_timeout_s
    last = ""
    screen = ""
    answered = 0
    answered_at = 0.0
    while loop.time() < deadline:
        screen = await term.screen()
        step = adapter.readiness(screen)
        if step.action == "fail":
            return f"{step.reason}; the screen ended: {_tail(screen)}"
        if step.action == "keys" and step.keys and (screen != last or loop.time() - answered_at >= GATE_REPEAT_S) and answered < GATE_ANSWERS_MAX:
            await term.write(keys=list(step.keys), note=f"self-check readiness: {step.reason}")
            answered += 1
            last = screen
            answered_at = loop.time()
        event = await until({EventKind.READY, EventKind.TURN_FAILED}, cfg.ready_poll_ms / 1000)
        if event is not None:
            return None if event.kind is EventKind.READY else str(event.payload.get("failure") or "failed")
    return f"not ready after {cfg.ready_timeout_s:g} s; the screen ended: {_tail(screen)}"


async def _session_end(terminals: Terminals, terminal_id: str, until: Any, timeout: float) -> StaffEvent | None:
    """The session end the CLI reports, or None: given up a moment after the terminal has exited."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    exited_at: float | None = None
    while loop.time() < deadline:
        event = await until({EventKind.SESSION_ENDED}, 0.2)
        if event is not None:
            return event
        if exited_at is None:
            if (await terminals.get(terminal_id))["status"] != "running":
                exited_at = loop.time()
        elif loop.time() - exited_at >= SESSION_END_AFTER_EXIT_S:
            return None
    return None


def _tail(screen: str) -> str:
    """The last lines of a screen that says something, on one line: enough to see what it waits on."""
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    return " / ".join(lines[-6:])[-400:]


async def _exit_code(terminals: Terminals, terminal_id: str, timeout: float) -> int | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        view = await terminals.get(terminal_id)
        if view["status"] != "running":
            code = view.get("exit_code")
            return int(code) if code is not None else None
        await asyncio.sleep(0.2)
    return None


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = ["PROMPT", "session_check"]
