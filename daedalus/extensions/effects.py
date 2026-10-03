"""Dispatch committed effects through registered handlers and retain uncertain outcomes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from daedalus.stores.control import ControlDenied
from daedalus.stores.outbox import Claim, OutboxStore

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EffectOutcome:
    state: Literal["completed", "failed", "unknown", "deferred"]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EffectResolution:
    state: Literal["completed", "failed"]
    evidence: dict[str, Any]


class Handler(Protocol):
    async def run(self, claim: Claim, check: Callable[[Claim], Awaitable[None]]) -> EffectOutcome:
        """Recheck the claim immediately before the external effect; report its proven outcome."""


class EffectDispatcher:
    def __init__(self, store: OutboxStore) -> None:
        self.store = store
        self.handlers: dict[str, Handler] = {}
        self.wake = asyncio.Event()
        self.delivery_ready = asyncio.Event()
        self.postponed: set[str] = set()

    def register(self, kind: str, handler: Handler) -> None:
        if kind in self.handlers:
            raise ValueError(f"an effect handler is already registered for {kind}")
        self.handlers[kind] = handler
        self.wake.set()

    def notify(self) -> None:
        self.wake.set()

    def enable(self) -> None:
        """The application opened its runtimes and finished recovering their previous work."""
        self.delivery_ready.set()

    async def reconcile(self) -> int:
        """Ask handlers for physical proof; an unknown outcome never re-enters delivery."""
        kinds = tuple(kind for kind, handler in self.handlers.items() if callable(getattr(handler, "reconcile", None)))
        resolved = 0
        for claim in await self.store.unknown(kinds):
            try:
                resolution = await self.handlers[claim.kind].reconcile(claim)
                if resolution is not None:
                    resolved += int(await self.store.reconcile(claim.id, generation=claim.generation, state=resolution.state, evidence=resolution.evidence))
            except Exception:  # noqa: BLE001 — keep uncertainty visible without starving other actions
                logger.exception("effect %s could not be reconciled", claim.id)
        return resolved

    async def step(self) -> bool:
        # A freed slot or a new command invalidates the old deferred sweep. Otherwise a lower
        # priority command can consume capacity while the urgent one is still excluded.
        if self.wake.is_set():
            self.wake.clear()
            self.postponed.clear()
        claim = await self.store.claim(tuple(self.handlers), exclude=tuple(self.postponed))
        if claim is None:
            return False
        try:
            await self.store.check(claim)
        except ControlDenied as exc:
            await self.store.finish(claim, state="failed", error=str(exc))
            return True
        if claim.kind == "task.launch" and self.wake.is_set():
            await self.store.defer(claim, reason="launch admission changed before execution")
            return True
        try:
            outcome = await self.handlers[claim.kind].run(claim, self.store.check)
        except asyncio.CancelledError:
            # The process may already have sent an effect. Cancellation is not proof that it did not.
            await asyncio.shield(self.store.finish(claim, state="unknown", error="effect dispatcher stopped; reconcile outcome"))
            raise
        except Exception as exc:  # noqa: BLE001 — an opaque handler error cannot prove absence of its effect
            logger.exception("effect %s failed with an uncertain outcome", claim.id)
            await self.store.finish(claim, state="unknown", error=f"{type(exc).__name__}: {exc}")
        else:
            if outcome.state == "deferred":
                await self.store.defer(claim, reason=outcome.error or "waiting for admission")
                self.postponed.add(claim.id)
            else:
                await self.store.finish(claim, state=outcome.state, error=outcome.error)
        return True

    async def run(self) -> None:
        await self.store.recover()
        # Extensions install asynchronously. A retained stop cannot be inspected against an empty
        # runtime map before recovery, and a retained merge must find its registered executor.
        await self.delivery_ready.wait()
        while True:
            self.wake.clear()
            self.postponed.clear()
            try:
                await self.reconcile()
                while await self.step():
                    await asyncio.sleep(0)
            except Exception:  # noqa: BLE001 — another queued action must remain inspectable after a failed read
                logger.exception("effect dispatch failed")
            try:
                # A wake after commit handles normal delivery. The bounded sweep also notices a
                # different process's committed command, without per-project polling tasks.
                await asyncio.wait_for(self.wake.wait(), timeout=30)
            except TimeoutError:
                pass


async def install(app: Application) -> list[asyncio.Task[None]]:
    from daedalus.extensions.comparison_launch import (
        ComparisonLaunchEffect,  # Lazy: handlers import dispatcher outcome contracts.
    )
    from daedalus.extensions.comparison_stop import (
        ComparisonStopEffect,  # Lazy: handlers import dispatcher outcome contracts.
    )
    from daedalus.extensions.coordinator_authority import (
        install_authority,  # Lazy: authority hooks follow application stores.
    )
    from daedalus.extensions.merge_effect import MergeEffect  # Lazy: handlers import the dispatcher outcome contracts.
    from daedalus.extensions.provider_resume import (
        ProviderResumeEffect,  # Lazy: the handler imports the dispatcher outcome contracts.
    )
    from daedalus.extensions.recurring import RecurringEffect  # Lazy: the scheduler installs after the dispatcher.
    from daedalus.extensions.runtime_transfers import (
        ArtifactTransferEffect,  # Lazy: the handler imports dispatcher outcomes.
    )
    from daedalus.extensions.task_controls import (
        TaskStopEffect,  # Lazy: the handler imports the dispatcher outcome contracts.
    )
    from daedalus.extensions.task_launch import TaskLaunchEffect  # Lazy: the handler imports dispatcher outcomes.

    dispatcher = EffectDispatcher(OutboxStore(app.db))
    dispatcher.register("task.stop", TaskStopEffect(app))
    dispatcher.register("task.launch", TaskLaunchEffect(app))
    comparison = ComparisonLaunchEffect(app)
    dispatcher.register("comparison.launch.first", comparison)
    dispatcher.register("comparison.launch.second", comparison)
    dispatcher.register("comparison.stop", ComparisonStopEffect(app))
    dispatcher.register("review.merge", MergeEffect(app))
    dispatcher.register("artifact.transfer", ArtifactTransferEffect(app))
    dispatcher.register("provider.resume", ProviderResumeEffect(app))
    recurring = RecurringEffect(app)
    for kind in ("agent", "message", "lazy", "wake"):
        dispatcher.register(f"schedule.{kind}", recurring)
    app.extensions["effects"] = dispatcher
    install_authority(app)
    return [asyncio.create_task(dispatcher.run(), name="effect-dispatcher")]
