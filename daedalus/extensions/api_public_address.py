"""The routes of Settings, Public address, on a native installation.

The app says what the operator wants and the launcher does it (``daedalus.extensions.tunnel_address``). A
server has an address of its own and is told so with ``native: false``, so the section is not drawn.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from daedalus.extensions.tunnel_address import EXTENSION_KEY
from daedalus.host import launcher_bridge

if TYPE_CHECKING:
    from daedalus.app import Application


class AddressBody(BaseModel):
    """A change of the address. What is left out stays as it is; an empty token forgets the one kept."""

    enabled: bool | None = None
    provider: str | None = Field(default=None, max_length=32)
    ngrok_token: str | None = Field(default=None, max_length=512)
    frp_token: str | None = Field(default=None, max_length=512)
    frp_server: str | None = Field(default=None, max_length=256)
    frp_domain: str | None = Field(default=None, max_length=256)
    sign_out: bool = False


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def address() -> Any:
        found = app.extensions.get(EXTENSION_KEY)
        if found is None:
            raise HTTPException(503, "the public address is not installed")
        return found

    async def exposed() -> dict[str, int]:
        """What the address would show, in numbers: the operator should see that before anyone else does."""
        links = await app.db.fetchone("SELECT COUNT(*) AS n FROM guest_links WHERE revoked_at IS NULL")
        services = await app.db.fetchone("SELECT COUNT(*) AS n FROM services WHERE share_mode IN ('public', 'key') AND status = 'running'")
        return {"links": int(links["n"]) if links else 0, "services": int(services["n"]) if services else 0}

    @api.get("/api/public-address")
    async def public_address_state(_: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not app.settings.native:
            return {"native": False}
        view = await address().refresh()
        return {"native": True, **view, "exposed": await exposed()}

    @api.put("/api/public-address")
    async def public_address_change(body: AddressBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not app.settings.native:
            raise HTTPException(404, "a server has an address of its own")
        try:
            view = await address().change(body.model_dump(exclude_none=True))
        except launcher_bridge.TunnelRefused as exc:
            raise HTTPException(400, str(exc)) from exc
        except launcher_bridge.LauncherUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"native": True, **view, "exposed": await exposed()}


__all__ = ["AddressBody", "register"]
