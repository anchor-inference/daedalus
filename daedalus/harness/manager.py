"""The harness manager: which command-line agents are installed in each environment, at which version
against the newest, signed in or not, with which agents and models; and installing, updating and
checking them, one button per CLI.

It is ``app.extensions["harness"]``, the one place the Harnesses screen, the hiring form, the ``Hire``
check and the orchestrator's ``Harnesses`` tool ask. It never updates on its own: the periodic check
only looks, and a version changes when the operator presses Update — never under a staff member who
is working, because the CLI's files are replaced while it runs.

After every install or update the CLI is checked: its version, that it is a version the adapter
supports, that it is signed in, and — through the adapter's own self-check once the adapter exists —
that a session starts and one tiny prompt on the cheapest model gets an answer. What could not be
run is recorded as skipped, never as passed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

import httpx

from daedalus.harness import ADAPTERS
from daedalus.harness.capabilities import CAPABILITIES, capabilities, version_supported, version_tested
from daedalus.harness.catalog import HarnessCatalog
from daedalus.harness.contract import (
    AgentEntry,
    Catalog,
    CheckResult,
    CheckStep,
    EnvironmentPort,
    EnvironmentUnavailable,
    InstallInfo,
    LoginState,
    ProgramNotFound,
)
from daedalus.harness.tools import NODE_VERSION, TOOLING, Fetch, Plan, node_install_script, stable_version, tooling

if TYPE_CHECKING:
    from daedalus.config import HarnessConfig
    from daedalus.host.events import EventBus
    from daedalus.stores.harness import CatalogRow, HarnessStore

logger = logging.getLogger(__name__)

TICK_S = 60.0
"""How often the background task asks whether an environment is due for a check. The check itself
runs every ``catalog_ttl_s``; the tick only notices an environment that came up in between."""
FETCH_TIMEOUT_S = 20.0
FETCH_MAX_BYTES = 1 << 20
"""Release feeds answer with a version or a tag list; anything longer is not what was asked for."""
NODE = "node"
"""The pinned Node's key among the operations, beside the CLIs' names."""

OperationKind = Literal["check", "update", "install", "signin"]
PAST: dict[str, str] = {"check": "checked", "update": "updated", "install": "installed", "signin": "signed in"}
SelfCheckRunner = Callable[[EnvironmentPort, str, bool], Awaitable[CheckResult]]
"""An adapter's session check: ``(environment, model, model_turn)``. It launches the CLI, sees it get
ready, and — when ``model_turn`` — sends one tiny prompt on ``model`` and waits for the answer; an
empty model is the CLI's default. Registered in ``HarnessManager.self_checks`` by the adapter."""
LiveStaff = Callable[[str], Awaitable[list[dict[str, Any]]]]
FolderLookup = Callable[[str], Awaitable[tuple[str, str] | None]]
"""A folder id to ``(environment, absolute path)``, or None when there is no such folder."""


class HarnessRefused(Exception):
    """The request cannot be carried out, with the reason in words. ``status`` is the HTTP answer."""

    status = 400

    def __init__(self, message: str, *, status: int | None = None, staff: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        self.staff = staff or []
        """For a refusal because staff are working: who, so the operator can release them or wait."""


class VisibleRunner(Protocol):
    """Runs a program in a terminal the operator can open (``TerminalRunner`` in production)."""

    async def start(self, env: str, argv: list[str], *, title: str) -> str: ...

    async def wait(self, terminal_id: str, *, timeout: float) -> tuple[int | None, str]: ...


@dataclass(slots=True)
class Operation:
    kind: OperationKind
    env: str
    harness: str
    started_at: str
    terminal_id: str = ""
    target: str = ""

    def view(self) -> dict[str, Any]:
        return {"kind": self.kind, "started_at": self.started_at, "terminal_id": self.terminal_id or None, "target": self.target}


@dataclass(slots=True)
class NodeState:
    installed: bool = False
    version: str = ""
    path: str = ""
    checked_at: str | None = None

    def view(self) -> dict[str, Any]:
        return {"installed": self.installed, "version": self.version, "path": self.path, "pinned": NODE_VERSION, "current": self.version == NODE_VERSION, "checked_at": self.checked_at}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _age_s(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return None


def _newer(latest: str, installed: str) -> bool:
    new, old = stable_version(latest), stable_version(installed)
    return new is not None and old is not None and new > old


async def http_fetch(url: str) -> str:
    """GET a release feed from this host. Only ever the fixed URLs of the tooling."""
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, follow_redirects=True, headers={"Accept": "application/json, text/plain"}) as client:
        response = await client.get(url)
        response.raise_for_status()
        if len(response.content) > FETCH_MAX_BYTES:
            raise ValueError(f"{url} answered with {len(response.content)} bytes")
        return response.text


def merge_agents(project: Iterable[AgentEntry], user: Iterable[AgentEntry]) -> tuple[AgentEntry, ...]:
    """The project's agents first, then the user's that the project does not redefine: a CLI takes the
    project's definition of a name over the user's, so the form offers the one that will run."""
    mine = list(project)
    names = {a.name for a in mine}
    return (*mine, *(a for a in user if a.name not in names))


class HarnessManager(HarnessCatalog):
    def __init__(
        self,
        store: HarnessStore,
        *,
        ports: Callable[[str], EnvironmentPort | None],
        environments: Callable[[], list[str]],
        config: Callable[[], HarnessConfig],
        live_staff: LiveStaff,
        folder: FolderLookup | None = None,
        runner: VisibleRunner | None = None,
        fetch: Fetch = http_fetch,
        bus: EventBus | None = None,
    ) -> None:
        super().__init__(store)
        self.ports = ports
        """The environment port for a name, or None when that environment cannot be reached now."""
        self.environments = environments
        """The environments that can be reached now."""
        self.config = config
        self.live_staff = live_staff
        self.folder = folder
        self.runner = runner
        self.fetch = fetch
        self.bus = bus
        self.self_checks: dict[str, SelfCheckRunner] = {}
        """The adapters' session checks by harness. A CLI without one is checked without a session,
        and its record says the session step was skipped."""
        self.operations: dict[tuple[str, str], Operation] = {}
        """What is being done to a CLI in an environment now. One at a time per CLI and environment:
        two updates of one CLI would replace its files under each other."""
        self.node: dict[str, NodeState] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- reading ---------------------------------------------------------------------------

    def port(self, env: str) -> EnvironmentPort:
        if env not in ("container", "host"):
            raise HarnessRefused(f"an environment is container or host, not {env!r}")
        port = self.ports(env)
        if port is None:
            raise HarnessRefused(f"the {env} environment's terminal service is not available", status=503)
        return port

    def _entry(self, env: str, caps: Any, row: CatalogRow | None, offered: list[str] | None = None) -> dict[str, Any]:
        view = super()._entry(env, caps, row, offered)
        operation = self.operations.get((env, caps.harness))
        check = view.get("self_check") or {}
        view.update(
            update_available=bool(view["installed"]) and _newer(str(view.get("latest_version") or ""), str(view["installed_version"])),
            self_check_ok=check.get("ok") if check else None,
            operation=operation.view() if operation is not None else None,
            installable=not view["installed"] and self._install_problem(env, caps.harness, view) == "",
            install_problem=self._install_problem(env, caps.harness, view),
            can_sign_in=bool(tooling(caps.harness).sign_in),
            unavailable=self._unavailable(env, caps.harness, view),
        )
        return view

    def _install_problem(self, env: str, harness: str, view: dict[str, Any]) -> str:
        if view["installed"]:
            return ""
        tool = tooling(harness)
        if tool.npm_package and env == "container" and not self.node.get(env, NodeState()).installed:
            return "needs Node: install Node first"
        return ""

    def _unavailable(self, env: str, harness: str, view: dict[str, Any]) -> str:
        """Why a staff member on this CLI cannot be launched in ``env`` now; empty when it can."""
        label = capabilities(harness).label
        if not view["installed"]:
            return view.get("error") or f"{label} is not installed in the {env} environment"
        if view["logged_in"] == "no":
            return f"{label} is not signed in in the {env} environment"
        blocked = self._blocked(env, harness, view)
        if blocked:
            return blocked
        if harness not in ADAPTERS:
            return f"Daedalus cannot run {label} as staff yet"
        return ""

    def _blocked(self, env: str, harness: str, view: dict[str, Any]) -> str:
        """What only the manager knows against a launch: an operation on the CLI, a major it does not
        support, a failed self-check, an untested version when those are not allowed."""
        label = capabilities(harness).label
        operation = self.operations.get((env, harness))
        if operation is not None and operation.kind in ("update", "install"):
            return f"{label} is being {PAST[operation.kind]} in the {env} environment"
        if view["installed"] and not view["supported"]:
            return f"{label} {view['installed_version']} is a major version the adapter does not support"
        check = view.get("self_check") or {}
        if check and not check.get("ok"):
            failed: dict[str, Any] = next((s for s in check.get("steps") or [] if not s.get("ok") and not s.get("skipped")), {})
            return f"{label}'s last self-check failed" + (f" at {failed.get('name')}: {failed.get('detail')}" if failed else "")
        if view["installed"] and not view["tested"] and not self.config().allow_untested:
            return f"{label} {view['installed_version']} is outside the tested versions"
        return ""

    async def launch_blocker(self, env: str, harness: str) -> str:
        """For the staff runtime before a launch, beside its own checks (environment reachable,
        installed, signed in): why the manager holds this CLI back now, or empty. A CLI never checked
        is not held back here; the runtime decides that."""
        capabilities(harness)
        return self._blocked(env, harness, self._entry(env, CAPABILITIES[harness], await self.store.catalog_row(env, harness)))

    async def entry(self, env: str, harness: str) -> dict[str, Any]:
        capabilities(harness)
        return self._entry(env, CAPABILITIES[harness], await self.store.catalog_row(env, harness), await self.store.offered_models(env, harness))

    async def offer_models(self, env: str, harness: str, models: list[str] | None) -> dict[str, Any]:
        """Offer only ``models`` of a CLI in ``env`` — on the hiring form and in the orchestrator's
        ``Harnesses`` — or every one again with None or an empty list. Only a model the CLI listed can
        be chosen, so a typo never becomes the one model offered. Answers the row as it now is."""
        if env not in ("container", "host"):
            raise HarnessRefused(f"an environment is container or host, not {env!r}")
        capabilities(harness)
        if models:
            listed = (await self.entry(env, harness))["all_models"]
            unknown = [m for m in models if m not in listed]
            if unknown:
                raise HarnessRefused(f"{CAPABILITIES[harness].label} in the {env} environment lists no model {', '.join(repr(m) for m in unknown)}")
        await self.store.set_offered_models(env, harness, models)
        entry = await self.entry(env, harness)
        await self._publish("harness.models", {"env": env, "harness": harness, "models": entry["models"] if entry["models_chosen"] else None})
        return entry

    async def unavailable(self, env: str, harness: str) -> str:
        """For the staff runtime before a launch: why this CLI cannot run a staff member now."""
        return str((await self.entry(env, harness))["unavailable"])

    async def hire_problem(self, env: str, harness: str) -> str:
        """Why a member on this CLI should not be hired in ``env``: a CLI the last check found missing
        or of an unsupported major. A CLI never checked there is not refused — the check may simply
        not have run yet — and a sign-in or an update can come after the hire."""
        row = await self.store.catalog_row(env, harness)
        if row is None or not row.checked_at:
            return ""
        view = self._entry(env, CAPABILITIES[harness], row)
        label = CAPABILITIES[harness].label
        if not view["installed"]:
            return f"{label} is not installed in the {env} environment" + (f" ({view['error']})" if view["error"] else "")
        if not view["supported"]:
            return f"{label} {view['installed_version']} is a major version Daedalus does not support"
        return ""

    async def hire_warning(self, env: str, harness: str) -> str:
        """What a hire of this CLI in ``env`` should be told without being refused: that its version
        is outside the tested range and has not passed a self-check yet. Empty otherwise."""
        view = await self.entry(env, harness)
        if view["version_guard"] != "unverified":
            return ""
        low, high = view["tested_versions"]
        return (
            f"{view['label']} {view['installed_version']} in the {env} environment is outside the versions the adapter was tested with "
            f"({low} to below {high}) and has not passed a self-check on this version yet; run Check on the Harnesses screen before relying on it"
        )

    async def catalog(self, env: str, harness: str, folder_id: str | None = None) -> Catalog:
        """What a CLI offers in ``env``: the user-level lists from the last check, and — for a folder —
        the agents the folder itself defines, read now, because they change with the project."""
        tool = tooling(harness)
        row = await self.store.catalog_row(env, harness)
        user = Catalog(
            agents=tuple(AgentEntry(name=str(a.get("name") or ""), source=str(a.get("source") or "user"), description=str(a.get("description") or ""), model=str(a.get("model") or "")) for a in (row.agents if row else [])),
            models=tuple(row.models) if row else (),
            modes=tuple(row.modes) if row and row.modes else tool.modes,
            profiles=tuple(row.profiles) if row else (),
            efforts=tuple(row.efforts) if row and row.efforts else tool.efforts,
        )
        if not folder_id or row is None or not row.installed or self.folder is None:
            return user
        found = await self.folder(folder_id)
        if found is None or found[0] != env:
            return user
        try:
            project = await tool.catalog(self.port(env), found[1])
        except (HarnessRefused, EnvironmentUnavailable, ProgramNotFound) as exc:
            logger.debug("no project catalog of %s in %s: %s", harness, found[1], exc)
            return user
        # An agent marked as the project's comes from the folder's own files. One with no source comes
        # from a CLI that lists the folder's and the user's agents together (OpenCode); of those, the
        # names the user-level list lacks are the folder's.
        user_names = {a.name for a in user.agents}
        own = [a for a in project.agents if a.source == "project" or (not a.source and a.name not in user_names)]
        return replace(user, agents=merge_agents((replace(a, source="project") for a in own), user.agents))

    async def catalog_view(self, env: str, folder_id: str | None = None) -> dict[str, dict[str, Any]]:
        """Every CLI of ``env`` as the hiring form reads it, keyed by harness."""
        out: dict[str, dict[str, Any]] = {}
        for entry in await self.harnesses(env):
            harness = entry["harness"]
            catalog = await self.catalog(env, harness, folder_id) if folder_id else None
            agents = [{"name": a.name, "source": a.source, "description": a.description, "model": a.model} for a in catalog.agents] if catalog else entry["agents"]
            out[harness] = {
                "installed": entry["installed"],
                "version": entry["installed_version"],
                "latest": entry["latest_version"],
                # The form reads a boolean: true signed in, false signed out, absent when unknown.
                "logged_in": {"yes": True, "no": False}.get(entry["logged_in"]),
                "login_detail": entry["login_detail"],
                "agents": agents,
                "models": entry["models"],
                "all_models": entry["all_models"],
                "models_chosen": entry["models_chosen"],
                "modes": entry["modes"] or list(tooling(harness).modes),
                "efforts": entry["efforts"] or list(tooling(harness).efforts),
                "profiles": entry["profiles"],
                "error": entry["error"],
                "checked_at": entry["checked_at"],
                "supported": entry["supported"],
                "tested": entry["tested"],
                "tested_versions": entry["tested_versions"],
                "version_guard": entry["version_guard"],
                "unavailable": entry["unavailable"],
            }
        return out

    def node_state(self, env: str) -> dict[str, Any]:
        return self.node.get(env, NodeState()).view()

    # -- checking --------------------------------------------------------------------------

    async def check(self, env: str, harness: str | None = None, *, refresh_latest: bool = False) -> list[dict[str, Any]]:
        """Look at one CLI or all of them in ``env`` and record what was found. A CLI being installed
        or updated is left alone: its operation checks it when it ends."""
        if harness is not None:
            capabilities(harness)
        port = self.port(env)
        names = [harness] if harness else list(TOOLING)
        await self._check_node(env, port)
        for name in names:
            operation = self.operations.get((env, name))
            if operation is not None and operation.kind in ("update", "install"):
                continue
            await self._check_one(env, name, port, refresh_latest=refresh_latest)
        return [e for e in await self.harnesses(env) if harness is None or e["harness"] == harness]

    async def _check_one(self, env: str, harness: str, port: EnvironmentPort, *, refresh_latest: bool) -> CatalogRow:
        tool = tooling(harness)
        errors: list[str] = []
        login = LoginState("unknown")
        catalog: Catalog | None = None
        try:
            info = await tool.installed(port)
        except EnvironmentUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - one CLI that cannot be asked does not stop the others
            logger.warning("checking %s in %s failed: %s", harness, env, exc)
            info = InstallInfo(installed=False, detail=f"could not be checked: {exc}")
        if info.installed:
            try:
                login = await tool.login_state(port)
            except EnvironmentUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - sign-in unknown is shown as unknown
                login = LoginState("unknown", f"could not tell: {exc}"[:300])
            try:
                catalog = await tool.catalog(port, None)
            except EnvironmentUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - the previous lists are kept (the store's rule)
                errors.append(f"agents and models could not be read: {exc}")
        else:
            errors.append(info.detail)
        previous = await self.store.catalog_row(env, harness)
        age = _age_s(previous.latest_checked_at) if previous else None
        # A CLI that reports its own latest version is asked only when it is the CLI: a look-alike
        # binary of the same name is not run beyond its version line.
        if (info.installed or not tool.latest_from_cli) and (refresh_latest or age is None or age > self.config().latest_ttl_s):
            await self._refresh_latest(env, harness, port, errors)
        row = await self.store.record_check(env, harness, install=info, login=login, catalog=catalog, error="; ".join(e for e in errors if e))
        await self._publish("harness.check", {"env": env, "harness": harness, "ok": row.installed and not row.error, "installed": row.installed, "version": row.installed_version, "logged_in": row.logged_in, **({"error": row.error} if row.error else {})})
        return row

    async def _refresh_latest(self, env: str, harness: str, port: EnvironmentPort, errors: list[str]) -> str:
        try:
            latest = await tooling(harness).latest(port, self.fetch)
        except (ProgramNotFound, NotImplementedError):
            return ""
        except EnvironmentUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - a release feed that is down is a note, not a failure
            errors.append(f"the latest version could not be learnt: {exc}"[:500])
            return ""
        await self.store.record_latest(env, harness, latest)
        return latest

    async def _check_node(self, env: str, port: EnvironmentPort) -> NodeState:
        state = NodeState(checked_at=_now())
        try:
            result = await port.run(["node", "--version"], timeout=20)
        except ProgramNotFound:
            result = None
        except EnvironmentUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - an allowlist refusal is "no node" as far as installs go
            logger.debug("node in %s: %s", env, exc)
            result = None
        if result is not None and result.exit_code == 0:
            state = NodeState(installed=True, version=result.stdout.strip().lstrip("v"), path=result.path, checked_at=state.checked_at)
        self.node[env] = state
        return state

    async def self_check(self, env: str, harness: str) -> CheckResult:
        """Check an installed CLI and record the result. See the module's description for the steps."""
        port = self.port(env)
        tool = tooling(harness)
        caps = capabilities(harness)
        started = time.monotonic()
        steps: list[CheckStep] = []

        def step(name: str, ok: bool, detail: str = "", *, since: float, skipped: bool = False) -> None:
            steps.append(CheckStep(name=name, ok=ok, detail=detail[:500], duration_ms=int((time.monotonic() - since) * 1000), skipped=skipped))

        at = time.monotonic()
        info = await tool.installed(port)
        step("version", info.installed, info.version if info.installed else (info.detail or "not installed"), since=at)
        if info.installed:
            supported = version_supported(caps, info.version)
            if not supported:
                words = f"{info.version} is not major {caps.supported_major}"
            elif version_tested(caps, info.version):
                words = f"{info.version} is within the tested versions"
            else:
                words = f"{info.version} is outside the tested versions {caps.tested_versions[0]}–{caps.tested_versions[1]}"
            step("supported", supported, words, since=at)
            at = time.monotonic()
            try:
                login = await tool.login_state(port)
            except Exception as exc:  # noqa: BLE001 - reported as the step's failure
                login = LoginState("unknown", str(exc))
            if login.state == "unknown":
                step("signin", True, f"could not tell: {login.detail}", since=at, skipped=True)
            else:
                step("signin", login.state == "yes", login.detail or ("signed in" if login.state == "yes" else "not signed in"), since=at)
        runner = self.self_checks.get(harness)
        if info.installed and all(s.ok for s in steps if not s.skipped):
            if runner is None:
                step("session", True, "no adapter runs this CLI yet, so no session was started", since=time.monotonic(), skipped=True)
            else:
                row = await self.store.catalog_row(env, harness)
                model = tool.cheapest_model(row.models if row else [])
                at = time.monotonic()
                try:
                    session = await runner(port, model, self.config().self_check_model_turn)
                    steps.extend(session.steps)
                except Exception as exc:  # noqa: BLE001 - the adapter's check failing is the result
                    step("session", False, f"the session check raised: {exc}", since=at)
        result = CheckResult(ok=bool(steps) and all(s.ok for s in steps if not s.skipped), steps=tuple(steps), version=info.version, duration_ms=int((time.monotonic() - started) * 1000))
        await self.store.record_self_check(env, harness, result)
        return result

    # -- operations ------------------------------------------------------------------------

    def _claim(self, kind: OperationKind, env: str, harness: str, target: str = "") -> Operation:
        """Take the one slot of ``(env, harness)``. No await between the test and the set, so two
        requests racing in this event loop cannot both win."""
        current = self.operations.get((env, harness))
        if current is not None:
            label = "Node" if harness == NODE else capabilities(harness).label
            raise HarnessRefused(f"{label} is already being {PAST[current.kind]} in the {env} environment", status=409)
        operation = Operation(kind=kind, env=env, harness=harness, started_at=_now(), target=target)
        self.operations[(env, harness)] = operation
        return operation

    def _release(self, operation: Operation) -> None:
        if self.operations.get((operation.env, operation.harness)) is operation:
            del self.operations[(operation.env, operation.harness)]

    def _spawn(self, coro: Awaitable[Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _refuse_if_working(self, env: str, harness: str) -> None:
        working = [s for s in await self.live_staff(harness) if s.get("env") == env]
        if working:
            names = ", ".join(f"{s['name']} ({s['project']})" for s in working[:5])
            raise HarnessRefused(f"{capabilities(harness).label} is running staff in the {env} environment: {names}. Release them or wait for them to finish, then update.", status=409, staff=working)

    async def prepare_update(self, env: str, harness: str) -> Operation:
        """Everything that can refuse an update, before it starts: a reachable environment, no other
        operation on the CLI, nobody working on it. The slot is held from here on."""
        capabilities(harness)
        self.port(env)
        operation = self._claim("update", env, harness)
        try:
            await self._refuse_if_working(env, harness)
        except BaseException:
            self._release(operation)
            raise
        return operation

    async def start_update(self, env: str, harness: str) -> dict[str, Any]:
        """Refuse now or start the update in the background; progress arrives as ``harness.check``
        and the end as ``harness.updated``."""
        operation = await self.prepare_update(env, harness)
        self._spawn(self._run_update(operation), f"harness-update-{env}-{harness}")
        return {"state": "started", "operation": operation.view()}

    async def update(self, env: str, harness: str) -> dict[str, Any]:
        """Update and wait for the result (the background task's body, for callers that can wait)."""
        return await self._run_update(await self.prepare_update(env, harness))

    async def _run_update(self, operation: Operation) -> dict[str, Any]:
        env, harness = operation.env, operation.harness
        tool = tooling(harness)
        outcome: dict[str, Any] = {"env": env, "harness": harness, "ok": False, "changed": False, "from": "", "to": "", "error": "", "check": None}
        try:
            port = self.port(env)
            before = await tool.installed(port)
            outcome["from"] = before.version
            if not before.installed:
                outcome["error"] = f"{capabilities(harness).label} is not installed; install it first"
                return outcome
            errors: list[str] = []
            latest = await self._refresh_latest(env, harness, port, errors)
            if not latest:
                outcome["error"] = "; ".join(errors) or "the latest version is not known"
                return outcome
            operation.target = latest
            if not _newer(latest, before.version):
                outcome.update(ok=True, to=before.version, error="")
                return outcome
            plan = tool.update_plan(env, before, latest)
            problem = await self._plan_problem(env, port, plan)
            if problem:
                outcome["error"] = problem
                return outcome
            result = await port.run(list(plan.argv), timeout=self.config().update_timeout_s)
            row = await self._check_one(env, harness, port, refresh_latest=False)
            outcome["to"] = row.installed_version
            outcome["changed"] = row.installed_version != before.version
            if result.exit_code != 0 or result.timed_out:
                outcome["error"] = f"{' '.join(plan.argv[:3])} failed: " + ("timed out" if result.timed_out else (result.stderr or result.stdout).strip()[-500:] or f"exit code {result.exit_code}")
            elif plan.target and row.installed_version != plan.target:
                outcome["error"] = f"the updater ended, but the version is {row.installed_version or 'unknown'}, not {plan.target}"
            check = await self.self_check(env, harness)
            outcome["check"] = {"ok": check.ok, "steps": [{"name": s.name, "ok": s.ok, "skipped": s.skipped, "detail": s.detail} for s in check.steps]}
            outcome["ok"] = not outcome["error"] and check.ok
            if outcome["error"] == "" and not check.ok:
                outcome["error"] = "updated, but the self-check failed"
            return outcome
        except HarnessRefused as exc:
            outcome["error"] = exc.message
            return outcome
        except EnvironmentUnavailable as exc:
            outcome["error"] = f"the {env} environment went away: {exc}"
            return outcome
        except Exception as exc:  # noqa: BLE001 - the operator is told what happened, whatever it was
            logger.exception("updating %s in %s failed", harness, env)
            outcome["error"] = f"the update failed: {exc}"
            return outcome
        finally:
            self._release(operation)
            await self._publish(
                "harness.updated",
                {"env": env, "harness": harness, "kind": "update", "version": outcome["to"] or outcome["from"], "previous": outcome["from"], "ok": outcome["ok"], "changed": outcome["changed"], "check_ok": (outcome["check"] or {}).get("ok"), **({"error": outcome["error"]} if outcome["error"] else {})},
            )

    async def update_all(self, env: str) -> list[dict[str, Any]]:
        """Update every installed CLI with a newer version, one after the other. One refused (staff
        working, already busy) is reported and skipped; the others go on."""
        results = []
        for entry in await self.harnesses(env):
            if not entry["update_available"]:
                continue
            try:
                results.append(await self.update(env, entry["harness"]))
            except HarnessRefused as exc:
                results.append({"env": env, "harness": entry["harness"], "ok": False, "refused": True, "error": exc.message, "staff": exc.staff})
        return results

    def start_update_all(self, env: str) -> dict[str, Any]:
        self.port(env)
        self._spawn(self.update_all(env), f"harness-update-all-{env}")
        return {"state": "started"}

    async def _plan_problem(self, env: str, port: EnvironmentPort, plan: Plan) -> str:
        if not plan.needs_node:
            return ""
        node = await self._check_node(env, port)
        if node.installed:
            return ""
        return "npm needs Node, which is not installed in this environment" + (": install Node first" if env == "container" else "")

    async def install(self, env: str, harness: str) -> dict[str, Any]:
        """Install a CLI in a terminal the operator can watch, then check it. Returns once the
        terminal is started; the end arrives as ``harness.updated`` with ``kind: install``."""
        capabilities(harness)
        if self.runner is None:
            raise HarnessRefused("installs need the terminals service, which is not running here", status=503)
        port = self.port(env)
        operation = self._claim("install", env, harness)
        try:
            tool = tooling(harness)
            before = await tool.installed(port)
            if before.installed:
                raise HarnessRefused(f"{capabilities(harness).label} {before.version} is already installed; update it instead", status=409)
            latest = ""
            if tool.npm_package:
                errors: list[str] = []
                latest = await self._refresh_latest(env, harness, port, errors)
                if not latest:
                    # A pinned major cannot be kept without knowing which version to ask for.
                    raise HarnessRefused("; ".join(errors) or "the version to install is not known", status=502)
            plan = tool.install_plan(env, port.home, latest)
            problem = await self._plan_problem(env, port, plan)
            if problem:
                raise HarnessRefused(problem, status=409)
            argv = list(plan.argv) if plan.argv else ["sh", "-c", plan.script]
            operation.target = plan.target
            operation.terminal_id = await self.runner.start(env, argv, title=f"Installing {capabilities(harness).label}")
        except BaseException:
            self._release(operation)
            raise
        self._spawn(self._finish_install(operation), f"harness-install-{env}-{harness}")
        return {"state": "started", "operation": operation.view()}

    async def _finish_install(self, operation: Operation) -> dict[str, Any]:
        env, harness = operation.env, operation.harness
        outcome: dict[str, Any] = {"env": env, "harness": harness, "ok": False, "version": "", "error": "", "terminal_id": operation.terminal_id, "check": None}
        try:
            assert self.runner is not None
            code, tail = await self.runner.wait(operation.terminal_id, timeout=self.config().install_timeout_s)
            port = self.port(env)
            row = await self._check_one(env, harness, port, refresh_latest=False)
            outcome["version"] = row.installed_version
            if code is None:
                outcome["error"] = "the install did not finish in time; its terminal is still open"
            elif code != 0:
                outcome["error"] = f"the installer exited with {code}: " + (tail.strip().splitlines() or [""])[-1][:300]
            elif not row.installed:
                outcome["error"] = f"the installer finished, but {tooling(harness).program} is not found on the environment's PATH"
            if row.installed:
                check = await self.self_check(env, harness)
                outcome["check"] = {"ok": check.ok}
                outcome["ok"] = not outcome["error"] and check.ok
            return outcome
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            logger.exception("installing %s in %s failed", harness, env)
            outcome["error"] = f"the install failed: {exc}"
            return outcome
        finally:
            self._release(operation)
            await self._publish(
                "harness.updated",
                {"env": env, "harness": harness, "kind": "install", "version": outcome["version"], "previous": "", "ok": outcome["ok"], "terminal_id": operation.terminal_id, "check_ok": (outcome["check"] or {}).get("ok"), **({"error": outcome["error"]} if outcome["error"] else {})},
            )

    async def install_node(self, env: str) -> dict[str, Any]:
        """The pinned Node into the container environment's home, in a terminal the operator watches."""
        if env != "container":
            raise HarnessRefused("the host environment uses the operator's own Node; the pinned Node is for the container")
        if self.runner is None:
            raise HarnessRefused("installs need the terminals service, which is not running here", status=503)
        port = self.port(env)
        operation = self._claim("install", env, NODE, target=NODE_VERSION)
        try:
            node = await self._check_node(env, port)
            if node.installed and node.version == NODE_VERSION:
                raise HarnessRefused(f"Node {NODE_VERSION} is already installed", status=409)
            operation.terminal_id = await self.runner.start(env, ["sh", "-c", node_install_script()], title=f"Installing Node {NODE_VERSION}")
        except BaseException:
            self._release(operation)
            raise
        self._spawn(self._finish_node(operation), f"harness-node-{env}")
        return {"state": "started", "operation": operation.view()}

    async def _finish_node(self, operation: Operation) -> None:
        error = ""
        try:
            assert self.runner is not None
            code, tail = await self.runner.wait(operation.terminal_id, timeout=self.config().install_timeout_s)
            node = await self._check_node(operation.env, self.port(operation.env))
            if code != 0:
                error = "the install did not finish in time" if code is None else f"the installer exited with {code}: " + (tail.strip().splitlines() or [""])[-1][:300]
            elif node.version != NODE_VERSION:
                error = f"Node {NODE_VERSION} was installed, but PATH finds {node.version or 'no node'} first"
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            logger.exception("installing Node failed")
            error = f"the install failed: {exc}"
        finally:
            self._release(operation)
            state = self.node.get(operation.env, NodeState())
            await self._publish("harness.updated", {"env": operation.env, "harness": NODE, "kind": "install", "version": state.version, "ok": not error, "terminal_id": operation.terminal_id, **({"error": error} if error else {})})

    async def sign_in(self, env: str, harness: str) -> dict[str, Any]:
        """Open the CLI's own sign-in in a terminal of that environment for the operator to complete.
        The manager never reads a token: it asks the CLI afterwards whether it is signed in."""
        tool = tooling(harness)
        if self.runner is None:
            raise HarnessRefused("sign-in needs the terminals service, which is not running here", status=503)
        port = self.port(env)
        installed = await tool.installed(port)
        if not installed.installed:
            raise HarnessRefused(f"{capabilities(harness).label} is not installed in the {env} environment", status=409)
        terminal_id = await self.runner.start(env, list(tool.sign_in), title=f"Sign in to {capabilities(harness).label}")
        self._spawn(self._after_sign_in(env, harness, terminal_id), f"harness-signin-{env}-{harness}")
        return {"terminal_id": terminal_id, "argv": list(tool.sign_in)}

    async def _after_sign_in(self, env: str, harness: str, terminal_id: str) -> None:
        assert self.runner is not None
        with contextlib.suppress(Exception):
            await self.runner.wait(terminal_id, timeout=self.config().install_timeout_s)
        with contextlib.suppress(HarnessRefused, EnvironmentUnavailable):
            await self.check(env, harness)

    # -- the background check --------------------------------------------------------------

    async def run(self) -> None:
        """Check each reachable environment when its last check is older than ``catalog_ttl_s``."""
        while True:
            for env in self.environments():
                # Asking for the port is what lets the host read the CLIs' transcripts there (the
                # extension registers their directories as it makes one). A restart with a fresh
                # catalog checked nothing, so nothing asked, and every transcript — the Feed, a late
                # acknowledgement, ReadStaff — was refused as "not under an allowed root".
                self.ports(env)
                try:
                    if await self.due(env):
                        await self.check(env)
                except (HarnessRefused, EnvironmentUnavailable) as exc:
                    logger.info("harness check of %s skipped: %s", env, exc)
                except Exception:
                    logger.exception("harness check of %s failed", env)
            await asyncio.sleep(TICK_S)

    async def due(self, env: str) -> bool:
        rows = await self.store.catalog_rows(env)
        ages = [a for a in (_age_s(r.checked_at) for r in rows) if a is not None]
        return len(rows) < len(TOOLING) or not ages or max(ages) > self.config().catalog_ttl_s or env not in self.node

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.publish(event_type, payload)
        except Exception:
            logger.exception("publishing %s failed", event_type)


__all__ = ["NODE", "HarnessManager", "HarnessRefused", "NodeState", "Operation", "SelfCheckRunner", "VisibleRunner", "http_fetch", "merge_agents"]
