"""The browser: the browser daemons of this installation, reached through ``daedalus.browser``.

Installs the service and the agent's side of it (``manager.service_hooks["browser"]``, which the
browser tools and command-line staff's copies of them call), ties a session's browser to the
session's life, and tells an owner when the operator gives its browser back — into the session, or to
the staff member as a message.

Installed only where the installation has a browser daemon at all (``capabilities.browser``).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from protocore.contracts.llm import LLMObservabilityContext, LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.browser.agent import BrowserAgent
from daedalus.browser.cli import TOOL_SET, StaffBrowser
from daedalus.browser.model import Owner
from daedalus.browser.monitor import TIMEOUT_SECONDS, InjectionMonitor
from daedalus.browser.notes import SiteNotes
from daedalus.browser.owners import DatabaseOwners
from daedalus.browser.service import Browsers
from daedalus.browser.workflows import DRAFT_TIMEOUT_SECONDS, Workflows
from daedalus.config import keyproxy_base
from daedalus.extensions.notifications import Draft
from daedalus.host.engine_factory import TENANT
from daedalus.terminals.update import DaemonUpdate

EXTRACT_TIMEOUT_SECONDS = 60.0
"""One part of a page read for ``BrowserText(query=…)``: longer than the monitor's word, since the
answer is a list."""
IMAGE_BINARY = Path("/usr/local/bin/browserd")
"""Where the image puts the browser daemon (``deploy/Dockerfile``), in both of its targets."""

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)


def build(app: Application) -> Browsers:
    assert app.manager is not None
    manager = app.manager
    settings = app.settings

    async def wake(owner: Owner, text: str) -> None:
        """Tell the owner something without its asking: the one message a give-back is."""
        team = app.extensions.get("staff")
        staff_id = owner.staff_id if owner.kind == "session" else owner.id
        if staff_id and team is not None:
            member = await manager.staff.get(staff_id)
            if member is not None and await team.live_of(member) is not None:  # type: ignore[attr-defined]
                # A staff member hears it the way it hears its team: as a message into the turn.
                await team.tell(member, text, when="now", by="operator")  # type: ignore[attr-defined]
                return
        if owner.kind == "session" and owner.session_id:
            await manager.submit(owner.session_id, text, as_answer=False, origin="browser")

    def wall(env: str) -> dict[str, Any]:
        """The network wall's rules for ``env``'s daemon (``docs/architecture/browser.md``, The
        network wall). The daemon cannot know them: this installation's own ports, the ranges its
        services are published on, the operator's allowlist and LAN addresses."""
        ranges = [r for r in (_port_range(settings.services_port_range), _port_range(settings.terminals_port_range)) if r]
        # The installation's own doors, sealed in both environments: in a container they are on the
        # Docker host, which the local sites setting can open to the browser.
        sealed = set(manager.sealed_ports())
        # The key proxy is a loopback port natively; whatever reaches it spends the keys.
        with contextlib.suppress(ValueError):
            keys = urlsplit(keyproxy_base())
            if keys.hostname in ("127.0.0.1", "localhost", "::1") and keys.port:
                sealed.add(keys.port)
        rules: dict[str, Any] = {
            "sealed_ports": sorted(sealed),
            "services_ports": ranges,
            "local_sites": app.config.browser.local_sites,
            "lan_allow": list(app.config.browser.lan_allow),
        }
        if env == "container":
            # The daemon's own network reaches this container's published ports on the Docker host;
            # an address the agent prints for its service (127.0.0.1:8103) is sent there, and so is a
            # local site's, and the host's LAN address is the same machine.
            rules["loopback_rewrite"] = "host.docker.internal"
            with contextlib.suppress(ValueError):
                rules["host_addrs"] = [str(ipaddress.ip_address(settings.services_public_host.strip()))]
        allow = [h for h in app.config.policy.egress_allow if h.strip()]
        if allow:
            rules["egress_allow"] = allow
        return rules

    update = DaemonUpdate(settings.rebuild_trigger_dir, binary=IMAGE_BINARY, service="browser", program="browserd") if settings.browser_container_dir is not None and not settings.native else None
    return Browsers(
        app.db,
        run_dirs={"container": settings.browser_container_dir, "host": settings.browser_host_dir},
        config=lambda: app.config.browser,
        owners=DatabaseOwners(app.db),
        bus=manager.bus,
        wake=wake,
        wall=wall,
        daemon_update=update,
    )


def _port_range(text: str) -> list[int] | None:
    low, _, high = text.strip().partition("-")
    if not low.strip().isdigit():
        return None
    first = int(low)
    last = int(high) if high.strip().isdigit() else first
    return [first, last] if 0 < first <= last <= 65535 else None


async def install(app: Application) -> list[asyncio.Task[None]]:
    manager = app.manager
    assert manager is not None
    service = build(app)

    async def small_model(text: str, *, preset: str, purpose: str, max_tokens: int, timeout: float) -> str:
        """One request to the preset named for the purpose, else a middle one of the table: reading a
        page for a word or a list is not what the agent's own model is for."""
        config = app.config
        if preset not in config.presets:
            preset = config.middle_preset() or ""
        provider, model = manager.providers.rungs_for(config, preset or None)[0]
        request = LLMRequest(
            model=model,
            messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)])],
            max_tokens=max_tokens,
            temperature=0.0,
            extra={"enable_thinking": False},
            observability=LLMObservabilityContext(tenant_id=TENANT, call_purpose=purpose, call_category="browser"),
        )
        response = await asyncio.wait_for(provider.complete_text(request), timeout=timeout)
        return "".join(b.text for b in response.message.content_blocks if isinstance(b, TextBlock))

    async def classify(text: str) -> str:
        return await small_model(text, preset=app.config.browser.injection_monitor_preset, purpose="browser_injection_monitor", max_tokens=120, timeout=TIMEOUT_SECONDS)

    async def extract(text: str) -> str:
        return await small_model(text, preset=app.config.browser.extract_preset, purpose="browser_extract", max_tokens=2000, timeout=EXTRACT_TIMEOUT_SECONDS)

    async def noted(note: dict[str, Any]) -> None:
        """The operator hears of a proposed note where they hear of everything waiting for them."""
        notifications = getattr(app, "notifications", None)
        if notifications is None:
            return
        await notifications.post(Draft(
            "system", f"A site note for {note['host']} waits for you", f"{note['text']}\n\nSettings → Browser → Site notes: approve it, or discard it.",
            kind="browser_note", project_id=note.get("project_id") or None, dedupe_key=f"browser_note:{note['id']}", source="browser",
        ))

    async def draft(text: str) -> str:
        """The operator's recorded steps drafted into a procedure: the model pages are read with."""
        return await small_model(text, preset=app.config.browser.extract_preset, purpose="browser_procedure", max_tokens=2500, timeout=DRAFT_TIMEOUT_SECONDS)

    notes = SiteNotes(app.db)
    agent = BrowserAgent(service, InjectionMonitor(classify), extract=extract, notes=notes, on_note=noted)
    app.extensions["browser_notes"] = notes
    async def lasting(project_id: str | None) -> str:
        """A procedure recorded in a chat's own throwaway project is kept for every agent, as an
        agent's site note from there is (``daedalus.tools.browser``)."""
        project = await manager.projects.get(project_id) if project_id else None
        return project.id if project is not None and not project.settings.ephemeral else ""

    app.extensions["browser_workflows"] = Workflows(service, notes, draft=draft, scope=lasting)
    app.extensions["browser"] = service
    app.extensions["browser_agent"] = agent
    manager.service_hooks["browser"] = agent

    async def session_deleted(session_id: str) -> None:
        await service.close_owned("session", session_id)

    # Command-line staff reach the same tools through their launch's MCP entry; every CLI runtime the
    # harness installed offers them from its next launch on.
    staff = StaffBrowser(agent, team=lambda: app.extensions.get("staff"), manager=manager)
    team = app.extensions.get("staff")
    for runtime in dict(getattr(team, "runtimes", None) or {}).values():
        sets = getattr(runtime, "tool_sets", None)
        if isinstance(sets, dict):
            sets[TOOL_SET] = staff

    manager.delete_hooks.append(session_deleted)
    tasks = await service.start()
    if service.daemon_update is not None:
        # In the background: it runs a program, and nothing about the start waits for its answer.
        tasks.append(asyncio.create_task(service.daemon_update.probe(), name="browser-image-version"))  # type: ignore[arg-type]

    async def closer() -> None:
        # Cancelled at shutdown like every background task; the connections go with it, which leaves
        # the daemons and their browsers running for the next start to find.
        try:
            await asyncio.Event().wait()
        finally:
            await service.close()

    return [*tasks, asyncio.create_task(closer(), name="browser-close")]
