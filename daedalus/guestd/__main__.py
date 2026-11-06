"""``python -m guestd``: the two listeners, the stand-in and the clock, in one process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import signal
import socket
import sys
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
SOCKET_NAME = "control.sock"
TOKEN_NAME = "control.token"


class Server(uvicorn.Server):
    """A server that leaves the process's signals alone: two of them run here, and one handler stops both."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def public_config(app: PublicDoor, *, host: str, port: int, trusted_proxies: str) -> uvicorn.Config:
    """The public server's settings. Who may name a visitor's address is spelled out, as on the API.

    uvicorn's defaults believe ``X-Forwarded-For`` from loopback, which would let any visitor behind a
    local tunnel client name their own address to the attempt limits, and would believe nobody behind
    a proxy reached through Docker's gateway, putting every visitor in one bucket. ``forwarded_allow_ips``
    is passed even when off, or uvicorn reads it from the environment. The access log stays off:
    every invite is in a path.
    """
    trusted = trusted_proxies.strip()
    return uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False, lifespan="off", proxy_headers=bool(trusted), forwarded_allow_ips=trusted, limit_concurrency=64)


def control_token(run_dir: Path) -> str:
    """The token the host reads from the shared run directory; made once, readable by no one else."""
    path = run_dir / TOKEN_NAME
    with contextlib.suppress(FileNotFoundError):
        existing = path.read_text().strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(token)
    return token


def control_socket(run_dir: Path) -> socket.socket:
    path = run_dir / SOCKET_NAME
    path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    os.chmod(path, 0o600)
    sock.listen(16)
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
    state_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    store = Store(state_dir / "guest.sqlite")
    mailbox = Mailbox(store, public_base=lambda: store.setting("public_base"))
    stand_in = StandIn(store, mailbox, KeyProxyCaller(os.environ.get("GUEST_MODEL_BASE", "")))
    public = Server(public_config(
        PublicDoor(mailbox), host=os.environ.get("GUEST_HOST", "0.0.0.0"), port=int(os.environ.get("GUEST_DOOR_PORT", "8764")),
        trusted_proxies=os.environ.get("GUEST_TRUSTED_PROXIES", ""),
    ))
    control = Server(uvicorn.Config(ControlDoor(store, mailbox, control_token(run_dir)), log_level="warning", access_log=False, lifespan="off"))
    loop = asyncio.get_running_loop()

    def stop() -> None:
        public.should_exit = True
        control.should_exit = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop)
    background = [asyncio.create_task(stand_in.serve(), name="stand-in"), asyncio.create_task(sweep(store, mailbox), name="sweep")]
    try:
        await asyncio.gather(public.serve(), control.serve(sockets=[control_socket(run_dir)]))
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        store.close()
        with contextlib.suppress(OSError):
            (run_dir / SOCKET_NAME).unlink()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
