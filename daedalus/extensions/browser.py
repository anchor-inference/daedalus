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

from daedalus.browser import login
from daedalus.browser.agent import SWITCH_ASK, WEBGL_ASK, WEBGL_MODES, WEBGL_WORDS, BrowserAgent, webgl_line
from daedalus.browser.cli import TOOL_SET, StaffBrowser
from daedalus.browser.control import EVENTS as CONTROL_EVENTS
from daedalus.browser.control import ChromeControl
from daedalus.browser.model import USER, BrowserError, Owner
from daedalus.browser.monitor import TIMEOUT_SECONDS, InjectionMonitor
from daedalus.browser.notes import SiteNotes
from daedalus.browser.owners import DatabaseOwners
from daedalus.browser.panel import (
    CHROME_CHAT_LINE,
    PANEL_LINE,
    PANEL_SESSION_KEY,
    ChromeChannel,
    PanelGrants,
    PanelHerald,
    waits_for_operator,
)
from daedalus.browser.replay import RecordedSkills
from daedalus.browser.service import Browsers
from daedalus.browser.user import Pairing, RelayDesk
from daedalus.browser.workflows import DRAFT_TIMEOUT_SECONDS, Workflows
from daedalus.config import keyproxy_base
from daedalus.extensions.notifications import Draft
from daedalus.host.engine_factory import TENANT
from daedalus.host.events import EventFilter
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

    async def wake(owner: Owner, text: str, *, only_if_running: bool = False) -> None:
        """Tell the owner something without its asking: the one message a give-back is.

        ``only_if_running`` is for news the owner did not ask for, such as the operator handing back
        the controls they took without being asked: it goes into a turn that is running and is dropped
        otherwise. Submitted to an idle session it started a new turn, so the agent woke up after its
        work was done just to say it had seen the operator's changes."""
        team = app.extensions.get("staff")
        staff_id = owner.staff_id if owner.kind == "session" else owner.id
        if staff_id and team is not None:
            member = await manager.staff.get(staff_id)
            if member is not None and await team.live_of(member) is not None:  # type: ignore[attr-defined]
                # A staff member hears it the way it hears its team: as a message into the turn.
                await team.tell(member, text, when="now", by="operator")  # type: ignore[attr-defined]
                return
        if only_if_running and not (owner.session_id and manager.is_running(owner.session_id)):
            return
        if owner.kind == "session" and owner.session_id:
            await manager.submit(owner.session_id, text, as_answer=False, origin="browser")

    def wall(env: str) -> dict[str, Any]:
        """The network wall's rules for ``env``'s daemon (``docs/architecture/browser.md``, The
        network wall). The daemon cannot know them: this installation's own ports, the ranges its
        services are published on, the operator's allowlist and LAN addresses."""
        # In a container the host's own services range too: a session in a host folder starts its
        # servers on the host, on the host's side of the Docker bridge, which is the very address the
        # loopback rewrite below leads to. Every other port of the host stays shut. Natively a host
        # folder is this machine's and its services take the ordinary range.
        texts = [settings.services_port_range, settings.terminals_port_range] + ([] if settings.native else [settings.host_services_port_range])
        ranges = [r for r in (_port_range(text) for text in texts) if r]
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
            "lan_sites": app.config.browser.lan_sites,
        }
        if env == "container":
            # The daemon's own network reaches this container's published ports on the Docker host;
            # an address the agent prints for its service (127.0.0.1:8103) is sent there, and so is a
            # local site's, and the host's LAN address is the same machine.
            rules["loopback_rewrite"] = "host.docker.internal"
            with contextlib.suppress(ValueError):
                rules["host_addrs"] = [str(ipaddress.ip_address(settings.services_public_host.strip()))]
        allow = [h for h in app.config.policy.egress_allow if h.strip()]
        if env == USER:
            # The operator's Chrome has its own network and no wall of the daemon's around it; what
            # stands in its place is the navigation guard on the agent's tabs, judged against the
            # sites the operator always grants. Present and possibly empty: every other site is an
            # ask, which the host turns into the operator's question about the site.
            rules["egress_allow"] = list(app.config.browser.user.granted_sites)
        elif allow:
            # The address a host service is announced at (ServiceStart says http://<it>:<port>) is
            # the installation's own, not the web the allowlist is about; the wall still opens only
            # the services ranges on it.
            services = app.extensions.get("services")
            with contextlib.suppress(Exception):
                if services is not None and not settings.native and (address := services.host.address()):  # type: ignore[attr-defined]
                    allow = [*allow, address]
            rules["egress_allow"] = allow
        return rules

    update = DaemonUpdate(settings.rebuild_trigger_dir, binary=IMAGE_BINARY, service="browser", program="browserd") if settings.browser_container_dir is not None and not settings.native else None
    return Browsers(
        app.db,
        run_dirs={"container": settings.browser_container_dir, "host": settings.browser_host_dir, USER: settings.browser_user_dir},
        config=lambda: app.config.browser,
        owners=DatabaseOwners(app.db),
        bus=manager.bus,
        wake=wake,
        wall=wall,
        daemon_update=update,
    )


async def chrome_answered(app: Application, service: Browsers, session_id: str, allowed: bool) -> None:
    """The operator answered the agent's ask to work in their Chrome. A yes switches the conversation
    through the panel's own path, consent recorded as the panel records it; either way the agent is
    woken with the answer, since it ended its turn to wait for it."""
    assert app.manager is not None
    state = await app.manager.get_state(session_id)
    staff_id = str(state.metadata.get("staff_id") or "") if state is not None else ""
    owner = Owner("session", session_id, session_id=session_id, staff_id=staff_id or None)
    live = service.user_chromes()
    name = ", ".join(str(c.get("profile_name") or "Chrome") for c in live) or "Chrome"
    if allowed:
        try:
            await service.switch_conversation(session_id, USER, consent=True)
        except BrowserError as exc:
            text = f"[browser] The operator said yes to their Chrome, but the conversation could not be switched: {exc.message}. Tell them, or use your own browser if they agree."
        else:
            text = (
                f"[browser] The operator switched this conversation to their own Chrome «{name}». BrowserOpen now opens your tabs there, "
                "signed in as them; every new site still asks them first."
                + (" Several are connected: name one with BrowserOpen(chrome=…); chrome='?' lists them." if len(live) > 1 else "")
            )
    else:
        text = "[browser] The operator declined to let you work in their Chrome. Use your own browser (BrowserOpen) for the task, or stop and tell them what it needs."
    if service.wake is not None:
        await service.wake(owner, text)


async def webgl_answered(service: Browsers, session_id: str, request: dict[str, Any], allowed: bool) -> None:
    """The operator answered the agent's ask for WebGL in its browser. A yes switches the profile's
    browser to the mode — restarting it with its tabs when it runs — and either way the agent is
    woken with the answer, since it ended its turn to wait for it. The owner is the one that asked,
    project included, because the project decides which profile is switched."""
    mode = str(request.get("webgl") or "")
    owner = Owner("session", session_id, project_id=str(request.get("project_id") or "") or None, session_id=session_id, staff_id=str(request.get("staff_id") or "") or None)
    if mode not in WEBGL_MODES:
        logger.warning("a WebGL request of %s names no mode (%r); nothing was switched", session_id, mode)
        return
    words = WEBGL_WORDS[mode]
    if allowed:
        try:
            result = await service.webgl(owner, mode, actor="operator")
        except BrowserError as exc:
            text = f"[browser] The operator allowed {words} in your browser, but it could not be switched: {exc.message}. Tell them."
        else:
            if result.get("relaunched"):
                failed = [u for g in result.get("groups") or [] if isinstance(g, dict) for u in g.get("failed") or []]
                again = f" These pages did not reopen: {', '.join(failed[:5])}." if failed else ""
                head = f"[browser] The operator allowed {words} in your browser. It restarted in that mode with your tabs (their ids changed: BrowserTabs lists them).{again}"
            else:
                head = f"[browser] The operator allowed {words} in your browser. It starts in that mode when you open it (BrowserOpen)."
            text = f"{head}\n{webgl_line(result)}"
    else:
        text = f"[browser] The operator declined {words} in your browser. Do the task without it, or stop and tell them what it needs."
    if service.wake is not None:
        await service.wake(owner, text)


async def chrome_line(manager: Any, service: Browsers, state: Any) -> str:
    """The turn context's line on the operator's Chrome, for a session that has the browser tools on
    an installation that offers it: whether it is connected, and whether this conversation is in it.
    The same words while nothing changes, and no clock or count in them, so they cost one line and
    never a cache miss; in the turn context rather than the prompt because a Chrome comes and goes."""
    if not service.user_offered():
        return ""
    if manager.registry_for(state).get("BrowserOpen") is None or "BrowserOpen" in manager.blocked_tools_for(state):
        return ""
    session_id = state.session.id
    if await service.choices.env(session_id) == USER:
        connected, name = await service.conversation_chrome(session_id)
        live = [c for c in service.user_chromes() if c.get("connected")]
        line = f"- Browser: the operator gave this conversation their own Chrome «{name or 'Chrome'}»; the browser tools work there" + ("." if connected else ", which is not connected now.")
        if not await service.choices.chrome_of(session_id) and len(live) > 1:
            line = f"- Browser: the operator gave this conversation their own Chrome, and several are connected: «{name}». Name one with BrowserOpen(chrome=…) before you work there."
        # Said up front because an agent refused on the developer console told the operator nothing
        # could be done, when its own browser opens the store: the limit is Chrome's, not the task's.
        line += " Chrome keeps every extension off the Chrome Web Store and its developer console: for those, ask the operator to switch to «Agent's browser», which opens them."
        # Only while the conversation is in their Chrome: switched back to the agent's browser, "work in
        # their Chrome" would send the agent somewhere it cannot go.
        if state.metadata.get(PANEL_SESSION_KEY):
            return line + "\n" + PANEL_LINE
        return line + ("\n" + CHROME_CHAT_LINE if getattr(state, "kind", "chat") == "chrome" else "")
    connected, name, _ = service.user_connection()
    name = name or "Chrome"
    if connected:
        return (
            f"- Browser: the operator's Chrome «{name}» is connected, but this conversation uses your own browser, not theirs. "
            "Only the operator moves it there: when they ask for their browser, call BrowserAskOperatorChrome(reason)."
        )
    return "- Browser: the operator's Chrome is offered here but not connected now; this conversation uses your own browser."


def panel_base(settings: Any) -> str:
    """The app's address as the operator's Chrome reaches it: this computer's port natively, the
    public address's origin on a server. A paired Chrome frames the address it was paired with
    instead, which is the one it is known to reach; this is only what the host calls itself."""
    if settings.native:
        return f"http://127.0.0.1:{settings.api_port}"
    public = urlsplit(str(settings.miniapp_public_url or ""))
    return f"{public.scheme}://{public.netloc}" if public.scheme in ("http", "https") and public.netloc else ""


async def relay_changed(herald: PanelHerald, kind: str, data: dict[str, Any]) -> None:
    """A Chrome that connects is handed its side panel's credential at once, down its own relay."""
    if kind == "relay.connected":
        await herald.grant(str(data.get("chrome") or ""))


async def turn_ended(manager: Any, service: Browsers, session_id: str, status: str) -> None:
    """A turn of a conversation in the operator's Chrome ended: its tabs close unless it waits for the
    operator (a question, or an action they must approve). What the operator hears of the end is the
    app's notification, which reaches the extension as one of its channels (``ChromeChannel``); this
    hook used to tell the extension itself as well, and every end sounded twice."""
    if await service.choices.env(session_id) != USER:
        return
    state = manager.live_state(session_id)
    if state is None or status == "awaiting" or waits_for_operator(state):
        return
    await close_turn_tabs(service, session_id)


async def close_turn_tabs(service: Browsers, session_id: str) -> bool:
    """Close the agent's tabs in the operator's Chrome once its turn is over and it waits for nobody.

    Its tabs sit in the operator's own window, and a session that ended its turn with five open,
    three on the same page, left them to be closed by hand. Left open: a browser the operator holds
    or the agent handed over (the control is not the agent's, or a hand-over is pending), since that
    page is what they are looking at; and everything when the operator turned this off. Whether it
    closed anything."""
    if not service.config().user.close_tabs:
        return False
    row = await service.find_user(Owner("session", session_id))
    if row is None or row["control_owner"] != "agent" or row.get("needs_json"):
        return False
    try:
        await service.close_group(row["id"], actor="system", reason="turn_end")
    except BrowserError as exc:
        logger.info("the agent's tabs of %s were left open at the end of its turn: %s", row["id"], exc.message)
        return False
    return True


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
            "system", f"A site note for {note['host']} waits for you", f"{note['text']}\n\nSettings → Environments → Site notes: approve it, or discard it.",
            kind="browser_note", project_id=note.get("project_id") or None, dedupe_key=f"browser_note:{note['id']}", source="browser",
        ))

    async def draft(text: str) -> str:
        """The operator's recorded steps drafted into a procedure: the model pages are read with."""
        return await small_model(text, preset=app.config.browser.extract_preset, purpose="browser_procedure", max_tokens=2500, timeout=DRAFT_TIMEOUT_SECONDS)

    notes = SiteNotes(app.db)
    agent = BrowserAgent(service, InjectionMonitor(classify), extract=extract, notes=notes, on_note=noted)
    app.extensions["browser_notes"] = notes

    async def trusted(session_id: str) -> set[str]:
        state = await manager.get_state(session_id)
        return {str(site) for site in (state.metadata.get(login.TRUST_KEY) or [])} if state is not None else set()

    async def trust(session_id: str, site: str) -> None:
        """The operator's "always for this session" for a saved sign-in: kept with the session, so a
        restart keeps it and the session's end takes it away."""
        state = await manager.get_state(session_id)
        if state is None:
            return
        for meta in (state.metadata, state.session.metadata):
            meta[login.TRUST_KEY] = [s for s in meta.get(login.TRUST_KEY) or [] if s != site][-49:] + [site]
        await manager.sessions.update_metadata(session_id, state.session.metadata)

    agent.logins.trusted, agent.logins.trust = trusted, trust
    agent.recorded = RecordedSkills(manager.settings.state_dir, manager.settings.skills_dir, manager.redactor.redact)
    async def lasting(project_id: str | None) -> str:
        """A procedure recorded in a chat's own throwaway project is kept for every agent, as an
        agent's site note from there is (``daedalus.tools.browser``)."""
        project = await manager.projects.get(project_id) if project_id else None
        return project.id if project is not None and not project.settings.ephemeral else ""

    app.extensions["browser_workflows"] = Workflows(service, notes, draft=draft, scope=lasting)
    app.extensions["browser"] = service
    app.extensions["browser_agent"] = agent
    settings = app.settings
    herald: PanelHerald | None = None
    if settings.browser_user_dir is not None:
        pairing = Pairing(app.db)
        app.extensions["browser_pairing"] = pairing
        if not settings.native:
            # A server's door for a paired Chrome; natively Chrome comes through the bridge instead.
            app.extensions["browser_relays"] = RelayDesk(pairing, lambda: settings.browser_user_dir)
        grants = PanelGrants(app.db)
        app.extensions["browser_panel"] = grants
        herald = PanelHerald(grants, call=service.call_extension, chromes=service.connected_chromes, base=lambda: panel_base(settings))
        app.extensions["browser_panel_herald"] = herald
        service.relay_hooks.append(lambda kind, data: relay_changed(herald, kind, data))
        notifications = getattr(app, "notifications", None)
        if notifications is not None:
            notifications.register_channel(ChromeChannel(herald, manager=manager, using=service.choices.using_user, bound=service.choices.chrome_of, offered=service.user_offered))
    manager.on_finished(lambda session_id, run_id, status: turn_ended(manager, service, session_id, status))
    control: ChromeControl | None = None
    if settings.browser_user_dir is not None:
        control = ChromeControl(service, manager)
        app.extensions["browser_control"] = control
        service.relay_hooks.append(lambda kind, data: control.refresh())
    manager.service_hooks["browser"] = agent

    async def session_deleted(session_id: str) -> None:
        await service.close_owned("session", session_id)
        await service.choices.forget(session_id)

    # Command-line staff reach the same tools through their launch's MCP entry; every CLI runtime the
    # harness installed offers them from its next launch on.
    staff = StaffBrowser(agent, team=lambda: app.extensions.get("staff"), manager=manager)
    team = app.extensions.get("staff")
    for runtime in dict(getattr(team, "runtimes", None) or {}).values():
        sets = getattr(runtime, "tool_sets", None)
        if isinstance(sets, dict):
            sets[TOOL_SET] = staff

    async def project_deleted(project_id: str) -> None:
        await service.close_project(project_id)

    manager.delete_hooks.append(session_deleted)
    manager.project_delete_hooks.append(project_deleted)
    manager.answer_hooks[SWITCH_ASK] = lambda session_id, request, allowed: chrome_answered(app, service, session_id, allowed)
    manager.answer_hooks[WEBGL_ASK] = lambda session_id, request, allowed: webgl_answered(service, session_id, request, allowed)
    manager.turn_line_hooks.append(lambda state: chrome_line(manager, service, state))

    tasks = await service.start()
    if herald is not None:
        tasks.append(asyncio.create_task(herald.keep_fresh(), name="browser-panel-grants"))
    if control is not None:
        # A switch to the operator's Chrome no longer makes a chat a Chrome one: it shows the mark only
        # while it controls their Chrome, which these two keep current.
        tasks.append(manager.bus.on(EventFilter(types=CONTROL_EVENTS), control.on_event, name="browser-control"))
        tasks.append(asyncio.create_task(control.watch(), name="browser-control-idle"))
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
