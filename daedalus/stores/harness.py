"""The harness tables: the catalog of command-line agents per environment, the launches staff
sessions run in, and how each message reached its CLI.

No SQL on these tables lives anywhere else. The orchestrator's staff tables (sessions, messages,
requests) are read and written by its own store; this one only keys to them.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from daedalus.harness.contract import Catalog, CheckResult, Delivery, InstallInfo, Launch, LoginState
from daedalus.stores.database import Database

ENVIRONMENTS = ("container", "host")


class HarnessStoreError(ValueError):
    """A write the store refuses, with the reason in words."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _list(text: str | None) -> list[Any]:
    try:
        value = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _dict(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True, slots=True)
class CatalogRow:
    env: str
    harness: str
    installed_version: str
    latest_version: str
    install_method: str
    binary_path: str
    logged_in: str
    login_detail: str
    agents: list[dict[str, Any]]
    models: list[str]
    modes: list[str]
    efforts: list[str]
    profiles: list[str]
    self_check: dict[str, Any]
    checked_at: str | None
    latest_checked_at: str | None
    error: str

    @property
    def installed(self) -> bool:
        return bool(self.installed_version)

    def view(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "harness": self.harness,
            "installed": self.installed,
            "installed_version": self.installed_version,
            "latest_version": self.latest_version,
            "install_method": self.install_method,
            "binary_path": self.binary_path,
            "logged_in": self.logged_in,
            "login_detail": self.login_detail,
            "agents": self.agents,
            "models": self.models,
            "modes": self.modes,
            "efforts": self.efforts,
            "profiles": self.profiles,
            "self_check": self.self_check,
            "checked_at": self.checked_at,
            "latest_checked_at": self.latest_checked_at,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class DeliveryRow:
    message_id: str
    launch_id: str
    via: str
    degraded_to: str
    client_ref: str
    enters: int
    written_at: str | None
    submitted_at: str | None
    acknowledged_at: str | None


def _catalog(row: Any) -> CatalogRow:
    return CatalogRow(
        env=row["env"],
        harness=row["harness"],
        installed_version=row["installed_version"],
        latest_version=row["latest_version"],
        install_method=row["install_method"],
        binary_path=row["binary_path"],
        logged_in=row["logged_in"],
        login_detail=row["login_detail"],
        agents=_list(row["agents_json"]),
        models=_list(row["models_json"]),
        modes=_list(row["modes_json"]),
        efforts=_list(row["efforts_json"]),
        profiles=_list(row["profiles_json"]),
        self_check=_dict(row["self_check_json"]),
        checked_at=row["checked_at"],
        latest_checked_at=row["latest_checked_at"],
        error=row["error"],
    )


def _launch(row: Any) -> Launch:
    return Launch(
        launch_id=row["launch_id"],
        staff_session_id=row["staff_session_id"],
        harness=row["harness"],
        env=row["env"],
        terminal_id=row["terminal_id"],
        companion_terminal_id=row["companion_terminal_id"],
        launch_dir=row["launch_dir"],
        session_ref=row["session_ref"],
        harness_version=row["harness_version"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        adapter_state=row["adapter_state"],
    )


def _delivery(row: Any) -> DeliveryRow:
    return DeliveryRow(
        message_id=row["message_id"],
        launch_id=row["launch_id"],
        via=row["via"],
        degraded_to=row["degraded_to"],
        client_ref=row["client_ref"],
        enters=int(row["enters"]),
        written_at=row["written_at"],
        submitted_at=row["submitted_at"],
        acknowledged_at=row["acknowledged_at"],
    )


_STAMP = {"written": "written_at", "submitted": "submitted_at", "acknowledged": "acknowledged_at"}


class HarnessStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- catalog ---------------------------------------------------------------------------

    async def record_check(self, env: str, harness: str, *, install: InstallInfo, login: LoginState, catalog: Catalog | None = None, error: str = "") -> CatalogRow:
        """What a check found. A check that could not read the catalog keeps the one it had: a list
        of agents that was right an hour ago is more useful to the hiring form than an empty one."""
        if env not in ENVIRONMENTS:
            raise HarnessStoreError(f"an environment is container or host, not {env!r}")
        at = _now()
        fields: dict[str, Any] = {
            "installed_version": install.version if install.installed else "",
            "install_method": install.method if install.installed else "",
            "binary_path": install.path if install.installed else "",
            "logged_in": login.state,
            "login_detail": login.detail[:500],
            "checked_at": at,
            "error": (error or ("" if install.installed else install.detail))[:2000],
        }
        if catalog is not None:
            fields.update(
                agents_json=json.dumps([{"name": a.name, "source": a.source, "description": a.description, "model": a.model} for a in catalog.agents]),
                models_json=json.dumps(list(catalog.models)),
                modes_json=json.dumps(list(catalog.modes)),
                efforts_json=json.dumps(list(catalog.efforts)),
                profiles_json=json.dumps(list(catalog.profiles)),
            )
        await self._upsert(env, harness, fields)
        row = await self.catalog_row(env, harness)
        assert row is not None
        return row

    async def record_latest(self, env: str, harness: str, version: str) -> None:
        await self._upsert(env, harness, {"latest_version": version, "latest_checked_at": _now()})

    async def record_self_check(self, env: str, harness: str, result: CheckResult) -> None:
        check = {
            "ok": result.ok,
            "version": result.version,
            "duration_ms": result.duration_ms,
            "at": _now(),
            "steps": [{"name": s.name, "ok": s.ok, "skipped": s.skipped, "detail": s.detail[:500], "duration_ms": s.duration_ms} for s in result.steps],
        }
        await self._upsert(env, harness, {"self_check_json": json.dumps(check)})

    async def _upsert(self, env: str, harness: str, fields: dict[str, Any]) -> None:
        names = list(fields)
        await self._db.execute(
            f"INSERT INTO harness_catalog(env, harness, {', '.join(names)}) VALUES (?, ?, {', '.join('?' for _ in names)}) "
            f"ON CONFLICT(env, harness) DO UPDATE SET {', '.join(f'{n} = excluded.{n}' for n in names)}",
            (env, harness, *fields.values()),
        )

    async def catalog_row(self, env: str, harness: str) -> CatalogRow | None:
        row = await self._db.fetchone("SELECT * FROM harness_catalog WHERE env = ? AND harness = ?", (env, harness))
        return _catalog(row) if row is not None else None

    async def catalog_rows(self, env: str | None = None) -> list[CatalogRow]:
        if env is None:
            rows = await self._db.fetchall("SELECT * FROM harness_catalog ORDER BY env, harness")
        else:
            rows = await self._db.fetchall("SELECT * FROM harness_catalog WHERE env = ? ORDER BY harness", (env,))
        return [_catalog(r) for r in rows]

    # -- the models offered -----------------------------------------------------------------

    # A choice of the operator's, not something a check found, so it lives beside the catalog rather
    # than in it: a check rewrites the row, and the choice must outlive every check.
    @staticmethod
    def _offered_key(env: str, harness: str) -> str:
        return f"harness.offered_models.{env}.{harness}"

    async def offered_models(self, env: str, harness: str) -> list[str] | None:
        """The models the operator chose to offer of this CLI in ``env``; None when every one is."""
        value = await self._db.kv_get(self._offered_key(env, harness))
        return [str(m) for m in value] if isinstance(value, list) and value else None

    async def set_offered_models(self, env: str, harness: str, models: Sequence[str] | None) -> None:
        """Offer only ``models`` of this CLI in ``env``; None or an empty list offers every one again."""
        if env not in ENVIRONMENTS:
            raise HarnessStoreError(f"an environment is container or host, not {env!r}")
        await self._db.kv_set(self._offered_key(env, harness), list(dict.fromkeys(models)) if models else None)

    # -- launches --------------------------------------------------------------------------

    async def open_launch(self, launch: Launch) -> Launch:
        """Record a launch. A staff session has at most one open launch; the database refuses a
        second, so two starts racing for one session cannot both register hooks."""
        try:
            await self._db.execute(
                "INSERT INTO harness_launches(launch_id, staff_session_id, harness, env, terminal_id, companion_terminal_id, launch_dir, session_ref, harness_version, started_at, adapter_state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    launch.launch_id, launch.staff_session_id, launch.harness, launch.env, launch.terminal_id, launch.companion_terminal_id,
                    launch.launch_dir, launch.session_ref, launch.harness_version, launch.started_at, launch.adapter_state,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HarnessStoreError(f"staff session {launch.staff_session_id} already has an open launch, or the launch id is taken") from exc
        found = await self.launch(launch.launch_id)
        assert found is not None
        return found

    async def update_launch(self, launch_id: str, *, terminal_id: str | None = None, companion_terminal_id: str | None = None, launch_dir: str | None = None, session_ref: str | None = None) -> Launch:
        """Fill in what is learnt after the row exists: the terminals once created, the directory the
        daemon made, the CLI's session id once the CLI reports it."""
        changes = {k: v for k, v in (("terminal_id", terminal_id), ("companion_terminal_id", companion_terminal_id), ("launch_dir", launch_dir), ("session_ref", session_ref)) if v is not None}
        if changes:
            await self._db.execute(f"UPDATE harness_launches SET {', '.join(f'{k} = ?' for k in changes)} WHERE launch_id = ?", (*changes.values(), launch_id))
        found = await self.launch(launch_id)
        if found is None:
            raise HarnessStoreError(f"no launch {launch_id}")
        return found

    async def end_launch(self, launch_id: str) -> bool:
        """End a launch once; true for the call that ended it."""
        async with self._db.transaction() as conn:
            # The adapter's state goes with the launch: a password for a server that no longer runs
            # has no business staying in the database, or in its backups.
            cursor = await conn.execute("UPDATE harness_launches SET ended_at = ?, adapter_state = '' WHERE launch_id = ? AND ended_at IS NULL", (_now(), launch_id))
            ended = cursor.rowcount == 1
            await cursor.close()
        return ended

    async def launch(self, launch_id: str) -> Launch | None:
        row = await self._db.fetchone("SELECT * FROM harness_launches WHERE launch_id = ?", (launch_id,))
        return _launch(row) if row is not None else None

    async def live_launch(self, launch_id: str) -> Launch | None:
        """The launch a hook post names, if it is still open. A post for an ended or unknown launch is
        dropped by the caller: a CLI that outlived its launch must not move a new session's status."""
        row = await self._db.fetchone("SELECT * FROM harness_launches WHERE launch_id = ? AND ended_at IS NULL", (launch_id,))
        return _launch(row) if row is not None else None

    async def open_launch_for(self, staff_session_id: str) -> Launch | None:
        row = await self._db.fetchone("SELECT * FROM harness_launches WHERE staff_session_id = ? AND ended_at IS NULL", (staff_session_id,))
        return _launch(row) if row is not None else None

    async def open_launches(self, env: str | None = None) -> list[Launch]:
        """Every open launch, for the host's reconcile at start."""
        if env is None:
            rows = await self._db.fetchall("SELECT * FROM harness_launches WHERE ended_at IS NULL ORDER BY started_at")
        else:
            rows = await self._db.fetchall("SELECT * FROM harness_launches WHERE ended_at IS NULL AND env = ? ORDER BY started_at", (env,))
        return [_launch(r) for r in rows]

    # -- deliveries ------------------------------------------------------------------------

    async def record_delivery(self, launch_id: str, delivery: Delivery) -> DeliveryRow:
        """Record how far a message got. Facts only accumulate: a later report never blanks the
        channel, the degradation or the client id an earlier one recorded, and each state's time is
        the first time it was reached."""
        stamp = _STAMP.get(delivery.state)
        at = _now()
        await self._db.execute(
            "INSERT INTO harness_deliveries(message_id, launch_id, via, degraded_to, client_ref, written_at, submitted_at, acknowledged_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET "
            "via = CASE WHEN excluded.via != '' THEN excluded.via ELSE via END, "
            "degraded_to = CASE WHEN excluded.degraded_to != '' THEN excluded.degraded_to ELSE degraded_to END, "
            "client_ref = CASE WHEN excluded.client_ref != '' THEN excluded.client_ref ELSE client_ref END, "
            "written_at = COALESCE(written_at, excluded.written_at), "
            "submitted_at = COALESCE(submitted_at, excluded.submitted_at), "
            "acknowledged_at = COALESCE(acknowledged_at, excluded.acknowledged_at)",
            (
                delivery.message_id, launch_id, delivery.via, delivery.degraded_to, delivery.client_ref,
                at if stamp == "written_at" else None, at if stamp == "submitted_at" else None, at if stamp == "acknowledged_at" else None,
            ),
        )
        found = await self.delivery(delivery.message_id)
        assert found is not None
        return found

    async def count_enter(self, message_id: str) -> int:
        """One more Enter sent for a message; the count is what caps the retries across a restart."""
        async with self._db.transaction() as conn:
            await conn.execute("UPDATE harness_deliveries SET enters = enters + 1 WHERE message_id = ?", (message_id,))
            cursor = await conn.execute("SELECT enters FROM harness_deliveries WHERE message_id = ?", (message_id,))
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise HarnessStoreError(f"no delivery recorded for message {message_id}")
        return int(row["enters"])

    async def delivery(self, message_id: str) -> DeliveryRow | None:
        row = await self._db.fetchone("SELECT * FROM harness_deliveries WHERE message_id = ?", (message_id,))
        return _delivery(row) if row is not None else None

    async def deliveries(self, message_ids: Sequence[str]) -> dict[str, DeliveryRow]:
        if not message_ids:
            return {}
        rows = await self._db.fetchall(f"SELECT * FROM harness_deliveries WHERE message_id IN ({', '.join('?' for _ in message_ids)})", tuple(message_ids))
        return {r["message_id"]: _delivery(r) for r in rows}

    async def last_team_call(self, launch_id: str) -> str | None:
        """When the launch last reported through its team tools, from the reports on record: each
        carries the call id the bridge gave it, and every call id begins with the launch's id. A host
        that takes a launch up after a restart never hears the tools' hello again, and this is its
        proof that they were loaded."""
        prefix = f"{launch_id}:"
        row = await self._db.fetchone(
            "SELECT MAX(at) AS at FROM app_events WHERE type = 'staff.report' AND substr(json_extract(payload_json, '$.call_id'), 1, ?) = ?",
            (len(prefix), prefix),
        )
        return str(row["at"]) if row is not None and row["at"] else None

    async def by_client_ref(self, launch_id: str, client_ref: str) -> DeliveryRow | None:
        """The message a CLI's echo names, so an acknowledgement finds its message after a restart."""
        if not client_ref:
            return None
        row = await self._db.fetchone("SELECT * FROM harness_deliveries WHERE launch_id = ? AND client_ref = ?", (launch_id, client_ref))
        return _delivery(row) if row is not None else None


__all__ = ["CatalogRow", "DeliveryRow", "HarnessStore", "HarnessStoreError"]
