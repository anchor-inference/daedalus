"""Authenticated runtime observations used by contextual reconnect controls."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.browser.model import NotFound
from daedalus.extensions.browser_state import browser_ownership
from daedalus.extensions.runtime_hosts import RuntimeHosts
from daedalus.extensions.runtime_state import session_runtime_state, task_runtime_state
from daedalus.extensions.runtime_transfers import RuntimeTransfers
from daedalus.stores.control import ControlConflict, ControlDenied, Principal

if TYPE_CHECKING:
    from daedalus.app import Application


class HostEnrollBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=120)
    public_key: str = Field(min_length=40, max_length=48)
    ssh_host: str | None = Field(default=None, min_length=1, max_length=253)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, min_length=1, max_length=64)
    remote_root: str | None = Field(default=None, min_length=2, max_length=256)
    expected_collection_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class HostChallengeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_collection_revision: int = Field(ge=1)
    expected_host_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)
    candidate_public_key: str | None = Field(default=None, min_length=40, max_length=48)


class HostProbeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_collection_revision: int = Field(ge=1)
    expected_host_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class HostObservationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(min_length=1, max_length=80)
    nonce: str = Field(min_length=1, max_length=80)
    public_key: str = Field(min_length=40, max_length=48)
    signature: str = Field(min_length=80, max_length=96)


class HostDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    reason: str = Field(min_length=1, max_length=1000)
    observed_generation: int = Field(ge=1)
    expected_host_revision: int = Field(ge=1)
    expected_collection_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class TransferStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_id: str = Field(min_length=1, max_length=100)
    host_id: str = Field(min_length=1, max_length=100)
    expected_entity_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


class TransferRetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_entity_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


def _host_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ControlDenied):
        return HTTPException(403, str(exc))
    if isinstance(exc, ControlConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(404, "requested resource not found")
    return HTTPException(422, str(exc))


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def hosts() -> RuntimeHosts:
        return RuntimeHosts(app.db, identity_file=app.settings.remote_ssh_identity_file)

    def transfers() -> RuntimeTransfers:
        return RuntimeTransfers(app)

    def wake_effects() -> None:
        dispatcher = app.extensions.get("effects")
        if dispatcher is not None:
            dispatcher.notify()

    @api.get("/api/artifact-manifests/{manifest_id}/transfer-readiness")
    async def transfer_readiness(manifest_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await transfers().readiness(manifest_id)
        except KeyError as exc:
            raise HTTPException(404, "no such artifact") from exc

    @api.get("/api/artifact-transfers/{transfer_id}")
    async def transfer_view(transfer_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await transfers().view(transfer_id)
        except KeyError as exc:
            raise HTTPException(404, "no such transfer") from exc

    @api.post("/api/projects/{project_id}/artifact-transfers")
    async def start_transfer(project_id: str, body: TransferStartBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            result = await transfers().start(Principal.operator(who), project_id, **body.model_dump())
        except (ControlDenied, ControlConflict, KeyError, ValueError) as exc:
            raise _host_error(exc) from exc
        wake_effects()
        return result

    @api.post("/api/artifact-transfers/{transfer_id}/retry")
    async def retry_transfer(transfer_id: str, body: TransferRetryBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            result = await transfers().retry(Principal.operator(who), transfer_id, **body.model_dump())
        except (ControlDenied, ControlConflict, KeyError, ValueError) as exc:
            raise _host_error(exc) from exc
        wake_effects()
        return result

    @api.get("/api/runtime/hosts")
    async def list_hosts(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        return await hosts().list()

    @api.post("/api/runtime/hosts")
    async def enroll_host(body: HostEnrollBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await hosts().enroll(Principal.operator(who), **body.model_dump())
        except (ControlDenied, ControlConflict, KeyError, ValueError) as exc:
            raise _host_error(exc) from exc

    @api.post("/api/runtime/hosts/{host_id}/challenge")
    async def challenge_host(host_id: str, body: HostChallengeBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await hosts().challenge(Principal.operator(who), host_id, **body.model_dump())
        except (ControlDenied, ControlConflict, KeyError, ValueError) as exc:
            raise _host_error(exc) from exc

    @api.post("/api/runtime/hosts/{host_id}/probe")
    async def probe_host(host_id: str, body: HostProbeBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await hosts().probe(Principal.operator(who), host_id, **body.model_dump())
        except (ControlDenied, ControlConflict, KeyError, ValueError) as exc:
            raise _host_error(exc) from exc

    @api.post("/api/runtime/hosts/{host_id}/observe")
    async def observe_host(host_id: str, body: HostObservationBody) -> dict[str, Any]:
        try:
            return await hosts().observe(host_id, **body.model_dump())
        except (ControlDenied, ControlConflict, KeyError, ValueError) as exc:
            raise _host_error(exc) from exc

    @api.post("/api/runtime/hosts/{host_id}/decision")
    async def decide_host(host_id: str, body: HostDecisionBody, who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await hosts().decide(Principal.operator(who), host_id, **body.model_dump())
        except (ControlDenied, ControlConflict, KeyError, ValueError) as exc:
            raise _host_error(exc) from exc

    @api.get("/api/board/{task_id}/runtime-state")
    async def task_runtime(task_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await task_runtime_state(app, task_id)
        except KeyError as exc:
            raise HTTPException(404, "no such task") from exc

    @api.get("/api/sessions/{session_id}/runtime-state")
    async def session_runtime(session_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await session_runtime_state(app, session_id)
        except KeyError as exc:
            raise HTTPException(404, "no such session") from exc

    @api.get("/api/runtime/browsers/{group_id}/ownership")
    async def browser_runtime(group_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        service = app.extensions.get("browser")
        if service is None:
            raise HTTPException(503, "browser service is unavailable")
        try:
            return await browser_ownership(service, group_id)
        except NotFound as exc:
            raise HTTPException(404, "no such browser") from exc
