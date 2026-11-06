"""``python -m guestd``: the two sockets, the stand-in and the clock, in one process with no network.

Nothing here listens on a network. Strangers arrive through ``front.py`` over one unix socket, the host
dials the other, and the key proxy's guest listener is a third. The container runs with no network
at all, so what this process could reach if it were taken over is those three sockets.
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
from collections.abc import Iterator
from pathlib import Path

import uvicorn

from .control import ControlDoor
from .mailbox import Mailbox
from .public import PublicDoor
from .standin import KeyProxyCaller, StandIn
from .store import Store

logger = logging.getLogger("guestd")

SWEEP_SECONDS = 60.0
CONTROL_SOCKET = "control.sock"
PUBLIC_SOCKET = "public.sock"
TOKEN_NAME = "control.token"
TOKEN_MAX_BYTES = 256
TOKEN_POLL_SECONDS = 1.0


class Server(uvicorn.Server):
    """A server that leaves the process's signals alone: two of them run here, and one handler stops both."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def public_config(app: PublicDoor) -> uvicorn.Config:
    """The access log stays off: every invite is in a path."""
    return uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off", proxy_headers=False, forwarded_allow_ips="")


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


def listener(path: Path, mode: int) -> socket.socket:
    path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    os.chmod(path, mode)
    sock.listen(64)
    return sock


async def sweep(store: Store, mailbox: Mailbox) -> None:
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            store.sweep()
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
    mailbox = Mailbox(store, public_base=lambda: store.setting("public_base"))
    stand_in = StandIn(store, mailbox, KeyProxyCaller(os.environ.get("GUEST_MODEL_SOCKET", "")))
    stand_in.recover()
    public = Server(public_config(PublicDoor(mailbox)))
    control = Server(uvicorn.Config(ControlDoor(store, mailbox, token), log_level="warning", access_log=False, lifespan="off"))
    loop = asyncio.get_running_loop()

    def stop() -> None:
        public.should_exit = True
        control.should_exit = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop)
    background = [asyncio.create_task(stand_in.serve(), name="stand-in"), asyncio.create_task(sweep(store, mailbox), name="sweep")]
    try:
        # The socket the front reaches is open to anyone who can see it, and only the front can: no other
        # container mounts its volume. The control socket needs the host's token as well.
        await asyncio.gather(public.serve(sockets=[listener(front_dir / PUBLIC_SOCKET, 0o666)]), control.serve(sockets=[listener(run_dir / CONTROL_SOCKET, 0o600)]))
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        store.close()
        for path in (run_dir / CONTROL_SOCKET, front_dir / PUBLIC_SOCKET):
            with contextlib.suppress(OSError):
                path.unlink()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
