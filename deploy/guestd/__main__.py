"""``python -m guestd``: the two sockets, the stand-in and the clock, in one process with no network.

Nothing here listens on a network. Strangers arrive through ``front.py`` over one unix socket, the host
dials the other, and the key proxy's guest listener is a third. The container runs with no network
at all, so what this process could reach if it were taken over is those three sockets.

Natively the launcher starts this with the same three, and on a system with no unix sockets in Python
(Windows), or a folder too deep for one, ``GUEST_LISTEN=tcp`` puts them on loopback ports the system
hands out, each named in an ``<name>.endpoint`` file. The control port still needs the host's token. The
public port trusts the front's word on who connected, which on loopback any program of this user could
imitate, and all that could buy it is another visitor's share of a rate limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import uvicorn

from .confine import confine, is_loopback
from .control import ControlDoor
from .files import Bodies, Desk
from .mailbox import Mailbox
from .public import PublicDoor
from .standin import KeyProxyCaller, StandIn
from .store import Store

logger = logging.getLogger("guestd")

SWEEP_SECONDS = 60.0
CONTROL = "control"
PUBLIC = "public"
TOKEN_NAME = "control.token"
TOKEN_MAX_BYTES = 256
TOKEN_POLL_SECONDS = 1.0


class Server(uvicorn.Server):
    """A server that leaves the process's signals alone: two of them run here, and one handler stops both."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


GRACE_SECONDS = 2
"""How long a stop waits for requests in hand. Without a bound a held read or the host's feed keeps this
process alive for half a minute after it was told to end, and natively a launcher that starts again in that
time finds this one still here, removing the new door's sockets as it leaves."""


def public_config(app: PublicDoor) -> uvicorn.Config:
    """The access log stays off: every invite is in a path."""
    return uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off", proxy_headers=False, forwarded_allow_ips="", timeout_graceful_shutdown=GRACE_SECONDS)


def host_token(token_dir: Path, *, wait: bool = True) -> str:
    """The token the host wrote. This process only reads it, and may start before the host has."""
    path = token_dir / TOKEN_NAME
    while True:
        with contextlib.suppress(FileNotFoundError):
            token = path.read_bytes()[:TOKEN_MAX_BYTES].decode("ascii", "replace").strip()
            if token:
                return token
        if not wait:
            raise FileNotFoundError(path)
        time.sleep(TOKEN_POLL_SECONDS)


def listener(directory: Path, name: str, mode: int) -> socket.socket:
    """This door's socket called ``name``, in the form this installation uses."""
    path = directory / f"{name}.sock"
    endpoint = directory / f"{name}.endpoint"
    path.unlink(missing_ok=True)
    endpoint.unlink(missing_ok=True)
    if os.environ.get("GUEST_LISTEN") == "tcp":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(64)
        # Written after listening, and by rename, so a reader finds no address that does not answer yet.
        scratch = endpoint.with_suffix(".tmp")
        scratch.write_text(f"tcp:127.0.0.1:{sock.getsockname()[1]}\n")
        scratch.replace(endpoint)
        return sock
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    os.chmod(path, mode)
    sock.listen(64)
    return sock


def on_stop(loop: asyncio.AbstractEventLoop, stop: Callable[[], None]) -> None:
    """Ask for ``stop`` when this process is told to end. Windows has no ``add_signal_handler``, and what
    the launcher sends there, by console event, is ``SIGBREAK``."""
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop)
    except NotImplementedError:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGBREAK):
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop))


def may_dial(socket_path: str, endpoint: str) -> Callable[[Any], bool]:
    """What this process has reason to connect to: the key proxy's guest listener and nothing else."""

    def allowed(address: Any) -> bool:
        if isinstance(address, str | bytes):
            return address == socket_path or address == os.fsencode(socket_path)
        if not is_loopback(address) or not endpoint:
            return False
        with contextlib.suppress(OSError):
            return Path(endpoint).read_text().strip().endswith(f":{address[1]}")
        return False

    return allowed


async def sweep(store: Store, mailbox: Mailbox) -> None:
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            store.sweep()
            # After the links the retention deleted: their files' bodies are what is left of them on disk.
            mailbox.desk.forget(store.clock())
        except Exception:
            logger.exception("the sweep failed")
        mailbox.notify()


async def main() -> None:
    state_dir = Path(os.environ.get("GUEST_STATE_DIR", "/var/lib/guestd"))
    run_dir = Path(os.environ.get("GUEST_RUN_DIR", "/run/daedalus-guest"))
    front_dir = Path(os.environ.get("GUEST_FRONT_DIR", "/run/daedalus-guest-front"))
    token_dir = Path(os.environ.get("GUEST_TOKEN_DIR", "/run/daedalus-guest-token"))
    for directory in (state_dir, run_dir, front_dir):
        directory.mkdir(parents=True, exist_ok=True)
    token = await asyncio.to_thread(host_token, token_dir)
    store = Store(state_dir / "guest.sqlite")
    mailbox = Mailbox(store, public_base=lambda: store.setting("public_base"), desk=Desk(store, Bodies(state_dir / "files")))
    model_socket = os.environ.get("GUEST_MODEL_SOCKET", "")
    model_endpoint = os.environ.get("GUEST_MODEL_ENDPOINT", "")
    stand_in = StandIn(store, mailbox, KeyProxyCaller(model_socket, endpoint=model_endpoint))
    stand_in.recover()
    public = Server(public_config(PublicDoor(mailbox)))
    control = Server(uvicorn.Config(ControlDoor(store, mailbox, token), log_level="warning", access_log=False, lifespan="off", timeout_graceful_shutdown=GRACE_SECONDS))
    loop = asyncio.get_running_loop()

    def stop() -> None:
        public.should_exit = True
        control.should_exit = True

    on_stop(loop, stop)
    if os.environ.get("GUEST_CONFINE"):
        confine(may_dial(model_socket, model_endpoint))
    background = [asyncio.create_task(stand_in.serve(), name="stand-in"), asyncio.create_task(sweep(store, mailbox), name="sweep")]
    try:
        # The socket the front reaches is open to anyone who can see it, and only the front can: no other
        # container mounts its volume. The control socket needs the host's token as well.
        await asyncio.gather(public.serve(sockets=[listener(front_dir, PUBLIC, 0o666)]), control.serve(sockets=[listener(run_dir, CONTROL, 0o600)]))
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        store.close()
        for path in (run_dir / f"{CONTROL}.sock", run_dir / f"{CONTROL}.endpoint", front_dir / f"{PUBLIC}.sock", front_dir / f"{PUBLIC}.endpoint"):
            with contextlib.suppress(OSError):
                path.unlink()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
