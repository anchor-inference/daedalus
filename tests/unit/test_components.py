"""The optional pieces: detecting them, installing one, and asking the launcher for the two it owns.

Three things are checked here that no other suite can see, because each of them is about the seam
between this process and something outside it:

* what a component reads as in each of the two installation shapes, with the outside world faked
  rather than probed, so the container case can be exercised on a machine and the native case in CI;
* the install state machine — one at a time, a refusal that names what can do it instead, progress
  that reaches a watcher, and a failure that is reported rather than swallowed;
* the launcher bridge against a real HTTP server that enforces the launcher's own rules, so that
  "the launcher accepts a tokened request from the app" is a measurement and not an assumption.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host import components, launcher_bridge, toolchain
from daedalus.host.component_install import Busy, Installer, NotInstallable
from daedalus.speech.service import LocalSpeech
from daedalus.speech.tts_service import LocalTts


def settings_for(tmp_path: Path, *, native: bool) -> Settings:
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True)
    return Settings(_env_file=None, state_dir=state, native=native, bot_repo_dir=tmp_path / "data" / "daedalus")  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _forget_probes() -> Any:
    """The toolchain caches its answers for the life of the process, which is what a test is not."""
    toolchain._state.clear()
    yield
    toolchain._state.clear()


def present(monkeypatch: pytest.MonkeyPatch, names: set[str]) -> None:
    """Exactly these binaries are on the PATH, and nothing else is."""
    monkeypatch.setattr(components.shutil, "which", lambda name: f"/usr/bin/{name}" if name in names else None)


# -- detection ---------------------------------------------------------------------------------


def test_a_container_without_the_speech_extra_says_so_rather_than_what_its_image_usually_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The published image carries the extra. This one does not, and the page must say what is true."""
    monkeypatch.setattr(components, "engine_installed", lambda: False)
    present(monkeypatch, {"git", "rg", "opusdec"})
    registry = components.Registry(settings_for(tmp_path, native=False), RuntimeConfig())
    speech = registry.status(components.SPEECH)
    assert speech.state == "missing"
    # Nothing in a container can sync an extra into the image it is running, so no button is offered.
    assert speech.installable is False and speech.fix


def test_a_native_installation_offers_to_sync_the_speech_extra_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(components, "engine_installed", lambda: False)
    present(monkeypatch, {"uv", "git", "rg"})
    registry = components.Registry(settings_for(tmp_path, native=True), RuntimeConfig())
    speech = registry.status(components.SPEECH)
    assert speech.state == "missing" and speech.installable and speech.how == "extra"
    assert speech.requires_restart is False, "the engine is imported on demand; it appears without one"


def test_without_uv_the_native_installation_says_what_to_run_instead_of_offering_a_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(components, "engine_installed", lambda: False)
    present(monkeypatch, set())
    speech = components.Registry(settings_for(tmp_path, native=True), RuntimeConfig()).status(components.SPEECH)
    assert speech.installable is False
    assert "--extra speech" in speech.fix


def test_the_browser_and_node_are_the_launchers_natively_and_an_image_tag_in_a_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(toolchain, "status", lambda name: "not here")
    present(monkeypatch, set())

    native = components.Registry(settings_for(tmp_path / "a", native=True), RuntimeConfig())
    assert native.status(components.BROWSER).how == "launcher"
    assert native.status(components.NODE).how == "launcher"
    # Both are found through the environment the supervisor was started with, which a running process
    # cannot give itself: an install of either is only half done until something restarts it.
    assert native.status(components.BROWSER).requires_restart is True
    assert native.status(components.NODE).requires_restart is True

    docker = components.Registry(settings_for(tmp_path / "b", native=False), RuntimeConfig())
    browser = docker.status(components.BROWSER)
    assert browser.installable is False and ":browser" in browser.fix
    # No published image carries Node at all, so a container is not missing it — it cannot have it.
    assert docker.status(components.NODE).state == "unavailable"


def test_a_component_a_skill_declares_names_the_skills_it_holds_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skills = tmp_path / "data" / "daedalus" / "skills"
    (skills / "web-thing").mkdir(parents=True)
    (skills / "web-thing" / "SKILL.md").write_text("---\nname: web-thing\nrequires: browser, node\n---\nbody\n", encoding="utf-8")
    (skills / "writing").mkdir()
    (skills / "writing" / "SKILL.md").write_text("---\nname: writing\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(toolchain, "status", lambda name: "not here")
    present(monkeypatch, set())
    registry = components.Registry(settings_for(tmp_path, native=True), RuntimeConfig())
    assert registry.status(components.BROWSER).skills == ["web-thing"]
    assert registry.status(components.NODE).skills == ["web-thing"]


def test_an_installed_component_does_not_list_skills_as_held_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skills = tmp_path / "data" / "daedalus" / "skills"
    (skills / "web-thing").mkdir(parents=True)
    (skills / "web-thing" / "SKILL.md").write_text("---\nrequires: browser\n---\n", encoding="utf-8")
    monkeypatch.setattr(toolchain, "status", lambda name: "ok")
    present(monkeypatch, set())
    registry = components.Registry(settings_for(tmp_path, native=True), RuntimeConfig())
    assert registry.status(components.BROWSER).state == "installed"
    assert registry.status(components.BROWSER).skills == []


def test_the_model_components_are_a_count_and_a_size_rather_than_a_yes_or_no(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    present(monkeypatch, set())
    settings = settings_for(tmp_path, native=True)
    installed = settings.state_dir / "models" / "stt" / "gigaam-ru"
    installed.mkdir(parents=True)
    (installed / "encoder.onnx").write_bytes(b"x" * 4096)
    registry = components.Registry(settings, RuntimeConfig())
    entry = registry.status(components.STT_MODELS)
    assert entry.installed_count == 1 and entry.total_count > 1
    assert entry.disk_bytes == 4096
    # Nothing here downloads a model: the picker does, one at a time, with its own catalogue.
    assert entry.installable is False and entry.how == "models"


def test_every_component_in_the_catalogue_is_probed_and_answers_a_known_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    present(monkeypatch, set())
    registry = components.Registry(settings_for(tmp_path, native=True), RuntimeConfig())
    view = registry.view()
    assert [entry["id"] for entry in view["components"]] == list(components.ORDER)  # type: ignore[index]
    assert set(components.ORDER) == set(components.CATALOGUE)
    for entry in view["components"]:  # type: ignore[union-attr]
        assert entry["state"] in ("installed", "missing", "installing", "unavailable"), entry
        assert entry["how"] in components.HOW, entry
        assert entry["detail"], entry


def test_a_chosen_voice_with_no_runtime_to_speak_it_is_what_the_summary_calls_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's case: the page looked configured and said nothing about the missing half."""
    monkeypatch.setattr(components, "engine_installed", lambda: False)
    monkeypatch.setattr(toolchain, "status", lambda name: "not here")
    present(monkeypatch, {"uv"})
    config = RuntimeConfig()
    config.companion.tts.local_voice = "ru-dmitri"
    summary = components.Registry(settings_for(tmp_path, native=True), config).summary()
    assert components.SPEECH in summary["needed"]  # type: ignore[operator]
    # Nothing configured wants the browser here, so it is missing without being needed: a mark beside
    # every optional piece a portable build leaves out is a mark that means nothing.
    assert components.BROWSER in summary["missing"] and components.BROWSER not in summary["needed"]  # type: ignore[operator]


def test_nothing_is_needed_when_nothing_that_wants_it_is_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(components, "engine_installed", lambda: False)
    present(monkeypatch, {"uv"})
    assert components.Registry(settings_for(tmp_path, native=True), RuntimeConfig()).summary()["needed"] == []


# -- the launcher bridge -----------------------------------------------------------------------


class FakeLauncher:
    """A stand-in for the launcher's loopback page, enforcing the rules the real one enforces.

    The point of running a real server rather than patching ``httpx`` is that those rules are the
    whole security argument for this bridge: a request that carries the token, names the loopback
    address and sets no ``Sec-Fetch-Site`` is accepted, and one that does not is refused. A fake that
    checked nothing would pass whatever this module sent.
    """

    def __init__(self, token: str) -> None:
        self.token = token
        self.actions: list[str] = []
        self.busy = ""
        self.failure = ""
        self.refusals: list[str] = []
        self.known: set[str] | None = None
        """The actions this launcher has, or None for one that has every action asked of it."""
        self.jobs = True
        """Whether this launcher hands out a job id per action, as the shipped one does. False is a
        launcher older than jobs, which the app still has to be able to wait on."""
        self.job_states: dict[str, dict[str, str]] = {}
        self.last_job = ""
        self.polls = 0
        """How many times the app has asked this launcher how it is getting on. Tests wait on this
        rather than on a duration: the hand-over is then ordered by the code under test, and a host
        busy enough to spend a sleep before the poll it was meant to cover cannot change the
        outcome."""
        self.finish_after = 0
        """Poll number at which the work finishes, from inside the poll itself. 0 never finishes on
        its own. This is what models the launcher that was already done before anybody looked."""
        self.finish_error = ""
        self.conflict = ""
        """Non-empty: this launcher is already doing something else and refuses what it is asked."""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _refuse(self, why: str) -> None:
                outer.refusals.append(why)
                self.send_response(403)
                self.end_headers()

            def _json(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/api/jobs/") or self.path == "/api/status":
                    outer.polls += 1
                    if outer.finish_after and outer.polls >= outer.finish_after:
                        outer.finish(error=outer.finish_error)
                if self.path.startswith("/api/jobs/"):
                    job = outer.job_states.get(self.path.removeprefix("/api/jobs/"))
                    return self._json(200, job) if job else self._json(404, {"error": "no such job"})
                if self.path != "/api/status":
                    self.send_response(404)
                    self.end_headers()
                    return
                self._json(200, {"busy": outer.busy, "failure": outer.failure})

            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                site = self.headers.get("Sec-Fetch-Site")
                if site and site not in ("same-origin", "none"):
                    return self._refuse("cross-site")
                if (origin := self.headers.get("Origin")) and not origin.endswith(f":{outer.port}"):
                    return self._refuse("origin")
                if self.headers.get("Host") != f"127.0.0.1:{outer.port}":
                    return self._refuse("host")
                if self.headers.get(launcher_bridge.HEADER) != outer.token:
                    return self._refuse("token")
                action = self.path.removeprefix("/api/action/")
                if not self.path.startswith("/api/action/") or (outer.known is not None and action not in outer.known):
                    self.send_response(404)
                    self.end_headers()
                    return
                if outer.conflict:
                    body = outer.conflict.encode()
                    self.send_response(409)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                outer.actions.append(action)
                if not outer.jobs:
                    self.send_response(202)
                    self.end_headers()
                    return
                outer.last_job = f"j{len(outer.job_states) + 1}"
                outer.job_states[outer.last_job] = {"id": outer.last_job, "action": action, "state": "running", "error": ""}
                self._json(202, {"started": True, "job": outer.last_job})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def write_file(self, state_dir: Path, *, pid: int | None = None) -> None:
        import os

        launcher_bridge.instance_path(state_dir).write_text(
            json.dumps({"port": self.port, "token": self.token, "pid": pid if pid is not None else os.getpid()}),
            encoding="utf-8",
        )

    def finish(self, *, error: str = "") -> None:
        """The launcher puts the work down: the job it took ends, and it stops reading as busy."""
        self.busy = ""
        if error:
            self.failure = error
        job = self.job_states.get(self.last_job)
        if job is not None:
            job["state"] = "failed" if error else "done"
            job["error"] = error

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


async def polled(launcher: FakeLauncher, times: int = 1) -> None:
    """Hold until the app has actually asked the launcher how it is getting on, ``times`` more times.

    The thing every one of these tests needs to order is "the install is under way and has looked at
    the launcher at least once"; a sleep says that only on a host that is not busy, and the poll
    interval these tests patch down to ten milliseconds is a window a loaded host can spend several
    of before the sleep returns. The bound is long enough that nothing but a hang can reach it.
    """
    target = launcher.polls + times
    async with asyncio.timeout(60):
        while launcher.polls < target:
            await asyncio.sleep(0.001)


@pytest.fixture
def launcher() -> Any:
    fake = FakeLauncher("a-secret-the-launcher-minted")
    yield fake
    fake.close()


def test_the_handover_file_is_read_and_the_token_never_reaches_a_log_or_a_repr(tmp_path: Path, launcher: FakeLauncher) -> None:
    settings = settings_for(tmp_path, native=True)
    launcher.write_file(settings.state_dir)
    found = launcher_bridge.read(settings.state_dir)
    assert found is not None and found.port == launcher.port
    assert found.token == launcher.token
    # Everything that prints a Launcher — a traceback, a log line, a failing assertion — must not
    # print the one thing in it that is a key.
    assert launcher.token not in repr(found)


def test_a_file_left_behind_by_a_launcher_that_is_gone_is_not_believed(tmp_path: Path, launcher: FakeLauncher) -> None:
    settings = settings_for(tmp_path, native=True)
    launcher.write_file(settings.state_dir, pid=2**22 - 1)
    assert launcher_bridge.read(settings.state_dir) is None


def test_a_missing_or_broken_handover_file_is_simply_no_launcher(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, native=True)
    assert launcher_bridge.read(settings.state_dir) is None
    launcher_bridge.instance_path(settings.state_dir).write_text("{not json", encoding="utf-8")
    assert launcher_bridge.read(settings.state_dir) is None
    launcher_bridge.instance_path(settings.state_dir).write_text('{"port": 0, "token": ""}', encoding="utf-8")
    assert launcher_bridge.read(settings.state_dir) is None


async def test_the_launcher_accepts_a_tokened_request_from_the_app_under_its_own_rules(
    tmp_path: Path, launcher: FakeLauncher
) -> None:
    settings = settings_for(tmp_path, native=True)
    launcher.write_file(settings.state_dir)
    found = launcher_bridge.read(settings.state_dir)
    assert found is not None
    job = await launcher_bridge.act(found, "extra/node")
    assert launcher.actions == ["extra/node"] and launcher.refusals == []
    # The id is the whole of the wait that follows: without it the app can only watch a field that
    # reads the same before the work starts and after it ends.
    assert job and (await launcher_bridge.job(found, job))["state"] == "running"


async def test_a_request_without_the_token_is_refused_and_says_so(tmp_path: Path, launcher: FakeLauncher) -> None:
    wrong = launcher_bridge.Launcher(port=launcher.port, token="not the token", pid=1)
    with pytest.raises(launcher_bridge.LauncherUnavailable) as raised:
        await launcher_bridge.act(wrong, "extra/node")
    assert launcher.refusals == ["token"]
    assert "not the token" not in str(raised.value)


async def test_an_action_this_launcher_does_not_have_names_the_launcher_as_the_old_half(
    tmp_path: Path, launcher: FakeLauncher
) -> None:
    """A launcher from before this build answers 404 to `extra/speech`, and the sentence has to say
    which of the two halves is old rather than reading as a bug in the app."""
    launcher.known = {"extra/node", "extra/browser"}
    found = launcher_bridge.Launcher(port=launcher.port, token=launcher.token, pid=1)
    with pytest.raises(launcher_bridge.LauncherUnavailable, match="older than this build"):
        await launcher_bridge.act(found, "extra/speech")


# -- the install state machine -----------------------------------------------------------------


def missing_speech(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, native: bool = True) -> tuple[Settings, components.Status]:
    monkeypatch.setattr(components, "engine_installed", lambda: False)
    present(monkeypatch, {"uv"})
    settings = settings_for(tmp_path, native=native)
    return settings, components.Registry(settings, RuntimeConfig()).status(components.SPEECH)


async def test_a_component_that_cannot_be_installed_here_refuses_with_the_command_that_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, status = missing_speech(tmp_path, monkeypatch, native=False)
    with pytest.raises(NotInstallable) as raised:
        Installer(settings).start(status)
    assert raised.value.fix or raised.value.reason


async def test_one_install_at_a_time_and_the_second_is_told_what_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: FakeLauncher
) -> None:
    monkeypatch.setattr("daedalus.host.component_install.LAUNCHER_POLL", 0.01)
    settings, speech = missing_speech(tmp_path, monkeypatch)
    launcher.write_file(settings.state_dir)
    launcher.busy = "installing browser"
    installer = Installer(settings)
    browser = components.Status(components.BROWSER, "missing", "not here", installable=True, how="launcher")
    installer.start(browser)
    await polled(launcher)
    with pytest.raises(Busy) as raised:
        installer.start(speech)
    assert raised.value.component_id == components.BROWSER
    launcher.finish()
    await installer.wait(components.BROWSER)
    # And the lane is free again the moment the first one is over, rather than at the next request.
    assert installer.running_id() == ""


async def test_a_failing_install_is_reported_on_the_stream_rather_than_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, status = missing_speech(tmp_path, monkeypatch)
    installer = Installer(settings)
    # The checkout the sync would run in does not exist, so uv fails — the ordinary way this goes
    # wrong on a machine, rather than an injected exception.
    async with installer.watch() as queue:
        installer.start(status)
        await installer.wait(components.SPEECH)
        frames = []
        while not queue.empty():
            frames.append(await queue.get())
    assert [f["state"] for f in frames][0] == "queued"
    assert frames[-1]["state"] == "failed" and frames[-1]["error"]
    assert installer.running_id() == "", "a finished install must not hold the lane"


async def test_progress_reaches_a_watcher_and_a_finished_launcher_install_asks_for_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: FakeLauncher
) -> None:
    monkeypatch.setattr("daedalus.host.component_install.LAUNCHER_POLL", 0.01)
    settings = settings_for(tmp_path, native=True)
    launcher.write_file(settings.state_dir)
    launcher.busy = "installing node"
    installer = Installer(settings)
    node = components.Status(components.NODE, "missing", "not here", installable=True, how="launcher")
    async with installer.watch() as queue:
        installer.start(node)
        await polled(launcher, 2)  # it has been seen busy at least once
        launcher.finish()  # the launcher put the work down
        await installer.wait(components.NODE)
        frames = []
        while not queue.empty():
            frames.append(await queue.get())
    assert launcher.actions == ["extra/node"]
    assert any(f["step"] == "installing node" for f in frames), "the launcher's own progress line is forwarded"
    assert frames[-1]["state"] == "installed" and frames[-1]["restart_required"] is True


async def test_what_the_launcher_failed_at_becomes_the_reason_the_install_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: FakeLauncher
) -> None:
    monkeypatch.setattr("daedalus.host.component_install.LAUNCHER_POLL", 0.01)
    settings = settings_for(tmp_path, native=True)
    launcher.write_file(settings.state_dir)
    launcher.busy = "installing browser"
    installer = Installer(settings)
    browser = components.Status(components.BROWSER, "missing", "not here", installable=True, how="launcher")
    installer.start(browser)
    await polled(launcher, 2)  # it has been seen busy at least once
    launcher.finish(error="the download did not verify")
    await installer.wait(components.BROWSER)
    frame = installer.progress_of(components.BROWSER) or {}
    assert frame["state"] == "failed" and "did not verify" in str(frame["error"])


async def test_an_install_the_launcher_finished_between_two_polls_is_not_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: FakeLauncher
) -> None:
    """The launcher is never seen busy at all, and the install still ends as an install.

    Node already cached, a re-install that is a no-op: the work is over before the first poll. On the
    single ``busy`` field that is indistinguishable from a launcher which has not picked the work up
    yet, so the app used to wait out its whole bound and then report a failure for a component that
    was in fact installed. The job says which of the two it is.
    """
    monkeypatch.setattr("daedalus.host.component_install.LAUNCHER_POLL", 0.01)
    settings = settings_for(tmp_path, native=True)
    launcher.write_file(settings.state_dir)
    installer = Installer(settings)
    node = components.Status(components.NODE, "missing", "not here", installable=True, how="launcher")
    launcher.finish_after = 1  # over before anybody looked: the first poll finds it already done
    installer.start(node)
    await installer.wait(components.NODE)
    frame = installer.progress_of(components.NODE) or {}
    assert frame["state"] == "installed", f"a finished install was read as something else: {frame}"


async def test_a_launcher_that_never_takes_the_work_is_not_waited_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: FakeLauncher
) -> None:
    """A launcher too old for jobs, busy with something the app did not ask for.

    It answers 202 and then never turns busy, because the action was refused inside its goroutine.
    Nothing here can measure the component either — node and the browser are found through the
    environment the process was started with — so the only honest answer is to stop waiting and say
    what probably happened, rather than hold the one install lane for the whole bound.
    """
    monkeypatch.setattr("daedalus.host.component_install.LAUNCHER_POLL", 0.01)
    monkeypatch.setattr("daedalus.host.component_install.LAUNCHER_IDLE_POLLS", 3)
    settings = settings_for(tmp_path, native=True)
    launcher.jobs = False
    launcher.write_file(settings.state_dir)
    installer = Installer(settings)
    node = components.Status(components.NODE, "missing", "not here", installable=True, how="launcher")
    installer.start(node)
    await installer.wait(components.NODE)
    frame = installer.progress_of(components.NODE) or {}
    assert frame["state"] == "failed"
    assert "never started" in str(frame["error"]), frame["error"]
    # And the class name of an exception is never what the operator is shown.
    assert "TimeoutError" not in str(frame["error"])


async def test_a_launcher_already_doing_something_else_refuses_rather_than_being_waited_on(
    tmp_path: Path, launcher: FakeLauncher
) -> None:
    launcher.conflict = "update is already running"
    settings = settings_for(tmp_path, native=True)
    launcher.write_file(settings.state_dir)
    installer = Installer(settings)
    browser = components.Status(components.BROWSER, "missing", "not here", installable=True, how="launcher")
    installer.start(browser)
    await installer.wait(components.BROWSER)
    frame = installer.progress_of(components.BROWSER) or {}
    assert frame["state"] == "failed" and "update is already running" in str(frame["error"])


def test_a_download_with_nowhere_to_land_is_refused_before_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: FakeLauncher
) -> None:
    """A full disk used to surface as whatever raw text the tool underneath last wrote."""
    settings = settings_for(tmp_path, native=True)
    launcher.write_file(settings.state_dir)
    monkeypatch.setattr(
        "daedalus.host.component_install.shutil.disk_usage",
        lambda path: SimpleNamespace(total=2 * 1024 * 1024, used=1024 * 1024, free=1024 * 1024),
    )
    browser = components.Status(components.BROWSER, "missing", "not here", installable=True, how="launcher")
    with pytest.raises(NotInstallable) as raised:
        Installer(settings).start(browser)
    assert "MB free" in raised.value.reason and "1 MB" in raised.value.reason


async def test_a_headless_start_with_no_launcher_says_what_to_run_instead(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, native=True)
    installer = Installer(settings)
    node = components.Status(components.NODE, "missing", "not here", installable=True, how="launcher")
    installer.start(node)
    await installer.wait(components.NODE)
    frame = installer.progress_of(components.NODE) or {}
    assert frame["state"] == "failed" and "no launcher is running" in str(frame["error"])
    assert installer.launcher_present() is False


async def test_the_restart_is_the_launchers_and_is_refused_with_a_command_when_there_is_none(tmp_path: Path) -> None:
    installer = Installer(settings_for(tmp_path, native=True))
    with pytest.raises(NotInstallable) as raised:
        await installer.restart()
    assert raised.value.fix


async def test_the_restart_reaches_the_launcher_as_its_own_action(tmp_path: Path, launcher: FakeLauncher) -> None:
    settings = settings_for(tmp_path, native=True)
    launcher.write_file(settings.state_dir)
    await Installer(settings).restart()
    assert launcher.actions == ["restart"]


# -- the API -----------------------------------------------------------------------------------


class FakeManager:
    async def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return []

    async def get_state(self, session_id: str) -> None:
        return None

    class providers:  # noqa: N801
        @staticmethod
        def available() -> list[str]:
            return []

        @staticmethod
        async def close_retired() -> None:
            return None


class FakeApp:
    def __init__(self, tmp_path: Path, *, native: bool) -> None:
        self.settings = settings_for(tmp_path, native=native)
        self.config = RuntimeConfig()
        self.manager = FakeManager()
        self.front: Any = None
        self.extensions: dict[str, Any] = {}
        self.speech = LocalSpeech(self.settings.state_dir, self.config)
        self.tts = LocalTts(self.settings.state_dir, self.config)
        self.components = Installer(self.settings)

    async def save_config(self, config: RuntimeConfig, *, expected_revision: str | None = None) -> None:
        self.config = config


HEAD = {"X-Daedalus-Token": "tok"}


@pytest.fixture
def native_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(components, "engine_installed", lambda: False)
    present(monkeypatch, {"uv"})
    app = FakeApp(tmp_path, native=True)
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        client.app_state = app  # type: ignore[attr-defined]
        yield client


def test_the_page_is_served_with_the_mode_the_disk_and_every_component(native_client: TestClient) -> None:
    body = native_client.get("/api/components", headers=HEAD).json()
    assert body["mode"] == "native"
    assert [c["id"] for c in body["components"]] == list(components.ORDER)
    assert body["launcher"] is False and body["busy"] == ""
    assert components.SPEECH in body["missing"]


def test_a_component_nobody_has_heard_of_is_a_404(native_client: TestClient) -> None:
    assert native_client.post("/api/components/not-a-thing/install", headers=HEAD).status_code == 404
    assert native_client.post("/api/components/not-a-thing/cancel", headers=HEAD).status_code == 404


def test_a_component_this_installation_cannot_install_answers_501_with_the_way_that_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(components, "engine_installed", lambda: False)
    monkeypatch.setattr(toolchain, "status", lambda name: "not here")
    present(monkeypatch, set())
    app = FakeApp(tmp_path, native=False)
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        answer = client.post(f"/api/components/{components.BROWSER}/install", headers=HEAD)
        assert answer.status_code == 501
        assert ":browser" in answer.json()["detail"]


def test_installing_something_already_installed_answers_installed_without_doing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(components, "engine_installed", lambda: True)
    present(monkeypatch, {"uv"})
    app = FakeApp(tmp_path, native=True)
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        body = client.post(f"/api/components/{components.SPEECH}/install", headers=HEAD).json()
        assert body["state"] == "installed"
        assert app.components.running_id() == ""


def test_a_second_install_while_one_runs_is_a_409_naming_the_first(native_client: TestClient) -> None:
    app = native_client.app_state  # type: ignore[attr-defined]
    app.components._running_id = components.BROWSER
    app.components._task = None
    answer = native_client.post(f"/api/components/{components.SPEECH}/install", headers=HEAD)
    assert answer.status_code == 409 and components.BROWSER in answer.json()["detail"]


def test_the_capability_report_carries_the_components_so_the_shell_needs_no_second_poll(native_client: TestClient) -> None:
    body = native_client.get("/api/capabilities", headers=HEAD).json()
    assert body["components"]["mode"] == "native"
    assert components.SPEECH in body["components"]["missing"]
    assert "selfdev" in body, "the components must ride on this answer rather than replace it"


async def test_the_stream_replays_what_is_already_known_before_it_waits(tmp_path: Path) -> None:
    """A page opened half-way through an install must see the install, not an empty screen.

    Driven through the installer rather than through an HTTP client: an event stream held open by a
    test client is a connection nothing ever disconnects, and the generator is written to keep the
    connection alive until the reader goes away. What matters here is the order — what has already
    happened, and then what happens next.
    """
    installer = Installer(settings_for(tmp_path, native=True))
    installer._publish(components.SPEECH, "running", step="uv sync")
    replayed = installer.all_progress()
    assert [f["id"] for f in replayed] == [components.SPEECH]
    assert replayed[0]["step"] == "uv sync"
    async with installer.watch() as queue:
        installer._publish(components.SPEECH, "installed", step="done")
        frame = await queue.get()
    assert frame["state"] == "installed"


def test_the_stream_is_mounted_and_behind_the_same_authentication_as_everything_else(native_client: TestClient) -> None:
    routes = {getattr(route, "path", "") for route in native_client.app.routes}  # type: ignore[attr-defined]
    assert "/api/components/stream" in routes
    assert native_client.get("/api/components/stream").status_code == 401


def test_every_sentence_the_page_shows_comes_as_a_key_the_app_can_translate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detail line is the largest body text on a card, and it used to be this module's English.

    A Russian page with an English sentence in the middle of it reads as a gap, not as a term of
    art. So every state every probe can return carries a key, and every key has a row in the
    dictionary. The fix line is exempt: it is a command, and a command is the same in both.
    """
    dictionary = (Path(__file__).resolve().parents[2] / "miniapp" / "src" / "i18n.ts").read_text(encoding="utf-8")
    seen: set[str] = set()
    for native in (True, False):
        settings = settings_for(tmp_path / ("native" if native else "docker"), native=native)
        for found in (set(), {"git", "rg", "opusdec", "bwrap", "uv", "node", "npx"}):
            present(monkeypatch, found)
            for entry in components.Registry(settings, RuntimeConfig()).view()["components"]:  # type: ignore[union-attr]
                assert entry["detail_key"], f"{entry['id']} says {entry['detail']!r} with no key to say it by"
                seen.add(str(entry["detail_key"]))
    for key in sorted(seen):
        assert f'"{key}"' in dictionary, key


def test_every_component_id_the_page_can_show_has_a_name_in_the_app(native_client: TestClient) -> None:
    """The ids are the join between this module and the dictionary; a new one with no row is a
    bracketed key on the page that exists to explain it. The app's own suite asserts the other half."""
    dictionary = Path(__file__).resolve().parents[2] / "miniapp" / "src" / "i18n.ts"
    text = dictionary.read_text(encoding="utf-8")
    for component_id in components.ORDER:
        assert f'"comp.name.{component_id}"' in text, component_id
        assert f'"comp.what.{component_id}"' in text, component_id
    for component in components.CATALOGUE.values():
        for key in component.enables:
            assert f'"comp.enables.{key}"' in text, key
