"""The vendor clients the key proxy imitates, and keeping each imitation current.

A subscription upstream is served the way the vendor's own CLI would call it, and that imitation
carries the CLI's version: in the User-Agent, in Claude Code's attribution stamp, in Grok's client
version header. The vendor ships every few days, and a version left behind is refused outright —
Anthropic answers ``claude_code_version_too_old``, Grok's chat proxy answers 426 — so a constant in
the code went stale between two image builds and every call to that subscription failed with it.

Each client is described here by a :class:`ClientSpec`: its built-in field values, the variable that
pins its version by hand, and how to learn the newest release. The values in force are a profile:
the built-in ones, overlaid by what an update stored in the keys file under ``KEYPROXY_CLIENT_<NAME>``
— the one file this process may already write, which it reads again on every call, so an update
holds from the next request with no rebuild and no restart, in a container and in a native
installation alike. Adding a client is adding a spec; the checking, the storing, the endpoints and
the screen in the app take it as it comes.

The stored value is one line of JSON: the fields the update set, the stored fields before it (what
Revert puts back), and whether the first request after it has answered yet. A request refused in a
way that names the client's version puts the previous profile back on its own (:func:`verify`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("keyproxy.clients")

CHECK_SECONDS = 6 * 3600.0
"""How often the newest releases are asked for. A vendor ships about weekly; the refusal for a stale
version comes some days after a release, so a check a few times a day is early enough."""
CHECK_JITTER_SECONDS = 15 * 60.0
FIRST_CHECK_SECONDS = 60.0
RETRY_SECONDS = 15 * 60.0
"""The first wait after a failed check; it doubles per failure up to :data:`CHECK_SECONDS`."""
VARIABLE_PREFIX = "KEYPROXY_CLIENT_"

NPM_REGISTRY = "https://registry.npmjs.org"
GROK_RELEASES = ("https://x.ai/cli/stable", "https://storage.googleapis.com/grok-build-public-artifacts/cli/stable")
"""Where Grok's own installer reads the stable version: one line, the version, from x.ai first and
from the bucket behind it when that does not answer."""

_VERSION = re.compile(r"^\d+(?:\.\d+){1,3}$")
_IDENTITY_REFUSAL = re.compile(r"version|outdated|too.old|upgrade|unsupported.client", re.IGNORECASE)


def version_key(version: str) -> tuple[int, ...]:
    """A release version as numbers, for ordering; anything not a plain dotted version sorts first."""
    if not _VERSION.match(version or ""):
        return ()
    return tuple(int(part) for part in version.split("."))


def newer(candidate: str, than: str) -> bool:
    return version_key(candidate) > version_key(than)


async def npm_latest(client: httpx.AsyncClient, package: str) -> str:
    """The ``latest`` dist-tag of an npm package, read from the registry's per-version document."""
    response = await client.get(f"{NPM_REGISTRY}/{package.replace('/', '%2F')}/latest", headers={"accept": "application/json"}, timeout=20.0)
    if response.status_code != 200:
        raise RuntimeError(f"the npm registry answered {response.status_code} for {package}")
    version = str(response.json().get("version") or "")
    if not version_key(version):
        raise RuntimeError(f"the npm registry named no release version for {package}: {version!r}")
    return version


async def grok_latest(client: httpx.AsyncClient) -> str:
    failures = []
    for url in GROK_RELEASES:
        try:
            response = await client.get(url, timeout=20.0)
        except httpx.HTTPError as exc:
            failures.append(f"{url}: {type(exc).__name__}")
            continue
        version = response.text.strip().splitlines()[0].strip() if response.status_code == 200 and response.text.strip() else ""
        if version_key(version):
            return version
        failures.append(f"{url}: HTTP {response.status_code}")
    raise RuntimeError("no Grok release version: " + "; ".join(failures))


def installed_grok_version() -> str:
    """The grok CLI mounted beside its login (``<grok home>/bin/grok`` → ``grok-<version>-…``), or "".

    The operator's own CLI updates itself, so a version read from it is one the chat proxy accepted
    recently; a profile older than it is never the one in force (see :func:`profile`).
    """
    auth = Path(os.environ.get("KEYPROXY_GROK_AUTH", os.path.expanduser("~/.grok/auth.json")))
    try:
        name = (auth.parent / "bin" / "grok").resolve().name
    except OSError:
        return ""
    found = re.search(r"(\d+\.\d+\.\d+)", name)
    return found.group(1) if found else ""


@dataclass(frozen=True)
class ClientSpec:
    """One imitated client. ``defaults`` holds every field its headers are built from, ``version`` among them.

    ``follows`` names, for the operator, what changes with the version on its own because it is
    built from it. Every other field is kept by an update unless ``derive`` can say what the new
    release uses; the update reports the kept ones by name, since only a field it did not move can
    still be stale.
    """

    name: str
    label: str
    defaults: dict[str, str]
    pin_variable: str
    latest: Callable[[httpx.AsyncClient], Awaitable[str]]
    follows: tuple[str, ...] = ()
    installed: Callable[[], str] | None = None
    derive: Callable[[str], dict[str, str]] | None = None
    upstream: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    """What the operator knows a field as: the header it fills. A field without one is shown by its key."""

    @property
    def variable(self) -> str:
        return VARIABLE_PREFIX + self.name.upper()


CLAUDE_BETAS = (
    "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
    "context-management-2025-06-27,per-turn-control-2026-07-01,mid-conversation-tool-changes-2026-07-01"
)
"""The fixed betas Claude Code 2.1.309 and 2.1.310 send on a subscriber's main loop, in their order.

Against 2.1.296 the list lost ``prompt-caching-scope-2026-01-05`` and gained per-turn control and
mid-conversation tool changes. ``advanced-tool-use`` is sent only when tools are deferred behind tool
search, which the proxy never does, and the effort and cache-TTL betas are added per request."""

SPECS: dict[str, ClientSpec] = {
    spec.name: spec
    for spec in (
        ClientSpec(
            name="claude",
            label="Claude Code",
            defaults={
                "version": "2.1.310",
                # The Anthropic SDK and the runtime the CLI is built on. They move between releases
                # less often than the version, and only a look inside the release can say to what.
                "stainless_package_version": "0.128.0",
                "stainless_runtime_version": "v26.3.0",
                "betas": CLAUDE_BETAS,
            },
            pin_variable="KEYPROXY_CLAUDE_CLI_VERSION",
            latest=lambda client: npm_latest(client, "@anthropic-ai/claude-code"),
            follows=("User-Agent", "cc_version attribution stamp"),
            upstream="claude",
            labels={"stainless_package_version": "X-Stainless-Package-Version", "stainless_runtime_version": "X-Stainless-Runtime-Version", "betas": "anthropic-beta"},
        ),
        ClientSpec(
            name="codex",
            label="Codex CLI",
            defaults={
                "version": "0.164.0",
                "originator": "codex_cli_rs",
                # What the CLI appends to its name and version: the system, the machine, the terminal.
                "platform": "(Ubuntu 24.4.0; x86_64) xterm-256color",
            },
            pin_variable="KEYPROXY_CODEX_CLI_VERSION",
            latest=lambda client: npm_latest(client, "@openai/codex"),
            follows=("User-Agent",),
            upstream="codex",
            labels={"originator": "originator", "platform": "User-Agent platform"},
        ),
        ClientSpec(
            name="grok",
            label="Grok CLI",
            defaults={"version": "1.0.70"},
            pin_variable="KEYPROXY_GROK_CLIENT_VERSION",
            latest=grok_latest,
            follows=("User-Agent", "x-grok-client-version"),
            installed=installed_grok_version,
            upstream="grok",
        ),
    )
}


@dataclass
class _Hooks:
    """What the proxy lends this module: its view of the keys file, and the means to write one variable of it."""

    read: Callable[[], dict[str, str]] = field(default=lambda: dict(os.environ))
    write: Callable[[str, str], None] | None = None
    writable: Callable[[], bool] = field(default=lambda: False)


_hooks = _Hooks()
_latest: dict[str, dict[str, Any]] = {}
"""Per client: ``latest``, ``checked_at`` (wall clock), ``error``, ``failures`` and ``due`` (monotonic)."""
_write_lock = asyncio.Lock()


def configure(read: Callable[[], dict[str, str]], write: Callable[[str, str], None], writable: Callable[[], bool]) -> None:
    """Called once by the proxy. While ``writable`` says no, the profiles are read-only and an update says why."""
    _hooks.read = read
    _hooks.write = write
    _hooks.writable = writable


def writable() -> bool:
    return _hooks.write is not None and _hooks.writable()


def stored(name: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """The JSON an update left under the client's variable, or ``{}``; a damaged value is ``{}``, not an error."""
    raw = (env if env is not None else _hooks.read()).get(SPECS[name].variable, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        logger.warning("%s is not JSON; the built-in %s profile is used", SPECS[name].variable, name)
        return {}
    return value if isinstance(value, dict) else {}


def _fields(raw: Any, spec: ClientSpec) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {key: str(value) for key, value in raw.items() if key in spec.defaults and isinstance(value, str) and value.strip()}


def profile(name: str) -> dict[str, Any]:
    """The values in force for one client: every field, plus ``source`` saying where the version came from.

    The version is the newest of the built-in one, the stored one and the installed CLI's, so an
    image that ships a newer default is never held back by an older update stored before it, and a
    Grok CLI the operator updated is followed without asking. The stored fields other than the
    version apply only while the stored version is the one in force: they were learned for that
    release, not for whatever the image brought later. The pin variable wins over everything.
    """
    spec = SPECS[name]
    env = _hooks.read()
    saved = stored(name, env)
    saved_fields = _fields(saved.get("fields"), spec)
    candidates = [(spec.defaults["version"], "default")]
    if saved_fields.get("version"):
        candidates.append((saved_fields["version"], "stored"))
    installed = spec.installed() if spec.installed else ""
    if installed:
        candidates.append((installed, "installed"))
    version, source = max(candidates, key=lambda pair: version_key(pair[0]))
    fields = dict(spec.defaults)
    if source == "stored":
        fields.update(saved_fields)
    pin = env.get(spec.pin_variable, "").strip()
    if pin:
        version, source = pin, "environment"
    fields["version"] = version
    return {**fields, "source": source}


def version(name: str) -> str:
    return str(profile(name)["version"])


def _due(name: str) -> bool:
    state = _latest.get(name)
    return state is None or time.monotonic() >= float(state.get("due", 0.0))


async def check(client: httpx.AsyncClient, names: list[str] | None = None, *, force: bool = False) -> None:
    """Ask for the newest release of each client that is due (or of every one, with ``force``).

    A failure is remembered with its reason and tried again after a wait that doubles per failure,
    so an unreachable registry is neither hammered nor left unasked for six hours.
    """
    for name in names or list(SPECS):
        if not force and not _due(name):
            continue
        state = _latest.setdefault(name, {"latest": "", "checked_at": None, "error": "", "failures": 0, "due": 0.0})
        try:
            found = await SPECS[name].latest(client)
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            state["failures"] = int(state.get("failures") or 0) + 1
            state["error"] = str(exc)[:200] or type(exc).__name__
            state["due"] = time.monotonic() + min(CHECK_SECONDS, RETRY_SECONDS * 2 ** (state["failures"] - 1))
            logger.warning("could not learn the newest %s release: %s", name, state["error"])
            continue
        state.update(latest=found, checked_at=time.time(), error="", failures=0, due=time.monotonic() + CHECK_SECONDS + random.uniform(0, CHECK_JITTER_SECONDS))


async def watch(client: Callable[[], httpx.AsyncClient]) -> None:
    """The background check: soon after the start, then whenever a client falls due.

    ``client`` is asked for the HTTP client at each round rather than handed one, so the check always
    uses the one the application holds now.
    """
    await asyncio.sleep(FIRST_CHECK_SECONDS)
    while True:
        try:
            await check(client())
        except Exception:  # noqa: BLE001 — a failed look is tried again later, never the end of the proxy
            logger.exception("the client release check failed")
        await asyncio.sleep(60.0)


def view(name: str, logged_in: bool | None = None) -> dict[str, Any]:
    """One client as the app shows it: what is in force, the newest release, and what an update would move."""
    spec = SPECS[name]
    current = profile(name)
    saved = stored(name)
    state = _latest.get(name) or {}
    latest = str(state.get("latest") or "")
    pinned = current["source"] == "environment"
    previous = saved.get("previous") if isinstance(saved.get("previous"), dict) else None
    return {
        "name": name,
        "label": spec.label,
        "upstream": spec.upstream,
        "logged_in": logged_in,
        "version": current["version"],
        "source": current["source"],
        "default_version": spec.defaults["version"],
        "installed_version": (spec.installed() if spec.installed else "") or None,
        "pinned_by": spec.pin_variable if pinned else None,
        "latest": latest or None,
        "checked_at": state.get("checked_at"),
        "check_error": state.get("error") or None,
        "stale": bool(latest) and newer(latest, str(current["version"])) and not pinned,
        "fields": {key: current[key] for key in spec.defaults},
        "field_labels": {key: spec.labels.get(key, key) for key in spec.defaults},
        "follows": list(spec.follows),
        "kept_on_update": [key for key in spec.defaults if key != "version"] if spec.derive is None else [],
        "previous_version": (str(previous.get("version") or "") or None) if previous is not None else None,
        "updated_at": saved.get("updated_at"),
        "pending": bool(saved.get("pending")),
        "verified_at": saved.get("verified_at"),
        "reverted": saved.get("reverted") if isinstance(saved.get("reverted"), dict) else None,
    }


class ClientError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _write(name: str, value: dict[str, Any] | None) -> None:
    if _hooks.write is None or not writable():
        raise ClientError(409, "this key proxy reads its settings from its environment only, so it cannot store a client update")
    _hooks.write(SPECS[name].variable, json.dumps(value, separators=(",", ":"), sort_keys=True) if value else "")


async def update(name: str, client: httpx.AsyncClient, target: str = "") -> dict[str, Any]:
    """Move one client to ``target`` (the newest release when empty) and say what moved and what was kept.

    Only the version is set unless the spec can derive more; every other field keeps its value and is
    reported, so a header that may now be stale is named rather than silently carried. The stored
    fields from before are kept as the one step Revert goes back to.
    """
    spec = _spec(name)
    if not target:
        await check(client, [name], force=not (_latest.get(name) or {}).get("latest"))
        target = str((_latest.get(name) or {}).get("latest") or "")
        if not target:
            raise ClientError(502, f"the newest {spec.label} release is not known: {(_latest.get(name) or {}).get('error') or 'not checked yet'}")
    if not version_key(target):
        raise ClientError(400, f"{target!r} is not a release version")
    async with _write_lock:
        before = profile(name)
        if before["source"] == "environment":
            raise ClientError(409, f"{spec.label} is pinned to {before['version']} by {spec.pin_variable}; remove it to update from here")
        saved = stored(name)
        fields = {"version": target, **(spec.derive(target) if spec.derive else {})}
        _write(name, {
            "fields": fields,
            # The stored fields are what Revert restores; the version is only what the screen names.
            "previous": {"fields": _fields(saved.get("fields"), spec), "version": before["version"]},
            "updated_at": time.time(),
            "pending": True,
        })
    after = profile(name)
    if after["version"] != target:
        # A newer default or installed CLI already outranks the target: nothing the update stored is in force.
        logger.warning("%s update to %s stored, but %s from %s stays in force", name, target, after["version"], after["source"])
    changed = {key: {"from": before[key], "to": after[key]} for key in spec.defaults if before[key] != after[key]}
    kept = {key: after[key] for key in spec.defaults if key != "version" and key not in fields}
    logger.warning("%s client updated: %s", name, ", ".join(f"{key} {move['from']} → {move['to']}" for key, move in changed.items()) or "nothing moved")
    return {"client": view(name), "changed": changed, "kept": kept}


async def revert(name: str, *, reason: str = "") -> dict[str, Any]:
    """Put back the stored fields from before the last update. ``reason`` set is the automatic revert."""
    spec = _spec(name)
    async with _write_lock:
        saved = stored(name)
        if not isinstance(saved.get("previous"), dict):
            raise ClientError(409, f"{spec.label} has no earlier profile to go back to")
        before = profile(name)
        previous = _fields(saved["previous"].get("fields"), spec)
        record = {"from": before["version"], "at": time.time(), "reason": reason or "reverted by the operator", "automatic": bool(reason)}
        _write(name, {"fields": previous, "updated_at": time.time(), "reverted": record})
    record["to"] = version(name)
    logger.warning("%s client reverted %s → %s%s", name, record["from"], record["to"], f" ({reason})" if reason else "")
    return {"client": view(name), "reverted": record}


async def verify(name: str, status: int, text: str = "") -> None:
    """Judge the update by the first answer to a completion after it.

    An answer that went through confirms it. A refusal that names the client — a version too old, an
    outdated client, 426 Upgrade Required — puts the previous profile back at once, since every call
    would fail the same way. Any other refusal (a rate limit, a bad request, a server fault) says
    nothing about the identity, and the next answer is judged instead.
    """
    if name not in SPECS:
        return
    saved = stored(name)
    if not saved.get("pending"):
        return
    if status < 400:
        async with _write_lock:
            current = stored(name)
            if current.get("pending"):
                current.pop("pending", None)
                current["verified_at"] = time.time()
                _write(name, current)
        return
    if status == 426 or (status in (400, 401, 403) and _IDENTITY_REFUSAL.search(text or "")):
        try:
            await revert(name, reason=f"HTTP {status}: {(text or '').strip()[:160]}")
        except ClientError as exc:
            logger.warning("could not revert the %s client after a refusal: %s", name, exc)


def _spec(name: str) -> ClientSpec:
    spec = SPECS.get(name)
    if spec is None:
        raise ClientError(404, f"no imitated client called {name!r}")
    return spec


__all__ = [
    "CHECK_SECONDS",
    "SPECS",
    "ClientError",
    "ClientSpec",
    "check",
    "configure",
    "installed_grok_version",
    "newer",
    "profile",
    "revert",
    "stored",
    "update",
    "verify",
    "version",
    "version_key",
    "view",
    "watch",
    "writable",
]
