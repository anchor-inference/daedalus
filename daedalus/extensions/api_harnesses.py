"""The Harnesses routes: the command-line agents of each environment, their catalog for the hiring
form, and the buttons — check, install, update, sign in.

Like ``api.py`` this module may import the HTTP framework; the manager behind it may not. Every
operation that takes minutes answers at once and finishes in the background, reporting through the
event bus (``harness.check``, ``harness.updated``); what can refuse (the environment is down, the CLI
is busy, staff are working on it) refuses before anything starts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from daedalus.harness.capabilities import CAPABILITIES
from daedalus.harness.manager import HarnessManager, HarnessRefused

if TYPE_CHECKING:
    from daedalus.app import Application

Env = Literal["container", "host"]


class EnvBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: Env = "container"


class CheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: Env = "container"
    harness: str | None = None


class ModelsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: Env = "container"
    models: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] | None = Field(default=None, max_length=500)
    """The models to offer; null or an empty list offers every one again."""


def refused(exc: HarnessRefused) -> JSONResponse:
    """A refusal with its reason as ``detail`` (what the app shows), and who is working when that
    is the reason, so the screen can list them."""
    return JSONResponse({"detail": exc.message, "staff": exc.staff}, status_code=exc.status)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def manager() -> HarnessManager:
        found = app.extensions.get("harness")
        if not isinstance(found, HarnessManager):
            raise HTTPException(503, "the harness manager is not running")
        return found

    def known(harness: str) -> str:
        if harness not in CAPABILITIES:
            raise HTTPException(404, f"no command-line harness is called {harness!r}")
        return harness

    @api.get("/api/harnesses")
    async def harnesses(env: Env = "container", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """The Harnesses screen: one row per CLI of ``env``, the pinned Node, and which environments
        can be reached now (the screen offers Host only when the host's terminal service answers)."""
        harness = manager()
        rows = await harness.harnesses(env)
        checked = [r["checked_at"] for r in rows if r["checked_at"]]
        return {
            "env": env,
            "environments": harness.environments(),
            "rows": rows,
            "node": harness.node_state(env),
            "checked_at": max(checked) if checked else None,
            "updates": sum(1 for r in rows if r["update_available"]),
        }

    @api.get("/api/harnesses/catalog")
    async def harness_catalogs(env: Env = "container", folder_id: str | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Every CLI of ``env`` as the hiring form reads it, keyed by harness. With ``folder_id`` the
        agents that folder defines are read now and listed first."""
        return await manager().catalog_view(env, folder_id)

    @api.get("/api/harnesses/{harness}/catalog")
    async def harness_catalog(harness: str, env: Env = "container", folder_id: str | None = None, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        catalog = await manager().catalog(env, known(harness), folder_id)
        return {"env": env, "harness": harness, **asdict(catalog)}

    @api.put("/api/harnesses/{harness}/models")
    async def harness_models(harness: str, body: ModelsBody, _: dict[str, Any] = Depends(auth)) -> Any:
        """Which of a CLI's models the hiring form and the orchestrator offer; every one stays usable."""
        try:
            return {"row": await manager().offer_models(body.env, known(harness), body.models)}
        except HarnessRefused as exc:
            return refused(exc)

    @api.post("/api/harnesses/check")
    async def harness_check(body: CheckBody, _: dict[str, Any] = Depends(auth)) -> Any:
        """Look again now, the latest versions included."""
        try:
            rows = await manager().check(body.env, known(body.harness) if body.harness else None, refresh_latest=True)
        except HarnessRefused as exc:
            return refused(exc)
        return {"env": body.env, "rows": rows, "node": manager().node_state(body.env)}

    @api.post("/api/harnesses/update-all", status_code=202)
    async def harness_update_all(body: EnvBody, _: dict[str, Any] = Depends(auth)) -> Any:
        try:
            return manager().start_update_all(body.env)
        except HarnessRefused as exc:
            return refused(exc)

    @api.post("/api/harnesses/node/install", status_code=202)
    async def harness_node_install(body: EnvBody, _: dict[str, Any] = Depends(auth)) -> Any:
        try:
            return await manager().install_node(body.env)
        except HarnessRefused as exc:
            return refused(exc)

    @api.post("/api/harnesses/{harness}/update", status_code=202)
    async def harness_update(harness: str, body: EnvBody, _: dict[str, Any] = Depends(auth)) -> Any:
        try:
            return await manager().start_update(body.env, known(harness))
        except HarnessRefused as exc:
            return refused(exc)

    @api.post("/api/harnesses/{harness}/install", status_code=202)
    async def harness_install(harness: str, body: EnvBody, _: dict[str, Any] = Depends(auth)) -> Any:
        try:
            return await manager().install(body.env, known(harness))
        except HarnessRefused as exc:
            return refused(exc)

    @api.post("/api/harnesses/{harness}/login-terminal")
    async def harness_sign_in(harness: str, body: EnvBody, _: dict[str, Any] = Depends(auth)) -> Any:
        try:
            return await manager().sign_in(body.env, known(harness))
        except HarnessRefused as exc:
            return refused(exc)
