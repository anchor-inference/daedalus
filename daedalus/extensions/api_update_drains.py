"""Authenticated update preparation with saved state independent of response delivery."""

from __future__ import annotations

import asyncio
import secrets
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.update_drain import UpdateDrainRuntime
from daedalus.stores.control import ControlConflict, ControlDenied, Entity, Principal, Scope

if TYPE_CHECKING:
    from daedalus.app import Application


class DrainBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    policy: Literal['checkpoint_supported', 'stop_all']
    candidate_bot_sha: str = Field(pattern=r'^[0-9a-f]{40}([0-9a-f]{24})?$')
    candidate_core_sha: str = Field(pattern=r'^[0-9a-f]{40}([0-9a-f]{24})?$')
    candidate_compatibility_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_host_generation: int = Field(ge=1)
    expected_collection_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)



class DrainRecheckBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    expected_host_generation: int = Field(ge=1)
    expected_collection_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class DrainDecisionBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    decision: Literal['commit', 'abort']
    candidate_bot_sha: str = Field(pattern=r'^[0-9a-f]{40}([0-9a-f]{24})?$')
    candidate_core_sha: str = Field(pattern=r'^[0-9a-f]{40}([0-9a-f]{24})?$')
    candidate_compatibility_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    checkpoint_refs_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_host_generation: int = Field(ge=1)
    expected_collection_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class DrainRecoveryBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    commit_receipt_id: str = Field(min_length=1)
    installed_bot_sha: str = Field(pattern=r'^[0-9a-f]{40}([0-9a-f]{24})?$')
    installed_core_sha: str = Field(pattern=r'^[0-9a-f]{40}([0-9a-f]{24})?$')
    expected_host_generation: int = Field(ge=1)
    expected_collection_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    async def launcher_authorized(request: Request, _: dict[str, Any] = Depends(auth)) -> Principal:
        path = getattr(getattr(app, 'settings', None), 'supervisor_token_path', None)
        if not isinstance(path, Path):
            raise HTTPException(403, 'the update supervisor is unavailable')
        try:
            expected = path.read_text(encoding='utf-8').strip()
        except OSError as exc:
            raise HTTPException(403, 'the update supervisor is unavailable') from exc
        supplied = request.headers.get('x-daedalus-supervisor-token', '')
        if not expected or not secrets.compare_digest(expected, supplied):
            raise HTTPException(403, 'the update supervisor credential is invalid')
        return Principal('launcher:supervisor', 'operator')

    @api.get('/api/runtime/updates/drain')
    async def read(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        runtime = UpdateDrainRuntime(app)
        return {**await runtime.drains.read(), 'host_generation': app.executions.generation,
                'collection_revision': await runtime.drains.control.revision(
                    Scope('global', 'global'), Entity('collection', 'global'))}

    @api.post('/api/runtime/updates/drain')
    async def prepare(body: DrainBody, principal: Principal = Depends(launcher_authorized)) -> dict[str, Any]:
        runtime = UpdateDrainRuntime(app)
        try:
            return await runtime.prepare(principal, **body.model_dump())
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, 'no such update drain') from exc

    @api.post('/api/runtime/updates/drain/{drain_id}/decision')
    async def decide(drain_id: str, body: DrainDecisionBody,
                     principal: Principal = Depends(launcher_authorized)) -> dict[str, Any]:
        runtime = UpdateDrainRuntime(app)
        try:
            return await runtime.drains.decide(principal, drain_id=drain_id, **body.model_dump())
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, 'no such update drain') from exc

    @api.post('/api/runtime/updates/drain/{drain_id}/recheck')
    async def recheck(drain_id: str, body: DrainRecheckBody,
                      principal: Principal = Depends(launcher_authorized)) -> dict[str, Any]:
        runtime = UpdateDrainRuntime(app)
        try:
            return await runtime.recheck(principal, drain_id=drain_id, **body.model_dump())
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, 'no such update drain') from exc

    @api.post('/api/runtime/updates/drain/{drain_id}/recover')
    async def recover(drain_id: str, body: DrainRecoveryBody,
                      principal: Principal = Depends(launcher_authorized)) -> dict[str, Any]:
        if app.guard.skip_recovery:
            raise HTTPException(409, 'boot recovery is paused after repeated crashes')
        # The launcher reports the pair it started; the successor checks its own
        # checkout before that report can open the admission fence.
        for path, sha in ((app.settings.bot_repo_dir, body.installed_bot_sha),
                          (app.settings.core_repo_dir, body.installed_core_sha)):
            try:
                result = await asyncio.to_thread(subprocess.run, ['git', '-C', str(path), 'rev-parse', 'HEAD'],
                                                 capture_output=True, text=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise HTTPException(409, 'the installed checkout could not be verified') from exc
            if result.returncode or result.stdout.strip() != sha:
                raise HTTPException(409, 'the installed checkout differs from the successor report')
        runtime = UpdateDrainRuntime(app)
        try:
            result = await runtime.drains.recover(principal, drain_id=drain_id, **body.model_dump())
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, 'no such update drain') from exc
        if result['checkpoint_refs']:
            await app.manager.resume_unfinished({reference['run_id'] for reference in result['checkpoint_refs']})
        if effects := app.extensions.get('effects'):
            effects.enable()
        from daedalus.extensions.auto_handoff import (
            recover as recover_handoffs,  # Lazy: only a verified successor may restart saved assignments.
        )

        await recover_handoffs(app)
        return {'command': result, **await runtime.drains.read()}
