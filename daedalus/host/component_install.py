"""Putting a missing component in place, one at a time, with the work visible while it happens.

Three things an installer of this kind has to get right, and each of them is a decision in the code
below rather than a detail.

**One at a time.** Two ``uv sync`` runs into the same environment at once is how a site-packages
directory ends up half written, and two launcher actions at once is refused by the launcher anyway.
So a second request while one is running is a 409 naming what is running, not a queue.

**The work is somewhere else.** A Python extra is a subprocess of this process, and its output is
lines this module can forward. Node and the headless browser are the launcher's: it was asked over
the loopback bridge, it answered "started", and what it does next is visible only as its own status.
So progress here is honest about which of the two it is — a line of output while there are lines, and
otherwise the plain fact that the launcher is working on it.

**A finished install is not always a working component.** The speech engine is imported on demand and
appears at once; Node and the browser are found through the environment the supervisor started with,
so the process has to be restarted before it can see them. That is carried on every frame as
``restart_required`` rather than discovered by an operator whose new browser does not work.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from daedalus.config import Settings
from daedalus.host import components, launcher_bridge, searxng

logger = logging.getLogger(__name__)

SYNC_TIMEOUT = 900.0
"""How long a ``uv sync`` may take before it is killed. Wheels over a slow link, not a resolution."""

LAUNCHER_BASE_TIMEOUT = 300.0
"""What the wait for the launcher allows before the download itself is counted: finding the machine's
link, unpacking, and a launcher that is slow to pick the work up."""

LAUNCHER_SECONDS_PER_MB = 6.0
"""Added to the base for every megabyte the component declares — about 170 kB/s, which is a bad link
rather than a normal one. A bound scaled to the work is a bound that can say something when it is
reached; one round number for a fifteen-megabyte extra and a hundred-megabyte browser cannot."""

LAUNCHER_POLL = 2.0
"""How often the launcher's status is asked while it works. Its own page polls at about this rate."""

LAUNCHER_IDLE_POLLS = 15
"""How many polls a launcher too old to hand out job ids may go without picking the work up before
the app stops waiting. It does one action at a time, so a launcher busy with something else would
otherwise be waited out for the whole bound."""

DISK_HEADROOM = 3
"""What a download needs free, as a multiple of its own size: the archive, what it unpacks into, and
room for the installation to go on working. A full disk surfaces as whatever raw text the tool
underneath last wrote, which is not a thing an operator can act on."""

QUEUE_SIZE = 64


class Busy(RuntimeError):
    """Something is already installing. Carries what, so the answer can name it."""

    def __init__(self, component_id: str) -> None:
        super().__init__(component_id)
        self.component_id = component_id


class NotInstallable(RuntimeError):
    """This component cannot be installed by this process. Carries the sentence that says what can."""

    def __init__(self, reason: str, fix: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.fix = fix


class Installer:
    """The state machine behind ``POST /api/components/{id}/install``.

    Held by the application for its lifetime, because the progress of an install has to outlive the
    request that started it: the browser is fetched over minutes and the page that asked for it may
    be reloaded, closed and opened again while it happens.
    """

    def __init__(self, settings: Settings, *, after: Callable[[str, bool], Awaitable[None]] | None = None) -> None:
        self.settings = settings
        self.after = after
        """Called with a component id and whether it is now installed, once an install or a removal
        is through — where the application adjusts what depends on it (WebSearch, for SearXNG)."""
        self._installing: object | None = None
        self._progress: dict[str, dict[str, object]] = {}
        self._task: asyncio.Task[None] | None = None
        self._running_id = ""
        self._process: asyncio.subprocess.Process | None = None
        self._watchers: list[asyncio.Queue[dict[str, object]]] = []

    # -- what is happening --------------------------------------------------------------

    def running_id(self) -> str:
        """The component being installed, or an empty string. A finished task is not running."""
        if self._task is not None and self._task.done():
            self._running_id = ""
        return self._running_id

    def progress_of(self, component_id: str) -> dict[str, object] | None:
        return self._progress.get(component_id)

    def all_progress(self) -> list[dict[str, object]]:
        return list(self._progress.values())

    def launcher_present(self) -> bool:
        """Whether a launcher is holding this installation — the answer the page needs before it
        offers a button that only a launcher can honour."""
        return launcher_bridge.read(self.settings.state_dir) is not None

    @contextlib.asynccontextmanager
    async def watch(self) -> AsyncIterator[asyncio.Queue[dict[str, object]]]:
        """Frames for as long as the caller holds the queue.

        Bounded, and a full queue drops rather than blocks: a client that stopped reading must not be
        able to stall an install.
        """
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._watchers.append(queue)
        try:
            yield queue
        finally:
            with contextlib.suppress(ValueError):
                self._watchers.remove(queue)

    def _publish(self, component_id: str, state: str, *, step: str = "", step_key: str = "", error: str = "", restart: bool | None = None) -> dict[str, object]:
        # The restart a component needs is a property of the component, not of the moment: every frame
        # carries it, so a page that joins the stream half-way through knows what the end will ask for.
        if restart is None:
            known = components.CATALOGUE.get(component_id)
            restart = bool(known and known.requires_restart)
        # ``step`` is a line of output — uv's, or the launcher's own — and stays as it was written.
        # ``step_key`` is for the one step that is this module's own sentence rather than somebody
        # else's output, so the page can write it in the reader's language.
        frame: dict[str, object] = {"id": component_id, "state": state, "step": step, "step_key": step_key, "error": error, "restart_required": restart}
        self._progress[component_id] = frame
        for queue in list(self._watchers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(frame)
        return frame

    # -- starting and stopping ----------------------------------------------------------

    def start(self, status: components.Status) -> dict[str, object]:
        """Begin installing one component. Returns the first frame; the work is a background task.

        Raises :class:`Busy` when something else is running and :class:`NotInstallable` when this
        process cannot do it here — the two answers the endpoint turns into 409 and 501.
        """
        if (running := self.running_id()) and running != status.id:
            raise Busy(running)
        if running == status.id:
            return self._progress[status.id]
        if not status.installable:
            raise NotInstallable(status.detail or f"{status.id} cannot be installed from here", status.fix)
        component = components.CATALOGUE[status.id]
        self._require_disk(component)
        self._running_id = status.id
        frame = self._publish(status.id, "queued", step="starting", restart=component.requires_restart)
        runner = {"extra": self._sync_extra, "service": self._install_service}.get(status.how, self._ask_launcher)
        self._task = asyncio.ensure_future(self._run(runner, status.id, component.requires_restart))
        return frame

    def _require_disk(self, component: components.Component) -> None:
        """Refuse a download this machine has no room for, before it starts rather than in the middle.

        The multiple is what an unpack costs: the archive and the tree it becomes are both on the
        disk at once, and an installation with nothing left over stops working in ways that have
        nothing to do with the component. Asked of the state directory, because that is where the
        installation's own folder is and therefore what fills up.
        """
        if not component.download_bytes:
            return
        try:
            free = shutil.disk_usage(self.settings.state_dir).free
        except OSError:
            return  # not knowing is not a reason to refuse; the install says so itself if it runs out
        needed = component.download_bytes * DISK_HEADROOM
        if free < needed:
            mb = 1024 * 1024
            raise NotInstallable(
                f"{component.id} needs about {needed // mb} MB free while it is unpacked, and this installation has {free // mb} MB",
                "free some space and try again",
            )

    async def _run(self, runner, component_id: str, restart: bool) -> None:  # type: ignore[no-untyped-def]
        try:
            await runner(component_id)
        except asyncio.CancelledError:
            self._publish(component_id, "cancelled", step="stopped")
            raise
        except NotInstallable as exc:
            self._publish(component_id, "failed", error=exc.reason)
        except TimeoutError:
            # A bare TimeoutError carries no message at all, and the frame under it used to read
            # "TimeoutError: " — the class name and a colon — for a component that might be there.
            self._publish(component_id, "failed", error=f"installing {component_id} took longer than this installation waits, and the app stopped watching")
        except Exception as exc:  # noqa: BLE001 - the frame is the report; nothing above this reads it
            logger.warning("installing %s failed: %s: %s", component_id, type(exc).__name__, exc)
            self._publish(component_id, "failed", error=f"{type(exc).__name__}: {exc}")
        else:
            self._settle(component_id)
            await self._after(component_id, True)
            self._publish(component_id, "installed", step="done", restart=restart)
        finally:
            self._running_id = ""
            self._process = None
            self._installing = None

    async def _after(self, component_id: str, installed: bool) -> None:
        if self.after is None:
            return
        try:
            await self.after(component_id, installed)
        except Exception as exc:  # noqa: BLE001 - the component is in place either way; this is the wiring around it
            logger.warning("adjusting the configuration after %s changed failed: %s: %s", component_id, type(exc).__name__, exc)

    def _settle(self, component_id: str) -> None:
        """Let go of whatever cached the component's absence, now that it is there.

        The speech engine's presence is asked once and remembered — it cannot appear while a container
        runs — which stops being true the moment an installation can install it into itself. A cache
        that outlives the fact it caches is how a finished install goes on reading as missing.
        """
        if component_id == components.SPEECH:
            from daedalus.speech import service as speech_service  # Lazy: only once an install lands

            speech_service.forget_engine()

    async def wait(self, component_id: str) -> None:
        """Block until this component's install is over. For the callers that need it done, not started."""
        task = self._task
        if task is None or self.running_id() != component_id:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)

    def cancel(self, component_id: str) -> bool:
        """Stop an install, where the step underneath can be stopped.

        A ``uv sync`` is a child of this process and is killed. A launcher action is not: the launcher
        owns it, and asking it to stop half way through unpacking a browser would leave the folder in
        a state neither side describes. So this reports what it could do rather than claiming both.
        """
        if self.running_id() != component_id or self._task is None:
            return False
        process = self._current_process()
        if process is None:
            return False
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        self._task.cancel()
        return True

    def cancellable(self, component_id: str) -> bool:
        """Whether :meth:`cancel` would do anything: there is a child process of ours to kill."""
        return self.running_id() == component_id and self._current_process() is not None

    def _current_process(self) -> asyncio.subprocess.Process | None:
        if self._process is not None:
            return self._process
        # A SearXNG install runs its steps through an object of its own, which holds the child.
        return getattr(self._installing, "process", None)

    # -- the two ways a component arrives -----------------------------------------------

    async def _sync_extra(self, component_id: str) -> None:
        """A Python extra, synced into the environment this process is importing from.

        ``--inexact`` is what keeps it from being destructive: without it uv removes everything the
        lock does not mention, which in a running installation is whatever other extra was installed
        earlier. ``--frozen`` keeps it from relocking, which would be a change to a tracked file.

        What it does not keep it from is upgrading: a dependency the lock has moved is replaced on
        disk under a live interpreter, and this process keeps whatever it has already imported until
        it is restarted. That is why ``requires_restart`` being false for the speech engine is a
        statement about the new import and not about the environment as a whole.
        """
        uv = shutil.which("uv")
        if uv is None:
            raise NotInstallable("uv is not on the PATH, so the extra cannot be synced from here",
                                 f"uv sync --frozen --inexact --extra {component_id}")
        repo = Path(self.settings.bot_repo_dir)
        self._publish(component_id, "running", step=f"uv sync --extra {component_id}")
        process = await asyncio.create_subprocess_exec(
            uv, "sync", "--frozen", "--inexact", "--extra", component_id,
            cwd=str(repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._process = process
        tail: list[str] = []
        assert process.stdout is not None
        try:
            async with asyncio.timeout(SYNC_TIMEOUT):
                async for raw in process.stdout:
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    tail.append(line)
                    del tail[:-20]
                    self._publish(component_id, "running", step=line)
                await process.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise NotInstallable(f"installing {component_id} took longer than {int(SYNC_TIMEOUT)} s and was stopped") from None
        if process.returncode:
            last = " / ".join(tail[-4:])[:400]
            # The engine ships wheels and no sdist, so a platform with no prebuilt wheel fails at
            # resolution rather than while building. That is not a broken installation, and an
            # operator should not go looking for one.
            raise NotInstallable(
                f"{component_id} could not be installed: {last}. If this says no matching distribution, "
                f"this platform has no prebuilt wheel for it."
            )

    async def _install_service(self, component_id: str) -> None:
        """SearXNG: put it in the installation's folder, and wait for the supervisor to bring it up.

        Done by this process rather than the launcher, because it is the same work in both shapes —
        a source archive and a ``uv`` environment — and the container has no launcher to ask.
        """
        root = searxng.root_for(self.settings.state_dir, self.settings.runtime_dir)
        template = Path(self.settings.bot_repo_dir) / "deploy" / "searxng" / "settings.yml"
        install = searxng.Install(root, template, lambda line: self._publish(component_id, "running", step=line))
        self._installing = install
        try:
            url = await install.run()
        except searxng.InstallFailed as exc:
            raise NotInstallable(str(exc)) from None
        if not url:
            logger.info("searxng installed; nothing has started it yet")

    async def remove(self, component_id: str) -> None:
        """Take a removable component away. Only SearXNG is: everything else is the runtime's own."""
        if component_id != components.SEARXNG:
            raise NotInstallable(f"{component_id} cannot be removed from here")
        if (running := self.running_id()):
            raise Busy(running)
        root = searxng.root_for(self.settings.state_dir, self.settings.runtime_dir)
        try:
            await searxng.remove(root)
        except searxng.InstallFailed as exc:
            raise NotInstallable(str(exc)) from None
        finally:
            self._progress.pop(component_id, None)
        await self._after(component_id, False)

    async def _ask_launcher(self, component_id: str) -> None:
        """Node or the headless browser: the launcher's folder, so the launcher's job.

        If no launcher is holding the installation — a headless native start, or one the operator
        stopped — this says so with the command that does the same thing from a terminal, rather than
        failing with a connection error nobody can act on.
        """
        launcher = launcher_bridge.read(self.settings.state_dir)
        if launcher is None:
            raise NotInstallable(
                "no launcher is running, and only the launcher can put this into the installation's runtime folder",
                f"daedalus-desktop --extra {component_id}",
            )
        self._publish(component_id, "running", step="the launcher was asked", step_key="comp.step.launcher")
        try:
            job = await launcher_bridge.act(launcher, f"extra/{component_id}")
        except launcher_bridge.LauncherBusy as exc:
            raise NotInstallable(f"the launcher could not take this on: {exc}", f"daedalus-desktop --extra {component_id}") from None
        await self._wait_for_launcher(component_id, launcher, job)

    async def _wait_for_launcher(self, component_id: str, launcher: launcher_bridge.Launcher, job: str) -> None:
        """Watch the action the launcher was given until it is over, then report what became of it.

        The launcher answers 202 with the id of the job it claimed for the action, and that id is the
        whole of this wait: asked about it, the launcher says running, done, or failed and why. What
        it replaced was a watch on the single ``busy`` field, which cannot tell "not picked up yet"
        from "finished a moment ago" — they are the same empty string. An action that ended between
        two polls and one the launcher never took at all therefore looked identical, and both left
        the app waiting out its whole bound and then reporting a failure for a component that may
        well have been installed all along.

        The bound is the download rather than a round number, because a bound that is about the work
        is one that can say something when it is reached.
        """
        bound = LAUNCHER_BASE_TIMEOUT + components.CATALOGUE[component_id].download_bytes / (1024 * 1024) * LAUNCHER_SECONDS_PER_MB
        started = False
        idle = 0
        try:
            async with asyncio.timeout(bound):
                while True:
                    if job:
                        answer = await launcher_bridge.job(launcher, job)
                        if not answer:
                            raise NotInstallable(f"the launcher no longer knows the job it took for {component_id}; it was restarted while it worked")
                        if answer["state"] == "failed":
                            raise NotInstallable(f"the launcher could not install {component_id}: {answer['error'] or 'it did not say why'}")
                        if answer["state"] == "done":
                            return
                        busy, _failure = await launcher_bridge.busy(launcher)
                        self._publish(component_id, "running", step=busy, step_key="" if busy else "comp.step.launcher")
                    else:
                        # An older launcher hands out no job ids, so the only thing it says about
                        # itself is whether it is busy. Watch that — and give up on a launcher that
                        # never picks the work up, rather than waiting out the bound on one that is
                        # doing something else entirely.
                        busy, failure = await launcher_bridge.busy(launcher)
                        if failure:
                            raise NotInstallable(f"the launcher could not install {component_id}: {failure}")
                        if busy:
                            started = True
                            self._publish(component_id, "running", step=busy)
                        elif started:
                            return
                        else:
                            idle += 1
                            if idle > LAUNCHER_IDLE_POLLS:
                                raise NotInstallable(f"the launcher never started installing {component_id}; it does one thing at a time and may be busy with something else")
                            self._publish(component_id, "running", step="the launcher was asked", step_key="comp.step.launcher")
                    await asyncio.sleep(LAUNCHER_POLL)
        except TimeoutError:
            raise NotInstallable(
                f"the launcher was still installing {component_id} after {int(bound)} s and the app stopped watching; "
                "the launcher's own window says where it got to",
            ) from None

    # -- the restart the launcher offers ------------------------------------------------

    async def restart(self) -> str:
        """Ask the launcher to restart the agent, so a new PATH and a new browser are in force.

        Only the launcher can do this natively: the supervisor's environment is decided when the
        launcher starts it, and a process cannot give itself a PATH it was not started with.
        """
        launcher = launcher_bridge.read(self.settings.state_dir)
        if launcher is None:
            raise NotInstallable(
                "no launcher is running, so the restart has to be done where this installation was started",
                "daedalus-desktop restart",
            )
        await launcher_bridge.act(launcher, "restart")
        return "the launcher was asked to restart the agent"


__all__ = ["Busy", "Installer", "NotInstallable"]
