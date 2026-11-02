"""cli/scripts/install.sh against a release served on loopback.

The script installs daedalus-cli from the newest desktop release only after the release's signature
over its tag and SHA256SUMS verifies and the archive matches SHA256SUMS. These run it end to end
with a key made here written into its release_keys line, where the project's key stands, and a
release signed the way desktop/sign-release.sh signs (minisign's legacy Ed25519 over the message).
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import tarfile
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

SCRIPT = Path(__file__).resolve().parents[2] / "cli" / "scripts" / "install.sh"
TAG = "desktop-v0.23.0"
KEY_ID = bytes.fromhex("0102030405060708")

needed = ("sh", "curl", "openssl", "tar", "awk", "od", "dd")
pytestmark = pytest.mark.skipif(
    platform.system() not in ("Linux", "Darwin") or any(shutil.which(tool) is None for tool in needed),
    reason="the installer runs on Linux and macOS with curl and OpenSSL",
)


def test_install_script_is_plain_ascii() -> None:
    # macOS /bin/sh (bash 3.2) folds a multibyte character after an unbraced $name into the name,
    # which desktop/install.sh once turned into an "unbound variable" abort on every Mac.
    bad = [i + 1 for i, line in enumerate(SCRIPT.read_bytes().splitlines()) if any(b > 0x7F for b in line)]
    assert not bad, f"non-ASCII bytes on lines {bad}"


def test_the_client_installers_trust_the_same_keys_as_the_desktop_installers() -> None:
    # One release, one signature: a key rotated in desktop/ and not here would leave the client's
    # installers refusing every release signed by the new key, or trusting a retired one.
    root = SCRIPT.parents[2]

    def keys(path: Path, pattern: str) -> str:
        found = re.findall(pattern, path.read_text(), flags=re.MULTILINE)
        assert len(found) == 1, f"{path} names its release keys {len(found)} times"
        return found[0]

    shell = r'^release_keys="([^"]*)"$'
    powershell = r"^\$ReleaseKeys = @\(([^)]*)\)$"
    assert keys(SCRIPT, shell) == keys(root / "desktop" / "install.sh", shell)
    assert keys(root / "cli" / "scripts" / "install.ps1", powershell) == keys(root / "desktop" / "install.ps1", powershell)


def asset_name() -> str:
    system = {"Linux": "linux", "Darwin": "darwin"}[platform.system()]
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine().lower()]
    return f"daedalus-cli-{system}-{arch}.tar.gz"


def archive() -> bytes:
    program = b"#!/bin/sh\necho 'daedalus-cli 0.23.0'\n"
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, body, mode in (("daedalus-cli", program, 0o755), ("LICENSE", b"MIT\n", 0o644)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(body), mode
            tar.addfile(info, io.BytesIO(body))
    return out.getvalue()


def public_line(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(b"Ed" + KEY_ID + raw).decode()


def signature(key: Ed25519PrivateKey, sums: bytes) -> bytes:
    """SHA256SUMS.sig as `minisign -S -l` writes it over the message the release is signed with."""
    message = f"daedalus-release {TAG}\n".encode() + sums
    body = base64.b64encode(b"Ed" + KEY_ID + key.sign(message)).decode()
    return f"untrusted comment: signature from a test key\n{body}\ntrusted comment: daedalus-release {TAG}\nAAAA\n".encode()


class Release:
    """A release folder and the API listing that names it, served on 127.0.0.1."""

    def __init__(self, root: Path, key: Ed25519PrivateKey) -> None:
        self.root = root
        self.files = root / "dl" / TAG
        self.files.mkdir(parents=True)
        (root / "api").mkdir()
        # The newest plain tag wins over a draft, a prerelease and an older release listed first.
        (root / "api" / "releases").write_text(
            '[{"tag_name":"desktop-v0.22.0","draft":false,"prerelease":false},'
            '{"tag_name":"desktop-v0.24.0","draft":true,"prerelease":false},'
            '{"tag_name":"desktop-v0.25.0-rc1","draft":false,"prerelease":true},'
            f'{{"tag_name":"{TAG}","draft":false,"prerelease":false,"author":{{"login":"someone"}}}}]'
        )
        body = archive()
        (self.files / asset_name()).write_bytes(body)
        sums = f"{hashlib.sha256(body).hexdigest()}  {asset_name()}\n{'0' * 64}  daedalus-desktop-linux-amd64.tar.gz\n".encode()
        (self.files / "SHA256SUMS").write_bytes(sums)
        (self.files / "SHA256SUMS.sig").write_bytes(signature(key, sums))


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def release(tmp_path: Path, key: Ed25519PrivateKey) -> Iterator[tuple[Release, str]]:
    served = Release(tmp_path / "srv", key)
    handler = partial(SimpleHTTPRequestHandler, directory=str(served.root))
    handler.log_message = lambda *args: None  # type: ignore[attr-defined]
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield served, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def run(tmp_path: Path, base: str, key: Ed25519PrivateKey, *args: str) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "install.sh"
    text, count = re.subn(r'\nrelease_keys="[^"\n]*"\n', f'\nrelease_keys="{public_line(key)}"\n', SCRIPT.read_text())
    assert count == 1, "install.sh has no single release_keys line to fill"
    script.write_text(text)
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    env.update(PREFIX=str(tmp_path / "bin"), HOME=str(tmp_path), NO_PROXY="*",
               DAEDALUS_RELEASES_API=f"{base}/api", DAEDALUS_DOWNLOAD_BASE=f"{base}/dl")
    return subprocess.run(["sh", str(script), *args], env=env, capture_output=True, text=True, timeout=60)  # noqa: S603, S607


def test_it_installs_the_newest_signed_release_under_both_names(tmp_path: Path, release: tuple[Release, str], key: Ed25519PrivateKey) -> None:
    _, base = release
    done = run(tmp_path, base, key)
    assert done.returncode == 0, done.stderr
    assert f"for {TAG}" in done.stdout and "Checksum matches." in done.stdout
    installed = tmp_path / "bin" / "daedalus-cli"
    assert os.access(installed, os.X_OK)
    assert os.readlink(tmp_path / "bin" / "dae") == "daedalus-cli"
    said = subprocess.run([str(tmp_path / "bin" / "dae"), "version"], capture_output=True, text=True, check=True)  # noqa: S603
    assert said.stdout.strip() == "daedalus-cli 0.23.0"


def test_an_archive_that_does_not_match_its_checksum_is_not_installed(tmp_path: Path, release: tuple[Release, str], key: Ed25519PrivateKey) -> None:
    served, base = release
    with (served.files / asset_name()).open("ab") as damaged:
        damaged.write(b"x")
    done = run(tmp_path, base, key)
    assert done.returncode != 0
    assert "does not match its checksum" in done.stderr
    assert not (tmp_path / "bin").exists()


def test_a_release_signed_by_another_key_is_refused(tmp_path: Path, release: tuple[Release, str]) -> None:
    _, base = release
    done = run(tmp_path, base, Ed25519PrivateKey.generate())
    assert done.returncode != 0
    assert "does not verify" in done.stderr
    assert not (tmp_path / "bin").exists()


def test_checksums_changed_after_signing_are_refused(tmp_path: Path, release: tuple[Release, str], key: Ed25519PrivateKey) -> None:
    served, base = release
    sums = served.files / "SHA256SUMS"
    sums.write_text(sums.read_text().replace("0" * 64, "1" * 64))
    done = run(tmp_path, base, key)
    assert done.returncode != 0
    assert "does not verify" in done.stderr


def test_an_unsigned_release_is_refused(tmp_path: Path, release: tuple[Release, str], key: Ed25519PrivateKey) -> None:
    served, base = release
    (served.files / "SHA256SUMS.sig").unlink()
    done = run(tmp_path, base, key)
    assert done.returncode != 0
    assert "only installs signed releases" in done.stderr
    assert not (tmp_path / "bin").exists()
