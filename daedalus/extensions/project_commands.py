"""Project settings and folder commands with one revision and one durable event per write."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

from daedalus.host.events import AppEvent, EventBus
from daedalus.stores.control import ControlConflict, ControlStore, Entity, Mutation, Principal, Scope, now, one
from daedalus.stores.projects import ENVIRONMENTS, ProjectError, ProjectStore, normalise_root

if TYPE_CHECKING:
    from daedalus.stores.database import Database


async def _rows(conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...]) -> list[aiosqlite.Row]:
    async with conn.execute(sql, params) as cursor:
        return list(await cursor.fetchall())


def _named(rows: list[aiosqlite.Row]) -> str:
    return ", ".join(str(row["title"] or row["id"]) for row in rows)


class ProjectCommands:
    """Only the authenticated operator may change the project's mutable settings and folder map."""

    def __init__(self, db: Database, projects: ProjectStore, bus: EventBus,
                 check_env: Callable[[str | None], None],
                 reload_project: Callable[[str], Awaitable[None]]) -> None:
        self.db = db
        self.projects = projects
        self.bus = bus
        self.control = ControlStore(db)
        self.check_env = check_env
        self.reload_project = reload_project

    async def _mutate(self, principal: Principal, project_id: str, operation: str,
                      client_operation_id: str, expected_entity_revision: int,
                      payload: dict[str, Any], effect: Any, change: str) -> dict[str, Any]:
        if principal.origin_class != "operator":
            raise PermissionError("project settings need an authenticated operator")
        events: list[AppEvent] = []

        async def commit(conn: aiosqlite.Connection, mutation: Mutation) -> dict[str, Any]:
            response = await effect(conn, mutation)
            events.append(await self.bus.persist_in(
                conn, "project.changed", {"change": change, "actor": "operator", "receipt_id": mutation.receipt_id},
                project_id=project_id,
            ))
            return {"project_id": project_id, "change": change, **response}

        async with self.bus.transaction_guard():
            response = await self.control.mutate(
                principal, Scope("project", project_id), operation, client_operation_id,
                expected_entity_revision, Entity("project", project_id), payload, commit,
            )
            await self.projects.list()
            await self.reload_project(project_id)
            if not events:
                row = await self.db.fetchone(
                    "SELECT * FROM app_events WHERE type = 'project.changed'"
                    " AND project_id = ? AND json_extract(payload_json,'$.receipt_id') = ?",
                    (project_id, response["receipt_id"]),
                )
                if row is not None and int(row["seq"]) > self.bus.head:
                    events.append(AppEvent(
                        seq=int(row["seq"]), at=str(row["at"]), type="project.changed",
                        payload=json.loads(row["payload_json"]), project_id=project_id,
                    ))
            for event in events:
                self.bus.announce_committed(event)
        return response

    async def settings(self, principal: Principal, project_id: str, *,
                       client_operation_id: str, expected_entity_revision: int,
                       name: str | None = None, snapshots: bool | None = None,
                       default_env: str | None = None, keep: bool = False,
                       setup_command: str | None = None, archived: bool | None = None) -> dict[str, Any]:
        payload = {"name": name, "snapshots": snapshots, "default_env": default_env, "keep": keep,
                   "setup_command": setup_command, "archived": archived}

        async def effect(conn: aiosqlite.Connection, _: Mutation) -> dict[str, Any]:
            self.check_env(default_env)
            if default_env is not None and default_env not in ENVIRONMENTS:
                raise ProjectError("a project runs its agents in the container or on the host")
            if name is not None and not name.strip():
                raise ProjectError("a project needs a name")
            current = await one(conn, "SELECT name,settings FROM projects WHERE id = ?", (project_id,))
            assert current is not None
            settings_patch: dict[str, Any] = {}
            if archived is not None:
                try:
                    stored = json.loads(current["settings"] or "{}")
                except ValueError:
                    stored = {}
                stored = stored if isinstance(stored, dict) else {}
                # The installation's own projects and a chat's scratch project are not the
                # operator's to put away: the one is always needed, the other goes with its chat.
                if archived and (stored.get("system") or stored.get("ephemeral")):
                    raise ProjectError("only a project of the operator's own can be archived")
                if bool(stored.get("archived", False)) != archived:
                    settings_patch["archived"] = archived
                    await conn.execute(
                        "INSERT INTO project_journal(project_id,at,author,kind,text,refs_json) VALUES (?,?,'operator','note',?,'{}')",
                        (project_id, now(), "archived the project" if archived else "restored the project from the archive"),
                    )
            if snapshots is not None:
                settings_patch["snapshots"] = snapshots
            if default_env is not None:
                settings_patch["default_env"] = default_env
            if setup_command is not None:
                # One line: it runs as ``bash -lc``, where a second line's success would hide the
                # first line's failure; ``&&`` says what should stop the setup.
                if "\n" in setup_command.strip():
                    raise ProjectError("the setup command is one line; join commands with &&")
                settings_patch["setup_command"] = setup_command.strip()
            if keep:
                settings_patch["ephemeral"] = False
            await conn.execute(
                "UPDATE projects SET name = ?,settings = json_patch(CASE WHEN json_valid(settings)"
                " THEN settings ELSE '{}' END,?) WHERE id = ?",
                (current["name"] if name is None else name.strip(), json.dumps(settings_patch), project_id),
            )
            return {}

        change = "kept" if keep else "archived" if archived else "restored" if archived is False else "settings"
        return await self._mutate(principal, project_id, "project.settings", client_operation_id,
                                  expected_entity_revision, payload, effect, change)

    async def add_folder(self, principal: Principal, project_id: str, *,
                         client_operation_id: str, expected_entity_revision: int,
                         path: str, label: str = "", env: str | None = None,
                         readonly: bool = False) -> dict[str, Any]:
        payload = {"path": path, "label": label, "env": env, "readonly": readonly}

        async def effect(conn: aiosqlite.Connection, mutation: Mutation) -> dict[str, Any]:
            self.check_env(env)
            target = normalise_root(path)
            self.projects._refuse_reserved(target)
            folder_env = env or self.projects.local_env
            if folder_env not in ENVIRONMENTS:
                raise ProjectError("a folder lives in the container or on the host")
            existing = await _rows(conn, "SELECT f.path,f.project_id,p.name FROM project_folders f"
                                   " JOIN projects p ON p.id = f.project_id", ())
            for row in existing:
                other = Path(row["path"])
                if other == target and row["project_id"] == project_id:
                    raise ProjectError(f"{row['name']} already has the folder {target}")
                if target in other.parents:
                    raise ProjectError(f"that folder contains the project {row['name']} ({other})")
                if other in target.parents:
                    raise ProjectError(f"that folder is inside the project {row['name']} ({other})")
            position = await one(conn, "SELECT count(*) AS n FROM project_folders WHERE project_id = ?", (project_id,))
            folder_id = f"f-{mutation.object_id[:12]}"
            is_git = folder_env == self.projects.local_env and (target / ".git").exists()
            await conn.execute(
                "INSERT INTO project_folders(id,project_id,path,label,env,is_git,readonly,position,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (folder_id, project_id, str(target), label.strip(), folder_env, int(is_git),
                 int(readonly), int(position["n"]), now()),
            )
            await self._journal(conn, project_id, folder_id, f"added the folder {target} ({folder_env}{', read-only' if readonly else ''})")
            return {"folder_id": folder_id}

        async with self.projects._write:
            return await self._mutate(principal, project_id, "project.folder.add", client_operation_id,
                                      expected_entity_revision, payload, effect, "folders")

    async def change_folder(self, principal: Principal, project_id: str, folder_id: str, *,
                            client_operation_id: str, expected_entity_revision: int,
                            label: str | None = None, readonly: bool | None = None,
                            position: int | None = None) -> dict[str, Any]:
        payload = {"folder_id": folder_id, "label": label, "readonly": readonly, "position": position}

        async def effect(conn: aiosqlite.Connection, _: Mutation) -> dict[str, Any]:
            if position is not None and position < 0:
                raise ProjectError("a folder position is zero or greater")
            folders = await _rows(conn, "SELECT * FROM project_folders WHERE project_id = ? ORDER BY position,created_at,id", (project_id,))
            current = next((row for row in folders if row["id"] == folder_id), None)
            if current is None:
                raise KeyError(folder_id)
            wanted = min(position, len(folders) - 1) if position is not None else current["position"]
            if (position is not None and
                    ((wanted == 0 and current["position"] != 0) or (current["position"] == 0 and wanted != 0))):
                working = await self._sessions(conn, project_id, None)
                if working:
                    raise ControlConflict(f"{_named(working)} works in the primary folder; changing which folder is first would move it")
            await conn.execute("UPDATE project_folders SET label = ?,readonly = ? WHERE id = ?",
                               (current["label"] if label is None else label.strip(),
                                current["readonly"] if readonly is None else int(readonly), folder_id))
            if position is not None:
                ordered = [row for row in folders if row["id"] != folder_id]
                ordered.insert(wanted, current)
                for index, row in enumerate(ordered):
                    await conn.execute("UPDATE project_folders SET position = ? WHERE id = ?", (index, row["id"]))
            changes = [
                *([f"label {label.strip()!r}"] if label is not None and label.strip() != current["label"] else []),
                *(["read-only" if readonly else "writable"] if readonly is not None and bool(readonly) != bool(current["readonly"]) else []),
                *([f"position {wanted}"] if position is not None and wanted != current["position"] else []),
            ]
            if changes:
                await self._journal(conn, project_id, folder_id,
                                    f"changed the folder {current['path']}: {', '.join(changes)}")
            return {"folder_id": folder_id}

        async with self.projects._write:
            return await self._mutate(principal, project_id, "project.folder.change", client_operation_id,
                                      expected_entity_revision, payload, effect, "folders")

    async def remove_folder(self, principal: Principal, project_id: str, folder_id: str, *,
                            client_operation_id: str, expected_entity_revision: int) -> dict[str, Any]:
        payload = {"folder_id": folder_id}

        async def effect(conn: aiosqlite.Connection, _: Mutation) -> dict[str, Any]:
            folders = await _rows(conn, "SELECT * FROM project_folders WHERE project_id = ? ORDER BY position,created_at,id", (project_id,))
            current = next((row for row in folders if row["id"] == folder_id), None)
            if current is None:
                raise KeyError(folder_id)
            if len(folders) == 1:
                raise ControlConflict(f"{current['path']} is the only folder of this project")
            working = await self._sessions(conn, project_id, folder_id,
                                           include_primary=current["position"] == 0)
            if working:
                raise ControlConflict(f"{_named(working)} works in {current['path']}; move or remove it first")
            await conn.execute("DELETE FROM project_folders WHERE id = ? AND project_id = ?", (folder_id, project_id))
            for index, row in enumerate(item for item in folders if item["id"] != folder_id):
                await conn.execute("UPDATE project_folders SET position = ? WHERE id = ?", (index, row["id"]))
            await self._journal(conn, project_id, folder_id, f"removed the folder {current['path']}")
            return {"folder_id": folder_id}

        async with self.projects._write:
            return await self._mutate(principal, project_id, "project.folder.remove", client_operation_id,
                                      expected_entity_revision, payload, effect, "folders")

    @staticmethod
    async def _sessions(conn: aiosqlite.Connection, project_id: str, folder_id: str | None,
                        *, include_primary: bool = False) -> list[aiosqlite.Row]:
        named = "COALESCE(json_extract(CASE WHEN json_valid(metadata) THEN metadata ELSE '{}' END,'$.folder_id'),'')"
        if folder_id is None:
            predicate, params = f"{named} = ''", (project_id,)
        elif include_primary:
            predicate, params = f"({named} = '' OR {named} = ?)", (project_id, folder_id)
        else:
            predicate, params = f"{named} = ?", (project_id, folder_id)
        return await _rows(conn, f"SELECT id,title FROM sessions WHERE project_id = ? AND {predicate}", params)

    @staticmethod
    async def _journal(conn: aiosqlite.Connection, project_id: str, folder_id: str, text: str) -> None:
        await conn.execute(
            "INSERT INTO project_journal(project_id,at,author,kind,text,refs_json)"
            " VALUES (?,?,'system','folder',?,?)",
            (project_id, now(), text, json.dumps({"folder_id": folder_id})),
        )
