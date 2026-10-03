"""Versioned project ceilings and the evidence binding them to one CLI attempt."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import aiosqlite

from daedalus.stores.control import ControlConflict, canonical, now, one

LIMIT_FIELDS = ("memory_bytes", "cpu_millis", "process_count", "disk_bytes")


def limits(profile: aiosqlite.Row) -> dict[str, int]:
    return {field: int(profile[field]) for field in LIMIT_FIELDS}


async def latest_in(conn: aiosqlite.Connection, project_id: str) -> aiosqlite.Row | None:
    return await one(conn, "SELECT * FROM resource_profile_versions WHERE project_id = ?"
                     " ORDER BY revision DESC LIMIT 1", (project_id,))


async def set_profile_in(conn: aiosqlite.Connection, *, project_id: str, state: str,
                         memory_bytes: int, cpu_millis: int, process_count: int,
                         disk_bytes: int, min_free_disk_bytes: int,
                         expected_profile_revision: int | None, receipt_id: str) -> dict[str, Any]:
    if state not in ("enabled", "disabled"):
        raise ValueError("invalid resource profile state")
    values = (memory_bytes, cpu_millis, process_count, disk_bytes, min_free_disk_bytes)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise ValueError("resource ceilings must be nonnegative integers")
    if state == "enabled" and (memory_bytes < 16 << 20 or cpu_millis < 10 or process_count < 1):
        raise ValueError("enabled ceilings need at least 16 MiB, ten millicores and one process")
    if state == "enabled" and disk_bytes:
        raise ValueError("this environment cannot enforce a disk-space quota")
    if memory_bytes > 1 << 50 or cpu_millis > 100000 or process_count > 65536 or min_free_disk_bytes > 1 << 50:
        raise ValueError("resource ceiling exceeds the supported range")
    previous = await latest_in(conn, project_id)
    if (int(previous["revision"]) if previous is not None else None) != expected_profile_revision:
        raise ControlConflict("the resource profile changed; review current limits before saving")
    revision = int(previous["revision"]) + 1 if previous is not None else 1
    await conn.execute(
        "INSERT INTO resource_profile_versions(project_id,revision,state,memory_bytes,cpu_millis,"
        "process_count,disk_bytes,min_free_disk_bytes,receipt_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (project_id, revision, state, *values, receipt_id, now()),
    )
    return {"project_id": project_id, "profile_revision": revision, "state": state,
            "limits": dict(zip(LIMIT_FIELDS, values[:4], strict=True)),
            "min_free_disk_bytes": min_free_disk_bytes}


def strict_target(profile: aiosqlite.Row, *, env: str, harness: str,
                  capability: dict[str, Any]) -> dict[str, Any]:
    """Reject an unsupported effective boundary before a launch command changes the board."""
    if harness == "daedalus":
        raise ControlConflict("in-process native workers cannot have per-attempt CPU, memory or process ceilings")
    if harness == "opencode":
        raise ControlConflict("OpenCode transcript export needs an exec side channel outside the strict sandbox")
    if int(profile["disk_bytes"]):
        raise ControlConflict("strict disk-space quotas are unavailable on this environment")
    if env not in ("container", "host") or not capability.get("available") or capability.get("kind") != "cgroup_v2":
        raise ControlConflict("the CLI environment has no proven delegated resource containment")
    if capability.get("sandbox") != "ok":
        raise ControlConflict("the CLI environment cannot deny cgroupfs writes inside a sandbox")
    instance = capability.get("daemon_instance")
    if not isinstance(instance, str) or not instance:
        raise ControlConflict("the terminal daemon generation is unknown")
    return {"profile_revision": int(profile["revision"]), "limits": limits(profile),
            "env": env, "daemon_instance": instance,
            "min_free_disk_bytes": int(profile["min_free_disk_bytes"])}


def disk_snapshot(observation: dict[str, Any] | None, *, path: str, minimum: int,
                  admitted: dict[str, Any] | None = None) -> dict[str, Any]:
    """Accept only a fresh daemon observation of the selected workspace volume, never a reservation."""
    if minimum <= 0:
        raise ValueError("a positive free-space threshold is required")
    if not isinstance(observation, dict) or observation.get("available") is not True:
        reason = observation.get("reason") if isinstance(observation, dict) else "no observation"
        raise ControlConflict(f"selected workspace free space is unknown: {reason}")
    expected_path = hashlib.sha256(path.encode()).hexdigest()
    fields = ("free_bytes", "device_id", "mount_id", "observed_at", "path_digest", "workspace_exists")
    if (type(observation.get("free_bytes")) is not int or observation["free_bytes"] < 0
            or any(not isinstance(observation.get(field), str) or not observation[field]
                   for field in fields[1:5]) or observation["path_digest"] != expected_path
            or type(observation.get("workspace_exists")) is not bool):
        raise ControlConflict("selected workspace volume observation is incomplete or stale")
    if observation["free_bytes"] < minimum:
        raise ControlConflict("selected workspace has less free space than the configured minimum")
    if admitted is not None and (observation["device_id"], observation["mount_id"]) != (
            admitted["device_id"], admitted["mount_id"]):
        raise ControlConflict("selected workspace volume changed after the launch command")
    return {field: observation[field] for field in fields}


async def bind_attempt_in(conn: aiosqlite.Connection, *, attempt_id: str, project_id: str,
                          host_generation: int, resource: dict[str, Any]) -> None:
    profile = await latest_in(conn, project_id)
    if (profile is None or profile["state"] != "enabled"
            or int(profile["revision"]) != resource["profile_revision"]
            or limits(profile) != resource["limits"]
            or int(profile["min_free_disk_bytes"]) != resource["min_free_disk_bytes"]):
        raise ControlConflict("the resource profile changed before the attempt was bound")
    await conn.execute(
        "INSERT INTO attempt_resource_bindings(attempt_id,project_id,profile_revision,host_generation,"
        "env,daemon_instance,limits_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (attempt_id, project_id, profile["revision"], str(host_generation), resource["env"],
         resource["daemon_instance"], canonical(resource["limits"]), "reserved", now(), now()),
    )
    if resource["min_free_disk_bytes"]:
        if not resource.get("disk_admission") or not resource.get("disk_preparation") or not resource.get("folder_id"):
            raise ControlConflict("the selected workspace has no complete disk preflight")
        await conn.execute(
            "INSERT INTO attempt_disk_preflights(attempt_id,project_id,profile_revision,folder_id,"
            "min_free_disk_bytes,admission_json,preparation_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (attempt_id, project_id, profile["revision"], resource["folder_id"],
             resource["min_free_disk_bytes"], canonical(resource["disk_admission"]),
             canonical(resource["disk_preparation"]), now()),
        )


async def record_disk_entry_in(conn: aiosqlite.Connection, *, attempt_id: str,
                               observation: dict[str, Any]) -> None:
    pinned = await one(conn, "SELECT admission_json,min_free_disk_bytes FROM attempt_disk_preflights"
                       " WHERE attempt_id = ?",
                       (attempt_id,))
    if pinned is None:
        raise ControlConflict("the attempt has no selected workspace disk preflight")
    admitted = json.loads(pinned["admission_json"])
    if (observation.get("device_id"), observation.get("mount_id"), observation.get("path_digest")) != (
            admitted["device_id"], admitted["mount_id"], admitted["path_digest"]):
        raise ControlConflict("the selected workspace volume or path changed before runtime entry")
    if (type(observation.get("free_bytes")) is not int
            or observation["free_bytes"] < pinned["min_free_disk_bytes"]
            or observation.get("workspace_exists") is not True):
        raise ControlConflict("the selected workspace has no verified free space before runtime entry")
    await conn.execute("INSERT INTO attempt_disk_entry_observations(id,attempt_id,observation_json,observed_at)"
                       " VALUES (?,?,?,?)", (uuid.uuid4().hex, attempt_id, canonical(observation), now()))


async def bind_launch_in(conn: aiosqlite.Connection, *, attempt_id: str,
                         host_generation: int, launch_id: str) -> dict[str, Any]:
    row = await one(conn, "SELECT * FROM attempt_resource_bindings WHERE attempt_id = ?", (attempt_id,))
    if row is None or row["host_generation"] != str(host_generation):
        raise ControlConflict("resource containment belongs to another host generation")
    if row["launch_id"] not in (None, launch_id):
        raise ControlConflict("the attempt already has a different resource launch")
    await conn.execute("UPDATE attempt_resource_bindings SET launch_id = ?,updated_at = ?"
                       " WHERE attempt_id = ? AND host_generation = ?", (launch_id, now(), attempt_id, str(host_generation)))
    return {"scope": {"attempt_id": attempt_id, "host_generation": str(host_generation),
                      "launch_id": launch_id},
            "limits": json.loads(row["limits_json"]), "expected_instance": row["daemon_instance"]}


async def observe_in(conn: aiosqlite.Connection, *, attempt_id: str, launch_id: str,
                     kind: str, evidence: dict[str, Any], terminal_id: str | None = None) -> None:
    row = await one(conn, "SELECT * FROM attempt_resource_bindings WHERE attempt_id = ?", (attempt_id,))
    if row is None or row["launch_id"] != launch_id:
        raise ControlConflict("resource observation has no matching attempt launch")
    if evidence.get("scope") != {"attempt_id": attempt_id, "host_generation": row["host_generation"],
                                 "launch_id": launch_id}:
        raise ControlConflict("resource observation describes another execution")
    if evidence.get("daemon_instance") != row["daemon_instance"]:
        raise ControlConflict("resource observation belongs to a different terminal daemon")
    if kind not in ("spawn", "sample", "stop", "exit", "unknown"):
        raise ValueError("invalid resource observation kind")
    if kind == "unknown":
        previous = await one(conn, "SELECT observation_kind,reason FROM attempt_resource_observations"
                             " WHERE attempt_id = ? ORDER BY observed_at DESC,id DESC LIMIT 1", (attempt_id,))
        if (previous is not None and previous["observation_kind"] == "unknown"
                and previous["reason"] == str(evidence.get("reason") or "")):
            return
    await conn.execute(
        "INSERT INTO attempt_resource_observations(id,attempt_id,host_generation,daemon_instance,launch_id,"
        "terminal_id,observation_kind,enforced,populated,memory_peak_bytes,cpu_usage_usec,processes,"
        "oom_kills,pids_max_events,reason,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, attempt_id, row["host_generation"], row["daemon_instance"], launch_id,
         terminal_id, kind, int(bool(evidence.get("enforced"))),
         int(evidence["populated"]) if evidence.get("populated") is not None else None,
         evidence.get("memory_peak_bytes"), evidence.get("cpu_usage_usec"), evidence.get("processes"),
         evidence.get("oom_kills"), evidence.get("pids_max_events"), str(evidence.get("reason") or ""), now()),
    )
    if kind == "spawn" and evidence.get("enforced"):
        await conn.execute("UPDATE attempt_resource_bindings SET state = 'enforced',updated_at = ?"
                           " WHERE attempt_id = ? AND state = 'reserved'", (now(), attempt_id))
    elif kind == "unknown":
        await conn.execute("UPDATE attempt_resource_bindings SET state = 'unknown',updated_at = ?"
                           " WHERE attempt_id = ?", (now(), attempt_id))
    elif kind in ("exit", "stop") and evidence.get("enforced") and evidence.get("populated") is False:
        await conn.execute("UPDATE attempt_resource_bindings SET state = 'released',updated_at = ?"
                           " WHERE attempt_id = ?", (now(), attempt_id))


async def released_in(conn: aiosqlite.Connection, attempt_id: str) -> bool:
    row = await one(conn, "SELECT state,launch_id FROM attempt_resource_bindings WHERE attempt_id = ?", (attempt_id,))
    if row is None:
        return True
    if row["launch_id"] is None:
        return False
    return row["state"] == "released" and await one(conn,
        "SELECT 1 FROM attempt_resource_observations WHERE attempt_id = ? AND launch_id = ?"
        " AND observation_kind IN ('stop','exit') AND enforced = 1 AND populated = 0",
        (attempt_id, row["launch_id"])) is not None


async def for_staff_in(conn: aiosqlite.Connection, staff_session_id: str) -> dict[str, Any] | None:
    row = await one(conn, "SELECT b.* FROM attempt_resource_bindings b JOIN execution_attempts a"
                    " ON a.id = b.attempt_id WHERE a.staff_session_id = ?", (staff_session_id,))
    if row is None:
        return None
    if row["state"] == "released":
        # Transcript export after proved physical exit is a host read, not worker execution.
        return None
    if not row["launch_id"]:
        raise ControlConflict("the worker's resource attempt has no bound launch")
    return {"scope": {"attempt_id": row["attempt_id"], "host_generation": row["host_generation"],
                      "launch_id": row["launch_id"]},
            "limits": json.loads(row["limits_json"]), "expected_instance": row["daemon_instance"]}
