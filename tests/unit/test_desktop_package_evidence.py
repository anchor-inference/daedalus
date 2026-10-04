import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "desktop_package_evidence.py"
SPEC = importlib.util.spec_from_file_location("desktop_package_evidence", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixture(tmp_path, version="desktop-v1.2.3"):
    package = tmp_path / "Daedalus-linux-amd64.deb"
    package.write_bytes(b"package fixture\n")
    launcher = tmp_path / "daedalus-desktop"
    launcher.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
    launcher.chmod(0o755)
    return package, launcher


def record(package, launcher, **kwargs):
    return MODULE.record(
        package,
        launcher,
        "linux-amd64",
        "stable",
        "desktop-v1.2.3",
        system="Linux",
        machine="x86_64",
        **kwargs,
    )


def test_evidence_binds_package_version_and_partial_lifecycle(tmp_path):
    package, launcher = fixture(tmp_path)
    evidence = record(package, launcher, passed=("install", "start", "offline_first_run"))

    assert evidence["package_sha256"] == hashlib.sha256(package.read_bytes()).hexdigest()
    assert evidence["observed_version"] == "desktop-v1.2.3"
    assert evidence["runtime_smoke"] == "passed"
    assert evidence["checks"]["update"] == "unknown"
    assert evidence["checks"]["uninstall"] == "unknown"
    assert evidence["stable_eligible"] is False


def test_wrong_version_refuses_evidence(tmp_path):
    package, launcher = fixture(tmp_path, "desktop-v1.2.2")
    with pytest.raises(MODULE.InvalidEvidence, match="version_mismatch"):
        record(package, launcher, passed=MODULE.CHECKS)


def test_cross_arch_and_offline_update_do_not_claim_stable(tmp_path):
    package, launcher = fixture(tmp_path)
    with pytest.raises(MODULE.InvalidEvidence, match="abi_mismatch"):
        MODULE.record(package, launcher, "linux-arm64", "stable", "desktop-v1.2.3", system="Linux", machine="x86_64")

    evidence = record(package, launcher, passed=("install", "start", "uninstall", "offline_first_run"))
    assert evidence["checks"]["update"] == "unknown"
    assert evidence["stable_eligible"] is False


def test_invalid_target_and_missing_package_refuse_evidence(tmp_path):
    package, launcher = fixture(tmp_path)
    with pytest.raises(MODULE.InvalidEvidence, match="unsupported_target"):
        MODULE.record(package, launcher, "linux-riscv64", "stable", "desktop-v1.2.3")
    package.unlink()
    with pytest.raises(MODULE.InvalidEvidence, match="invalid_fixture"):
        record(package, launcher)


def test_offline_command_writes_evidence_and_refuses_mismatch(tmp_path):
    package, launcher = fixture(tmp_path)
    output = tmp_path / "evidence.json"
    command = [
        sys.executable,
        str(SCRIPT),
        "--package", str(package),
        "--launcher", str(launcher),
        "--target", "linux-amd64",
        "--channel", "canary",
        "--expected-version", "desktop-v1.2.3",
        "--passed", "install",
        "--output", str(output),
    ]
    assert subprocess.run(command, capture_output=True).returncode == 0
    evidence = json.loads(output.read_text())
    assert evidence["checks"]["install"] == "passed"
    assert evidence["stable_eligible"] is False

    package.write_bytes(b"changed package\n")
    command[command.index("desktop-v1.2.3")] = "desktop-v1.2.4"
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode != 0
    assert "version_mismatch" in failed.stderr
    assert json.loads(output.read_text()) == evidence
