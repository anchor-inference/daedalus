"""Command-line agents: what is installed where, and what each one can do, as ``app.extensions["harness"]``.

The staff lifecycle is the staff extension's; this one holds what is particular to the CLIs
themselves — the harness manager — so the hiring form and the orchestrator ask one place whether a
harness can be chosen, and the Harnesses screen installs, updates and checks them. It also puts one
staff runtime per CLI that has an adapter into the team's ``runtimes``, and takes up the CLIs a
previous host left running.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING, cast

from daedalus.harness import (  # noqa: F401 — importing an adapter registers it
    ADAPTERS,
    claude,
    codex,
    cursor,
    grok,
    opencode,
    pi,
)
from daedalus.harness.manager import HarnessManager
from daedalus.harness.ports import TerminalRunner
from daedalus.harness.runtime import CliStaffRuntime, RuntimeEnvironment, install_runtimes
from daedalus.harness.selfcheck import session_check
from daedalus.stores.harness import HarnessStore

if TYPE_CHECKING:
    from daedalus.app import Application
    from daedalus.extensions.staff import Team
    from daedalus.harness.contract import EnvironmentPort
    from daedalus.terminals.service import Terminals

logger = logging.getLogger(__name__)

CATALOG_ROOTS = (".claude/agents", ".claude/projects", ".codex", ".pi/agent", ".grok/sessions")
"""What of each environment's home the host reads, besides the project folders: the user's own
agent definitions, Claude Code's transcripts, Codex's profiles and its rollouts, pi's settings (to
learn its provider) and its sessions, Grok Build's sessions (its transcripts). OpenCode keeps its
transcripts in its own store, read through ``opencode export``. The daemon's deny list keeps every
credential file under them unreadable all the same.

Claude's transcripts were missing here for a while, unnoticed, because the tests made the whole home
readable; ``catalog_roots`` is what the tests now use, so a transcript directory left off this list
fails them."""


def catalog_roots(home: str) -> list[str]:
    """The roots under ``home`` that the host asks the daemon to let it read."""
    return [f"{home.rstrip('/')}/{path}" for path in CATALOG_ROOTS]


def build(app: Application, store: HarnessStore) -> HarnessManager:
    manager = app.manager
    assert manager is not None

    def terminals() -> Terminals | None:
        return cast("Terminals | None", app.extensions.get("terminals"))

    def port(env: str) -> EnvironmentPort | None:
        service = terminals()
        if service is None:
            return None
        status = next((s for s in service.environments() if s.env == env), None)
        if status is None or not status.available:
            return None
        if status.home:
            service.set_extra_roots(env, "harness-catalog", catalog_roots(status.home))
        return RuntimeEnvironment(service, env, home=status.home, actor="harness")

    def environments() -> list[str]:
        service = terminals()
        return [s.env for s in service.environments() if s.available] if service is not None else []

    async def folder(folder_id: str) -> tuple[str, str] | None:
        found = await manager.projects.folder_by_id(folder_id)
        return (found.env, str(found.path)) if found is not None else None

    service = terminals()
    return HarnessManager(
        store,
        ports=port,
        environments=environments,
        config=lambda: app.config.harness,
        live_staff=manager.staff.live_by_harness,
        folder=folder,
        runner=TerminalRunner(service) if service is not None else None,
        bus=manager.bus,
    )


async def install(app: Application) -> list[asyncio.Task[None]]:
    store = HarnessStore(app.db)
    harness = build(app, store)
    app.extensions["harness"] = harness

    async def closer() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await harness.close()

    tasks = [asyncio.create_task(harness.run(), name="harness-check"), asyncio.create_task(closer(), name="harness-close")]
    team = cast("Team | None", app.extensions.get("staff"))
    terminals = cast("Terminals | None", app.extensions.get("terminals"))
    if terminals is not None:
        # The session part of each CLI's self-check: one short session through the adapter.
        for name, factory in ADAPTERS.items():
            harness.self_checks[name] = functools.partial(session_check, factory(), terminals, lambda: app.config.harness)
    if team is None or terminals is None:
        # Without the team or the terminals there is nothing to run a CLI for or in; command-line
        # members are then refused at assignment with the runtime's absence as the reason.
        return tasks
    runtimes = install_runtimes(
        ADAPTERS,
        team.runtimes,
        terminals=terminals,
        store=store,
        ingress=team.ingress,
        lookup=team.live,
        config=lambda: app.config.harness,
        blocker=harness.launch_blocker,
    )
    if not runtimes:
        return tasks

    async def take_up(runtime: CliStaffRuntime) -> None:
        try:
            count = await runtime.reconcile()
        except Exception:  # noqa: BLE001 — one CLI's reconcile failing leaves the others to theirs
            logger.exception("the %s sessions left by the previous host were not taken up", runtime.kind)
            return
        if count:
            logger.info("%d %s staff session(s) taken up from the previous host", count, runtime.kind)
        # Once, now that the sessions are held again: rows the previous host left stale (grey over an
        # idle prompt, or working on a task that is over) are settled before the queue looks at them,
        # so the members' waiting tasks go to them rather than wait behind a status nobody corrects.
        try:
            settled = await team.settle(screens=("working", "no_signal"))
        except Exception:  # noqa: BLE001 — the ticker settles them later
            logger.exception("settling the %s sessions taken up failed", runtime.kind)
            return
        if settled:
            logger.info("%d %s staff session(s) settled after the restart", settled, runtime.kind)
        team.queue.pump_soon()

    async def keeper() -> None:
        # Cancelled at shutdown like every background task; the CLIs keep running in their daemons.
        try:
            await asyncio.Event().wait()
        finally:
            for runtime in runtimes:
                runtime.close()

    return tasks + [asyncio.create_task(take_up(r), name=f"harness-reconcile-{r.kind}") for r in runtimes] + [asyncio.create_task(keeper(), name="harness-runtimes")]
