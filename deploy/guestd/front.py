"""``python -m guestd.front``: the small process that holds the published port.

The door has no network, so something has to stand on one. This is that something, and it is kept too
simple to be interesting: it reads one request's head, answers nothing by itself but its own refusals,
and passes the request down one unix socket. It holds no state, no key and no token.

It is also where a stranger's connection is bounded, because this is what they connect to: a head must
arrive within seconds, an address may hold only so many connections, and a connection has a whole
life of less than a minute. Half-open sockets from one address used to give everyone else a 503.

It alone says who connected. Whatever a client sent under that name is dropped, and the door believes
the header because nothing but this process can reach the door's socket. ``X-Forwarded-For`` counts
only when it came from a peer named in ``GUEST_TRUSTED_PROXIES``. A wildcard there would let any
visitor name their own address to the limits, so it is refused at start.

Only the first request of a connection is read, a body is passed by its ``Content-Length`` and nothing
else, and ``Transfer-Encoding`` is refused: there is one way to tell where a request ends, so a second
one cannot be smuggled past this process.

Natively this stands on loopback, and what arrives there from outside comes through the local router of
a tunnel (``tunneld``). That router is the peer ``GUEST_TRUSTED_PROXIES`` names there, and it writes the
visitor's address itself, whatever the visitor sent. With ``GUEST_LISTEN=tcp`` the door's socket is a loopback
port and ``public.endpoint`` beside the socket's name says which. Only then: that folder is the door's to
write, and a door taken over behind its wall must not be able to send strangers to any daemon on this machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from .confine import confine, is_loopback

logger = logging.getLogger("guestd.front")

HEAD_SECONDS = 10.0
LIFE_SECONDS = 45.0
"""A read holds 30 s at most, so this is a slow request's whole life, not an idle timeout to be reset."""
MAX_HEAD = 16 * 1024
MAX_BODY = 64 * 1024
MAX_PIECE_BODY = 512 * 1024 + 1024
"""A piece of a file (``PUT /a/files/<id>``), the one request with a larger body: the door's own bound with room
for nothing more. Passed down as it arrives, never held whole, so a few hundred at once stay small."""
PIECE_PATH = re.compile(r"/a/files/f_[0-9a-f]{16}(\?offset=[0-9]{1,15})?")
PASS_BYTES = 64 * 1024
PER_ADDRESS = 8
OVERALL = 256
CLIENT_HEADER = "x-guest-client"
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-connection", "proxy-authorization", "proxy-authenticate", "te", "trailer", "upgrade",
    "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip", CLIENT_HEADER,
})
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "OPTIONS"})


class Refused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def parse_trusted(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """The peers allowed to name a visitor. A list of addresses or networks, and never everyone."""
    networks = []
    for item in (part.strip() for part in value.split(",")):
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise ValueError(f"GUEST_TRUSTED_PROXIES: {item!r} is not an address or a network") from exc
        if network.prefixlen == 0:
            raise ValueError(f"GUEST_TRUSTED_PROXIES: {item!r} trusts everyone, and any visitor could then name their own address")
        networks.append(network)
    return tuple(networks)


def is_trusted(peer: str, trusted: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None) or address
    return any(mapped.version == network.version and mapped in network for network in trusted)


def rewrite(head: bytes, peer: str, trusted: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]) -> tuple[bytes, str, int]:
    """The head to send down, the visitor's address, and how many body bytes follow. Raises ``Refused``."""
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError as exc:  # latin-1 cannot fail; kept so the shape of the refusal is one
        raise Refused(400, "unreadable request") from exc
    lines = text[:-4].split("\r\n")
    request = lines[0].split(" ")
    if len(request) != 3 or request[0] not in METHODS or request[2] not in ("HTTP/1.1", "HTTP/1.0") or not request[1].startswith("/") or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in request[1]):
        raise Refused(400, "unreadable request")
    kept: list[str] = []
    forwarded = ""
    length: int | None = None
    for line in lines[1:]:
        # A folded line, a bare CR or LF, or a name with a space are how one request is read as two.
        name, colon, value = line.partition(":")
        if not colon or not name or line[0] in " \t" or any(ch in line for ch in "\r\n\0") or name != name.strip():
            raise Refused(400, "unreadable request")
        lower = name.lower().replace("_", "-")
        if lower == "transfer-encoding":
            raise Refused(400, "send a Content-Length")
        # The exact name alone: a proxy writes that one. ``X_Forwarded_For`` folds to the same word, and a
        # visitor's sorted after the real line, so it won and named any address it liked.
        if name.lower() == "x-forwarded-for":
            forwarded = value
        if lower in HOP_BY_HOP:
            continue
        if lower == "content-length":
            if length is not None or not value.strip().isdigit():
                raise Refused(400, "unreadable request")
            length = int(value.strip())
        kept.append(f"{name}:{value}")
    bound = MAX_PIECE_BODY if request[0] == "PUT" and PIECE_PATH.fullmatch(request[1]) else MAX_BODY
    if (length or 0) > bound:
        raise Refused(413, "too long")
    client = peer
    if is_trusted(peer, trusted) and forwarded:
        # The last entry is the one the trusted proxy wrote; anything before it the visitor did.
        named = forwarded.rsplit(",", 1)[-1].strip()
        with contextlib.suppress(ValueError):
            client = str(ipaddress.ip_address(named))
    out = "\r\n".join([lines[0], f"{CLIENT_HEADER}: {client}", "connection: close", *kept, "", ""])
    return out.encode("latin-1"), client, length or 0


def refusal(status: int, detail: str, retry_after: int | None = None) -> bytes:
    body = json.dumps({"status": "refused", "detail": detail}).encode()
    reason = {400: "Bad Request", 408: "Request Timeout", 413: "Content Too Large", 429: "Too Many Requests", 502: "Bad Gateway"}.get(status, "Error")
    headers = [
        f"HTTP/1.1 {status} {reason}", "content-type: application/json", f"content-length: {len(body)}", "x-content-type-options: nosniff", "cache-control: no-store",
        "connection: close",
    ]
    if retry_after is not None:
        headers.append(f"retry-after: {retry_after}")
    return ("\r\n".join(headers) + "\r\n\r\n").encode() + body


def door_port(socket_path: str) -> int | None:
    """The door's port when it listens on one: named in a file beside its socket, read each time
    because a door that restarts is handed another."""
    with contextlib.suppress(OSError, ValueError):
        return int(Path(socket_path).with_suffix(".endpoint").read_text().strip().rsplit(":", 1)[1])
    return None


async def dial_door(socket_path: str, *, tcp: bool) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    port = door_port(socket_path) if tcp else None
    if tcp and port is None:
        raise OSError("the door has not said where it is yet")
    return await (asyncio.open_connection("127.0.0.1", port) if tcp else asyncio.open_unix_connection(socket_path))


class Front:
    def __init__(self, socket_path: str, trusted: str = "", *, tcp: bool = False, per_address: int = PER_ADDRESS, overall: int = OVERALL, head_seconds: float = HEAD_SECONDS, life_seconds: float = LIFE_SECONDS) -> None:
        self.socket_path = socket_path
        self.tcp = tcp
        self.trusted = parse_trusted(trusted)
        self.per_address = per_address
        self.overall = overall
        self.head_seconds = head_seconds
        self.life_seconds = life_seconds
        self.open: Counter[str] = Counter()
        self.total = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = str((writer.get_extra_info("peername") or ("",))[0])
        # A trusted proxy holds many visitors' connections on one address; each of them is counted once
        # the head has said whose it is. Anyone else is counted before they have sent a byte.
        direct = not is_trusted(peer, self.trusted)
        counted = peer if direct else ""
        self.total += 1
        if counted:
            self.open[counted] += 1
        try:
            if self.total > self.overall or (counted and self.open[counted] > self.per_address):
                writer.write(refusal(429, "Too many connections.", 10))
                await writer.drain()
                return
            async with asyncio.timeout(self.life_seconds):
                try:
                    async with asyncio.timeout(self.head_seconds):
                        head = await reader.readuntil(b"\r\n\r\n")
                except TimeoutError:
                    writer.write(refusal(408, "The request took too long to arrive."))
                    await writer.drain()
                    return
                except asyncio.LimitOverrunError:
                    writer.write(refusal(400, "The request head is too long."))
                    await writer.drain()
                    return
                except asyncio.IncompleteReadError:
                    return
                new_head, client, length = rewrite(head, peer, self.trusted)
                if not counted:
                    counted = client
                    self.open[counted] += 1
                    if self.open[counted] > self.per_address:
                        writer.write(refusal(429, "Too many connections.", 10))
                        await writer.drain()
                        return
                await self.pass_on(reader, writer, new_head, length)
        except Refused as exc:
            writer.write(refusal(exc.status, exc.detail))
            with contextlib.suppress(OSError):
                await writer.drain()
        except (TimeoutError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self.total -= 1
            if counted:
                self.open[counted] -= 1
                if self.open[counted] <= 0:
                    del self.open[counted]
            writer.close()
            with contextlib.suppress(OSError, asyncio.CancelledError):
                await writer.wait_closed()

    async def pass_on(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, head: bytes, length: int) -> None:
        try:
            door_reader, door_writer = await dial_door(self.socket_path, tcp=self.tcp)
        except OSError:
            writer.write(refusal(502, "This door is closed for the moment. Try again in a minute.", 60))
            await writer.drain()
            return
        try:
            door_writer.write(head)
            left = length
            while left:
                piece = await reader.readexactly(min(left, PASS_BYTES))
                left -= len(piece)
                door_writer.write(piece)
                await door_writer.drain()
            await door_writer.drain()
            while chunk := await door_reader.read(65536):
                writer.write(chunk)
                await writer.drain()
        finally:
            door_writer.close()
            with contextlib.suppress(OSError):
                await door_writer.wait_closed()


async def main() -> None:
    front = Front(
        os.path.join(os.environ.get("GUEST_FRONT_DIR", "/run/daedalus-guest-front"), "public.sock"), os.environ.get("GUEST_TRUSTED_PROXIES", ""),
        tcp=os.environ.get("GUEST_LISTEN") == "tcp",
    )
    server = await asyncio.start_server(front.handle, os.environ.get("GUEST_HOST", "0.0.0.0"), int(os.environ.get("GUEST_DOOR_PORT", "8764")), limit=MAX_HEAD)
    if os.environ.get("GUEST_CONFINE"):
        confine(lambda address: address == front.socket_path or (front.tcp and is_loopback(address) and address[1] == door_port(front.socket_path)))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(main())
    except ValueError as exc:
        sys.exit(str(exc))
    except KeyboardInterrupt:
        sys.exit(0)
