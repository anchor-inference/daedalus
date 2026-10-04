"""Create a project's first scoped task and goal under one authenticated operator command."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from daedalus.extensions.board_commands import insert_task
from daedalus.host.events import AppEvent, EventBus
from daedalus.stores.control import ControlStore, Entity, Mutation, Principal, Scope, now
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec, ProjectError, ProjectSettings, ProjectStore, normalise_root


async def _rows(conn: aiosqlite.Connection, sql: str) -> list[aiosqlite.Row]:
    async with conn.execute(sql) as cursor:
        return list(await cursor.fetchall())


class ProjectStart:
    """The global collection receipt owns project, goal, folder and first contract together."""

    def __init__(self, db: Database, projects: ProjectStore, bus: EventBus) -> None:
        self.db = db
        self.projects = projects
        self.bus = bus
        self.control = ControlStore(db)

    async def create(self, principal: Principal, *, client_operation_id: str,
                     expected_collection_revision: int, name: str, goal: str, constraints: str,
                     task_title: str, checks: list[str], owner_intent: str,
                     folder: FolderSpec | None = None) -> dict[str, Any]:
        if principal.origin_class != "operator":
            raise PermissionError("the first project needs an authenticated operator")
        label, objective, boundaries, title = name.strip(), goal.strip(), constraints.strip(), task_title.strip()
        if not label or len(label) > 80 or not objective or len(objective) > 4000:
            raise ValueError("project name or goal is missing or too long")
        if not boundaries or len(boundaries) > 4000 or not title or len(title) > 200:
            raise ValueError("first task and constraints are required within their bounds")
        if not 1 <= len(checks) <= 12 or any(not text.strip() or len(text.strip()) > 200 for text in checks):
            raise ValueError("one to twelve checkable criteria are required")
        if owner_intent not in ("manual", "later"):
            raise ValueError("choose whether to do the task yourself or assign it later")
        checked = [text.strip() for text in checks]
        managed_root = self.projects._managed_root
        if folder is None and managed_root is None:
            raise ProjectError("automatic project folders are not configured")
        if folder is not None and folder.env not in (None, "container", "host"):
            raise ProjectError("a folder lives in the container or on the host")
        payload = {"name": label, "goal": objective, "constraints": boundaries,
                   "task_title": title, "checks": checked, "owner_intent": owner_intent,
                   "folder": {"path": folder.path, "label": folder.label, "env": folder.env,
                              "readonly": folder.readonly} if folder else None}
        events: list[AppEvent] = []

        async def effect(conn: aiosqlite.Connection, mutation: Mutation) -> dict[str, Any]:
            project_id = mutation.object_id[:12]
            if folder is None:
                assert managed_root is not None
            chosen = normalise_root(folder.path) if folder else managed_root / project_id
            self.projects._refuse_reserved(chosen)
            for row in await _rows(conn, "SELECT path FROM project_folders"):
                other = Path(row["path"])
                if other != chosen and (other in chosen.parents or chosen in other.parents):
                    raise ProjectError("the project folder overlaps another project")
            # A managed directory is created only after commit. A rejected command never leaves one.
            stamp = now()
            env = folder.env if folder and folder.env else self.projects.local_env
            settings = ProjectSettings(snapshots=folder is None, default_env=env)
            folder_id = f"f-{uuid.uuid5(uuid.NAMESPACE_URL, project_id + ':folder').hex[:12]}"
            await conn.execute("INSERT INTO projects(id,name,created_at,settings,system) VALUES (?,?,?,?,?)",
                               (project_id, label, stamp, json.dumps(settings.dump()), ""))
            await conn.execute("INSERT INTO project_folders(id,project_id,path,label,env,is_git,readonly,position,created_at)"
                               " VALUES (?,?,?,?,?,?,?,?,?)",
                               (folder_id, project_id, str(chosen), folder.label.strip() if folder else "", env,
                                int(env == self.projects.local_env and (chosen / ".git").exists()),
                                int(folder.readonly) if folder else 0, 0, stamp))
            await conn.execute("INSERT INTO project_goal_revisions(project_id,goal_revision,body,origin_kind,origin_ref,created_at,checks_json)"
                               " VALUES (?,1,?,'operator',?,?,?)", (project_id, objective, mutation.receipt_id, stamp,
                                                                      json.dumps(checked)))
            for section, body in (("goals", objective), ("constraints", boundaries),
                                  ("done_when", "\n".join(checked))):
                await conn.execute("INSERT INTO project_briefs(project_id,section,body,updated_at,updated_by)"
                                   " VALUES (?,?,?,?,'operator')", (project_id, section, body, stamp))
            await conn.execute("INSERT INTO planning_budgets(project_id,goal_contract_revision,max_depth,max_tasks,max_tokens)"
                               " VALUES (?,1,3,50,100000)", (project_id,))
            task_object = uuid.uuid5(uuid.NAMESPACE_URL, mutation.object_id + ":task").hex
            task = await insert_task(conn, Mutation(mutation.receipt_id, task_object, 1),
                                     scope=Scope("project", project_id), title=title,
                                     acceptance=checked[0], checklist=checked, dependencies=[], priority=3,
                                     brief={"objective": objective, "deliverable": title, "boundaries": boundaries,
                                            "done_when": "\n".join(checked)}, notes="", source_session_id=None,
                                     origin_kind="operator", folder_id=folder_id)
            await conn.execute("INSERT INTO project_journal(project_id,at,author,kind,text,refs_json)"
                               " VALUES (?,?,'operator','decision',?,?)",
                               (project_id, stamp,
                                "The operator will complete the first task." if owner_intent == "manual"
                                else "The first task remains unassigned until a member is chosen.",
                                json.dumps({"task_id": task["task_id"], "owner_intent": owner_intent,
                                            "receipt_id": mutation.receipt_id})))
            for kind, body in (("project.changed", {"change": "created", "actor": "operator",
                                                     "receipt_id": mutation.receipt_id}),
                               ("task.created", {"task_id": task["task_id"], "title": title,
                                                 "actor": "operator", "actor_id": principal.actor_id})):
                events.append(await self.bus.persist_in(conn, kind, body, project_id=project_id))
            return {"project_id": project_id, "task_id": task["task_id"], "goal_revision": 1,
                    "contract_revision": 1, "owner_intent": owner_intent}

        async with self.projects._write, self.bus.transaction_guard():
            receipt = await self.control.mutate(principal, Scope("global", "global"), "project.guided_start",
                                                client_operation_id, expected_collection_revision,
                                                Entity("collection", "global"), payload, effect)
            await self.projects.list()
            if folder is None:
                project = await self.projects.get(receipt["project_id"])
                assert project is not None
                await self.projects.ensure_reachable(project.primary)
            for event in events:
                self.bus.announce_committed(event)
            return receipt
