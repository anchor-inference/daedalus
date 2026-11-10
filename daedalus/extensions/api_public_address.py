"""The routes of Settings, Public address, on a native installation, and of a phone's QR.

The app says what the operator wants and the launcher does it (``daedalus.extensions.tunnel_address``). A
server has an address of its own and is told so with ``native: false``, so the section is not drawn.

A QR for a service works on any installation. Same Wi-Fi comes first, because it costs nothing and goes
nowhere: a door opens on the home network for an hour and the code is spent there. Otherwise it goes
through the address. Either way a private service stays private: the code opens it for that one phone.
A service with a key gets a code too and never its key, which lasts and which a QR would show on plain http.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from daedalus import public_address
from daedalus.extensions import api_services_door
from daedalus.extensions.tunnel_address import EXTENSION_KEY
from daedalus.host import launcher_bridge
from daedalus.host.installation_reach import secrets_hidden_from_commands
from daedalus.host.reveal import is_local_client
from daedalus.security.two_factor import qr_rows
from daedalus.stores import pairing

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
    trust_ngrok: bool = False


def register(api: FastAPI, app: Application, auth: Callable[..., Any], *, door_config: Callable[[Any], Any]) -> None:
    lan = api_services_door.Lan(door_config, api)

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
        return await drawn(await address().refresh())

    async def tokens_hidden() -> bool:
        """Whether a command the agent runs cannot reach the file a token is in, or the installation's other
        secrets, which the form has to say when it is not so (:func:`secrets_hidden_from_commands`)."""
        return await secrets_hidden_from_commands(app)

    async def drawn(view: dict[str, Any]) -> dict[str, Any]:
        """What the settings need beyond the launcher's view. The QR is drawn here, as the two-factor one is."""
        reachable = view.get("address") if view.get("enabled") and view.get("state") in ("up", "reconnecting") else ""
        return {"native": True, **view, "exposed": await exposed(), "qr": qr_rows(str(reachable)) if reachable else [], "tokens_hidden": await tokens_hidden()}

    async def way(kind: str, base: str, slug: str, row: dict[str, Any]) -> dict[str, Any]:
        """One way for a phone to reach a service. A public one has a link that works as it is; anything
        else gets a code for this one door."""
        url = f"{base}/s/{slug}/"
        if (row.get("share_mode") or "local") != "public":
            url += "?phone=" + quote(await pairing.mint_phone(app.db, slug, lan=kind == "wifi"), safe="")
        return {"kind": kind, "url": url, "qr": qr_rows(url)}

    @api.post("/api/sessions/{session_id}/services/{name}/phone")
    async def service_on_a_phone(session_id: str, name: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        services = app.extensions.get("services")
        row = await services.get(session_id, name) if services is not None else None  # type: ignore[attr-defined]
        if row is None or not row.get("port"):
            raise HTTPException(404, "no such service with a port")
        slug = await services.ensure_slug(session_id, name)  # type: ignore[union-attr]
        row = await services.get(session_id, name) or row  # type: ignore[union-attr]
        ways = []
        host = api_services_door.lan_address() if app.settings.native else ""
        base = lan.base(app.settings, host) if host else ""
        if base:
            ways.append(await way("wifi", base, slug, row))
        public = public_address.services_base(app.settings)
        if public and not is_local_client(urlsplit(public).hostname):
            ways.append(await way("tunnel" if app.settings.native else "address", public, slug, row))
        return {"ways": ways, "mode": row.get("share_mode") or "local", "code_minutes": pairing.PHONE_CODE_MINUTES, "pass_hours": pairing.PHONE_PASS_HOURS}

    @api.delete("/api/sessions/{session_id}/services/{name}/phone")
    async def service_off_phones(session_id: str, name: str, _: dict[str, Any] = Depends(auth)) -> dict[str, bool]:
        services = app.extensions.get("services")
        row = await services.get(session_id, name) if services is not None else None  # type: ignore[attr-defined]
        if row is None:
            raise HTTPException(404, "no such service")
        if row.get("share_slug"):
            await pairing.forget_phones(app.db, str(row["share_slug"]))
        return {"ok": True}

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
        return await drawn(view)


__all__ = ["AddressBody", "register"]
