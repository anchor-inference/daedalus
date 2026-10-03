"""Deliver one operator-approved provider continuation with a fenced input receipt."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.stores.control import ControlConflict, ControlDenied, now, one
from daedalus.stores.outbox import Claim
from daedalus.stores.provider_holds import pinned_target_in, validate_hold_in

if TYPE_CHECKING:
    from daedalus.app import Application


class ProviderResumeEffect:
    def __init__(self, app: Application) -> None:
        self.app = app

    async def _hold(self, claim: Claim) -> dict[str, object]:
        row = await self.app.db.fetchone("SELECT * FROM provider_resume_holds WHERE id = ?",
                                         (claim.payload["hold_id"],))
        if row is None or row["resume_action_id"] != claim.id or row["state"] not in ("resume_queued", "resumed", "unknown"):
            raise ControlConflict("the provider resume intent is not current")
        if (row["session_id"], row["failed_run_id"], row["provider_id"], row["model"]) != (
                    claim.payload["session_id"], claim.payload["failed_run_id"],
                    claim.payload["provider_id"], claim.payload["model"]):
            raise ControlConflict("the provider resume payload differs from its saved decision")
        return dict(row)

    async def _record_resumed(self, hold: dict[str, object], claim: Claim, run_id: str) -> None:
        async with self.app.db.transaction() as conn:
            run = await one(conn, "SELECT session_id FROM runs WHERE id = ?", (run_id,))
            pinned = await one(conn, "SELECT 1 FROM provider_hold_events WHERE hold_id = ?"
                               " AND event = 'run_pinned' AND action_id = ? AND run_id = ?",
                               (hold["id"], claim.id, run_id))
            if run is None or run["session_id"] != hold["session_id"] or pinned is None:
                raise ControlConflict("the resumed run has no matching durable session")
            await conn.execute("UPDATE provider_resume_holds SET state = 'resumed',resumed_run_id = ?,"
                               "updated_at = ? WHERE id = ? AND resume_action_id = ? AND state IN ('resume_queued','unknown')",
                               (run_id, now(), hold["id"], claim.id))
            existing = await one(conn, "SELECT 1 FROM provider_hold_events WHERE hold_id = ? AND event = 'resumed'",
                                 (hold["id"],))
            if existing is None:
                await conn.execute("INSERT INTO provider_hold_events(id,hold_id,event,action_id,run_id,occurred_at)"
                                   " VALUES (lower(hex(randomblob(16))),?,'resumed',?,?,?)",
                                   (hold["id"], claim.id, run_id, now()))

    async def _record_unfinished(self, hold: dict[str, object], claim: Claim, *,
                                 state: str, reason: str) -> None:
        async with self.app.db.transaction() as conn:
            await conn.execute("UPDATE provider_resume_holds SET state = ?,updated_at = ?"
                               " WHERE id = ? AND resume_action_id = ? AND state = 'resume_queued'",
                               (state, now(), hold["id"], claim.id))
            existing = await one(conn, "SELECT 1 FROM provider_hold_events WHERE hold_id = ? AND event = ?",
                                 (hold["id"], state))
            if existing is None:
                await conn.execute("INSERT INTO provider_hold_events(id,hold_id,event,action_id,reason,occurred_at)"
                                   " VALUES (lower(hex(randomblob(16))),?,?,?,?,?)",
                                   (hold["id"], state, claim.id, reason, now()))

    async def run(self, claim: Claim, check: Callable[[Claim], Awaitable[None]]) -> EffectOutcome:
        hold: dict[str, object] | None = None
        try:
            hold = await self._hold(claim)
            if hold["state"] == "resumed":
                return EffectOutcome("completed")
            async with self.app.db.transaction() as conn:
                await validate_hold_in(conn, str(hold["observation_id"]))
            await check(claim)
        except (ControlConflict, ControlDenied, ValueError) as exc:
            if hold is not None:
                await self._record_unfinished(hold, claim, state="invalidated", reason=str(exc))
            return EffectOutcome("failed", str(exc))
        try:
            # The manager pins a single provider/model for this continuation and creates its
            # input receipt before any transport. A changed context or budget is rejected there.
            run_id = await self.app.manager.resume_provider_hold(
                str(hold["session_id"]), str(hold["failed_run_id"]), str(hold["provider_id"]),
                str(hold["model"]), client_message_id=f"provider-resume:{claim.id}",
            )
            if not run_id:
                return EffectOutcome("unknown", "the resumed input has no proven run")
            await self._record_resumed(hold, claim, run_id)
            return EffectOutcome("completed")
        except Exception as exc:  # noqa: BLE001 — the input might already have been submitted
            assert hold is not None
            await self._record_unfinished(hold, claim, state="unknown", reason=type(exc).__name__)
            return EffectOutcome("unknown", f"resume outcome needs reconciliation: {type(exc).__name__}")

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        try:
            hold = await self._hold(claim)
        except ControlConflict:
            return None
        if hold["state"] == "resumed" and hold["resumed_run_id"]:
            return EffectResolution("completed", {"run_id": hold["resumed_run_id"],
                                                  "proof": "stored_resume_run"})
        receipt = await self.app.manager.live.receipt(str(hold["session_id"]),
                                                      f"provider-resume:{claim.id}")
        if receipt is None or not receipt.get("run_id") or receipt.get("status") != "consumed":
            return None
        run_id = str(receipt["run_id"])
        async with self.app.db.transaction() as conn:
            pinned = await pinned_target_in(conn, str(hold["session_id"]), run_id)
        if pinned is None or pinned["action_id"] != claim.id or pinned["hold_id"] != hold["id"]:
            return None
        await self._record_resumed(hold, claim, run_id)
        return EffectResolution("completed", {"run_id": run_id, "proof": "exact_input_receipt"})
