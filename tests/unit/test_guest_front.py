"""The process that holds the door's published port, tested over real loopback and unix sockets."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from guestd.front import Front, Refused, parse_trusted, rewrite


@asynccontextmanager
async def stack(tmp_path: Path, **options: object) -> AsyncIterator[tuple[int, list[bytes]]]:
    """A front on a free loopback port, in front of a door that notes what it was sent."""
    seen: list[bytes] = []

    async def door(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = next((int(line.split(b":")[1]) for line in head.lower().split(b"\r\n") if line.startswith(b"content-length:")), 0)
        seen.append(head + (await reader.readexactly(length) if length else b""))
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\n{}")
        await writer.drain()
        writer.close()

    path = str(tmp_path / "public.sock")
    inner = await asyncio.start_unix_server(door, path)
    front = Front(path, **options)  # type: ignore[arg-type]
    outer = await asyncio.start_server(front.handle, "127.0.0.1", 0, limit=16 * 1024)
    try:
        yield outer.sockets[0].getsockname()[1], seen
    finally:
        outer.close()
        inner.close()


async def ask(port: int, raw: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return answer


async def test_half_open_sockets_from_one_address_do_not_close_the_door_to_everyone(tmp_path: Path) -> None:
    async with stack(tmp_path, per_address=4, trusted="127.0.0.1", overall=100, head_seconds=0.3) as (port, seen):
        held = []
        for _ in range(64):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /robots.txt HTTP/1.1\r\nHost: x\r\nX-Slow: ")
            await writer.drain()
            held.append(writer)
        await asyncio.sleep(0.5)  # the head timeout has dropped every one of them
        answer = await ask(port, b"GET /a/x HTTP/1.1\r\nHost: x\r\nX-Forwarded-For: 198.51.100.9\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 200"), answer[:60]
        for writer in held:
            writer.close()


async def test_one_address_may_hold_only_so_many_connections(tmp_path: Path) -> None:
    async with stack(tmp_path, per_address=3) as (port, _):
        held = [await asyncio.open_connection("127.0.0.1", port) for _ in range(3)]
        await asyncio.sleep(0.05)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)  # refused before it says a word
        answer = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        assert answer.startswith(b"HTTP/1.1 429") and b"retry-after" in answer.lower()
        for _, writer in held:
            writer.close()


async def test_a_head_that_never_finishes_is_dropped(tmp_path: Path) -> None:
    async with stack(tmp_path, head_seconds=0.1) as (port, seen):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: x")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 2)).startswith(b"HTTP/1.1 408")
        assert seen == []
        writer.close()


async def test_who_connected_is_the_fronts_word_and_never_the_visitors(tmp_path: Path) -> None:
    async with stack(tmp_path) as (port, seen):
        await ask(port, b"GET /a/x HTTP/1.1\r\nHost: x\r\nX-Guest-Client: 203.0.113.9\r\nx_guest_client: 203.0.113.9\r\nX-Forwarded-For: 203.0.113.9\r\n\r\n")
        head = seen[0].decode().lower()
        assert head.count("x-guest-client") == 1 and "x-guest-client: 127.0.0.1" in head and "203.0.113.9" not in head and "x_guest_client" not in head


async def test_only_a_trusted_peer_may_name_a_visitor_and_only_its_own_entry_counts(tmp_path: Path) -> None:
    async with stack(tmp_path, trusted="127.0.0.1/32") as (port, seen):
        await ask(port, b"GET /a/x HTTP/1.1\r\nHost: x\r\nX-Forwarded-For: 203.0.113.9, 198.51.100.7\r\n\r\n")
        assert "x-guest-client: 198.51.100.7" in seen[0].decode().lower()


@pytest.mark.parametrize("value", ["*", "0.0.0.0/0", "::/0", "192.0.2.1, *", "nonsense"])
def test_a_trust_of_everyone_is_refused_at_start(value: str) -> None:
    with pytest.raises(ValueError, match="GUEST_TRUSTED_PROXIES"):
        parse_trusted(value)


@pytest.mark.parametrize("head", [
    b"POST /a/messages HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n",
    b"POST /a/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\n",
    b"POST /a/messages HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n",
    b"GET /a/x HTTP/1.1\r\nHost: x\r\n folded: line\r\n\r\n",
    b"GET /a/x HTTP/1.1\r\nHost : x\r\n\r\n",
    b"GET /a/x HTTP/1.1\r\nHost: x\nInjected: y\r\n\r\n",
    b"TRACE /a/x HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET a/x HTTP/1.1\r\nHost: x\r\n\r\n",
])
def test_a_request_that_could_be_read_two_ways_is_refused(head: bytes) -> None:
    with pytest.raises(Refused):
        rewrite(head, "192.0.2.1", ())


async def test_a_body_is_passed_by_its_length_and_nothing_after_it_is(tmp_path: Path) -> None:
    async with stack(tmp_path) as (port, seen):
        await ask(port, b"POST /a/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}GET /control/health HTTP/1.1\r\n\r\n")
        assert seen[0].endswith(b"{}") and b"control" not in seen[0]


async def test_a_body_too_long_is_refused_before_it_is_read(tmp_path: Path) -> None:
    async with stack(tmp_path) as (port, seen):
        answer = await ask(port, b"POST /a/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 99999999\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 413") and seen == []


async def test_its_own_answers_are_json_with_nosniff_and_a_closed_door_says_so(tmp_path: Path) -> None:
    front = Front(str(tmp_path / "absent.sock"))
    outer = await asyncio.start_server(front.handle, "127.0.0.1", 0)
    answer = await ask(outer.sockets[0].getsockname()[1], b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    outer.close()
    head, _, body = answer.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 502") and b"application/json" in head and b"nosniff" in head and json.loads(body)["status"] == "refused"


@pytest.mark.parametrize("name", ["X_Forwarded_For", "x_forwarded_for", "X-Forwarded_For", "X_Forwarded-For"])
def test_an_underscored_forwarded_header_names_nobody(name: str) -> None:
    """Go writes headers sorted, so ``X_`` follows ``X-``: the forged line came last and won."""
    head = f"GET /a/x HTTP/1.1\r\nHost: a\r\nX-Forwarded-For: 203.0.113.9\r\n{name}: 198.51.100.99\r\n\r\n".encode()
    out, client, _ = rewrite(head, "127.0.0.1", parse_trusted("127.0.0.1"))
    assert client == "203.0.113.9"
    assert b"198.51.100.99" not in out


def test_an_underscored_header_alone_leaves_the_peer() -> None:
    head = b"GET /a/x HTTP/1.1\r\nHost: a\r\nX_Forwarded_For: 198.51.100.99\r\n\r\n"
    assert rewrite(head, "127.0.0.1", parse_trusted("127.0.0.1"))[1] == "127.0.0.1"


async def test_a_door_cannot_steer_the_front_with_a_file_in_unix_form(tmp_path: Path) -> None:
    """The folder is the door's to write. In the socket form a file there used to win, and a stranger's
    request went to whatever daemon on this machine the door named."""
    other: list[bytes] = []

    async def daemon(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        other.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 6\r\nconnection: close\r\n\r\nSECRET")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(daemon, "127.0.0.1", 0)
    (tmp_path / "public.endpoint").write_text(f"tcp:127.0.0.1:{server.sockets[0].getsockname()[1]}\n")
    async with stack(tmp_path, trusted="127.0.0.1") as (port, seen):
        answer = await ask(port, b"GET /a/x HTTP/1.1\r\nHost: a\r\nAuthorization: Bearer x\r\n\r\n")
    server.close()
    assert b"SECRET" not in answer and not other and len(seen) == 1
