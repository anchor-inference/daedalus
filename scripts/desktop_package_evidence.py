"""Record desktop package evidence from an artifact and a launcher on this runner.

A package built for another architecture cannot prove that it starts here.
"""

import argparse
import hashlib
import json
import platform
import re
import subprocess
from pathlib import Path

TARGETS = {
    "linux-amd64": ("Linux", "x86_64", "amd64"),
    "linux-arm64": ("Linux", "aarch64", "arm64"),
    "windows-amd64": ("Windows", "AMD64", "x86_64"),
    "macos-amd64": ("Darwin", "x86_64"),
    "macos-arm64": ("Darwin", "arm64"),
}
CHECKS = ("install", "start", "uninstall", "offline_first_run", "update", "supply_provenance")


class InvalidEvidence(ValueError):
    pass


def record(package, launcher, target, channel, expected_version, passed=(), *, system=None, machine=None):
    """Bind observed version and lifecycle checks to package bytes and runner architecture."""
    if target not in TARGETS:
        raise InvalidEvidence("unsupported_target")
    if channel not in ("stable", "canary"):
        raise InvalidEvidence("invalid_channel")
    if not re.fullmatch(r"(?:desktop-v\d+\.\d+\.\d+|dev-[0-9a-f]{12})", expected_version):
        raise InvalidEvidence("invalid_version")
    if set(passed) - set(CHECKS):
        raise InvalidEvidence("invalid_check")
    package = Path(package)
    launcher = Path(launcher)
    if not package.is_file() or not launcher.is_file():
        raise InvalidEvidence("invalid_fixture")

    with package.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    try:
        result = subprocess.run([str(launcher), "--version"], capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InvalidEvidence("launcher_failed") from exc
    observed = result.stdout.strip()
    if result.returncode != 0 or observed != expected_version:
        raise InvalidEvidence("version_mismatch")

    system = system or platform.system()
    machine = machine or platform.machine()
    if system != TARGETS[target][0] or machine not in TARGETS[target][1:]:
        raise InvalidEvidence("abi_mismatch")

    checks = {name: "passed" if name in passed else "unknown" for name in CHECKS}
    return {
        "target": target,
        "channel": channel,
        "package_sha256": digest,
        "expected_version": expected_version,
        "observed_version": observed,
        "runner": {"system": system, "machine": machine},
        "checks": checks,
        "runtime_smoke": "passed" if checks["start"] == "passed" else "unknown",
        "stable_eligible": channel == "stable" and all(value == "passed" for value in checks.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--launcher", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--passed", action="append", default=[], choices=CHECKS)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        evidence = record(args.package, args.launcher, args.target, args.channel, args.expected_version, args.passed)
    except InvalidEvidence as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
