"""Command-line entry point.

``daedalus serve``  — run the bot (Telegram + API + scheduler).
``daedalus check``  — open the state, register tools and providers, exit.
``daedalus run``    — run one agent session from the terminal (no Telegram).
``daedalus doctor`` — check the deployment (config, state, git, providers); ``--fix`` applies safe repairs.
``daedalus db checkpoints-prune`` — apply the workspace-snapshot retention bounds now.
``daedalus auth pair`` — mint a one-time link that signs a browser in, for an installation without Telegram.
``daedalus self restart`` — ask the supervisor to preflight the checkout and restart onto it if it passes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daedalus.config import NO_MODEL_MESSAGE, RuntimeConfig, Settings


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings()
    if args.state_dir:
        settings.state_dir = Path(args.state_dir)
    if args.workspaces_dir:
        settings.workspaces_dir = Path(args.workspaces_dir)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    return settings


async def cmd_check(args: argparse.Namespace) -> int:
    from daedalus.host.session_runner import SessionManager  # Lazy: each subcommand imports only what it runs
    from daedalus.stores.database import Database  # Lazy: each subcommand imports only what it runs

    settings = _settings(args)
    config = RuntimeConfig.load(settings.config_path)
    db = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
    await db.open()
    try:
        manager = SessionManager(settings, config, db=db)
        await manager.start()
        tools = sorted(t.name for t in manager.tools.list_all())
        print(f"state: {settings.state_dir}")
        print(f"config: {settings.config_path}")
        print(f"repos: bot={settings.bot_repo_dir} core={settings.core_repo_dir}")
        print(f"providers: {', '.join(manager.providers.available()) or '(none configured)'}")
        found = config.default_preset()
        if found is None:
            print(f"model: none — {NO_MODEL_MESSAGE}")
        else:
            pid, preset = found
            print(f"model: {pid} = {preset.provider}/{preset.model} thinking={preset.thinking} effort={preset.reasoning_effort}")
        print(f"presets: {', '.join(config.presets) or '(none)'}"),
        print(f"tools ({len(tools)}): {', '.join(tools)}")
        skills = await manager.skills.list("daedalus")
        print(f"skills ({len(skills)}): {', '.join(s.name for s in skills) or '(none)'}")
        await manager.close()
    finally:
        await db.close()
    return 0


async def cmd_doctor(args: argparse.Namespace) -> int:
    from daedalus.doctor import (  # Lazy: each subcommand imports only what it runs
        DoctorContext,
        render_text,
        run_checks,
        summarize,
    )
    from daedalus.host.session_runner import SessionManager  # Lazy: each subcommand imports only what it runs
    from daedalus.stores.database import Database  # Lazy: each subcommand imports only what it runs

    settings = _settings(args)
    config = RuntimeConfig.load(settings.config_path)
    db = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
    await db.open()
    try:
        manager = SessionManager(settings, config, db=db)
        await manager.start()
        try:
            checks = await run_checks(DoctorContext(settings=settings, config=config, db=db, manager=manager, fix=args.fix))
        finally:
            await manager.close()
    finally:
        await db.close()
    if args.json:
        import json  # Lazy: only the --json output needs it

        print(json.dumps({"checks": [c.as_dict() for c in checks], "summary": summarize(checks)}, indent=2))
    else:
        print(render_text(checks))
    return 1 if summarize(checks)["fail"] else 0


async def cmd_run(args: argparse.Namespace) -> int:
    from daedalus.host.cli_session import run_terminal_session  # Lazy: each subcommand imports only what it runs

    settings = _settings(args)
    config = RuntimeConfig.load(settings.config_path)
    return await run_terminal_session(settings, config, prompt=args.prompt, title=args.title)


async def cmd_bench(args: argparse.Namespace) -> int:
    from daedalus.bench.manifest import Manifest  # Lazy: each subcommand imports only what it runs
    from daedalus.bench.runner import BenchRunner  # Lazy: each subcommand imports only what it runs

    if not args.state_dir:
        fallback = Settings().bench_state_dir
        if fallback is None:
            print("bench needs its own state directory: pass --state-dir (or set BENCH_STATE_DIR); the bot's database is never used for benchmarks", file=sys.stderr)
            return 2
        args.state_dir = str(fallback)
    settings = _settings(args)
    config = RuntimeConfig.load(settings.config_path)
    manifest = Manifest.load(Path(args.manifest))
    out_dir = Path(args.out) if args.out else settings.state_dir / "bench" / f"{manifest.name}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    only = [t.strip() for t in (args.only or "").split(",") if t.strip()] or None
    async with BenchRunner(settings, config, preset=args.preset, out_dir=out_dir) as runner:
        records = await runner.run(manifest, concurrency=args.concurrency, only=only)
    judged = [r for r in records if r.passed is not None]
    passed = sum(1 for r in judged if r.passed)
    cost = sum(r.cost_usd for r in records if r.cost_usd is not None)
    print(f"{manifest.name}: {passed}/{len(judged)} passed of {len(records)} tasks; cost ${cost:.4f}; records in {out_dir}")
    return 0


async def cmd_auth(args: argparse.Namespace) -> int:
    from daedalus.stores import pairing  # Lazy: each subcommand imports only what it runs
    from daedalus.stores.database import Database  # Lazy: each subcommand imports only what it runs

    settings = _settings(args)
    db = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
    await db.open()
    try:
        base = settings.miniapp_public_url or f"http://127.0.0.1:{settings.api_port}"
        # The state directory is the bot's, so this works the same run from a shell inside the container.
        url = await pairing.announce(db, settings.state_dir, base, note="cli")
    finally:
        await db.close()
    print(url)
    print(f"Opens once, within {pairing.TTL_MINUTES} minutes; using it revokes every other pairing link.")
    return 0


async def cmd_self(args: argparse.Namespace) -> int:
    """Ask the supervisor to apply what the checkout holds — the same op the app's Apply sends.

    The launcher runs this inside the container, because the supervisor's socket is not on the host.
    A plain stop and start of the containers would come back on whatever the checkout says without
    anything having checked it; this is the path the preflight is on.
    """
    from daedalus import supervisor_client  # Lazy: each subcommand imports only what it runs

    settings = _settings(args)
    try:
        result = await supervisor_client.call(settings.supervisor_address, "restart", token_path=settings.supervisor_token_path, reason=args.reason)
    except supervisor_client.SupervisorUnavailable as exc:
        print(f"no supervisor to ask ({exc}); this installation applies a change by starting over", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(result)
    return 0


async def cmd_db(args: argparse.Namespace) -> int:
    from daedalus.stores.database import Database  # Lazy: each subcommand imports only what it runs

    settings = _settings(args)
    db = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
    await db.open()
    try:
        started = time.monotonic()
        if args.db_command == "checkpoints-prune":
            return await _prune_checkpoints(settings, db, started)
        before, after = await db.vacuum()
    finally:
        await db.close()
    print(f"{before / 1e6:.1f} MB -> {after / 1e6:.1f} MB in {time.monotonic() - started:.1f}s")
    print("The file is now on incremental auto-vacuum; freed pages are given back as the bot runs.")
    return 0


async def _prune_checkpoints(settings: Settings, db: Any, started: float) -> int:
    """The retention pass the maintenance tick runs, by hand and with the numbers printed.

    The tick skips a store whose session has a turn in flight. This cannot: from outside the
    serving process there is no way to ask which those are, and ``git gc`` collects what is
    unreachable at the moment it runs. So it says as much, and the safe time for it is a bot that
    is idle — or the tick, which needs nothing said to it.
    """
    from daedalus.host.checkpoint_retention import (  # Lazy: each subcommand imports only what it runs
        CheckpointRetention,
        RetentionBounds,
    )

    config = RuntimeConfig.load(settings.config_path)
    retention = CheckpointRetention(db, workspaces_dir=settings.workspaces_dir)
    bounds = RetentionBounds.from_ops(config.ops)
    print(f"bounds: {bounds.keep_days} days, {bounds.total_max_gb:.1f} GB in total, the last {bounds.keep_last} per session always kept")
    print("This packs the snapshot repositories; run it while no agent is working, or leave it to the maintenance tick.")
    report = await retention.run(bounds)
    print(report.line() + f" in {time.monotonic() - started:.1f}s")
    return 0


async def cmd_serve(args: argparse.Namespace) -> int:
    from daedalus.app import serve  # Lazy: each subcommand imports only what it runs

    settings = _settings(args)
    return await serve(settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daedalus")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--workspaces-dir", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate configuration and registration, then exit")
    doctor = sub.add_parser("doctor", help="check the deployment: config, state, git, providers")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--fix", action="store_true", help="apply the safe fixes (stale snapshots, orphan workspaces)")
    run = sub.add_parser("run", help="run one session from the terminal")
    run.add_argument("--prompt", "-p", default=None, help="first message; omit for interactive input")
    run.add_argument("--title", default="terminal")
    sub.add_parser("serve", help="run the bot")
    auth = sub.add_parser("auth", help="ways into the app that need no Telegram")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_sub.add_parser("pair", help="mint a one-time pairing link and print it")
    itself = sub.add_parser("self", help="the agent's own code")
    itself_sub = itself.add_subparsers(dest="self_command", required=True)
    restart = itself_sub.add_parser("restart", help="preflight what the checkout holds and restart onto it if it passes")
    restart.add_argument("--reason", default="operator request")
    database = sub.add_parser("db", help="maintenance of the state database")
    database_sub = database.add_subparsers(dest="db_command", required=True)
    database_sub.add_parser("vacuum", help="rewrite the file, reclaiming free pages and switching it to incremental auto-vacuum")
    database_sub.add_parser("checkpoints-prune", help="apply the checkpoint retention bounds now: drop old snapshots and pack the stores")
    bench = sub.add_parser("bench", help="run a task manifest headless and record pass/turns/tokens/cost per task")
    bench.add_argument("manifest", help="JSON manifest: {name, tasks: [{id, prompt, setup, check, files, timeout_minutes, tags}], tools_off}")
    bench.add_argument("--preset", default=None, help="model preset id for every task (default: the configured default)")
    bench.add_argument("--concurrency", type=int, default=1)
    bench.add_argument("--only", default=None, help="comma-separated task ids")
    bench.add_argument("--out", default=None, help="output directory (default: <state>/bench/<name>-<timestamp>)")
    return parser


class _DiagFilter(logging.Filter):
    """The core logs its DIAG traces at WARNING; keep them out of production logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("DIAG ")


def _install_task_dump() -> None:
    """SIGUSR1 prints every asyncio task's stack — the way to see what a stuck bot awaits."""
    import signal  # Lazy: only the dump handler needs it
    import traceback  # Lazy: only the dump handler needs it

    def dump(signum: int, frame: object) -> None:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        sys.stderr.write("=== asyncio tasks ===\n")
        for task in asyncio.all_tasks(loop):
            sys.stderr.write(f"--- {task.get_name()} done={task.done()}\n")
            for f in task.get_stack(limit=12):
                sys.stderr.write("".join(traceback.format_stack(f, limit=1)))
        sys.stderr.flush()

    # Windows has no SIGUSR1: asking for it there ended every native start before the first line of
    # the app, three times, and the launcher reported only that the app never answered.
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, dump)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from daedalus.security.redact import (  # Lazy: logging is configured before the package is imported
        install_logging_filter,
        shared,
    )

    for handler_ in logging.getLogger().handlers:
        handler_.addFilter(_DiagFilter())
    install_logging_filter(shared())
    _install_task_dump()
    handler = {"check": cmd_check, "run": cmd_run, "serve": cmd_serve, "doctor": cmd_doctor, "bench": cmd_bench, "auth": cmd_auth, "db": cmd_db, "self": cmd_self}[args.command]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    sys.exit(main())
