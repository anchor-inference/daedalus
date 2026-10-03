"""Operator controls for a project's optional strict CLI resource profile."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope, one
from daedalus.stores.resource_profiles import latest_in, limits, set_profile_in

if TYPE_CHECKING:
    from daedalus.app import Application


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    memory_bytes: int = Field(ge=0)
    cpu_millis: int = Field(ge=0)
    process_count: int = Field(ge=0)
    disk_bytes: int = Field(ge=0)
    min_free_disk_bytes: int = Field(ge=0)
    expected_profile_revision: int | None = Field(ge=1)
    expected_entity_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.get("/api/projects/{project_id}/resource-profile")
    async def read_profile(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async with app.db.transaction() as conn:
            project = await one(conn, "SELECT entity_revision FROM projects WHERE id = ?", (project_id,))
            if project is None:
                raise HTTPException(404, "no such project")
            profile = await latest_in(conn, project_id)
            return {"project_id": project_id, "entity_revision": project["entity_revision"],
                    "configured": profile is not None,
                    "profile_revision": profile["revision"] if profile is not None else None,
                    "state": profile["state"] if profile is not None else "disabled",
                    "limits": limits(profile) if profile is not None else None,
                    "min_free_disk_bytes": profile["min_free_disk_bytes"] if profile is not None else 0,
                    "disk_quota_supported": False}

    @api.put("/api/projects/{project_id}/resource-profile")
    async def write_profile(project_id: str, body: ProfileBody,
                            who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        principal = Principal.operator(who)

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            return await set_profile_in(conn, project_id=project_id, state=body.state,
                                        memory_bytes=body.memory_bytes, cpu_millis=body.cpu_millis,
                                        process_count=body.process_count, disk_bytes=body.disk_bytes,
                                        min_free_disk_bytes=body.min_free_disk_bytes,
                                        expected_profile_revision=body.expected_profile_revision,
                                        receipt_id=mutation.receipt_id)

        try:
            return await ControlStore(app.db).mutate(
                principal, Scope("project", project_id), "resource.profile.set", body.client_operation_id,
                body.expected_entity_revision, Entity("project", project_id), body.model_dump(), effect,
            )
        except KeyError as exc:
            raise HTTPException(404, "no such project") from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/api/admission/resources")
    async def capability(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        terminals = app.extensions.get("terminals")
        if terminals is None:
            return {"container": {"available": False, "reason": "terminal service is unavailable"},
                    "host": {"available": False, "reason": "terminal service is unavailable"},
                    "native": {"available": False, "reason": "in-process runtime cannot be isolated per attempt"},
                    "disk_quota_supported": False}
        states = {}
        for env in ("container", "host"):
            try:
                states[env] = await terminals.containment_capability(env)
            except Exception as exc:
                states[env] = {"available": False, "reason": str(exc)}
        return {**states,
                "native": {"available": False, "reason": "in-process runtime cannot be isolated per attempt"},
                "disk_quota_supported": False}

    @api.get("/api/board/{task_id}/attempts/{attempt_id}/resources")
    async def attempt_resources(task_id: str, attempt_id: str,
                                _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async with app.db.transaction() as conn:
            attempt = await one(conn, "SELECT a.id,a.task_id,a.host_generation,a.runtime_kind,b.state,"
                                "b.profile_revision,b.daemon_instance,b.launch_id,b.limits_json"
                                " FROM execution_attempts a LEFT JOIN attempt_resource_bindings b"
                                " ON b.attempt_id = a.id WHERE a.id = ? AND a.task_id = ?",
                                (attempt_id, task_id))
            if attempt is None:
                raise HTTPException(404, "no such task attempt")
            observation = await one(conn, "SELECT * FROM attempt_resource_observations"
                                    " WHERE attempt_id = ? ORDER BY observed_at DESC,id DESC LIMIT 1",
                                    (attempt_id,))
            disk = await one(conn, "SELECT min_free_disk_bytes,admission_json,preparation_json"
                             " FROM attempt_disk_preflights WHERE attempt_id = ?", (attempt_id,))
            entry = await one(conn, "SELECT observation_json FROM attempt_disk_entry_observations"
                              " WHERE attempt_id = ? ORDER BY observed_at DESC,id DESC LIMIT 1", (attempt_id,))
        if attempt["profile_revision"] is None:
            return {"attempt_id": attempt_id, "kind": "none", "state": "not_configured",
                    "runtime_kind": attempt["runtime_kind"]}
        return {"attempt_id": attempt_id, "kind": "cgroup_v2", "state": attempt["state"],
                "runtime_kind": attempt["runtime_kind"], "profile_revision": attempt["profile_revision"],
                "host_generation": attempt["host_generation"], "daemon_instance": attempt["daemon_instance"],
                "launch_id": attempt["launch_id"], "limits": json.loads(attempt["limits_json"]),
                "observation": dict(observation) if observation is not None else None,
                "disk_preflight": {"min_free_disk_bytes": disk["min_free_disk_bytes"],
                                   "admission": json.loads(disk["admission_json"]),
                                   "preparation": json.loads(disk["preparation_json"]),
                                   "entry": json.loads(entry["observation_json"]) if entry is not None else None}
                                   if disk is not None else None}
