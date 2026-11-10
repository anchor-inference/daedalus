"""The GPU reaches the operator's commands and never a guest's.

A sandboxed command starts with a fresh ``/dev``, so a render node the container was given is missing from it unless
the sandbox binds it back; without it Chromium in a command drew WebGL on the processor. A guest's sandbox is built
from an empty root and must stay without the device, whatever the container holds.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.host import prompts
from daedalus.tools import shell

BWRAP = "/usr/bin/bwrap"


@pytest.fixture
def dri(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``/dev/dri`` in miniature: a render node and a display node, both character devices (links to
    ``/dev/null``, which is one), and a plain file that only has a render node's name."""
    folder = tmp_path / "dri"
    folder.mkdir()
    (folder / "renderD128").symlink_to("/dev/null")
    (folder / "card0").symlink_to("/dev/null")
    (folder / "renderD129").write_text("not a device")
    monkeypatch.setattr(shell, "DRI_DIR", folder)
    return folder


def triples(argv: list[str]) -> list[list[str]]:
    return [argv[i:i + 3] for i in range(len(argv))]


def test_only_render_nodes_that_are_devices_are_bound(dri: Path) -> None:
    assert shell.render_nodes() == [dri / "renderD128"]
    assert shell.gpu_binds() == ["--dev-bind", str(dri / "renderD128"), str(dri / "renderD128")]


def test_no_gpu_binds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bind of a node that is not there would make bubblewrap refuse every command."""
    monkeypatch.setattr(shell, "DRI_DIR", tmp_path / "absent")
    assert shell.gpu_binds() == []


def _operator_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell, "is_guest_turn", lambda session: False)
    monkeypatch.setattr(shell, "bwrap_status", lambda: "ok")
    monkeypatch.setattr(shell, "bwrap_path", lambda: BWRAP)
    monkeypatch.setattr(shell.operator_secrets, "shared", lambda: None)
    for name in ("STATE_DIR", "DAEDALUS_STATE", "DAEDALUS_LOCAL", "DAEDALUS_SECRETS", "DAEDALUS_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)


def test_an_operators_command_gets_the_render_node_after_the_fresh_dev(dri: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _operator_sandbox(monkeypatch)
    config = SimpleNamespace(sandbox="workspace", sandbox_extra_writable=[])
    argv, sandboxed = asyncio.run(shell.sandbox_argv("true", config, writable=[tmp_path], sealed=[], session_id="chat-1"))
    node = str(dri / "renderD128")
    assert sandboxed and ["--dev-bind", node, node] in triples(argv)
    assert argv.index("--dev-bind") > argv.index("--dev"), "bound before the fresh /dev, the node would be covered by it"
    assert str(dri / "card0") not in argv and str(dri / "renderD129") not in argv


def test_a_guests_command_never_gets_the_gpu(dri: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shell, "is_guest_turn", lambda session: True)
    monkeypatch.setattr(shell, "guest_sandbox_status", lambda: "ok")
    monkeypatch.setattr(shell, "guest_limits_status", lambda: "ok")
    monkeypatch.setattr(shell, "bwrap_path", lambda: BWRAP)
    argv, sandboxed = asyncio.run(shell.sandbox_argv("true", SimpleNamespace(sandbox="workspace"), writable=[tmp_path], session_id="guest-1"))
    assert sandboxed
    assert "--dev-bind" not in argv and not any(str(dri) in arg for arg in argv)
    assert "--dev-bind" not in shell.guest_sandbox_argv(BWRAP, writable=[tmp_path])


def test_the_chromium_switch_reaches_the_operators_commands_and_not_a_guests(dri: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(shell.CHROMIUM_GPU_ENV, "1")
    monkeypatch.setattr(shell, "is_guest_turn", lambda session: session == "guest-1")
    monkeypatch.setattr(shell.operator_secrets, "shared", lambda: None)
    assert shell.shell_environment("chat-1").get(shell.CHROMIUM_GPU_ENV) == "1"
    assert shell.CHROMIUM_GPU_ENV not in shell.shell_environment("guest-1")


def test_chromium_draws_on_the_gpu_only_when_asked_and_a_node_is_there(dri: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(shell.CHROMIUM_GPU_ENV, raising=False)
    assert not shell.chromium_gpu()
    monkeypatch.setenv(shell.CHROMIUM_GPU_ENV, "1")
    assert shell.chromium_gpu()
    monkeypatch.setattr(shell, "DRI_DIR", tmp_path / "absent")
    assert not shell.chromium_gpu()


def test_the_prompt_says_there_is_a_gpu_only_when_there_is_one() -> None:
    def section(gpu: bool) -> str:
        return prompts.environment_section(workspace=Path("/w"), bot_repo=Path("/b"), core_repo=Path("/c"), model="m", gpu=gpu)

    assert "has a GPU" not in section(False)
    said = section(True)
    assert "has a GPU" in said and "SwiftShader" in said and "webapp-testing" in said


def test_the_wrapper_adds_the_switches_only_when_asked() -> None:
    """The script stands where the image's Chromium was; the browser service runs the same binaries without the
    variable, and its browserd must keep the say over how its browser draws."""
    script = (Path(__file__).resolve().parents[2] / "deploy" / "chromium-gpu.sh").read_text()
    assert 'if [ "${DAEDALUS_CHROMIUM_GPU:-}" = 1 ]' in script
    assert 'exec "$real" --use-angle=vulkan --ignore-gpu-blocklist "$@"' in script, "a script's own switches come last and win"
    assert script.rstrip().endswith('exec "$real" "$@"')
