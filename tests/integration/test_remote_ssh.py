"""A loopback SSH server proves pinned identity and remote digest publication."""

from __future__ import annotations

import base64
import getpass
import hashlib
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.remote_ssh import PinnedSsh, RemoteHost, RemoteUnavailable, raw_ssh_key, transfer_file
from daedalus.extensions.runtime_hosts import RuntimeHosts
from daedalus.extensions.runtime_transfers import ArtifactTransferEffect, RuntimeTransfers
from daedalus.stores.control import Principal
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore


def _key(path: Path) -> None:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)


@pytest.fixture
def ssh_server(tmp_path: Path):  # type: ignore[no-untyped-def]
    if not all(shutil.which(name) for name in ("sshd", "ssh", "ssh-keygen", "ssh-keyscan")):
        pytest.skip("OpenSSH client and server are required")
    host_key = tmp_path / "hostkey"
    client_key = tmp_path / "clientkey"
    root = tmp_path / "remote"
    root.mkdir()
    _key(host_key)
    _key(client_key)
    authorized = tmp_path / "authorized_keys"
    authorized.write_text(client_key.with_suffix(".pub").read_text())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = tmp_path / "sshd_config"
    config.write_text("\n".join([
        f"Port {port}", "ListenAddress 127.0.0.1", f"HostKey {host_key}",
        f"AuthorizedKeysFile {authorized}", "PasswordAuthentication no", "PubkeyAuthentication yes",
        "UsePAM no", "StrictModes no", "PermitRootLogin no", "LogLevel ERROR",
        f"PidFile {tmp_path / 'sshd.pid'}", "Subsystem sftp internal-sftp", "",
    ]))
    process = subprocess.Popen([shutil.which("sshd"), "-D", "-f", str(config)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError((process.stderr.read() if process.stderr else b"").decode())
            with socket.socket() as probe:
                probe.settimeout(0.05)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.02)
        else:
            raise AssertionError("loopback sshd did not listen")
        blob = base64.b64decode(host_key.with_suffix(".pub").read_text().split()[1])
        host = RemoteHost("127.0.0.1", port, getpass.getuser(), raw_ssh_key(blob), str(root))
        yield PinnedSsh(str(client_key)), host, root
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_pinned_probe_and_atomic_digest_publish(ssh_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    transport, host, root = ssh_server
    probe = await transport.probe(host)
    assert probe.state == "supported"
    assert probe.capabilities["artifact_transfer"] is True
    source = tmp_path / "artifact"
    data = b"result bytes" * 1000
    source.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    first = await transfer_file(transport, host, source, name="artifact01", expected_digest=digest, expected_size=len(data))
    assert first == {"state": "published", "digest": digest, "size": len(data)}
    assert (root / "artifact01").read_bytes() == data
    assert not (root / ".artifact01.stage").exists()
    assert await transfer_file(transport, host, source, name="artifact01", expected_digest=digest, expected_size=len(data)) == first
    (root / "artifact01").write_bytes(b"tampered")
    assert (await transfer_file(transport, host, source, name="artifact01", expected_digest=digest, expected_size=len(data)))["digest"] == digest
    assert (root / "artifact01").read_bytes() == data


@pytest.mark.asyncio
async def test_wrong_pinned_key_never_runs_remote_command(ssh_server) -> None:  # type: ignore[no-untyped-def]
    transport, host, _ = ssh_server
    wrong = RemoteHost(host.host, host.port, host.user, base64.b64encode(b"\x01" * 32).decode(), host.root)
    result = await transport.probe(wrong)
    assert result.state == "identity_changed"
    assert result.candidate_public_key == host.public_key
    with pytest.raises(RemoteUnavailable, match="identity_changed"):
        await transport.run(wrong, "print('should not run')")


@pytest.mark.asyncio
async def test_host_probe_is_tied_to_pinned_ssh_capability(ssh_server, db: Database) -> None:  # type: ignore[no-untyped-def]
    transport, host, _ = ssh_server
    assert db._conn is not None
    hosts = RuntimeHosts(db, transport)
    operator = Principal.operator({"via": "cookie", "user_id": 1})
    enrolled = await hosts.enroll(operator, label="Worker", public_key=host.public_key,
                                  ssh_host=host.host, ssh_port=host.port, ssh_user=host.user, remote_root=host.root,
                                  expected_collection_revision=1, client_operation_id="enroll-loopback")
    result = await hosts.probe(operator, enrolled["host_id"], expected_collection_revision=2,
                               expected_host_revision=1, client_operation_id="probe-loopback")
    assert result["identity_state"] == "verified"
    assert result["artifact_transfer_state"] == "supported"
    assert result["effects_allowed"] is False
    listed = (await hosts.list())["items"][0]
    assert listed["reachability"] == "reachable"
    assert listed["artifact_transfer_state"] == "supported"
    disconnected = RuntimeHosts(db, PinnedSsh(None))
    unavailable = await disconnected.probe(operator, enrolled["host_id"], expected_collection_revision=3,
                                           expected_host_revision=2, client_operation_id="probe-no-client-key")
    assert unavailable["probe_state"] == "missing_identity"
    assert (await disconnected.list())["items"][0]["artifact_transfer_state"] == "unknown"


@pytest.mark.asyncio
async def test_mismatched_ssh_key_requires_operator_rotation_and_new_probe(ssh_server, db: Database) -> None:  # type: ignore[no-untyped-def]
    transport, host, _ = ssh_server
    assert db._conn is not None
    hosts = RuntimeHosts(db, transport)
    operator = Principal.operator({"via": "cookie", "user_id": 1})
    wrong_key = base64.b64encode(b"\x02" * 32).decode()
    enrolled = await hosts.enroll(operator, label="Worker", public_key=wrong_key,
                                  ssh_host=host.host, ssh_port=host.port, ssh_user=host.user, remote_root=host.root,
                                  expected_collection_revision=1, client_operation_id="enroll-wrong")
    changed = await hosts.probe(operator, enrolled["host_id"], expected_collection_revision=2,
                                expected_host_revision=1, client_operation_id="probe-wrong")
    assert changed["identity_state"] == "changed"
    assert changed["artifact_transfer_state"] == "unknown"
    listed = (await hosts.list())["items"][0]
    assert listed["pending_fingerprint"] and listed["effects_allowed"] is False
    approved = await hosts.decide(operator, enrolled["host_id"], decision="accept_rotation", reason="confirmed out of band",
                                  observed_generation=1, expected_host_revision=2,
                                  expected_collection_revision=3, client_operation_id="accept-real-key")
    assert approved["identity_state"] == "rotating"
    assert (await hosts.list())["items"][0]["artifact_transfer_state"] == "unknown"
    reprobed = await hosts.probe(operator, enrolled["host_id"], expected_collection_revision=4,
                                 expected_host_revision=3, client_operation_id="probe-real-key")
    assert reprobed["identity_state"] == "verified"
    assert reprobed["host_generation"] == 2
    assert reprobed["artifact_transfer_state"] == "supported"


@pytest.mark.asyncio
async def test_transfer_receipt_reconciles_remote_digest_after_unknown(ssh_server, db: Database, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    transport, host, remote_root = ssh_server
    assert db._conn is not None
    operator = Principal.operator({"via": "cookie", "user_id": 1})
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Project','now','{}')")
    hosts = RuntimeHosts(db, transport)
    enrolled = await hosts.enroll(operator, label="Worker", public_key=host.public_key,
                                  ssh_host=host.host, ssh_port=host.port, ssh_user=host.user, remote_root=host.root,
                                  expected_collection_revision=2, client_operation_id="transfer-host")
    await hosts.probe(operator, enrolled["host_id"], expected_collection_revision=3,
                      expected_host_revision=1, client_operation_id="transfer-probe")
    source = tmp_path / "source"
    data = b"durable artifact" * 1000
    source.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    await db.execute("INSERT INTO files(id,name,mime,size,sha256,origin,created_at)"
                     " VALUES ('file-one','output.txt','text/plain',?,?,'staff','now')", (len(data), digest))
    await db.execute("INSERT INTO artifact_manifests(id,project_id,artifact_kind,artifact_key,artifact_revision,digest,size_bytes,file_id,created_at)"
                     " VALUES ('manifest01','project','document','output.txt',1,?,?, 'file-one','now')", (digest, len(data)))

    class Files:
        async def get(self, file_id: str) -> SimpleNamespace:
            assert file_id == "file-one"
            return SimpleNamespace(size=len(data), sha256=digest)

        def path_of(self, _: SimpleNamespace) -> Path:
            return source

    app = SimpleNamespace(db=db, manager=SimpleNamespace(files=Files()))
    transfers = RuntimeTransfers(app)
    queued = await transfers.start(operator, "project", "manifest01", enrolled["host_id"],
                                   expected_entity_revision=1, client_operation_id="transfer-one")
    assert queued["state"] == "queued"
    (remote_root / ("." + queued["transfer_id"] + ".stage")).write_bytes(data[:4000])
    dispatcher = EffectDispatcher(OutboxStore(db))
    dispatcher.register("artifact.transfer", ArtifactTransferEffect(app, transport))
    assert await dispatcher.step()
    result = await transfers.view(queued["transfer_id"])
    assert result["state"] == "published"
    assert result["published_digest"] == digest
    assert (remote_root / queued["transfer_id"]).read_bytes() == data
    await db.execute("UPDATE effect_outbox SET state='unknown',claim_generation=claim_generation+1 WHERE id = ?", (queued["effect_id"],))
    await db.execute("UPDATE artifact_transfers SET state='unknown' WHERE id = ?", (queued["transfer_id"],))
    assert (await transfers.view(queued["transfer_id"]))["state"] == "unknown"
    assert await dispatcher.reconcile() == 1
    assert (await transfers.view(queued["transfer_id"]))["state"] == "published"
