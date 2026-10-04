"""Stopping a CLI attempt needs exact ownership and an empty kernel observation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from daedalus.extensions import resource_runtime
from daedalus.stores.control import ControlConflict

TARGET = {"scope": {"attempt_id": "attempt-one", "host_generation": "7",
                    "launch_id": "launch-one"}, "daemon_instance": "daemon-one", "env": "container"}


class Database:
    @asynccontextmanager
    async def transaction(self):
        yield object()


class Terminals:
    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.kills = []
        self.releases = []

    async def kill_attempt(self, env, scope, *, daemon_instance):
        self.kills.append((env, scope, daemon_instance))
        if self.error:
            raise self.error
        return self.response

    async def release_attempt(self, env, scope, *, daemon_instance):
        self.releases.append((env, scope, daemon_instance))


def app(generation: int, terminals: Terminals):
    async def host(conn):
        return generation

    return SimpleNamespace(db=Database(), executions=SimpleNamespace(_host=host),
                           extensions={"terminals": terminals})


async def test_stop_rejects_a_previous_host_generation_before_signalling(monkeypatch):
    async def owned(_app, _attempt):
        return TARGET

    monkeypatch.setattr(resource_runtime, "binding", owned)
    terminals = Terminals()
    with pytest.raises(ControlConflict, match="host generation changed"):
        await resource_runtime.stop(app(8, terminals), "attempt-one")
    assert terminals.kills == []


async def test_unknown_owner_does_not_signal_any_process(monkeypatch):
    async def unknown(_app, _attempt):
        return None

    monkeypatch.setattr(resource_runtime, "binding", unknown)
    terminals = Terminals()
    assert not await resource_runtime.stop(app(7, terminals), "attempt-one")
    assert terminals.kills == []


@pytest.mark.parametrize("result", [
    {"complete": False, "evidence": {"enforced": True, "populated": False}},
    {"complete": True, "evidence": {"enforced": True, "populated": True}},
    {"complete": True, "evidence": {"enforced": False, "populated": False}},
])
async def test_incomplete_or_unproved_stop_cannot_release_an_orphan(monkeypatch, result):
    async def owned(_app, _attempt):
        return TARGET

    observations = []

    async def record(_app, _target, *, kind, observation):
        observations.append((kind, observation))
        return observation["evidence"]["populated"] is False

    monkeypatch.setattr(resource_runtime, "binding", owned)
    monkeypatch.setattr(resource_runtime, "record", record)
    terminals = Terminals(response=result)
    assert not await resource_runtime.stop(app(7, terminals), "attempt-one")
    assert observations == [("sample", result)]
    assert terminals.releases == []


async def test_daemon_restart_keeps_stop_unknown_without_a_broad_kill(monkeypatch):
    async def owned(_app, _attempt):
        return TARGET

    observations = []

    async def record(_app, _target, *, kind, observation):
        observations.append((kind, observation))
        return False

    monkeypatch.setattr(resource_runtime, "binding", owned)
    monkeypatch.setattr(resource_runtime, "record", record)
    terminals = Terminals(error=RuntimeError("daemon generation changed"))
    assert not await resource_runtime.stop(app(7, terminals), "attempt-one")
    assert observations == [("unknown", {"enforced": False, "reason": "daemon generation changed"})]
    assert terminals.releases == []


async def test_verified_empty_handle_is_released(monkeypatch):
    async def owned(_app, _attempt):
        return TARGET

    async def record(_app, _target, *, kind, observation):
        assert kind == "stop" and observation["complete"] is True
        return True

    monkeypatch.setattr(resource_runtime, "binding", owned)
    monkeypatch.setattr(resource_runtime, "record", record)
    terminals = Terminals(response={"complete": True, "evidence": {"enforced": True, "populated": False}})
    assert await resource_runtime.stop(app(7, terminals), "attempt-one")
    assert terminals.releases == [("container", TARGET["scope"], "daemon-one")]
