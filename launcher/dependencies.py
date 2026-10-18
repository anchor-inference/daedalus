"""Dependency recipes and installation, outside the model's tool surface.

Only named packages from the installation's package managers are accepted. There is no command,
URL, alternate index or file path in the recipe. Package installation still executes publisher code;
the operator approves that trust boundary, not merely a download.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

EMPTY: dict[str, list[str]] = {"python": [], "system": []}
PYTHON_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}(?:==[A-Za-z0-9][A-Za-z0-9.!+_-]{0,79})?")
SYSTEM_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.@-]{0,79}")


def recipe(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict) or set(value) != set(EMPTY):
        raise ValueError("expected python and system package lists")
    result = {}
    for kind, pattern in (("python", PYTHON_PACKAGE), ("system", SYSTEM_PACKAGE)):
        items = value[kind]
        if not isinstance(items, list) or len(items) > 64:
            raise ValueError("at most 64 packages per package manager")
        if any(not isinstance(item, str) or not pattern.fullmatch(item) for item in items):
            raise ValueError("only package names and optional Python ==version pins are allowed")
        result[kind] = sorted(set(items))
    names = [re.sub(r"[-_.]+", "-", item.split("==")[0]).lower() for item in result["python"]]
    if len(set(names)) != len(names):
        raise ValueError("multiple requirements for the same Python package")
    return result


def encoded(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text("utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".pending")
    pending.write_text(encoded(value), encoding="utf-8")
    pending.replace(path)


def clear_maintenance(root: Path, job_id: str) -> None:
    marker = root / "maintenance"
    if marker.exists() and marker.read_text() == job_id:
        marker.unlink(missing_ok=True)


async def command(argv: list[str], *, limit: float = 1200, output_limit: int = 16000, on_output: Callable[[str], None] | None = None) -> str:
    """Bound output and lifetime without ever passing a package name through a shell."""
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
    env.update(DEBIAN_FRONTEND="noninteractive", PIP_CONFIG_FILE=os.devnull, UV_NO_CONFIG="1", UV_PYTHON_DOWNLOADS="never", GIT_TERMINAL_PROMPT="0", NONINTERACTIVE="1", HOMEBREW_NO_AUTO_UPDATE="1")
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env, start_new_session=os.name != "nt")
    output = bytearray()
    try:
        async with asyncio.timeout(limit):
            assert process.stdout is not None
            while chunk := await process.stdout.read(4096):
                if on_output:
                    on_output(chunk.decode("utf-8", errors="replace"))
                output.extend(chunk)
                del output[:-output_limit]
            await process.wait()
    except BaseException:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            await process.wait()
        raise
    text = output.decode("utf-8", errors="replace")
    if process.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} exited {process.returncode}: {text[-4000:]}")
    return text.strip()


class Dependencies:
    def __init__(self, repo: Path, state: Path, trigger: Path, *, native: bool) -> None:
        self.repo = repo
        self.root = state / "dependencies"
        self.trigger = trigger
        self.native = native
        self.manifest = repo / "deploy" / "dependencies" / "local.json"
        self.task: asyncio.Task[None] | None = None
        self.catalog_lock = asyncio.Lock()

    def current(self) -> dict[str, list[str]]:
        return recipe(read_json(self.manifest, EMPTY))

    def system_manager(self) -> str:
        if not self.native or (sys.platform.startswith("linux") and shutil.which("apt-get")):
            return "apt"
        if sys.platform == "darwin" and shutil.which("brew"):
            return "brew"
        return ""

    def capability(self) -> dict[str, Any]:
        manager = self.system_manager()
        if self.native:
            python = bool(shutil.which("uv"))
            system = manager == "brew" or (manager == "apt" and os.geteuid() == 0)
            reason = "" if python else "uv is required to create the isolated Python environment"
        else:
            heartbeat = self.trigger / "dependencies-alive"
            python = system = heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 120
            reason = "" if python else "start the updated rebuilder service to install packages into the Docker image"
        return {"mode": "native" if self.native else "docker", "python": python, "system": system, "manager": manager, "reason": reason}

    def active_bin(self) -> str:
        if not self.native:
            return "/opt/agent-python/bin" if Path("/opt/agent-python/bin/python").exists() else ""
        try:
            active = read_json(self.root / "active.json", {})
        except (OSError, ValueError):
            return ""
        generation = active.get("generation", "")
        if not re.fullmatch(r"[a-f0-9]{64}", generation):
            return ""
        return str(self.root / "python" / generation / ("Scripts" if os.name == "nt" else "bin"))

    def status(self) -> dict[str, Any]:
        job = read_json(self.root / "job.json", None)
        if job and not self.native and job["state"] in ("installing", "restarting"):
            progress = read_json(self.trigger / f"dependencies-{job['id']}.progress", {})
            if progress and progress.get("stage") != job.get("stage"):
                self.advance(job, progress["stage"], restart_at=progress.get("restart_at", 0))
            if job.get("stage") == "building":
                log_path = self.trigger / f"dependencies-{job['id']}.log"
                if log_path.exists():
                    with log_path.open("rb") as stream:
                        stream.seek(max(0, log_path.stat().st_size - 6000))
                        tail = stream.read().decode("utf-8", errors="replace")
                    markers = re.findall(r"DAEDALUS_DEPENDENCIES_STAGE=([a-z_]+)", tail)
                    detail = "exporting" if "exporting layers" in tail else markers[-1] if markers else job.get("detail_stage", "building")
                    if detail != job.get("detail_stage"):
                        job["detail_stage"] = detail
                        self.advance(job, "building", detail=detail)
        if job and not self.native and job["state"] in ("installing", "restarting"):
            result = self.trigger / f"dependencies-{job['id']}.result"
            if result.exists():
                outcome = result.read_text().strip()
                if outcome == "completed" and recipe(read_json(Path("/opt/agent-python/recipe.json"), EMPTY)) != job["recipe"]:
                    # The old process may see the sidecar's receipt before it is replaced. It must
                    # not call that an installed environment or reopen the run gate.
                    if time.time() - result.stat().st_mtime < 120:
                        return {"capability": self.capability(), "recipe": self.current(), "job": job}
                    outcome = "the rebuilt environment did not become active; inspect the rebuilder before retrying"
                if outcome == "completed":
                    job = {**job, "state": "completed", "error": ""}
                    self.advance(job, "completed")
                else:
                    job = self.fail(job, outcome)
        if job and self.native and job["state"] == "installing" and (self.task is None or self.task.done()):
            job = self.fail(job, "installation was interrupted; system packages already installed may remain")
        if job and self.native and job["state"] == "restarting" and self.task is None:
            # A supervisor can restart during the warning window. The next child already uses
            # the published environment; do not leave every page counting down forever.
            active = read_json(self.root / "active.json", {})
            if self.current() == job["recipe"] and active.get("generation") == fingerprint(job["recipe"]):
                job["state"] = "completed"
                self.advance(job, "completed")
            else:
                job = self.fail(job, "the installed environment did not become active after restart")
        if job and job["state"] in ("completed", "failed"):
            clear_maintenance(self.root, job["id"])
        if job:
            path = self.root / "install.log" if self.native else self.trigger / f"dependencies-{job['id']}.log"
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 6000))
                    job = {**job, "log": stream.read().decode("utf-8", errors="replace"), "last_output_at": path.stat().st_mtime}
        return {"capability": self.capability(), "recipe": self.current(), "job": job}

    def advance(self, job: dict[str, Any], stage: str, *, detail: str = "", restart_at: float = 0) -> None:
        now = time.time()
        job.update(stage=stage, updated_at=now, restart_at=restart_at)
        events = job.setdefault("progress", [])
        if not events or (events[-1]["stage"], events[-1].get("detail", "")) != (stage, detail):
            events.append({"at": now, "stage": stage, "detail": detail})
            del events[:-40]
        write_json(self.root / "job.json", job)

    def log_output(self, chunk: str) -> None:
        path = self.root / "install.log"
        previous = path.read_text(errors="replace") if path.exists() else ""
        path.write_text((previous + chunk)[-12000:])

    def fail(self, job: dict[str, Any], error: str) -> dict[str, Any]:
        if self.current() == job["recipe"]:
            write_json(self.manifest, job["previous"])
        if self.native:
            write_json(self.root / "active.json", job["previous_active"])
        failed = {**job, "state": "failed", "error": error[:4000]}
        self.advance(failed, "failed")
        clear_maintenance(self.root, job["id"])
        return failed

    async def inventory(self) -> dict[str, Any]:
        path = os.pathsep.join(filter(None, [self.active_bin(), os.environ.get("PATH", "")]))
        entries = []
        for name in ("python", "python3", "gcc", "g++", "make", "cmake", "git", "node", "npm", "ffmpeg", "rg", "uv"):
            executable = shutil.which(name, path=path)
            version = ""
            if executable:
                try:
                    version = (await command([executable, "--version"], limit=5)).splitlines()[0][:160]
                except (OSError, RuntimeError, TimeoutError):
                    version = "available"
            entries.append({"name": name, "available": bool(executable), "version": version})
        packages = []
        agent_python = shutil.which("python", path=self.active_bin()) if self.active_bin() else None
        if agent_python:
            raw = await command([agent_python, "-I", "-c", "import importlib.metadata,json; print(json.dumps(sorted((d.metadata.get('Name',''),d.version) for d in importlib.metadata.distributions())))"], limit=15)
            packages = json.loads(raw)
        return {**self.status(), "tools": entries, "packages": packages, "platform": self.platform_info()}

    def platform_info(self) -> dict[str, str]:
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            release = {}
        return {"system": platform.system(), "architecture": platform.machine(), "distribution": release.get("PRETTY_NAME", ""), "id": release.get("ID", ""), "version": release.get("VERSION_ID", "")}

    async def checked_preview(self, additions: Any) -> dict[str, Any]:
        proposal = self.preview(additions)
        packages = recipe(additions)["system"]
        if packages and self.system_manager() == "apt":
            # Apt exists on several distributions with incompatible package catalogues. Check the
            # configured repositories before review, in a separate cache; never install here.
            async with self.catalog_lock:
                lists = self.root / "apt-lists"
                (lists / "partial").mkdir(parents=True, exist_ok=True)
                stamp = lists / "checked"
                options = ["-o", f"Dir::State::lists={lists}"]
                if not stamp.exists() or time.time() - stamp.stat().st_mtime > 3600:
                    await command(["apt-get", *options, "-o", "APT::Update::Error-Mode=any", "update"], limit=60)
                    stamp.touch()
                output = await command(["env", "LC_ALL=C", "apt-cache", *options, "policy", "--", *packages], limit=15, output_limit=131072)
                candidates = {}
                package = ""
                for line in output.splitlines():
                    if line and not line[0].isspace() and line.endswith(":"):
                        package = line[:-1].split(":", 1)[0]
                    elif line.strip().startswith("Candidate:"):
                        candidates[package] = line.split(":", 1)[1].strip()
                missing = [name for name in packages if candidates.get(name, "(none)") == "(none)"]
                if missing:
                    distribution = self.platform_info()["distribution"] or "this system"
                    raise ValueError(f"Unavailable in the configured apt repositories of {distribution}: {', '.join(missing)}. Omit these packages and explain the unsupported tools; do not substitute unrelated packages")
        return proposal

    def preview(self, additions: Any) -> dict[str, Any]:
        additions = recipe(additions)
        current = self.current()
        combined = recipe({kind: current[kind] + additions[kind] for kind in EMPTY})
        if combined == current:
            raise ValueError("the recipe already contains these packages; request an addition")
        cap = self.capability()
        if additions["system"] and not cap["system"]:
            raise ValueError("system installation is unavailable: use the OS package manager with your own privileges; this application never elevates privileges")
        if not cap["python"]:
            raise ValueError(cap["reason"])
        patch = "".join(difflib.unified_diff(encoded(current).splitlines(True), encoded(combined).splitlines(True), fromfile="deploy/dependencies/local.json", tofile="deploy/dependencies/local.json"))
        return {"base": fingerprint(current), "recipe": combined, "patch": patch, "capability": cap}

    def accept(self, proposal: dict[str, Any], job_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("invalid installation id")
        previous = self.status()["job"]
        if previous and previous["state"] in ("installing", "restarting"):
            raise ValueError("an installation is already running")
        current = self.current()
        if proposal.get("base") != fingerprint(current):
            raise ValueError("the dependency recipe changed; prepare a new proposal")
        offered = proposal.get("capability", {})
        actual = self.capability()
        if any(offered.get(key) != actual[key] for key in ("mode", "manager")):
            raise ValueError("the installation environment changed; prepare a new proposal")
        desired = recipe(proposal.get("recipe"))
        if any(not set(current[k]).issubset(desired[k]) for k in EMPTY):
            raise ValueError("dependency requests may only add packages")
        additions = {k: sorted(set(desired[k]) - set(current[k])) for k in EMPTY}
        self.preview(additions)
        job = {"id": job_id, "state": "installing", "error": "", "recipe": desired, "previous": current, "started_at": time.time(), "progress": []}
        if self.native:
            job["previous_active"] = read_json(self.root / "active.json", {})
        else:
            # A retry reuses the proposal's id as the job id, so a previous attempt's trigger files are
            # still on the shared volume. Left in place, status() reads the stale result the instant the
            # new job starts and marks it failed before the rebuilder has begun — and the rebuilder,
            # triggered in parallel, rebuilds anyway. Clear them here, the one point every accepted
            # job passes through; the rebuilder writes each one fresh.
            for suffix in ("result", "progress", "log"):
                (self.trigger / f"dependencies-{job_id}.{suffix}").unlink(missing_ok=True)
        self.advance(job, "queued")
        (self.root / "install.log").write_text("")
        (self.root / "maintenance").write_text(job_id)
        return job

    async def install(self, job: dict[str, Any]) -> None:
        desired = recipe(job["recipe"])
        if not self.native:
            write_json(self.manifest, desired)
            pending = self.trigger / "dependencies.pending"
            pending.write_text(job["id"] + "\n")
            pending.replace(self.trigger / "dependencies-request")
            return
        self.advance(job, "environment")
        generation = fingerprint(desired)
        environment = self.root / "python" / generation
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        await command(["uv", "venv", "--allow-existing", "--python", sys.executable, str(environment)], on_output=self.log_output)
        current = self.current()
        added = sorted(set(desired["system"]) - set(current["system"]))
        if added:
            if self.system_manager() == "apt":
                self.advance(job, "catalog")
                await command(["apt-get", "update"], on_output=self.log_output)
                self.advance(job, "system")
                await command(["apt-get", "install", "-y", "--no-install-recommends", "--", *added], on_output=self.log_output)
            else:
                self.advance(job, "system")
                await command(["brew", "install", "--formula", "--", *added], on_output=self.log_output)
        # A source distribution may need the compiler or headers just approved above. Native
        # package managers are not transactional; that partial-install risk is shown at approval.
        if desired["python"]:
            self.advance(job, "python")
            await command(["uv", "pip", "install", "--python", str(python), "--default-index", "https://pypi.org/simple", "--", *desired["python"]], on_output=self.log_output)
        write_json(self.manifest, desired)
        write_json(self.root / "active.json", {"generation": generation})
        job["state"] = "restarting"
        self.advance(job, "restart_pending", restart_at=time.time() + 30)


async def build_image(manifest: Path) -> None:
    def output(chunk: str) -> None:
        print(chunk, end="", flush=True)

    desired = recipe(read_json(manifest, EMPTY))
    if desired["system"]:
        print("DAEDALUS_DEPENDENCIES_STAGE=catalog", flush=True)
        await command(["apt-get", "update"], on_output=output)
        print("DAEDALUS_DEPENDENCIES_STAGE=system", flush=True)
        await command(["apt-get", "install", "-y", "--no-install-recommends", "--", *desired["system"]], on_output=output)
    print("DAEDALUS_DEPENDENCIES_STAGE=environment", flush=True)
    await command(["uv", "venv", "--python", "/usr/bin/python3.12", "/opt/agent-python"], on_output=output)
    if desired["python"]:
        print("DAEDALUS_DEPENDENCIES_STAGE=python", flush=True)
        await command(["uv", "pip", "install", "--python", "/opt/agent-python/bin/python", "--default-index", "https://pypi.org/simple", "--", *desired["python"]], on_output=output)
    print("DAEDALUS_DEPENDENCIES_STAGE=image", flush=True)
    write_json(Path("/opt/agent-python/recipe.json"), desired)


if __name__ == "__main__":
    asyncio.run(build_image(Path(sys.argv[1])))
