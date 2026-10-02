"""What the harness manager asks of each command-line agent: is it installed and which version, what
is the newest, how is it updated or installed, is it signed in, which agents and models does it offer.

This is the half of an adapter that needs no terminal, kept apart so the Harnesses screen and the
hiring form work for every CLI from the start, before the adapter that runs it as staff exists. An
adapter reuses its CLI's tooling for the matching contract methods rather than asking again.

Everything here goes through an ``EnvironmentPort``: a program run for its output in that
environment, or a file read under the daemon's roots, never a CLI's credential file. The newest
version comes from the release feeds over HTTPS from this host (``Fetch``), except Grok's, which
asks its own updater; tests hand in recorded bodies instead of the network.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
import tomllib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

from daedalus.harness.capabilities import parse_version
from daedalus.harness.contract import (
    AgentEntry,
    Catalog,
    EnvironmentPort,
    EnvironmentUnavailable,
    ExecResult,
    InstallInfo,
    LoginState,
    ProgramNotFound,
)

logger = logging.getLogger(__name__)

Fetch = Callable[[str], Awaitable[str]]
"""GET a URL and return the body as text; raises on any failure. The only door to the network."""

VERSION_TIMEOUT_S = 20.0
"""A ``--version`` or a status command. Node CLIs start slowly on a loaded machine; none needs more."""
LIST_TIMEOUT_S = 45.0
"""Listing models may ask a provider; bounded so one slow CLI cannot hold a whole check."""
AGENT_FILE_BYTES = 16 << 10
"""An agent definition's front matter is at its top; the body is never needed here."""
AGENT_FILES_MAX = 200

NPM_PREFIX_DIR = ".npm-global"
"""The npm prefix of the container environment, under its home on the ``terminals-home`` volume, so
an image rebuild keeps every npm-installed CLI. Its ``bin`` must be on the environment's ``PATH``."""


# -- the pinned Node of the container environment ------------------------------------------------

NODE_VERSION = "24.21.0"
"""Codex, OpenCode and pi install from npm and pi runs on Node (it asks for 22.19 or newer). The image
has no Node, so the container environment gets this one in its home, from the Harnesses screen. A
new version is a change here, with its checksums, never a download of whatever is newest."""
NODE_SHA256 = {
    "x64": "6e1db87ef58b8819e5d5402eff1536491b18edd8eb7bee5ef7897876e88dc5ff",
    "arm64": "724282c3b43aec998aa9527380465b45d229e021b58035f5f4f63095eabfe5d5",
}
"""``node-v<version>-linux-<arch>.tar.gz`` from nodejs.org's SHASUMS256.txt. Gzip rather than xz,
because the image has ``tar`` and ``gzip`` but no ``xz``."""


def node_install_script() -> str:
    """A shell script that installs the pinned Node into ``$HOME/.local/share/daedalus/node`` and
    links ``node``, ``npm`` and ``npx`` into ``$HOME/.local/bin``.

    The archive is checked against the pinned checksum before it is unpacked, so a tampered or
    truncated download never runs. It downloads to a file rather than piping, so ``curl`` failing is
    the script failing (``sh`` has no ``pipefail``).
    """
    return f"""set -eu
case "$(uname -m)" in
  x86_64|amd64) arch=x64; sum={NODE_SHA256["x64"]} ;;
  aarch64|arm64) arch=arm64; sum={NODE_SHA256["arm64"]} ;;
  *) echo "Node {NODE_VERSION} is not offered for $(uname -m)"; exit 1 ;;
esac
name="node-v{NODE_VERSION}-linux-$arch"
root="$HOME/.local/share/daedalus/node"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
echo "Downloading Node {NODE_VERSION} ($arch)"
curl -fsSL -o "$tmp/node.tar.gz" "https://nodejs.org/dist/v{NODE_VERSION}/$name.tar.gz"
echo "$sum  $tmp/node.tar.gz" | sha256sum -c -
mkdir -p "$root" "$HOME/.local/bin" "$HOME/{NPM_PREFIX_DIR}"
rm -rf "$root/v{NODE_VERSION}"
mkdir "$root/v{NODE_VERSION}"
tar -xzf "$tmp/node.tar.gz" -C "$root/v{NODE_VERSION}" --strip-components=1
ln -sfn "$root/v{NODE_VERSION}" "$root/current"
for program in node npm npx; do ln -sfn "$root/current/bin/$program" "$HOME/.local/bin/$program"; done
echo "Node $("$HOME/.local/bin/node" --version) is installed"
"""


# -- reading what the CLIs print -----------------------------------------------------------------

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def stable_version(text: str) -> tuple[int, int, int] | None:
    """A plain ``major.minor.patch``, nothing else: a pre-release or a snapshot tag is never a target."""
    found = _SEMVER.match((text or "").strip())
    return (int(found[1]), int(found[2]), int(found[3])) if found else None


def parse_plain_version(body: str) -> str:
    """Claude Code's release feed answers with the bare version (``2.1.281``)."""
    text = (body or "").strip()
    if stable_version(text) is None:
        raise ValueError(f"the release feed answered {text[:80]!r}, not a version")
    return text.lstrip("v")


def parse_npm_dist_tags(body: str, *, major: int | None = None) -> str:
    """The version to install from npm's dist-tags of a package.

    Without ``major``: ``latest``. With it: ``latest`` when it is of that major, otherwise the newest
    stable version any tag points at in that major — OpenCode is pinned to major 1 and a ``latest``
    of 2 must never be installed. The tags document is small, where the package document is
    megabytes (OpenCode's is 25 MB).
    """
    tags = json.loads(body)
    if not isinstance(tags, dict):
        raise ValueError("npm answered something other than a tag list")
    latest = str(tags.get("latest") or "")
    found = stable_version(latest)
    if major is None or (found is not None and found[0] == major):
        if found is None:
            raise ValueError(f"npm's latest tag is {latest[:80]!r}, not a version")
        return latest
    candidates = [v for v in (stable_version(str(value)) for value in tags.values()) if v is not None and v[0] == major]
    if not candidates:
        raise ValueError(f"npm has no release of major {major} under any tag")
    return ".".join(str(part) for part in max(candidates))


def parse_frontmatter(text: str) -> dict[str, str]:
    """The ``key: value`` lines between the two ``---`` of a Markdown agent file. Enough for
    ``name``, ``description`` and ``model``; nested YAML is not read, because nothing here needs it."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key and not key.startswith((" ", "\t", "#")):
            out[key.strip()] = value.strip().strip("\"'")
    return out


def lines_of(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def first_line(result: ExecResult) -> str:
    for text in (result.stderr, result.stdout):
        lines = lines_of(text)
        if lines:
            return lines[0][:300]
    return f"exit code {result.exit_code}"


def unique(items: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for item in items:
        if item and item not in seen:
            seen[item] = None
    return tuple(seen)


def install_method(path: str) -> str:
    """How a CLI was installed, read from where ``PATH`` found it: ``npm`` (a global npm prefix,
    nvm's included), ``native`` (Claude Code's own installer), ``installer`` (a vendor's install
    script into a directory of its own), or empty when it cannot be told."""
    if not path:
        return ""
    if f"/{NPM_PREFIX_DIR}/" in path or "/node_modules/" in path or "/.nvm/" in path or "/lib/node" in path or "/npm/" in path:
        return "npm"
    if "/.local/share/claude/" in path or path.endswith("/.local/bin/claude"):
        return "native"
    if "/.opencode/" in path or "/.grok/" in path:
        return "installer"
    return ""


def npm_prefix(path: str) -> str:
    """The npm prefix a CLI was installed under, when it is the container's own: an update goes back
    into the same one, so the updated CLI is the one ``PATH`` finds."""
    marker = f"/{NPM_PREFIX_DIR}/"
    return path[: path.index(marker) + len(marker) - 1] if marker in path else ""


# -- the tooling of one CLI ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Plan:
    """A program to run for an install or an update: exactly this argv, or a shell script in a
    terminal the operator watches (``script``), for installers that are shell pipelines."""

    argv: tuple[str, ...] = ()
    script: str = ""
    target: str = ""
    """The version this should end at; empty when the CLI's own updater picks it."""
    needs_node: bool = False


class Tooling:
    """One CLI's version, update, install, sign-in and catalog. Subclasses fill in the specifics."""

    harness: ClassVar[str]
    program: ClassVar[str]
    npm_package: ClassVar[str] = ""
    """The npm package it is published as; empty for a CLI that is not installed from npm."""
    installer_url: ClassVar[str] = ""
    """The vendor's install script, for a CLI that is installed that way."""
    sign_in: ClassVar[tuple[str, ...]] = ()
    """The CLI's own sign-in, run in a terminal the operator types into. The subscription is the
    default wherever the CLI offers a choice, because that is how these CLIs are paid for here."""
    modes: ClassVar[tuple[str, ...]] = ()
    efforts: ClassVar[tuple[str, ...]] = ()
    cheap_markers: ClassVar[tuple[str, ...]] = ()
    """Substrings that mark a model as the cheap one of its family, in order of preference: the
    self-check spends its one prompt there."""
    major: ClassVar[int | None] = None
    """Pin installs and updates to this major version."""
    latest_from_cli: ClassVar[bool] = False
    """The newest version is asked of the installed CLI itself rather than of a release feed."""

    async def run(self, env: EnvironmentPort, argv: list[str], *, cwd: str | None = None, timeout: float = VERSION_TIMEOUT_S) -> ExecResult:
        return await env.run(argv, cwd=cwd, timeout=timeout)

    async def installed(self, env: EnvironmentPort) -> InstallInfo:
        result = await self.version_run(env)
        if isinstance(result, InstallInfo):
            return result
        version = self.version_of(result.stdout)
        if not version:
            return InstallInfo(installed=False, path=result.path, detail=f"{self.program} --version printed no version: {first_line(result)}")
        return InstallInfo(installed=True, path=result.path, version=version, method=install_method(result.path))

    async def version_run(self, env: EnvironmentPort) -> ExecResult | InstallInfo:
        """``<program> --version``, or what "not installed" looks like when it cannot answer."""
        try:
            result = await self.run(env, [self.program, "--version"])
        except ProgramNotFound:
            return InstallInfo(installed=False)
        if result.exit_code != 0 or result.timed_out:
            return InstallInfo(installed=False, path=result.path, detail=f"{self.program} --version failed: {first_line(result)}")
        return result

    def version_of(self, text: str) -> str:
        found = parse_version(text)
        return ".".join(str(part) for part in found) if found else ""

    async def latest(self, env: EnvironmentPort, fetch: Fetch) -> str:
        if not self.npm_package:
            raise NotImplementedError(f"{self.harness} names no release feed")
        return parse_npm_dist_tags(await fetch(npm_tags_url(self.npm_package)), major=self.major)

    def update_plan(self, env_name: str, info: InstallInfo, latest: str) -> Plan:
        """How to bring an installed CLI to ``latest``. An npm install goes back through npm at that
        exact version (and into the same prefix), which also keeps a pinned major; anything else runs
        the CLI's own updater."""
        if info.method == "npm" and self.npm_package:
            return Plan(argv=tuple(self.npm_install_argv(npm_prefix(info.path), latest)), target=latest, needs_node=True)
        return self.own_update(latest)

    def own_update(self, latest: str) -> Plan:
        raise NotImplementedError

    def npm_install_argv(self, prefix: str, version: str) -> list[str]:
        argv = ["npm", "install", "--global", "--no-fund", "--no-audit"]
        if prefix:
            argv += ["--prefix", prefix]
        return [*argv, f"{self.npm_package}@{version}" if version else self.npm_package]

    def install_plan(self, env_name: str, home: str, latest: str) -> Plan:
        """A first install. In the container an npm CLI goes into the container's own prefix on the
        home volume; on the host it goes wherever the operator's npm installs globally."""
        if self.npm_package:
            prefix = f"{home.rstrip('/')}/{NPM_PREFIX_DIR}" if env_name == "container" and home else ""
            return Plan(argv=tuple(self.npm_install_argv(prefix, latest)), target=latest, needs_node=True)
        if self.installer_url:
            return Plan(script=installer_script(self.installer_url), target="")
        raise NotImplementedError(f"no install method for {self.harness}")

    async def login_state(self, env: EnvironmentPort) -> LoginState:
        raise NotImplementedError

    async def catalog(self, env: EnvironmentPort, cwd: str | None) -> Catalog:
        """The agents, models and choices this CLI offers: the user's own with ``cwd`` None, the
        project's (only what lives in the folder) otherwise."""
        return Catalog(modes=self.modes, efforts=self.efforts)

    def cheapest_model(self, models: Iterable[str]) -> str:
        """The model the self-check uses; empty is the CLI's default, when nothing looks cheap."""
        listed = list(models)
        for marker in self.cheap_markers:
            for model in listed:
                if marker in model.lower():
                    return model
        return ""

    async def agent_files(self, env: EnvironmentPort, directory: str, suffix: str, source: str, parse: Callable[[str, str], AgentEntry | None]) -> list[AgentEntry]:
        """Agent definitions in one directory, each read only as far as its header. A directory that
        is not there, or not under the daemon's roots, is simply no agents."""
        try:
            names = [n for n in await env.list(directory) if n.endswith(suffix)][:AGENT_FILES_MAX]
        except Exception as exc:  # noqa: BLE001 - a missing or unreadable directory is no agents
            logger.debug("no agents in %s: %s", directory, exc)
            return []
        out = []
        for name in sorted(names):
            try:
                text = (await env.read(f"{directory}/{name}", limit=AGENT_FILE_BYTES)).decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001 - one unreadable file does not hide the others
                logger.debug("agent file %s/%s unreadable: %s", directory, name, exc)
                continue
            entry = parse(name[: -len(suffix)], text)
            if entry is not None:
                out.append(AgentEntry(name=entry.name, source=source, description=entry.description[:300], model=entry.model))
        return out


def npm_tags_url(package: str) -> str:
    return f"https://registry.npmjs.org/-/package/{package.replace('/', '%2F')}/dist-tags"


def installer_script(url: str) -> str:
    """Download a vendor's install script and run it. To a file first, so a failed download fails
    the script instead of feeding ``bash`` half a script (``sh`` has no ``pipefail``)."""
    return f"""set -eu
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
curl -fsSL -o "$tmp" {url}
bash "$tmp"
"""


def _markdown_agent(stem: str, text: str) -> AgentEntry | None:
    meta = parse_frontmatter(text)
    return AgentEntry(name=meta.get("name") or stem, source="", description=meta.get("description", ""), model=meta.get("model", ""))


def _toml_agent(stem: str, text: str) -> AgentEntry | None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    return AgentEntry(name=str(data.get("name") or stem), source="", description=str(data.get("description") or ""), model=str(data.get("model") or ""))


class ClaudeTooling(Tooling):
    harness = "claude"
    program = "claude"
    installer_url = "https://claude.ai/install.sh"
    sign_in = ("claude", "auth", "login", "--claudeai")
    # The staff store keeps "default" for the mode the flag calls "manual"; the adapter maps it.
    modes = ("default", "acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions")
    efforts = ("low", "medium", "high", "xhigh", "max")
    cheap_markers = ("haiku",)
    ALIASES: ClassVar[tuple[str, ...]] = ("fable", "opus", "sonnet", "haiku")
    RELEASES_URL: ClassVar[str] = "https://downloads.claude.ai/claude-code-releases/latest"
    # Claude Code has no command that prints the models its /model picker offers, but the picker's
    # identifiers are written in the program itself, so they are read from the installed program and an
    # update brings its models without a change here. A dated identifier duplicates an undated one, and
    # anything before 4.1 is no longer offered by the picker.
    MODEL_ID: ClassVar[re.Pattern[str]] = re.compile(r"claude-(opus|fable|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?![\d-])")
    FAMILY_ORDER: ClassVar[tuple[str, ...]] = ("opus", "fable", "sonnet", "haiku")
    OLDEST: ClassVar[tuple[int, int]] = (4, 1)
    # The terminal daemon runs only the programs of its list, and git is the one on it that searches a
    # file: ``git grep --no-index`` in the file's directory reads exactly the file named and nothing
    # else. It does not follow a symbolic link, which is what ``claude`` on PATH usually is, and the
    # list has nothing that resolves one, so the places each install method keeps the real program are
    # tried by name after the path itself.
    GREP: ClassVar[tuple[str, ...]] = ("git", "--no-pager", "grep", "--no-index", "--no-color", "--text", "--only-matching", "-h", "--extended-regexp", "-e", r"claude-(opus|fable|sonnet|haiku)-[0-9]+(-[0-9]+)*", "--")
    NPM_PACKAGE_DIR: ClassVar[str] = "lib/node_modules/@anthropic-ai/claude-code"
    _scanned: ClassVar[dict[str, tuple[str, ...]]] = {}
    """Found models by environment, program path and version. The native program is a quarter of a
    gigabyte; it is read once per version rather than at every check."""

    @classmethod
    def model_ids(cls, text: str) -> tuple[str, ...]:
        """The undated model identifiers of 4.1 and newer in ``text``: by family, newest first."""
        found: dict[str, tuple[int, int, int]] = {}
        for match in cls.MODEL_ID.finditer(text):
            family, major, minor = match.group(1), int(match.group(2)), int(match.group(3) or 0)
            if (major, minor) >= cls.OLDEST:
                found[match.group(0)] = (cls.FAMILY_ORDER.index(family), -major, -minor)
        return tuple(sorted(found, key=found.__getitem__))

    def program_places(self, home: str, path: str, version: str) -> list[tuple[str, str]]:
        """Where the program behind ``path`` may be, as (directory, name to search in it): the path
        itself, the native installer's versions directory, and an npm prefix's package directory."""
        places = [(posixpath.dirname(path), posixpath.basename(path))]
        if home and version:
            places.append((f"{home.rstrip('/')}/.local/share/claude/versions", version))
        places.append((f"{posixpath.dirname(posixpath.dirname(path))}/{self.NPM_PACKAGE_DIR}", "."))
        return places

    async def models(self, env: EnvironmentPort) -> tuple[str, ...]:
        """The aliases first (each follows its family's newest), then every version the program knows.
        Only the aliases when the program cannot be found or read: they always work."""
        found = await self.version_run(env)
        if isinstance(found, InstallInfo) or not found.path.startswith("/"):
            return self.ALIASES
        version = self.version_of(found.stdout)
        key = f"{env.name}:{found.path}:{version}"
        if key not in self._scanned:
            for directory, name in self.program_places(env.home, found.path, version):
                try:
                    result = await self.run(env, [*self.GREP, name], cwd=directory, timeout=LIST_TIMEOUT_S)
                except EnvironmentUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001 - a place that is not there is the next place
                    logger.debug("no claude program to read in %s: %s", directory, exc)
                    continue
                ids = self.model_ids(result.stdout) if result.exit_code == 0 else ()
                if ids:
                    self._scanned[key] = ids
                    break
            else:
                return self.ALIASES
        return self.ALIASES + self._scanned[key]

    async def latest(self, env: EnvironmentPort, fetch: Fetch) -> str:
        return parse_plain_version(await fetch(self.RELEASES_URL))

    def own_update(self, latest: str) -> Plan:
        return Plan(argv=("claude", "update"), target=latest)

    def update_plan(self, env_name: str, info: InstallInfo, latest: str) -> Plan:
        # Claude Code's own updater handles a native and an npm install alike, and the npm package
        # is no longer how it is meant to be installed.
        return self.own_update(latest)

    async def login_state(self, env: EnvironmentPort) -> LoginState:
        result = await self.run(env, ["claude", "auth", "status", "--json"])
        try:
            data = json.loads(result.stdout)
        except ValueError:
            return LoginState("unknown", first_line(result))
        if not isinstance(data, dict):
            return LoginState("unknown", "claude auth status printed no object")
        # Only the method and the plan: the account's address and organisation are not ours to show.
        detail = " · ".join(str(data[k]) for k in ("authMethod", "subscriptionType") if data.get(k) and data.get(k) != "none")
        return LoginState("yes" if data.get("loggedIn") is True else "no", detail)

    async def catalog(self, env: EnvironmentPort, cwd: str | None) -> Catalog:
        if cwd is not None:
            agents = await self.agent_files(env, f"{cwd.rstrip('/')}/.claude/agents", ".md", "project", _markdown_agent)
            return Catalog(agents=tuple(agents))
        agents = await self.agent_files(env, f"{env.home.rstrip('/')}/.claude/agents", ".md", "user", _markdown_agent) if env.home else []
        return Catalog(agents=tuple(agents), models=await self.models(env), modes=self.modes, efforts=self.efforts)


class CodexTooling(Tooling):
    harness = "codex"
    program = "codex"
    npm_package = "@openai/codex"
    # Device authorisation works in a terminal with no browser on the same machine, which is what a
    # container is; it signs in with the ChatGPT subscription.
    sign_in = ("codex", "login", "--device-auth")
    modes = ("read-only", "workspace-write", "danger-full-access")
    # As ``codex debug models`` listed them for 0.155.1: the newest models take all six, older ones
    # fewer; ``minimal`` is gone.
    efforts = ("low", "medium", "high", "xhigh", "max", "ultra")
    # "Luna" is the fast and affordable line ("Fast and affordable model for easier tasks").
    cheap_markers = ("luna", "mini", "nano")

    def own_update(self, latest: str) -> Plan:
        return Plan(argv=("codex", "update"), target=latest)

    async def login_state(self, env: EnvironmentPort) -> LoginState:
        result = await self.run(env, ["codex", "login", "status"])
        words = first_line(result) if (result.stdout or result.stderr).strip() else ""
        if result.exit_code == 0:
            return LoginState("yes", words)
        return LoginState("no", words)

    async def catalog(self, env: EnvironmentPort, cwd: str | None) -> Catalog:
        if cwd is not None:
            return Catalog(agents=tuple(await self.agent_files(env, f"{cwd.rstrip('/')}/.codex/agents", ".toml", "project", _toml_agent)))
        models: tuple[str, ...] = ()
        result = await self.run(env, ["codex", "debug", "models"], timeout=LIST_TIMEOUT_S)
        if result.exit_code == 0:
            models = parse_codex_models(result.stdout)
        agents: list[AgentEntry] = []
        profiles: tuple[str, ...] = ()
        if env.home:
            home = f"{env.home.rstrip('/')}/.codex"
            agents = await self.agent_files(env, f"{home}/agents", ".toml", "user", _toml_agent)
            try:
                profiles = tuple(sorted(n[: -len(".config.toml")] for n in await env.list(home) if n.endswith(".config.toml")))
            except Exception as exc:  # noqa: BLE001 - no Codex home yet is no profiles
                logger.debug("no codex profiles: %s", exc)
        return Catalog(agents=tuple(agents), models=models, modes=self.modes, profiles=profiles, efforts=self.efforts)


def parse_codex_models(text: str) -> tuple[str, ...]:
    """``codex debug models`` prints JSON, ``{"models": [{"slug": …, "visibility": "list"|"hide"}]}``
    (measured on 0.155.1); a model with ``visibility: hide`` is Codex's own (a reviewer, a reserve)
    and is not offered."""
    try:
        data: Any = json.loads(text)
    except ValueError:
        return ()
    items = data.get("models") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return ()
    shown = [item for item in items if not (isinstance(item, dict) and item.get("visibility") == "hide")]
    return unique(str(item.get("slug") or item.get("id") or "") if isinstance(item, dict) else str(item) for item in shown)


class OpenCodeTooling(Tooling):
    harness = "opencode"
    program = "opencode"
    npm_package = "opencode-ai"
    sign_in = ("opencode", "auth", "login")
    # OpenCode's agents carry their own permissions; it has no mode flag to choose.
    modes = ()
    efforts = ()
    cheap_markers = ("haiku", "mini", "flash", "nano")
    major = 1

    def own_update(self, latest: str) -> Plan:
        # Always with an explicit 1.x target: a bare upgrade installs 2.x, a different program.
        return Plan(argv=("opencode", "upgrade", latest), target=latest)

    async def login_state(self, env: EnvironmentPort) -> LoginState:
        result = await self.run(env, ["opencode", "auth", "list"])
        if result.exit_code != 0:
            return LoginState("unknown", first_line(result))
        providers = parse_opencode_credentials(result.stdout)
        if not providers:
            return LoginState("no", "")
        return LoginState("yes", ", ".join(providers))

    async def catalog(self, env: EnvironmentPort, cwd: str | None) -> Catalog:
        # Run in the folder, OpenCode lists the folder's agents with the user's; the manager keeps
        # the ones the user-level list does not have as the project's.
        result = await self.run(env, ["opencode", "agent", "list"], cwd=cwd, timeout=LIST_TIMEOUT_S)
        agents = parse_opencode_agents(result.stdout, source="" if cwd else "user") if result.exit_code == 0 else ()
        if cwd is not None:
            return Catalog(agents=agents)
        models = await self.run(env, ["opencode", "models"], timeout=LIST_TIMEOUT_S)
        return Catalog(agents=agents, models=unique(lines_of(models.stdout)) if models.exit_code == 0 else ())


def parse_opencode_agents(text: str, *, source: str) -> tuple[AgentEntry, ...]:
    """``opencode agent list``: one ``name (primary|subagent)`` per line; the kind becomes the description."""
    out = []
    for line in lines_of(text):
        found = re.match(r"^([\w.@/-]+)\s*(?:\(([^)]*)\))?", line)
        if found:
            out.append(AgentEntry(name=found[1], source=source, description=found[2] or ""))
    return tuple(out)


def parse_opencode_credentials(text: str) -> tuple[str, ...]:
    """The providers ``opencode auth list`` shows credentials for, by name only (``●  Anthropic oauth``)."""
    return unique(line.lstrip("●•* ").split()[0] for line in lines_of(text) if line[:1] in "●•*" and line.lstrip("●•* "))


class PiTooling(Tooling):
    harness = "pi"
    program = "pi"
    npm_package = "@earendil-works/pi-coding-agent"
    sign_in = ("pi",)
    """pi signs in from inside its TUI (``/login``); the terminal opens it and the operator types it."""
    modes = ()
    # ``--thinking`` as ``pi --help`` lists it (0.84.2); a model without reasoning clamps it to off.
    efforts = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
    cheap_markers = ("haiku", "mini", "flash", "nano")

    def own_update(self, latest: str) -> Plan:
        # ``pi update --help``: "pi update pi  Update pi only (self works as alias to pi)" — pi alone,
        # not the packages the operator installed into it.
        return Plan(argv=("pi", "update", "self"), target=latest)

    async def login_state(self, env: EnvironmentPort) -> LoginState:
        # Which provider pi uses is in its settings (not a credential file); pi itself then says
        # whether that provider is signed in. Never ``--credentials``, which prints the token.
        provider = ""
        if env.home:
            try:
                settings = json.loads((await env.read(f"{env.home.rstrip('/')}/.pi/agent/settings.json", limit=64 << 10)).decode("utf-8", "replace") or "{}")
                provider = str(settings.get("defaultProvider") or "") if isinstance(settings, dict) else ""
            except Exception as exc:  # noqa: BLE001 - no settings yet: pi was never set up
                logger.debug("pi settings unreadable: %s", exc)
        if not provider:
            return LoginState("unknown", "no default provider is set")
        result = await self.run(env, ["pi", "auth", "check", "--provider", provider, "--json", "--no-refresh"])
        try:
            data = json.loads(result.stdout)
        except ValueError:
            data = None
        # Measured (0.84.2): ``{"status": "ready", "provider", "authType"}``, or ``{"status":
        # "not_ready", "provider", "reason": "credentials_not_configured"}`` with exit code 1.
        if isinstance(data, dict) and data.get("status") in ("ready", "not_ready"):
            ready = data["status"] == "ready"
            detail = " · ".join(str(part) for part in (provider, data.get("authType") if ready else data.get("reason")) if part)
            return LoginState("yes" if ready else "no", detail)
        return LoginState("yes" if result.exit_code == 0 else "no", provider)

    async def catalog(self, env: EnvironmentPort, cwd: str | None) -> Catalog:
        if cwd is not None:
            return Catalog()
        result = await self.run(env, ["pi", "--list-models"], timeout=LIST_TIMEOUT_S)
        return Catalog(models=parse_pi_models(result.stdout) if result.exit_code == 0 else (), efforts=self.efforts)


def parse_pi_models(text: str) -> tuple[str, ...]:
    """``pi --list-models`` prints a table ``provider  model …`` under a header; each row becomes
    ``provider/model``, the form ``--model`` takes."""
    out = []
    for line in lines_of(text):
        parts = line.split()
        if len(parts) < 2 or parts[0].lower() == "provider":
            continue
        out.append(f"{parts[0]}/{parts[1]}")
    return unique(out)


GROK_VERSION = re.compile(r"^grok (\d+\.\d+\.\d+) \(")


class GrokTooling(Tooling):
    harness = "grok"
    program = "grok"
    latest_from_cli = True
    installer_url = "https://x.ai/cli/install.sh"
    sign_in = ("grok", "login", "--device-auth")
    modes = ("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan")
    efforts = ("low", "medium", "high")
    cheap_markers = ("fast", "mini")

    async def installed(self, env: EnvironmentPort) -> InstallInfo:
        """Grok Build, and not the community ``grok-cli`` that answers to the same name: its version
        line has the build hash in brackets, and it has an updater that reports in JSON."""
        result = await self.version_run(env)
        if isinstance(result, InstallInfo):
            return result
        found = GROK_VERSION.match(result.stdout.strip())
        report = parse_grok_check((await self.run(env, ["grok", "update", "--check", "--json"])).stdout) if found else None
        if found is None or report is None:
            return InstallInfo(installed=False, path=result.path, detail=f"unsupported binary: {first_line(result)!r} is not Grok Build")
        return InstallInfo(installed=True, path=result.path, version=found[1], method=str(report.get("installer") or install_method(result.path) or "installer"))

    async def latest(self, env: EnvironmentPort, fetch: Fetch) -> str:
        result = await self.run(env, ["grok", "update", "--check", "--json"], timeout=LIST_TIMEOUT_S)
        report = parse_grok_check(result.stdout)
        if report is None:
            raise ValueError(f"grok update --check said {first_line(result)}")
        if report.get("error"):
            raise ValueError(f"grok update --check: {report['error']}")
        latest = str(report.get("latestVersion") or "")
        if stable_version(latest) is None:
            raise ValueError(f"grok update --check named {latest[:80]!r} as the latest")
        return latest

    def own_update(self, latest: str) -> Plan:
        return Plan(argv=("grok", "update", "--version", latest), target=latest)

    def update_plan(self, env_name: str, info: InstallInfo, latest: str) -> Plan:
        return self.own_update(latest)

    async def login_state(self, env: EnvironmentPort) -> LoginState:
        # ``grok inspect --json`` says nothing about sign-in (measured, 1.0.41); ``grok models`` opens
        # with one line that does: "You are logged in with grok.com.", "You are using XAI_API_KEY.",
        # or "You are not authenticated."
        result = await self.run(env, ["grok", "models"], timeout=LIST_TIMEOUT_S)
        return parse_grok_login(result.stdout)

    async def catalog(self, env: EnvironmentPort, cwd: str | None) -> Catalog:
        data = await self._inspect(env, cwd)
        agents = tuple(
            AgentEntry(name=str(a.get("name")), source="project" if cwd else _grok_source(a), description=str(a.get("description") or "")[:300], model=str(a.get("model") or ""))
            for a in (data.get("agents") or [] if isinstance(data, dict) else [])
            if isinstance(a, dict) and a.get("name") and (cwd is None or _grok_source(a) == "project")
        )
        if cwd is not None:
            return Catalog(agents=agents)
        result = await self.run(env, ["grok", "models"], timeout=LIST_TIMEOUT_S)
        return Catalog(agents=agents, models=parse_grok_models(result.stdout) if result.exit_code == 0 else (), modes=self.modes, efforts=self.efforts)

    async def _inspect(self, env: EnvironmentPort, cwd: str | None = None) -> dict[str, Any]:
        result = await self.run(env, ["grok", "inspect", "--json"], cwd=cwd)
        try:
            data = json.loads(result.stdout)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


class CursorTooling(Tooling):
    harness = "cursor"
    program = "cursor-agent"
    installer_url = "https://cursor.com/install"
    sign_in = ("cursor-agent", "login")
    modes = ("ask", "plan", "agent")
    cheap_markers = ("flash", "mini", "fast")
    latest_from_cli = True

    async def latest(self, env: EnvironmentPort, fetch: Fetch) -> str:
        # Cursor's updater publishes no check-only command. Its CLI updates itself, so the catalog
        # can identify the installed version but cannot promise a remote version before updating.
        return (await self.installed(env)).version

    def own_update(self, latest: str) -> Plan:
        return Plan(argv=("cursor-agent", "update"), target="")

    async def login_state(self, env: EnvironmentPort) -> LoginState:
        result = await self.run(env, ["cursor-agent", "status"])
        line = first_line(result)
        if result.exit_code == 0 and "Logged in" in line:
            # Cursor prints an email address here. The harness catalog exposes only the state.
            return LoginState("yes")
        if "not logged" in line.lower() or "not authenticated" in line.lower():
            return LoginState("no")
        return LoginState("unknown", "Cursor did not report its sign-in state")

    async def catalog(self, env: EnvironmentPort, cwd: str | None) -> Catalog:
        if cwd is not None:
            return Catalog()
        result = await self.run(env, ["cursor-agent", "models"], timeout=LIST_TIMEOUT_S)
        models = parse_cursor_models(result.stdout) if result.exit_code == 0 else ()
        return Catalog(models=models, modes=self.modes)


def parse_cursor_models(text: str) -> tuple[str, ...]:
    """The model IDs before `` - `` in Cursor Agent's `models` output."""
    names = []
    for line in lines_of(text):
        name, sep, _ = line.strip().partition(" - ")
        if sep and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            names.append(name)
    return unique(names)


def _grok_source(agent: dict[str, Any]) -> str:
    """Where ``grok inspect`` says an agent comes from: ``{"type": "builtin"}`` in 1.0.41."""
    source = agent.get("source")
    kind = source.get("type") if isinstance(source, dict) else source
    return str(kind or "user")


def parse_grok_login(text: str) -> LoginState:
    first = next(iter(lines_of(text)), "")
    if "not authenticated" in first:
        return LoginState("no", "")
    if "logged in with" in first:
        return LoginState("yes", first.split("logged in with", 1)[1].strip().rstrip("."))
    if "XAI_API_KEY" in first:
        return LoginState("yes", "API key")
    return LoginState("unknown", first[:120])


def parse_grok_models(text: str) -> tuple[str, ...]:
    """``grok models``: the sign-in line, "Default model: …", then "  * name (default)" and "  - name"."""
    names = []
    for line in lines_of(text):
        stripped = line.strip()
        if stripped[:2] in ("* ", "- "):
            names.append(stripped[2:].split(" (")[0].strip())
    return unique(names)


def parse_grok_check(text: str) -> dict[str, Any] | None:
    """``grok update --check --json``: ``{currentVersion, latestVersion, updateAvailable, installer,
    channel, autoUpdate, error}``; None when it is not that."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or "latestVersion" not in data or "currentVersion" not in data:
        return None
    return data


TOOLING: dict[str, Tooling] = {t.harness: t for t in (ClaudeTooling(), CodexTooling(), OpenCodeTooling(), PiTooling(), GrokTooling(), CursorTooling())}
"""One per command-line harness, in the capability table's order."""


def tooling(harness: str) -> Tooling:
    try:
        return TOOLING[harness]
    except KeyError:
        raise KeyError(f"no command-line harness is called {harness!r}") from None


__all__ = [
    "NODE_SHA256",
    "NODE_VERSION",
    "NPM_PREFIX_DIR",
    "TOOLING",
    "ClaudeTooling",
    "CodexTooling",
    "Fetch",
    "GrokTooling",
    "OpenCodeTooling",
    "PiTooling",
    "Plan",
    "Tooling",
    "install_method",
    "installer_script",
    "node_install_script",
    "npm_prefix",
    "parse_codex_models",
    "parse_frontmatter",
    "parse_grok_check",
    "parse_grok_login",
    "parse_grok_models",
    "parse_npm_dist_tags",
    "parse_opencode_agents",
    "parse_opencode_credentials",
    "parse_pi_models",
    "parse_plain_version",
    "stable_version",
    "tooling",
]
