"""The host's end of a native public address: what it publishes, what it tells the door, whom it recognises."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from daedalus import doctor, public_address
from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import api_public_address, tunnel_address, tunnel_watch
from daedalus.extensions.tunnel_address import TunnelAddress
from daedalus.host import installation_reach, launcher_bridge, tunnels

ADDRESS = "https://x.example.test"


class Door:
    def __init__(self) -> None:
        self.client = SimpleNamespace(configured=True)
        self.told = 0
        self.fail = False

    async def publish_address(self) -> None:
        if self.fail:
            raise RuntimeError("the door is down")
        self.told += 1


def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, view: dict[str, Any] | Exception, *, native: bool = True, **settings: Any) -> tuple[TunnelAddress, Any]:
    options = {"state_dir": tmp_path / "state", "services_door_port": 8766, "guest_door_port": 8764, "services_public_host": "localhost", "native": native, **settings}
    app = SimpleNamespace(settings=Settings(_env_file=None, telegram_bot_token="123:abc", owner_user_id=1, **options), extensions={}, config=RuntimeConfig())  # type: ignore[call-arg]
    door = Door()
    app.extensions["guest_door"] = door
    answers = {"view": view, "changes": []}

    async def tunnel(_launcher: Any, change: dict[str, Any] | None = None) -> dict[str, Any]:
        if change is not None:
            answers["changes"].append(change)
        if isinstance(answers["view"], Exception):
            raise answers["view"]
        return answers["view"]

    monkeypatch.setattr(launcher_bridge, "read", lambda _state: launcher_bridge.Launcher(port=1, token="t", pid=0))
    monkeypatch.setattr(launcher_bridge, "tunnel", tunnel)
    address = TunnelAddress(app)
    address.answers = answers  # type: ignore[attr-defined]
    return address, app


async def test_an_address_that_is_up_goes_to_the_links_and_to_the_door(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, {"enabled": True, "state": "up", "address": ADDRESS, "pids": [41, 42]})
    assert public_address.services_base(app.settings) == "http://localhost:8766"
    await address.refresh()
    assert public_address.services_base(app.settings) == ADDRESS
    assert public_address.guest_base(app.settings) == ADDRESS
    assert app.extensions["guest_door"].told == 1
    await address.refresh()
    assert app.extensions["guest_door"].told == 1, "told once, not every five seconds"
    assert "x.example.test" in public_address.own_doors(app.settings).hosts, "the agent's rule on calling itself follows it too"


async def test_a_new_address_replaces_the_old_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, {"enabled": True, "state": "up", "address": ADDRESS})
    await address.refresh()
    address.answers["view"] = {"enabled": True, "state": "up", "address": "https://y.example.test"}  # type: ignore[attr-defined]
    await address.refresh()
    assert public_address.guest_base(app.settings) == "https://y.example.test"
    assert app.extensions["guest_door"].told == 2


async def test_a_reconnecting_address_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, {"enabled": True, "state": "up", "address": ADDRESS})
    await address.refresh()
    address.answers["view"] = {"enabled": True, "state": "reconnecting", "address": ADDRESS}  # type: ignore[attr-defined]
    await address.refresh()
    assert public_address.services_base(app.settings) == ADDRESS


@pytest.mark.parametrize("gone", [{"enabled": False, "state": "off"}, {"enabled": True, "state": "failed", "address": ADDRESS}, launcher_bridge.LauncherUnavailable("no")])
async def test_when_it_goes_away_links_go_back_to_what_they_were(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gone: Any) -> None:
    address, app = rig(tmp_path, monkeypatch, {"enabled": True, "state": "up", "address": ADDRESS, "pids": [41]})
    await address.refresh()
    address.answers["view"] = gone  # type: ignore[attr-defined]
    await address.refresh()
    assert public_address.services_base(app.settings) == "http://localhost:8766"
    assert public_address.guest_base(app.settings) == "http://localhost:8764"
    assert not address.own_pids
    assert app.extensions["guest_door"].told == 2


async def test_what_the_operator_named_by_hand_comes_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, {"enabled": True, "state": "up", "address": ADDRESS}, services_public_url="https://mine.example.test")
    await address.refresh()
    assert public_address.services_base(app.settings) == ADDRESS
    address.answers["view"] = {"enabled": False, "state": "off"}  # type: ignore[attr-defined]
    await address.refresh()
    assert public_address.services_base(app.settings) == "https://mine.example.test"


async def test_a_door_that_was_down_is_told_when_it_is_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, {"enabled": True, "state": "up", "address": ADDRESS})
    app.extensions["guest_door"].fail = True
    await address.refresh()
    assert app.extensions["guest_door"].told == 0
    app.extensions["guest_door"].fail = False
    await address.refresh()
    assert app.extensions["guest_door"].told == 1


async def test_an_old_launcher_and_no_launcher_are_said_apart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, _ = rig(tmp_path, monkeypatch, launcher_bridge.LauncherTooOld("older"))
    assert (await address.read())["reason"] == "old_launcher"
    address, _ = rig(tmp_path, monkeypatch, launcher_bridge.LauncherUnavailable("gone"))
    assert (await address.read())["reason"] == "no_launcher"
    monkeypatch.setattr(launcher_bridge, "read", lambda _state: None)
    assert (await address.read())["reason"] == "no_launcher"


async def test_the_bridge_reads_what_the_launcher_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(400, json={"error": {"message": "that does not look like a token"}})
        return httpx.Response(200, json={"state": "off"})

    real = httpx.AsyncClient
    monkeypatch.setattr(launcher_bridge.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    launcher = launcher_bridge.Launcher(port=1, token="the-launcher-token", pid=0)
    assert (await launcher_bridge.tunnel(launcher))["state"] == "off"
    with pytest.raises(launcher_bridge.TunnelRefused, match="token"):
        await launcher_bridge.tunnel(launcher, {"ngrok_token": "x"})
    assert all(r.headers[launcher_bridge.HEADER] == "the-launcher-token" for r in seen)


# -- the app's own tunnel is not a stranger ----------------------------------------------------------


def running(pid: int, kind: str = "cloudflared") -> tunnels.Running:
    return tunnels.Running(pid, kind, f"{kind} tunnel --url http://127.0.0.1:1", 60.0)


def test_the_apps_own_tunnel_is_told_by_pid_and_not_by_its_looks() -> None:
    found = [running(41), running(42), running(99)]
    assert [r.pid for r in tunnels.not_ours(found, {41, 42}) or []] == [99], "the same command line under another pid is a stranger"
    assert tunnels.not_ours(None, {41}) is None, "no look at all stays no look"


async def test_the_watch_keeps_quiet_about_the_apps_tunnel_and_tells_of_another(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, {"enabled": True, "state": "up", "address": ADDRESS, "pids": [41, 42]})
    app.extensions[tunnel_address.EXTENSION_KEY] = address
    app.notifications = None
    monkeypatch.setattr(tunnels, "running", lambda: [running(41), running(42)])
    watch = tunnel_watch.TunnelWatch(app)
    assert await watch.check() == []
    monkeypatch.setattr(tunnels, "running", lambda: [running(41), running(42), running(99, "ngrok")])
    assert [r.pid for r in await watch.check()] == [99]


async def test_the_doctor_says_the_same(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, {"enabled": True, "state": "up", "address": ADDRESS, "pids": [41], "provider": "cloudflare", "confirmed": True})
    await address.refresh()
    monkeypatch.setattr(tunnels, "running", lambda: [running(41)])
    ctx = doctor.DoctorContext(settings=app.settings, config=app.config, extensions={tunnel_address.EXTENSION_KEY: address})
    check = (await doctor._tunnels(ctx))[0]
    assert check.ok and "own" in check.message
    monkeypatch.setattr(tunnels, "running", lambda: [running(41), running(99)])
    check = (await doctor._tunnels(ctx))[0]
    assert not check.ok and "99" in check.message and "41" not in check.message


def test_a_stray_tunneld_counts_as_a_tunnel() -> None:
    from daedalus.host.policy import shell_segments

    assert tunnels.tunnel_in(["tunneld", "serve", "/tmp/x"], shell_segments)
    assert tunnels.tunnel_in(["/opt/app/tunneld", "serve", "/tmp/x"], shell_segments)
    assert not tunnels.tunnel_in(["tunneld", "version"], shell_segments)


@pytest.mark.parametrize(("view", "ok", "words"), [
    ({"enabled": False, "state": "off"}, True, "off"),
    ({"enabled": True, "state": "up", "address": ADDRESS, "provider": "ngrok", "confirmed": True}, True, "answering"),
    ({"enabled": True, "state": "up", "address": ADDRESS, "provider": "ngrok", "confirmed": False}, True, "not proved"),
    ({"enabled": True, "state": "reconnecting", "address": ADDRESS}, True, "reconnecting"),
    ({"enabled": True, "state": "failed", "provider": "ngrok", "code": "token_rejected"}, False, "token_rejected"),
])
async def test_the_doctors_line_on_the_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, view: dict[str, Any], ok: bool, words: str) -> None:
    address, app = rig(tmp_path, monkeypatch, view)
    ctx = doctor.DoctorContext(settings=app.settings, config=app.config, extensions={tunnel_address.EXTENSION_KEY: address})
    check = (await doctor._public_address(ctx))[0]
    assert check.ok is ok and words in check.message


async def test_the_doctor_says_what_stands_around_the_door(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, {"enabled": False, "state": "off"}, guest_run_dir=tmp_path / "run")
    ctx = doctor.DoctorContext(settings=app.settings, config=app.config, extensions={tunnel_address.EXTENSION_KEY: address})
    assert "network of this machine" in (await doctor._public_address(ctx))[1].message
    app.settings.guest_door_wall = "namespaces"
    assert "no network" in (await doctor._public_address(ctx))[1].message


async def test_a_server_has_nothing_to_say_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, {}, native=False)
    assert await doctor._public_address(doctor.DoctorContext(settings=app.settings, config=app.config, extensions={tunnel_address.EXTENSION_KEY: address})) == []
    assert await tunnel_address.install(app) == [], "a server follows no tunnel"
    assert tunnel_address.EXTENSION_KEY not in app.extensions


async def test_a_planted_launcher_file_on_a_server_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent writing ``launcher.json`` beside the state folder rewrote the public addresses, told the door,
    and hid its own tunnel from the watch, because the watch asked with no thought for where it was."""
    evil = {"enabled": True, "state": "up", "address": "https://evil.example.test", "pids": [4242]}
    address, app = rig(tmp_path, monkeypatch, evil, native=False, services_public_url="https://services.example.test", guest_public_url="https://guests.example.test")
    await tunnel_address.install(app)
    app.notifications = None
    monkeypatch.setattr(tunnels, "running", lambda: [running(4242)])
    found = await tunnel_watch.TunnelWatch(app).check()
    assert [r.pid for r in found] == [4242], "the agent's tunnel is still a stranger"
    await address.refresh()  # and should anything ever call this here, it does nothing
    assert public_address.services_base(app.settings) == "https://services.example.test"
    assert public_address.guest_base(app.settings) == "https://guests.example.test"
    assert app.extensions["guest_door"].told == 0 and not address.own_pids


def test_the_handover_file_is_sealed_in_a_container_too(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, telegram_bot_token="123:abc", owner_user_id=1, state_dir=tmp_path / "state", native=False)  # type: ignore[call-arg]
    assert tmp_path / "launcher.json" in settings.sealed_paths


def test_a_clients_own_port_is_sealed_as_the_launcher_states_it(tmp_path: Path) -> None:
    assert launcher_bridge.tunnel_ports(None) == ()
    assert launcher_bridge.tunnel_ports(tmp_path) == (), "no tunnel, no file"
    (tmp_path / "tunnel").mkdir()
    (tmp_path / "tunnel" / "status.json").write_text('{"ports": [40123, "x", 0, 70000]}')
    assert launcher_bridge.tunnel_ports(tmp_path) == (40123,)
    (tmp_path / "tunnel" / "status.json").write_text("not json")
    assert launcher_bridge.tunnel_ports(tmp_path) == ()


async def test_the_doctor_says_when_a_provider_names_no_visitor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, {"enabled": True, "state": "up", "address": ADDRESS, "provider": "tailscale", "confirmed": True, "visitor_unknown": True})
    checks = await doctor._public_address(doctor.DoctorContext(settings=app.settings, config=app.config, extensions={tunnel_address.EXTENSION_KEY: address}))
    assert any("one address" in c.message and not c.ok for c in checks)


# -- the routes ----------------------------------------------------------------------------------------


class Db:
    async def fetchone(self, sql: str, *_: Any) -> dict[str, int]:
        return {"n": 2 if "guest_links" in sql else 1}


def routes(address: TunnelAddress, app: Any) -> TestClient:
    app.db = Db()
    app.extensions[tunnel_address.EXTENSION_KEY] = address
    api = FastAPI()
    api_public_address.register(api, app, lambda: {}, door_config=lambda _: None)
    return TestClient(api)


def test_a_server_is_told_it_has_its_own_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, {}, native=False)
    client = routes(address, app)
    assert client.get("/api/public-address").json() == {"native": False}
    assert client.put("/api/public-address", json={"enabled": True}).status_code == 404


def test_the_settings_read_and_change_it_and_see_what_is_exposed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, {"enabled": False, "state": "off", "providers": {"ngrok": {"token": True}}})
    client = routes(address, app)
    state = client.get("/api/public-address").json()
    assert state["native"] and state["available"] and state["exposed"] == {"links": 2, "services": 1}
    client.put("/api/public-address", json={"enabled": True, "provider": "ngrok", "ngrok_token": "2abc"})
    assert address.answers["changes"] == [{"enabled": True, "provider": "ngrok", "ngrok_token": "2abc", "sign_out": False, "trust_ngrok": False}]  # type: ignore[attr-defined]


def test_the_form_is_told_when_a_command_could_read_a_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, {"enabled": False, "state": "off"})
    client = routes(address, app)
    monkeypatch.setattr(installation_reach, "bwrap_status", lambda: "ok")
    monkeypatch.setattr(installation_reach.sys, "platform", "linux")
    app.config.tools.exec.sandbox = "workspace"
    assert client.get("/api/public-address").json()["tokens_hidden"] is True
    app.config.tools.exec.sandbox = "off"
    assert client.get("/api/public-address").json()["tokens_hidden"] is False
    app.config.tools.exec.sandbox = "workspace"
    monkeypatch.setattr(installation_reach.sys, "platform", "darwin")
    assert client.get("/api/public-address").json()["tokens_hidden"] is False


def test_a_refused_change_is_said_in_the_launchers_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    address, app = rig(tmp_path, monkeypatch, launcher_bridge.TunnelRefused("that does not look like a token"))
    answer = routes(address, app).put("/api/public-address", json={"ngrok_token": "x y"})
    assert answer.status_code == 400 and "token" in answer.json()["detail"]
