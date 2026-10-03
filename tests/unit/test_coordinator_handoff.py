"""A failed preparation leaves the old coordinator; a committed swap survives retirement failure."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.extensions import api, orchestrator_ops
from daedalus.extensions.coordinator_handoff import CoordinatorHandoff
from daedalus.extensions.orchestrator import NotCurrent
from daedalus.extensions.recurring import Recurring
from daedalus.stores.control import ControlConflict, Principal, digest
from daedalus.stores.database import Database
from tests.unit.test_orchestrator import FALLBACK_PRESET, rig
from tests.unit.test_staff_runtime import close_team


async def prepared_rig(settings: Settings, db: Database, tmp_path: Path) -> Any:
    r = await rig(settings, db, tmp_path)
    r.orch.app._config_lock = asyncio.Lock()
    return r


def operator() -> Principal:
    return Principal.operator({"via": "token", "user_id": 1})


async def send(orch: Any, project: Any, *, operation_id: str = "replace-one", reason: str = "fresh context") -> dict[str, Any]:
    return await orch.replace_command(
        project.id, reason, principal=operator(), client_operation_id=operation_id,
        expected_entity_revision=project.entity_revision,
        expected_coordinator_session_id=project.settings.orchestrator.session_id,
    )


async def test_auth_failure_keeps_the_old_office_and_deduplicates_the_command(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        old = project.settings.orchestrator.session_id
        calls = 0

        async def denied(_url: str, _key: str | None) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            raise ValueError("HTTP 401")

        r.orch.catalogue_lookup = denied
        first = await send(r.orch, project)
        second = await send(r.orch, project)
        assert first == second and first["state"] == "blocked" and first["blocker"] == "authentication_failed"
        assert calls == 1
        assert (await r.refreshed()).settings.orchestrator.session_id == old
        rows = await db.fetchall("SELECT id,metadata FROM sessions WHERE project_id = ?", (project.id,))
        assert len(rows) == 1 and rows[0]["id"] == old
        with pytest.raises(ControlConflict, match="reused"):
            await send(r.orch, project, reason="another request")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_stale_revision_never_creates_a_preparation_or_receipt(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        with pytest.raises(ControlConflict):
            await r.orch.replace_command(
                project.id, "refresh", principal=operator(), client_operation_id="stale-at-start",
                expected_entity_revision=project.entity_revision + 1,
                expected_coordinator_session_id=project.settings.orchestrator.session_id,
            )
        assert await db.fetchone("SELECT id FROM coordinator_handoffs WHERE project_id = ?", (project.id,)) is None
        assert (await r.refreshed()).settings.orchestrator.session_id == project.settings.orchestrator.session_id
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_model_absent_keeps_old_office(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)

        async def absent(_url: str, _key: str | None) -> dict[str, Any]:
            return {"base_url": "http://127.0.0.1:1/v1", "models": ["another-model"]}

        r.orch.catalogue_lookup = absent
        result = await send(r.orch, project)
        assert result["state"] == "blocked" and result["blocker"] == "model_not_advertised"
        assert (await r.refreshed()).settings.orchestrator.session_id == project.settings.orchestrator.session_id
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_production_discovery_path_uses_configured_endpoint_without_inference(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        preset = r.manager.config.presets[r.manager.config.orchestrator_preset(project.settings.orchestrator.model)]
        r.provider.endpoint.id = preset.provider
        r.provider.endpoint.base_url = "http://127.0.0.1:1/v1"
        r.provider.endpoint.api_key = "test-key"
        r.manager.providers.rungs_for = lambda _config, _preset=None: [(r.provider, preset.model)]  # type: ignore[method-assign]
        observed = []

        async def catalogue(url: str, key: str | None) -> dict[str, Any]:
            observed.append((url, key))
            return {"base_url": url, "models": [preset.model]}

        monkeypatch.setattr(api, "lookup_openai_models", catalogue)
        r.orch.catalogue_lookup = None
        result = await send(r.orch, project)
        assert result["state"] == "retirement_pending"
        assert observed == [("http://127.0.0.1:1/v1", "test-key")]
        assert not r.provider.requests
        stored = await db.fetchone("SELECT readiness_json FROM coordinator_handoffs WHERE project_id = ?", (project.id,))
        assert stored is not None and "test-key" not in stored["readiness_json"]
        assert "http://127.0.0.1:1/v1" not in stored["readiness_json"]
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_project_model_change_during_catalogue_probe_blocks_the_swap(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        old = project.settings.orchestrator.session_id
        model = r.manager.config.presets[r.manager.config.orchestrator_preset(project.settings.orchestrator.model)].model

        async def changed(_url: str, _key: str | None) -> dict[str, Any]:
            await db.execute("UPDATE projects SET settings = json_set(settings,'$.orchestrator.model',?) WHERE id = ?",
                             (FALLBACK_PRESET, project.id))
            return {"base_url": "http://127.0.0.1:1/v1", "models": [model]}

        r.orch.catalogue_lookup = changed
        result = await send(r.orch, project)
        assert result["state"] == "blocked" and result["blocker"] == "configuration_changed"
        assert (await r.refreshed()).settings.orchestrator.session_id == old
        assert await db.fetchone("SELECT id FROM sessions WHERE project_id = ? AND id != ?", (project.id, old)) is None
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_old_office_can_change_settings_while_replacement_checks_catalogue(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        old = project.settings.orchestrator.session_id
        model = r.manager.config.presets[r.manager.config.orchestrator_preset(project.settings.orchestrator.model)].model
        entered = asyncio.Event()
        release = asyncio.Event()

        async def pending(_url: str, _key: str | None) -> dict[str, Any]:
            entered.set()
            await release.wait()
            return {"base_url": "http://127.0.0.1:1/v1", "models": [model]}

        r.orch.catalogue_lookup = pending
        replacing = asyncio.create_task(send(r.orch, project))
        await asyncio.wait_for(entered.wait(), 2)
        assert await asyncio.wait_for(r.orch.model_chosen(old, FALLBACK_PRESET), 2)
        release.set()
        blocked = await replacing
        assert blocked["state"] == "blocked" and blocked["blocker"] == "configuration_changed"
        after = await r.refreshed()
        assert after.settings.orchestrator.session_id == old
        assert after.settings.orchestrator.model == FALLBACK_PRESET
    finally:
        release.set()
        await close_team(r.manager)
        await r.manager.close()


async def test_interrupted_preparation_recovers_one_stable_blocked_response(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        old = project.settings.orchestrator.session_id
        handoff_id = "interrupted-handoff"
        successor = "prepared-only"
        request = {"reason": "fresh context", "expected_entity_revision": project.entity_revision,
                   "expected_coordinator_session_id": old}
        await r.orch._new_session(project, predecessor=old, reason="fresh context",
                                  prepared_handoff_id=handoff_id, session_id=successor)
        await db.execute("INSERT INTO coordinator_handoffs(id,project_id,actor_id,client_operation_id,request_digest,"
                         "expected_entity_revision,old_session_id,replacement_session_id,reason,state,created_at,updated_at)"
                         " VALUES (?,?,?,?,?,?,?,?,?,'preparing',?,?)",
                         (handoff_id, project.id, operator().actor_id, "replace-one", digest(request),
                          project.entity_revision, old, successor, "fresh context", "2026-10-03", "2026-10-03"))
        recovered = await send(r.orch, project)
        assert recovered == {"handoff_id": handoff_id, "state": "blocked", "old_active": True,
                             "receipt_id": None, "blocker": "preparation_interrupted"}
        assert await send(r.orch, project) == recovered
        assert (await r.refreshed()).settings.orchestrator.session_id == old
        assert await r.manager.get_state(successor) is None
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_swap_receipt_and_retirement_recover_after_failure(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        old = project.settings.orchestrator.session_id
        await db.execute("INSERT INTO schedules(id,name,prompt,workspace,created_at,target_session,enabled,next_run_at,kind)"
                         " VALUES ('wake-one','wake','review work','',?,?,1,?,'wake')",
                         ("2026-10-03T00:00:00+00:00", old, "2026-10-04T00:00:00+00:00"))
        model = r.manager.config.presets[r.manager.config.orchestrator_preset(project.settings.orchestrator.model)].model

        async def advertised(_url: str, _key: str | None) -> dict[str, Any]:
            return {"base_url": "http://127.0.0.1:1/v1", "models": [model]}

        r.orch.catalogue_lookup = advertised
        retire = r.orch._retire

        async def interrupted(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("retirement interrupted")

        r.orch._retire = interrupted  # type: ignore[method-assign]
        first = await send(r.orch, project)
        assert first["state"] == "retirement_pending" and first["session_id"] != old
        assert first["entity_revision"] == project.entity_revision + 1
        assert first["schedules_needing_approval"] == 1
        assert (await r.refreshed()).settings.orchestrator.session_id == first["session_id"]
        assert await send(r.orch, project) == first
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM coordinator_handoffs WHERE project_id = ?", (project.id,)))["n"] == 1
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM operation_receipts WHERE id = ?", (first["receipt_id"],)))["n"] == 1
        async with db.transaction() as conn:
            async with conn.execute("SELECT metadata FROM sessions WHERE id = ?", (old,)) as cursor:
                stale = await cursor.fetchone()
            assert json.loads(stale["metadata"]).get("orchestrator_of") is None
        r.orch._retire = retire  # type: ignore[method-assign]
        await CoordinatorHandoff(r.orch).reconcile(project.id)
        status = await CoordinatorHandoff(r.orch).view(project.id)
        assert status is not None and status["state"] == "completed" and status["new_active"]
        assert status["schedules_needing_approval"] == 1
        assert (await r.manager.get_state(old)).metadata["successor"] == first["session_id"]
        schedule = await db.fetchone("SELECT target_session,authority_state,grant_id,schedule_revision FROM schedules WHERE id = 'wake-one'")
        assert (schedule["target_session"], schedule["authority_state"], schedule["grant_id"],
                schedule["schedule_revision"]) == (first["session_id"], "needs_approval", None, 2)
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_handoff_retires_approved_wake_and_cancels_only_pending_delivery(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        old = project.settings.orchestrator.session_id
        recurring = Recurring(r.orch.app)
        revision = await db.fetchone(
            "SELECT revision FROM domain_collection_revisions WHERE scope_kind = 'project' AND scope_id = ?",
            (project.id,),
        )
        created = await recurring.create(
            operator(), name="Review reminder", prompt="Review the result", cron=None,
            run_at="2026-01-01T00:00:00Z", kind="wake", target_session=old,
            project_id=project.id, expires_at=(datetime.now(UTC) + timedelta(hours=23)).isoformat(),
            expected_collection_revision=revision["revision"], client_operation_id="wake-before-handoff",
        )
        cycle = await recurring.reserve(created["id"])
        assert cycle is not None
        before = await db.fetchone("SELECT grant_id FROM schedules WHERE id = ?", (created["id"],))
        model = r.manager.config.presets[r.manager.config.orchestrator_preset(project.settings.orchestrator.model)].model

        async def advertised(_url: str, _key: str | None) -> dict[str, Any]:
            return {"models": [model]}

        r.orch.catalogue_lookup = advertised
        result = await send(r.orch, await r.refreshed())
        assert result["state"] == "retirement_pending"
        assert result["schedules_needing_approval"] == 1
        schedule = await db.fetchone(
            "SELECT target_session,grant_id,authority_state,schedule_revision FROM schedules WHERE id = ?",
            (created["id"],),
        )
        assert (schedule["target_session"], schedule["grant_id"], schedule["authority_state"],
                schedule["schedule_revision"]) == (result["session_id"], None, "needs_approval", 2)
        grant = await db.fetchone("SELECT revoked_at FROM actor_grants WHERE id = ?", (before["grant_id"],))
        effect = await db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (cycle["effect_id"],))
        history = await db.fetchone(
            "SELECT target_session,schedule_revision FROM recurring_cycles WHERE id = ?", (cycle["id"],),
        )
        assert grant["revoked_at"] is not None and effect["state"] == "cancelled"
        assert (history["target_session"], history["schedule_revision"]) == (old, 1)
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_reply_lost_after_swap_replays_the_receipt_and_retires_old_office(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        old = project.settings.orchestrator.session_id
        service = CoordinatorHandoff(r.orch)
        original_swap = CoordinatorHandoff._swap

        async def lost_reply(self: CoordinatorHandoff, *args: Any, **kwargs: Any) -> dict[str, Any]:
            await original_swap(self, *args, **kwargs)
            raise OSError("connection closed after commit")

        monkeypatch.setattr(CoordinatorHandoff, "_swap", lost_reply)
        with pytest.raises(OSError, match="after commit"):
            await send(r.orch, project)
        assert (await r.refreshed()).settings.orchestrator.session_id != old
        monkeypatch.setattr(CoordinatorHandoff, "_swap", original_swap)
        replay = await send(r.orch, project)
        assert replay["state"] == "retirement_pending" and replay["receipt_id"]
        assert (await service.view(project.id))["state"] == "completed"
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM coordinator_handoffs WHERE project_id = ?", (project.id,)))["n"] == 1
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_an_admitted_old_mutation_fences_replacement_until_its_write_finishes(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        old = project.settings.orchestrator.session_id
        model = r.manager.config.presets[r.manager.config.orchestrator_preset(project.settings.orchestrator.model)].model

        async def advertised(_url: str, _key: str | None) -> dict[str, Any]:
            return {"base_url": "http://127.0.0.1:1/v1", "models": [model]}

        r.orch.catalogue_lookup = advertised
        entered = asyncio.Event()
        release = asyncio.Event()
        real_dispatch = orchestrator_ops.dispatch

        async def paused(_orch: Any, operation: str, _ops: Any, **kwargs: Any) -> str:
            assert operation == "journal" and kwargs["session_id"] == old
            await r.orch.current(old)
            entered.set()
            await release.wait()
            await r.manager.projects.record(project.id, "orchestrator", "decision", "old command completed")
            return "written"

        monkeypatch.setattr(orchestrator_ops, "dispatch", paused)
        writing = asyncio.create_task(r.orch.service("journal", session_id=old, op="write", text="old command completed"))
        await asyncio.wait_for(entered.wait(), 2)
        first = await send(r.orch, project)
        assert first["state"] == "blocked" and first["blocker"] == "mutation_in_flight"
        assert (await r.refreshed()).settings.orchestrator.session_id == old
        assert await db.fetchone("SELECT id FROM operation_receipts WHERE operation_kind = 'orchestrator.replace'") is None
        release.set()
        assert await writing == "written"
        monkeypatch.setattr(orchestrator_ops, "dispatch", real_dispatch)
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM project_journal WHERE project_id = ? AND text = ?",
                                  (project.id, "old command completed")))["n"] == 1
        second = await send(r.orch, await r.refreshed(), operation_id="replace-after-write")
        assert second["state"] == "retirement_pending"
        assert (await r.refreshed()).settings.orchestrator.session_id != old
        with pytest.raises(NotCurrent):
            await r.orch.service("journal", session_id=old, op="write", text="late command")
    finally:
        release.set()
        await close_team(r.manager)
        await r.manager.close()


async def test_old_session_model_choice_cannot_change_successor_after_handoff(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        old = project.settings.orchestrator.session_id
        old_model = project.settings.orchestrator.model
        model = r.manager.config.presets[r.manager.config.orchestrator_preset(old_model)].model

        async def advertised(_url: str, _key: str | None) -> dict[str, Any]:
            return {"base_url": "http://127.0.0.1:1/v1", "models": [model]}

        r.orch.catalogue_lookup = advertised
        entered = asyncio.Event()
        release = asyncio.Event()
        actual_current = r.orch.current
        seen = False

        async def pause_after_check(session_id: str) -> Any:
            nonlocal seen
            value = await actual_current(session_id)
            if session_id == old and not seen:
                seen = True
                entered.set()
                await release.wait()
            return value

        monkeypatch.setattr(r.orch, "current", pause_after_check)
        selecting = asyncio.create_task(r.orch.model_chosen(old, FALLBACK_PRESET))
        await asyncio.wait_for(entered.wait(), 2)
        replacement = await send(r.orch, project)
        assert replacement["state"] == "retirement_pending"
        release.set()
        assert await selecting is False
        after = await r.refreshed()
        assert after.settings.orchestrator.session_id == replacement["session_id"]
        assert after.settings.orchestrator.model == old_model
    finally:
        release.set()
        await close_team(r.manager)
        await r.manager.close()


async def test_old_run_completion_cannot_mark_successor_results_after_handoff(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        old = project.settings.orchestrator.session_id
        model = r.manager.config.presets[r.manager.config.orchestrator_preset(project.settings.orchestrator.model)].model

        async def advertised(_url: str, _key: str | None) -> dict[str, Any]:
            return {"base_url": "http://127.0.0.1:1/v1", "models": [model]}

        r.orch.catalogue_lookup = advertised
        entered = asyncio.Event()
        release = asyncio.Event()
        actual_fetch = db.fetchone
        marked: list[str] = []

        async def pause_run_lookup(query: str, params: Any = ()) -> Any:
            if query == "SELECT created_at FROM runs WHERE id = ?":
                entered.set()
                await release.wait()
            return await actual_fetch(query, params)

        async def marked_results(project_id: str, _started_at: str) -> list[int]:
            marked.append(project_id)
            return []

        monkeypatch.setattr(db, "fetchone", pause_run_lookup)
        monkeypatch.setattr(r.orch, "turn_ended", marked_results)
        finishing = asyncio.create_task(r.orch.on_run_finished(old, "finished-old-run", "completed"))
        await asyncio.wait_for(entered.wait(), 2)
        result = await send(r.orch, project)
        assert result["state"] == "retirement_pending"
        release.set()
        await finishing
        assert marked == []
    finally:
        release.set()
        await close_team(r.manager)
        await r.manager.close()


async def test_delayed_old_session_deletion_cannot_disable_successor(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        old = project.settings.orchestrator.session_id
        model = r.manager.config.presets[r.manager.config.orchestrator_preset(project.settings.orchestrator.model)].model

        async def advertised(_url: str, _key: str | None) -> dict[str, Any]:
            return {"base_url": "http://127.0.0.1:1/v1", "models": [model]}

        r.orch.catalogue_lookup = advertised
        entered = asyncio.Event()
        release = asyncio.Event()
        actual_fetch = db.fetchone

        async def pause_after_old_lookup(query: str, params: Any = ()) -> Any:
            value = await actual_fetch(query, params)
            if query.startswith("SELECT id FROM projects WHERE json_extract(settings") and params == (old,):
                entered.set()
                await release.wait()
            return value

        monkeypatch.setattr(db, "fetchone", pause_after_old_lookup)
        deleting = asyncio.create_task(r.orch.on_session_deleted(old))
        await asyncio.wait_for(entered.wait(), 2)
        result = await send(r.orch, project)
        assert result["state"] == "retirement_pending"
        release.set()
        await deleting
        after = await r.refreshed()
        assert after.settings.orchestrator.session_id == result["session_id"]
        assert after.settings.orchestrator.enabled
    finally:
        release.set()
        await close_team(r.manager)
        await r.manager.close()


async def test_deletion_inside_office_change_blocks_handoff_until_disabled(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await prepared_rig(settings, db, tmp_path)
    release = asyncio.Event()
    try:
        project = await r.orch.enable(r.project.id)
        old = project.settings.orchestrator.session_id
        model = r.manager.config.presets[r.manager.config.orchestrator_preset(project.settings.orchestrator.model)].model
        entered = asyncio.Event()
        update = r.manager.projects.update_orchestrator

        async def pause_disable(project_id: str, **changes: Any) -> Any:
            if changes.get("enabled") is False:
                entered.set()
                await release.wait()
            return await update(project_id, **changes)

        async def advertised(_url: str, _key: str | None) -> dict[str, Any]:
            return {"models": [model]}

        monkeypatch.setattr(r.manager.projects, "update_orchestrator", pause_disable)
        r.orch.catalogue_lookup = advertised
        deleting = asyncio.create_task(r.orch.on_session_deleted(old))
        await asyncio.wait_for(entered.wait(), 2)
        replacing = asyncio.create_task(send(r.orch, project))
        await asyncio.sleep(0)
        assert not replacing.done()
        release.set()
        await deleting
        result = await replacing
        assert result["state"] == "blocked"
        after = await r.refreshed()
        assert not after.settings.orchestrator.enabled and not after.settings.orchestrator.session_id
        assert await db.fetchone("SELECT id FROM operation_receipts WHERE operation_kind = 'orchestrator.replace'") is None
    finally:
        release.set()
        await close_team(r.manager)
        await r.manager.close()
