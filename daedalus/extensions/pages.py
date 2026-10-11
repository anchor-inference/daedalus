"""The agent's side of custom pages: what the page tools reach through ``manager.service_hooks["pages"]``.

The tools live below the extensions and may not import them, and the state directory where pages are
kept is sealed from a session's own commands; so the tools are handed a desk here that holds the one
page store the API also uses (its locks and its ``pages.changed`` events are shared with the app), the
services range a service page may point into, the templates the shipped skill carries, the page
check, which needs the browser service and a server of its own (``api_page_check``), and the sessions
a page's agent may be bound to.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.browser.model import Owner
from daedalus.extensions.api_page_check import CheckResult, CheckServer, bind_free, drive
from daedalus.extensions.services import parse_range, pid_alive
from daedalus.host.pages import PageError, PageStore, Served, store_for

if TYPE_CHECKING:
    from daedalus.app import Application

TEMPLATES = Path("daedalus-pages") / "templates"
"""Where the templates are, under the skills directory: the skill that explains them carries them."""


class PageDesk:
    def __init__(self, app: Application, *, check_host: str | None = None) -> None:
        self.app = app
        # Natively the browser runs on this machine and loopback is enough (and asks no firewall for
        # leave); in a container the browser's own container reaches the check through the port the
        # services range publishes on the Docker host, which needs every interface.
        self.check_host = check_host or ("127.0.0.1" if getattr(app.settings, "native", False) else "0.0.0.0")  # noqa: S104

    @property
    def store(self) -> PageStore:
        return store_for(self.app)

    @property
    def templates_dir(self) -> Path:
        return self.app.settings.skills_dir / TEMPLATES

    def services_range(self) -> tuple[int, int]:
        return parse_range(self.app.settings.services_port_range)

    async def service_rows(self, session_id: str | None = None) -> list[dict[str, Any]]:
        services = self.app.extensions.get("services")
        if services is None:
            return []
        return list(await services.rows(session_id))  # type: ignore[attr-defined]

    async def session_title(self, session_id: str) -> str | None:
        """A session's title, or None when there is no such session: what binding a page checks."""
        manager = self.app.manager
        state = await manager.get_state(session_id) if manager is not None else None
        return state.session.title if state is not None else None

    async def service_port(self, manifest: dict[str, Any], served: Served) -> int:
        """A service page's port for the check: a port of the services range, or the port of the
        named service the page's session runs. Anything outside the range is refused, so a page's
        target cannot point a check at the API, the key proxy or a daemon of this installation."""
        lo, hi = self.services_range()
        if isinstance(served.target, int):
            port = served.target
        else:
            rows = await self.service_rows(manifest.get("session_id"))
            running = [r for r in rows if r.get("name") == served.target and r.get("status") == "running" and pid_alive(r.get("pid")) and r.get("port")]
            if not running:
                raise PageError(f"the service {served.target!r} is not running; start it, then check again", 503)
            port = int(running[0]["port"])
        if not lo <= port <= hi:
            raise PageError(f"a check reaches only ports of the services range {lo}-{hi}, not {port}", 409)
        return port

    async def check(self, slug: str, *, draft: bool, session_id: str) -> CheckResult:
        browsers = self.app.extensions.get("browser")
        if browsers is None:
            raise PageError("this installation has no browser, so a page cannot be looked at here; its files are still checked by publishing and opening it", 503)
        manifest = await self.store.require(slug)
        server = CheckServer(self.store, manifest, draft=draft, kit_dir=self.app.settings.bot_repo_dir / "miniapp" / "dist" / "kit", resolve_port=self.service_port)
        served = server.served
        result = CheckResult(slug=slug, kind=served.kind, draft=draft)
        lo, hi = self.services_range()
        taken = {int(r["port"]) for r in await self.service_rows() if r.get("port") and r.get("status") == "running"}
        sock = await asyncio.to_thread(bind_free, lo, hi, self.check_host, taken)
        manager = self.app.manager
        state = manager.live_state(session_id) if manager is not None else None
        project = state.project.id if state is not None and state.project is not None else None
        owner = Owner("session", session_id, project_id=project, session_id=session_id)
        await server.start(sock)
        try:
            await drive(browsers, owner, f"agent:{session_id}", server, result)
        finally:
            await server.stop()
        result.exchanges = list(server.exchanges)
        return result


async def install(app: Application) -> list[asyncio.Task[None]]:
    assert app.manager is not None
    app.manager.service_hooks["pages"] = PageDesk(app)
    return []


__all__ = ["PageDesk", "install"]
