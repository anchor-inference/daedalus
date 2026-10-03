"""A pinned SSH transport for remote probes and content-addressed artifacts.

The client's own SSH configuration is ignored: only an operator-approved host key and an explicit
identity file may authenticate a remote host. A failed handshake never authorizes a retrying write.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shlex
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RemoteUnavailable(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RemoteHost:
    host: str
    port: int
    user: str
    public_key: str
    root: str


@dataclass(frozen=True, slots=True)
class Probe:
    state: str
    capabilities: dict[str, Any]
    candidate_public_key: str | None = None


def ssh_key_blob(public_key: str) -> bytes:
    raw = base64.b64decode(public_key, validate=True)
    if len(raw) != 32:
        raise ValueError("an Ed25519 public key is required")
    return struct.pack("!I", 11) + b"ssh-ed25519" + struct.pack("!I", 32) + raw


def raw_ssh_key(blob: bytes) -> str:
    if len(blob) != 51 or blob[:4] != struct.pack("!I", 11) or blob[4:15] != b"ssh-ed25519" or blob[15:19] != struct.pack("!I", 32):
        raise ValueError("not an Ed25519 SSH host key")
    return base64.b64encode(blob[19:]).decode()


PROBE_SCRIPT = """
import json, os, sys
root = sys.argv[1]
print(json.dumps({'protocol': 'daedalus-ssh-v1', 'root_exists': os.path.isdir(root),
                  'root_writable': os.path.isdir(root) and os.access(root, os.W_OK)}))
""".strip()


class PinnedSsh:
    def __init__(self, identity_file: str | None, *, ssh: str = "ssh", keyscan: str = "ssh-keyscan") -> None:
        self.identity_file = identity_file
        self.ssh = ssh
        self.keyscan = keyscan

    @staticmethod
    def _known_hosts(host: RemoteHost) -> str:
        label = host.host if host.port == 22 else f"[{host.host}]:{host.port}"
        return f"{label} ssh-ed25519 {base64.b64encode(ssh_key_blob(host.public_key)).decode()}\n"

    async def run(self, host: RemoteHost, script: str, *args: str, data: bytes = b"", timeout: float = 20) -> bytes:
        identity = Path(self.identity_file) if self.identity_file else None
        if identity is None or not identity.is_file():
            raise RemoteUnavailable("missing_identity")
        with tempfile.TemporaryDirectory(prefix="daedalus-ssh-") as directory:
            known = Path(directory) / "known_hosts"
            known.write_text(self._known_hosts(host))
            known.chmod(0o600)
            command = "python3 -c " + shlex.quote(script) + " " + " ".join(shlex.quote(value) for value in args)
            process = await asyncio.create_subprocess_exec(
                self.ssh, "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "IdentityAgent=none", "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={known}", "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "PasswordAuthentication=no",
                "-o", "KbdInteractiveAuthentication=no", "-o", "ConnectTimeout=5",
                "-o", "LogLevel=ERROR", "-i", str(identity), "-p", str(host.port),
                f"{host.user}@{host.host}", command,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout=timeout)
            except TimeoutError as exc:
                process.kill()
                await process.communicate()
                raise RemoteUnavailable("timeout") from exc
        if process.returncode != 0:
            error = stderr.decode(errors="replace")
            if "HOST IDENTIFICATION HAS CHANGED" in error or "Offending ED25519 key" in error:
                raise RemoteUnavailable("identity_changed")
            if "Permission denied" in error:
                raise RemoteUnavailable("authentication_refused")
            raise RemoteUnavailable("remote_unavailable")
        return stdout

    async def candidate_key(self, host: RemoteHost) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                self.keyscan, "-T", "5", "-p", str(host.port), "-t", "ed25519", host.host,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=8)
        except (OSError, TimeoutError):
            return None
        if process.returncode != 0:
            return None
        for line in stdout.decode(errors="replace").splitlines():
            fields = line.split()
            if len(fields) < 3 or fields[1] != "ssh-ed25519":
                continue
            try:
                return raw_ssh_key(base64.b64decode(fields[2], validate=True))
            except ValueError:
                continue
        return None

    async def probe(self, host: RemoteHost) -> Probe:
        try:
            raw = await self.run(host, PROBE_SCRIPT, host.root)
        except RemoteUnavailable as exc:
            candidate = await self.candidate_key(host) if exc.code == "identity_changed" else None
            return Probe(exc.code, {}, candidate)
        try:
            answer = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Probe("protocol_invalid", {})
        if answer.get("protocol") != "daedalus-ssh-v1" or answer.get("root_exists") is not True:
            return Probe("root_unavailable", {})
        if answer.get("root_writable") is not True:
            return Probe("root_readonly", {})
        return Probe("supported", {"artifact_transfer": True, "remote_python": True})


TRANSFER_SCRIPT = """
import fcntl, hashlib, json, os, pathlib, sys
root, name, expected_digest, expected_size, required_offset = sys.argv[1:6]
expected_size = int(expected_size)
required_offset = int(required_offset)
if not name.isalnum() or not os.path.isdir(root):
    raise SystemExit(2)
base = pathlib.Path(root)
stage = base / ('.' + name + '.stage')
final = base / name
lock = base / ('.' + name + '.lock')
lock_file = lock.open('a+b')
fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
if final.is_file():
    with final.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if digest == expected_digest and final.stat().st_size == expected_size:
        print(json.dumps({'state': 'published', 'digest': digest, 'size': expected_size}))
        raise SystemExit(0)
if stage.exists() and stage.stat().st_size > expected_size:
    stage.unlink()
hash_value = hashlib.sha256()
offset = stage.stat().st_size if stage.exists() else 0
if offset != required_offset:
    raise SystemExit(6)
if offset:
    with stage.open('rb') as existing:
        for part in iter(lambda: existing.read(1048576), b''):
            hash_value.update(part)
with stage.open('ab') as target:
    remaining = expected_size - offset
    while remaining:
        part = sys.stdin.buffer.read(min(1048576, remaining))
        if not part:
            raise SystemExit(3)
        target.write(part)
        hash_value.update(part)
        remaining -= len(part)
    target.flush()
    os.fsync(target.fileno())
actual = hash_value.hexdigest()
if actual != expected_digest:
    stage.unlink(missing_ok=True)
    raise SystemExit(4)
os.replace(stage, final)
with final.open('rb') as published:
    readback = hashlib.file_digest(published, 'sha256').hexdigest()
if readback != expected_digest or final.stat().st_size != expected_size:
    raise SystemExit(5)
print(json.dumps({'state': 'published', 'digest': readback, 'size': expected_size}))
""".strip()


INSPECT_SCRIPT = """
import hashlib, json, os, pathlib, sys
root, name = sys.argv[1:3]
if not name.isalnum() or not os.path.isdir(root):
    raise SystemExit(2)
base = pathlib.Path(root)
result = {}
for kind, path in (('stage', base / ('.' + name + '.stage')), ('published', base / name)):
    if path.is_file():
        with path.open('rb') as source:
            result[kind] = {'size': path.stat().st_size, 'digest': hashlib.file_digest(source, 'sha256').hexdigest()}
print(json.dumps(result))
""".strip()


RESET_SCRIPT = """
import os, pathlib, sys
root, name = sys.argv[1:3]
if not name.isalnum() or not os.path.isdir(root):
    raise SystemExit(2)
(pathlib.Path(root) / ('.' + name + '.stage')).unlink(missing_ok=True)
""".strip()


async def transfer_file(transport: PinnedSsh, host: RemoteHost, source: Path, *, name: str,
                        expected_digest: str, expected_size: int) -> dict[str, Any]:
    """Send bytes to a staging name; the remote side alone atomically publishes verified bytes."""
    if not name.isalnum() or len(name) > 100 or not source.is_file():
        raise ValueError("invalid transfer source or name")
    if source.stat().st_size != expected_size:
        raise ValueError("the source size changed")
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != expected_digest:
        raise ValueError("the source bytes changed")
    previous = await inspect_transfer(transport, host, name)
    published = previous.get("published") or {}
    if published.get("digest") == expected_digest and published.get("size") == expected_size:
        return {"state": "published", "digest": expected_digest, "size": expected_size}
    stage = previous.get("stage") or {}
    offset = int(stage.get("size") or 0)
    if offset > expected_size or offset < 0:
        offset = -1
    if offset:
        with source.open("rb") as stream:
            prefix = stream.read(offset) if offset >= 0 else b""
        if offset < 0 or hashlib.sha256(prefix).hexdigest() != stage.get("digest"):
            await transport.run(host, RESET_SCRIPT, host.root, name)
            offset = 0
    # A lost response is reconciled against the exact remote digest before any second write.
    with source.open("rb") as stream:
        stream.seek(offset)
        data = stream.read()
    raw = await transport.run(host, TRANSFER_SCRIPT, host.root, name, expected_digest, str(expected_size), str(offset),
                              data=data, timeout=max(30, expected_size / (1 << 20) * 10))
    try:
        answer = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RemoteUnavailable("transfer_response_invalid") from exc
    if answer.get("state") != "published" or answer.get("digest") != expected_digest or answer.get("size") != expected_size:
        raise RemoteUnavailable("transfer_digest_mismatch")
    return answer


async def inspect_transfer(transport: PinnedSsh, host: RemoteHost, name: str) -> dict[str, Any]:
    if not name.isalnum() or len(name) > 100:
        raise ValueError("invalid transfer name")
    try:
        answer = json.loads(await transport.run(host, INSPECT_SCRIPT, host.root, name))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RemoteUnavailable("transfer_state_invalid") from exc
    if not isinstance(answer, dict):
        raise RemoteUnavailable("transfer_state_invalid")
    return answer
