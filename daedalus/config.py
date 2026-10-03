"""Configuration: immutable environment settings and the mutable runtime config.

Two layers, deliberately separate:

* :class:`Settings` — secrets and machine facts, read once from the environment
  (``.env`` in development, container env in production). Never edited by the agent.
* :class:`RuntimeConfig` — everything the owner may change while the bot runs
  (model, thinking, spend thresholds, approval mode, ...). Stored as ``config.toml``
  on the state volume and edited through the bot commands and the Mini App.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def native_mode() -> bool:
    """Whether this process is the agent running on the operator's own machine rather than in a
    container. The launcher sets ``DAEDALUS_NATIVE`` when it starts the supervisor natively.

    It is read from the environment and not from the configuration: the difference it makes is what
    the paths and the defaults are, and those are decided before a configuration file is read.
    """
    return os.environ.get("DAEDALUS_NATIVE", "").strip().lower() in ("1", "true", "yes", "on")


def native_sandbox_default() -> str:
    """Whether Exec is confined by default on this installation.

    Natively on Linux, where bubblewrap is installed **and can actually create a namespace**, it is.
    Outside Docker bubblewrap needs no added capability and no relaxed seccomp profile, and there is
    no container around the agent to be the boundary in its place, so the one wall that is available
    is up by default.

    The presence of the binary is not the question, and asking it that way made a fresh installation
    dead on arrival: Ubuntu 24.04 and Debian 13 forbid unprivileged user namespaces by default and
    pull ``bwrap`` in with flatpak, Steam and half the desktops, and the sandbox fails closed — so
    every Exec was refused on a machine that had never been asked about a sandbox at all. So the
    default is decided by a real probe, run once and cached for the life of the process, and a
    machine that cannot sandbox starts with the sandbox off and the policy rules, which is what
    macOS, Windows and a container get too.
    """
    if not (native_mode() and sys.platform.startswith("linux")):
        return "off"
    from daedalus.tools.shell import bwrap_status  # Lazy: the tools package imports this module

    return "workspace" if bwrap_status() == "ok" else "off"


def env_path(name: str) -> Path | None:
    """A path the launcher put in the environment, or ``None`` where it did not."""
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else None


CONTAINER_KEYPROXY_BASE = "http://keyproxy:3200"
"""Where the key proxy answers inside a compose project: a service name on a private network."""

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
"""Addresses that mean "this machine", where a key proxy is indistinguishable from anything else."""


def keyproxy_configured() -> bool:
    """Whether this process was told where its key proxy is, rather than assuming the container's."""
    return bool(os.environ.get("KEYPROXY_BASE_URL", "").strip())


def keyproxy_base() -> str:
    """Where the key proxy answers. In a container it is a service name on a private network; on a
    machine it is a port on the loopback interface, and the launcher says which."""
    return os.environ.get("KEYPROXY_BASE_URL", "").strip().rstrip("/") or CONTAINER_KEYPROXY_BASE


def is_keyproxy_url(url: str) -> bool:
    """Whether a base URL is an address on this installation's key proxy — and nothing else.

    The bot sends the proxy its own API token, so this decides where a credential goes and is
    written as such: an address is the proxy's when it is under the one base this process was
    given, and the word ``keyproxy`` appearing anywhere in a URL means nothing at all. Testing for
    the word sent the token to whatever host an operator typed into *Add a model* — and, before
    that, made every native install look as if it held its own keys, because a loopback address
    says nothing about what is behind it and the proxy was never asked.
    """
    if not url:
        return False
    base = keyproxy_base()
    return url == base or url.startswith(base + "/")


def keyproxy_unresolved(url: str) -> bool:
    """Whether this may be the key proxy at an address this process was never told about.

    A native installation reaches the proxy on a loopback port that the launcher passes in the
    environment. A bot started without it — a unit file, a bare ``python -m daedalus``, a shell
    that did not come from the launcher — cannot tell a proxy port from any other local server, and
    the honest answer about such an endpoint is that nobody knows. It is not asked for keys and it
    is not called ready; the doctor says why.
    """
    if not url or is_keyproxy_url(url) or keyproxy_configured():
        return False
    host = urlsplit(url).hostname or ""
    return host in LOOPBACK_HOSTS or host == "keyproxy"


def keyproxy_upstream(url: str) -> str:
    """The upstream name a key-proxy base URL addresses (``…:3200/deepseek`` → ``deepseek``), or ""."""
    if not is_keyproxy_url(url):
        return ""
    tail = url[len(keyproxy_base()) :].strip("/")
    return tail.split("/", 1)[0] if tail else ""


def _runtime_paths() -> list[str]:
    """The directories this installation is made of, as the environment has them.

    The container's are fixed, so a constant could name them. A native installation's are wherever
    the operator dropped the launcher, and a constant naming ``/srv`` there would protect nothing
    while looking exactly as if it did.
    """
    named = [
        os.environ.get("STATE_DIR", "/srv/state"),
        os.environ.get("BOT_REPO_DIR", "/srv/daedalus"),
        os.environ.get("CORE_REPO_DIR", "/srv/protocore-exp"),
    ]
    if runtime_dir := os.environ.get("DAEDALUS_RUNTIME", "").strip():
        named.append(runtime_dir)
    if local_dir := os.environ.get("DAEDALUS_LOCAL", "").strip():
        named.append(local_dir)
    return [str(Path(path)) for path in named if path.strip()]


SYSTEM_NEVER_WRITABLE = ("/etc/ssl", "/usr/local", "/var/lib", "/opt/launcher", "/opt/agent-python", "/opt/dependency-recipe", "/run/daedalus", "/run/daedalus-rebuild")
"""Directories of the machine or the image that a sandbox is never opened onto, whichever mode this is."""


def sandbox_never_writable() -> frozenset[str]:
    """Directories the sandbox may never be told to write, for this installation.

    The agent's own state, its checkouts and the runtime it runs out of, plus the system paths above.
    Computed rather than written down, because in native mode the first three move with the
    installation and a fixed list would name directories that are not there.
    """
    return frozenset(SYSTEM_NEVER_WRITABLE) | frozenset(_runtime_paths())

ReasoningEffort = Literal["low", "medium", "high", "xhigh"]
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = ("low", "medium", "high", "xhigh")
"""Efforts a preset or a session may ask for. Hosted vendors map these onto their own names;
Bonsai's chat template only knows ``low``, ``medium`` and ``xhigh`` (and defaults to xhigh)."""
ApprovalMode = Literal["manual", "auto"]
ScheduleTopicMode = Literal["per_task", "per_run"]
TelegramMode = Literal["topics", "private"]

ProviderKind = Literal["deepseek", "openrouter", "opencode", "vllm", "llamacpp", "openai_compat"]
PROVIDER_KINDS: tuple[ProviderKind, ...] = ("deepseek", "openrouter", "opencode", "vllm", "llamacpp", "openai_compat")
"""``opencode`` is the OpenCode Go / Zen gateway: OpenAI-compatible, wants a stable session id per conversation in
``x-opencode-session`` for routing and prompt caching, and passes DeepSeek's thinking fields through as they are."""


class Settings(BaseSettings):
    """Environment-only settings."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("owner_user_id", "api_port", "usd_per_day", mode="before")
    @classmethod
    def _blank_means_default(cls, value: object, info: Any) -> object:
        """``OWNER_USER_ID=`` in an env file is an unset value, not the number "".

        A setup that writes every key it knows (the desktop launcher, a copied env.example) leaves the
        Telegram ones blank on an installation without Telegram; the bot must start on that."""
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value

    @field_validator("boot_id", mode="before")
    @classmethod
    def _stripped(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    telegram_bot_token: str = ""
    bench_state_dir: Path | None = None
    """State directory the Harbor adapter uses (``BENCH_STATE_DIR``): its own config.toml and database, never the bot's."""
    telegram_api_base: str = "https://api.telegram.org"
    telegram_local_mode: bool = False
    """True when ``telegram_api_base`` points at a local Bot API server (``--local``)."""
    owner_user_id: int = 0

    deepseek_api_key: str = ""
    openrouter_api_key: str = ""
    vllm_base_url: str = ""
    vllm_api_key: str = ""
    github_token: str = ""
    github_daedalus_token: str = ""
    """A token for the agent's own GitHub organisation (repositories it creates for its work)."""
    daedalus_github_org: str = ""
    """That organisation's login; empty = the agent has no organisation of its own."""
    telegram_api_hash: str = ""
    """Only the local Bot API server needs it; the bot reads it so the redactor can mask it."""

    native: bool = Field(default_factory=native_mode)
    """True when the agent is a process on the operator's machine rather than a container. It changes
    what the defaults are — the paths, the address services are reached at, the sandbox — and what the
    doctor has to say about the checks that only mean something inside a container."""

    state_dir: Path = Path("/srv/state")
    workspaces_dir: Path = Path("/srv/workspaces")
    bot_repo_dir: Path = _REPO_ROOT
    core_repo_dir: Path = _REPO_ROOT.parent / "protocore-exp"
    supervisor_socket: Path = Path("/run/daedalus/supervisor.sock")
    supervisor_tcp: str = ""
    """``host:port`` the supervisor listens on where a unix socket will not do — Windows, and a socket
    path too long for the system (a deep folder on macOS); empty otherwise, and then the socket above
    is what is used. One or the other, never both."""
    boot_id: str = Field(default="", validation_alias="DAEDALUS_BOOT_ID")
    """The id the desktop launcher gives each start of the supervisor, which the bot inherits and
    echoes on /app: how the launcher tells this process's answer from one by a stack a crashed
    launcher left on the same port. Random per start, not a secret; empty elsewhere. Read from
    ``DAEDALUS_BOOT_ID`` only: without the alias the settings would also take a bare ``BOOT_ID``
    from whatever environment the process inherited."""
    runtime_dir: Path | None = Field(default_factory=lambda: env_path("DAEDALUS_RUNTIME"))
    """The portable runtime a native installation runs out of: the interpreter executing this process,
    the environment it imports from, and the binaries the tools call. ``None`` in a container, where
    the image carries all three and no directory of the installation's own has to be named."""
    local_dir: Path | None = Field(default_factory=lambda: env_path("DAEDALUS_LOCAL"))
    """The native installation's local state, outside the data folder beside the runtime: the logs,
    the terminal and browser daemons' endpoints and tokens, the browser profiles with their logins.
    Sealed like the runtime; ``None`` in a container."""
    secrets_override: Path | None = Field(default_factory=lambda: env_path("DAEDALUS_SECRETS"))
    """Where the launcher keeps the provider keys, when that is not the default place under the state
    directory. Natively they sit beside the checkouts instead, so that no project root and no mount
    can reach them; the launcher says where, because the launcher is what wrote the file."""
    launcher_path: Path | None = Field(default_factory=lambda: env_path("DAEDALUS_LAUNCHER"))
    """The launcher's own executable, natively. It starts this process, restarts it and can replace
    it, which is the whole reason the agent is not allowed to touch it."""
    env_file_path: Path | None = Field(default_factory=lambda: env_path("DAEDALUS_ENV_FILE"))
    """The environment file the launcher owns, natively. It holds the Telegram bot token and the API
    hash — full control of one of the two front doors — and is therefore the installation's, not the
    agent's. ``None`` in a container, where the file is interpolated into a compose project the agent
    cannot reach anyway."""

    remote_ssh_identity_file: Path | None = Field(default=None, validation_alias="DAEDALUS_REMOTE_SSH_IDENTITY_FILE")
    """Operator-provided client key for explicitly paired remote hosts; absent disables SSH probes and transfers."""

    rebuild_trigger_dir: Path = Path("/run/daedalus-rebuild")
    """Shared with the rebuilder sidecar — the only container that can reach Docker. It is a rebuild
    channel only while something is on the other end of it, which the sidecar says by keeping a
    heartbeat in this directory fresh."""

    chrome_path: str = ""
    """The headless Chromium the browser skills drive (``CHROME_PATH``). Set by the image tag that
    carries one and empty in the one that does not; outside a container it names a browser already
    installed, or stays empty and the skills say the tools are not there."""
    playwright_browsers_path: str = ""
    """Where Playwright keeps its browsers (``PLAYWRIGHT_BROWSERS_PATH``); empty = its own default,
    which is a cache directory under the home folder."""

    api_host: str = "127.0.0.1"
    api_port: int = 8765
    services_port_range: str = "8100-8119"
    """Ports a session's services may listen on; the compose file publishes the same range from the container."""
    services_public_host: str = Field(default_factory=lambda: "127.0.0.1" if native_mode() else "")
    """The address the operator reaches those ports at (the docker host on the LAN); empty = shown as <host>.
    Natively there is no host but this one: a service the agent starts listens on the loopback interface of
    the operator's own machine, and that is the address to open."""
    miniapp_public_url: str = ""

    terminals_container_dir: Path | None = None
    """The run directory of the terminal daemon of the ``container`` environment (a volume this
    container shares with the terminals service): its endpoint, its token and its socket. Unset = no
    container terminals on this installation."""
    terminals_host_dir: Path | None = None
    """The same for the ``host`` environment: the operator's own machine, reached through a directory
    bind-mounted from it, or natively a directory the launcher gives the daemon. An empty directory
    reads as "not installed"."""
    terminals_port_range: str = "8120-8139"
    """Ports a server started in a container terminal is published on; the compose file publishes the
    same range from the terminals service. The agent's own services use ``services_port_range``."""
    browser_container_dir: Path | None = None
    """The run directory of the browser daemon of the ``container`` environment (a volume this
    container shares with the ``browser`` compose service): its endpoint, its token and its socket.
    Unset = no container browser; an empty directory = the service is not running."""
    browser_host_dir: Path | None = None
    """The same for the ``host`` environment: natively, a directory the launcher gives the browser
    daemon it starts beside the agent."""

    usd_per_day: float = 20.0
    """Daily spend cap. Enforced by the supervisor from its own environment, never from config.toml."""

    @property
    def supervisor_address(self) -> str:
        """Where the supervisor is asked for a restart, a rebuild or a rollback: a unix socket path,
        or ``tcp://host:port`` where the platform has no unix sockets."""
        if tcp := self.supervisor_tcp.strip():
            return f"tcp://{tcp}"
        return str(self.supervisor_socket)

    @property
    def sandbox_never_writable(self) -> frozenset[str]:
        """This installation's own directories, which a sandbox is never opened onto."""
        return frozenset(SYSTEM_NEVER_WRITABLE) | {str(self.state_dir), str(self.bot_repo_dir), str(self.core_repo_dir)}

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.toml"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "daedalus.sqlite"

    @property
    def blobs_dir(self) -> Path:
        return self.state_dir / "blobs"

    @property
    def snapshots_dir(self) -> Path:
        return self.state_dir / "snapshots"

    @property
    def secrets_dir(self) -> Path:
        return self.secrets_override or self.state_dir / "secrets"

    @property
    def worktrees_dir(self) -> Path:
        """Where self-development cuts its worktrees: beside the state directory, never inside it.

        A worktree is the agent's work — it edits, tests and commits there, and the session that
        opened it writes to it under the sandbox. The state directory is the installation, sealed
        whole so that the database, the journal, ``config.toml`` and the sign-in link cannot be read
        or written by the agent at all. Putting the work inside the sealed tree makes the two
        statements contradict each other, and the seal is the one that wins: every read, write and
        command in the worktree is refused. So the worktrees sit next to the state directory rather
        than in it, which is also where the workspaces and the checkouts already are.
        """
        return self.state_dir.parent / "worktrees"

    @property
    def supervisor_token_path(self) -> Path:
        """The shared secret the restart channel is opened with, where that channel is a loopback port
        rather than a socket file. A file with an owner is what a port does not have."""
        return self.state_dir / "supervisor.token"

    @property
    def sealed_paths(self) -> tuple[Path, ...]:
        """What the agent may neither read nor write: the parts of the installation that are the
        installation rather than its work.

        The provider keys, the whole state directory, the launcher's own executable, the file it
        keeps the Telegram credentials in, and the runtime the process is executing out of. In a
        container none of this needs saying — the keys are in another container, the state is a
        volume the policy already refuses to write, and there is no launcher binary to protect. On
        the operator's machine the agent runs as the operator, and a file mode protects nothing from
        a process that owns it.

        The state directory is sealed whole rather than by naming the files inside it. Enumerating
        them sealed the database and left beside it the rollback journal that holds the same rows,
        ``config.toml``, the session files and the one-shot link that signs the operator in — the
        installation itself, none of which is the agent's work.
        """
        paths = [self.secrets_dir, self.state_dir]
        if self.runtime_dir is not None:
            paths.append(self.runtime_dir)
        if self.local_dir is not None:
            paths.append(self.local_dir)
        if self.launcher_path is not None:
            paths.append(self.launcher_path)
        if self.env_file_path is not None:
            paths.append(self.env_file_path)
        # The launcher's handover file. It carries the token that authorises start, stop, update,
        # apply and the runtime extras — the whole installation — and it sits beside the state
        # directory rather than inside it, so sealing the state directory does not cover it. The app
        # reads it to ask the launcher for a component; the agent asks the app.
        if self.native:
            paths.append(self.state_dir.parent / "launcher.json")
            # What the launcher keeps to put the data back after an update: the verified backups and
            # the upgrade's own folder inside the data folder, and beside it the control folder of the
            # fenced switch, whose kept copies are whole copies of the data folder — the provider keys,
            # the state directory and launcher.json among them. Sealing the live files and leaving
            # their copies readable one folder over would seal nothing.
            data = self.state_dir.parent
            paths.extend((data / "backups", data / "upgrade", data.parent / ".daedalus-update"))
            # A copy a switch left beside the data folder (.<data>-slot-<op>) is a whole copy too. One
            # exists only while a switch runs — the agent is stopped then — or after a crash, when
            # nothing starts until `update resolve` has filed it under the control folder above; the
            # ones present at a start are named, and that is every one the agent could ever meet.
            paths.extend(sorted(data.parent.glob(f".{data.name}-slot-*")))
        paths.extend(self.sealed_everywhere)
        return tuple(paths)

    @property
    def sealed_everywhere(self) -> tuple[Path, ...]:
        """The part of the sealed set a command may not name even inside a container.

        The terminal daemons' and the browser daemons' run directories. A terminal daemon's token
        opens a shell — on the host environment, a shell on the operator's own machine, outside every
        wall the agent's container is. A browser daemon's token drives browsers holding the logins
        made in them, with none of the policy's confirmations, and reads what any of them shows. So
        unlike the rest of the sealed set, a container is no boundary for them: the directories are
        mounted into this container precisely so the app can reach the daemons.
        """
        dirs = (self.terminals_container_dir, self.terminals_host_dir, self.browser_container_dir, self.browser_host_dir)
        return tuple(p for p in dirs if p is not None)

    @property
    def skills_dir(self) -> Path:
        return self.bot_repo_dir / "skills"


NO_MODEL_MESSAGE = "No model is configured yet. Add one in the app: Settings \u2192 Models \u2192 Add a model."
"""What every entry point says when the preset table is empty. A fresh install ships no model:
which one to run is the operator's first decision, not a guess made for them."""


class NoModelConfigured(RuntimeError):
    """Raised where a model is needed and none is configured. A RuntimeError, so the callers that
    already turn a "this run cannot start" into a message (the API's 409, the chat reply) need
    nothing new."""

    def __init__(self, message: str = NO_MODEL_MESSAGE) -> None:
        super().__init__(message)


class ModelConfig(BaseModel):
    """Which model preset runs by default, and which ones stand in when it fails."""

    preset: str = ""
    """Empty until the operator adds one; the first preset in the table stands in for it."""
    chain: list[str] = Field(default_factory=list)
    """Fallback preset ids tried in order after the default one."""


class ModelPresetConfig(BaseModel):
    """A named model: a client (provider endpoint), a model id and everything about how it is run.

    Presets are the unit the operator picks, as the global default and per session; provider
    entries only describe endpoints (URL, key, timeout, prices).
    """

    provider: str = ""
    model: str = ""
    label: str = ""
    """Display label; empty shows ``provider/model``."""
    thinking: bool = True
    reasoning_effort: ReasoningEffort = "medium"
    images: bool = False
    """The model accepts images (needed for ImageView and for photos sent in chat)."""
    context_window: int = Field(default=128_000, ge=8_000, le=4_000_000)
    """Tokens of history a run may hold before compaction; set below the model's real window to keep runs cheap."""
    max_output_tokens: int = Field(default=32_000, ge=1_024, le=1_000_000)
    """Cap on one reply (``max_tokens``); thinking tokens count against it."""
    on_demand_tool_groups: bool | None = None
    """Whether the tool groups set to load on demand are held back for runs on this model. ``False`` gives
    every such group its tools up front ("when it fits"), except where a session chose on demand for a
    group itself; ``None`` takes the model's known behaviour (:func:`on_demand_tool_groups_for`). Some
    models load a held-back group when a task needs it; others never look, and do the job with a shell,
    git or another server's tools instead, which costs more than the definitions saved."""

    def display(self, preset_id: str = "") -> str:
        if self.label:
            return self.label
        return f"{self.provider}/{self.model}" if self.provider else preset_id


class ProviderConfig(BaseModel):
    """One configured provider endpoint (all OpenAI-compatible)."""

    kind: ProviderKind = "openai_compat"
    name: str = ""
    """Optional display name for this endpoint; empty shows its configuration id."""
    base_url: str = ""
    api_key: str = ""
    """Optional key for this endpoint, stored in ``config.toml`` on the state volume
    (masked in the Mini App, never echoed back). Empty means "no key" for self-hosted
    endpoints and "use the environment key" for the built-in kinds (deepseek, openrouter, vllm)."""
    timeout_seconds: float = 600.0
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    """Sampling temperature sent with every request to this endpoint; ``None`` leaves the core's default. Pin it for benchmarks."""
    max_advertised_tools: int | None = Field(default=None, ge=0)
    """The most tools this endpoint accepts in one request; ``None`` takes the known limit of the model
    (:func:`max_advertised_tools_for`), 0 means no limit. Past it the core holds tool groups back behind
    ToolSearch, MCP servers first, rather than send a request the endpoint refuses."""
    pricing: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per-model USD per 1M tokens overriding the built-in table (``daedalus.providers.pricing``):
    ``{"model": {"input", "output", "cache_hit"[, "*_off_peak", "peak_utc", "peak_weekdays_only"]}}``.
    A model with no price anywhere is recorded with an unknown cost."""


KNOWN_TOOL_LIMITS: tuple[tuple[str, int], ...] = (("grok", 350), ("gemini", 128))
"""Models whose API refuses a request with more tools than this, by a word of the model id. Grok answers
"Maximum tools limit reached" above 350; Gemini accepts at most 128 function declarations. Reached through
any endpoint (a router, a key proxy), so it is the model that decides, not the provider's kind."""


def max_advertised_tools_for(provider: ProviderConfig | None, model: str) -> int:
    """The most tools one request to ``model`` on ``provider`` may carry, 0 for no limit."""
    if provider is not None and provider.max_advertised_tools is not None:
        return provider.max_advertised_tools
    lowered = model.lower()
    return min((limit for word, limit in KNOWN_TOOL_LIMITS if word in lowered), default=0)


ON_DEMAND_GROUPS_OFF: tuple[str, ...] = ("qwen",)
"""Models that do worse with tool groups held back until asked for, by a word of the model id, as
``KNOWN_TOOL_LIMITS`` names models by one. Given the choice, Qwen did the job the long way — git in a
shell for a pull request against itself, a list of subagents for "pause the loop", another server's
browser — often enough to lose more tasks than the saved definitions are worth, where with the tools in
front of it it used them. Grok and DeepSeek load the group and do as well either way, for half the input."""


def on_demand_tool_groups_for(preset: ModelPresetConfig | None, model: str) -> bool:
    """Whether runs on ``model`` hold the on-demand groups back: the preset's own choice, else the model's.

    ``preset`` is the one the run was configured from, and its choice counts only while it describes the
    model that runs — a provider and model picked by hand run with the default preset's settings, and that
    preset's switch is about another model.
    """
    if preset is not None and preset.on_demand_tool_groups is not None and preset.model == model:
        return preset.on_demand_tool_groups
    lowered = model.lower()
    return not any(word in lowered for word in ON_DEMAND_GROUPS_OFF)


class PromptConfig(BaseModel):
    """Operator-editable part of the system prompt."""

    rules: str = ""
    """Working rules appended after the persona; empty means the built-in default text."""


class VisionConfig(BaseModel):
    """Model used by the ImageView tool (cheap, fast, image-capable)."""

    preset: str = ""
    """A preset with ``images = true``; empty picks the first image-capable preset."""
    max_output_tokens: int = Field(default=2000, ge=100, le=32_000)


class SearxngSearchConfig(BaseModel):
    """A self-hosted SearXNG instance (``deploy/searxng``), reached over the private network."""

    url: str = "http://searxng:8080"
    engines: str = ""
    """Comma-separated engine names sent with every query; empty = the instance's own defaults
    (``deploy/searxng/settings.yml`` already narrows them to the engines that answer)."""
    categories: str = "general"
    safesearch: int = Field(default=0, ge=0, le=2)


class DuckDuckGoSearchConfig(BaseModel):
    """DuckDuckGo's HTML page, scraped directly; the fallback that needs nothing installed."""

    url: str = "https://html.duckduckgo.com/html/"
    region: str = "wt-wt"


class SerperSearchConfig(BaseModel):
    """Google results through serper.dev; the key lives in the key proxy (``KEYPROXY_KEY_SERPER``)."""

    base_url: str = Field(default_factory=lambda: keyproxy_base() + "/serper")
    gl: str = ""
    """Country code for Google (``ru``, ``us``); empty = Google's default."""
    hl: str = ""
    """Interface language when the query names none."""


class KeenableSearchConfig(BaseModel):
    base_url: str = Field(default_factory=lambda: keyproxy_base() + "/keenable")
    snippet_max_length: int = Field(default=600, ge=180, le=10_000)


class TavilySearchConfig(BaseModel):
    base_url: str = Field(default_factory=lambda: keyproxy_base() + "/tavily")
    depth: Literal["basic", "advanced", "fast", "ultra-fast"] = "basic"


class ExaSearchConfig(BaseModel):
    base_url: str = Field(default_factory=lambda: keyproxy_base() + "/exa")
    type: Literal["auto", "instant", "fast", "deep"] = "auto"


class PerplexitySearchConfig(BaseModel):
    base_url: str = Field(default_factory=lambda: keyproxy_base() + "/perplexity")


class WebSearchConfig(BaseModel):
    """Which search backend answers ``WebSearch`` and what to try when it fails.

    Every backend maps onto the same tool contract, so switching here changes nothing the
    model sees. Keyed backends point at the key proxy and stay unavailable until the key
    is in ``keyproxy.env``.

    The default is the one that needs nothing installed. SearXNG answers better, but it is a
    container and 382 MB of it, so it is a choice an operator makes (``--profile search``) rather
    than the price of having a search tool at all; where it runs, it is the first fallback.
    """

    backend: str = "duckduckgo"
    fallback: list[str] = Field(default_factory=lambda: ["searxng"])
    """Tried in order when the backend errors or returns nothing."""
    results: int = Field(default=8, ge=1, le=30)
    timeout_seconds: float = Field(default=30.0, ge=1, le=600)
    searxng: SearxngSearchConfig = Field(default_factory=SearxngSearchConfig)
    duckduckgo: DuckDuckGoSearchConfig = Field(default_factory=DuckDuckGoSearchConfig)
    serper: SerperSearchConfig = Field(default_factory=SerperSearchConfig)
    keenable: KeenableSearchConfig = Field(default_factory=KeenableSearchConfig)
    tavily: TavilySearchConfig = Field(default_factory=TavilySearchConfig)
    exa: ExaSearchConfig = Field(default_factory=ExaSearchConfig)
    perplexity: PerplexitySearchConfig = Field(default_factory=PerplexitySearchConfig)


class WebToolsConfig(BaseModel):
    """WebFetch / WebSearch behaviour."""

    fetch_timeout_seconds: float = Field(default=60.0, ge=1, le=600)
    proxy: str = ""
    """HTTP(S)/SOCKS proxy URL for WebFetch and the directly scraped search backends, e.g. ``socks5://127.0.0.1:1080``; empty = direct."""
    user_agent: str = "Mozilla/5.0 (X11; Linux x86_64) Daedalus/0.1"
    fetch_max_chars: int = Field(default=40_000, ge=1_000, le=500_000)
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


SANDBOX_NEVER_WRITABLE = frozenset({"/srv/state", "/srv/daedalus", "/srv/protocore-exp"}) | frozenset(SYSTEM_NEVER_WRITABLE)
"""The container's version of the list, kept as the name the rest of the code and the tests know.
The one the validator really asks is :func:`sandbox_never_writable`, which adds this installation's
own directories — in a container they are the same three."""


class ExecToolsConfig(BaseModel):
    """Exec / Read / Find output handling (the timeout itself is ``limits.tool_timeout_seconds``)."""

    max_output_chars: int = Field(default=60_000, ge=2_000, le=1_000_000)
    sandbox: Literal["off", "workspace"] = Field(default_factory=native_sandbox_default)
    """``workspace``: run Exec inside bubblewrap with the whole filesystem read-only except the session
    workspace and a private /tmp, in its own PID namespace. Needs ``bwrap`` in the image; falls back to
    an unsandboxed run with a warning when it is missing. It defaults to ``workspace`` on a native
    Linux installation, where nothing else stands between Exec and the operator's machine, and to
    ``off`` where a container does."""
    sandbox_extra_writable: list[str] = Field(default_factory=list)
    """Extra paths the sandbox may write (e.g. the bot repository worktrees for self-development)."""

    @field_validator("sandbox_extra_writable")
    @classmethod
    def _writable_paths_are_specific(cls, value: list[str]) -> list[str]:
        for raw in value:
            path = Path(raw)
            if not path.is_absolute():
                raise ValueError(f"sandbox_extra_writable entries must be absolute paths: {raw!r}")
            if len(path.parts) < 3 or str(path) in (SANDBOX_NEVER_WRITABLE | sandbox_never_writable()):
                raise ValueError(f"{raw!r} would open too much to the sandbox; name a specific directory")
        return value


class ResultToolsConfig(BaseModel):
    """What happens to a tool result once the agent has moved past it.

    ``[tools.exec] max_output_chars`` bounds what ONE call returns. It says
    nothing about the twenty results already in the transcript, which the
    request carries again on every turn — which is how a handful of large reads
    fills the window. These three bound that: past the fresh window, a long
    result is cut to its head in the request only, and the whole result stays
    in the stored history.
    """

    fresh_count: int = Field(default=6, ge=0, le=200)
    """How many of the newest tool results are always shown whole — the window in which the
    agent is still working from what it read."""
    stale_max_chars: int = Field(default=2_000, ge=200, le=100_000)
    """Head kept of an older result that is longer than this; shorter ones are never cut."""
    trim_batch_chars: int = Field(default=40_000, ge=0, le=5_000_000)
    """How much trimmable excess must build up before any trimming happens. Every trim moves the
    prompt prefix and costs a cache miss on the whole request, so it is done in batches."""


class ToolGroupConfig(BaseModel):
    """The operator's choice for one group of host tools, ``[tools.groups.<name>]``."""

    model_config = ConfigDict(extra="forbid")
    load: Literal["eager", "auto", "lazy"]
    """``eager``: always in the tool list. ``auto``: in the list while the definitions fit the window,
    held back behind ToolSearch first when they do not. ``lazy``: only the group's catalogue line is
    in the prompt and the model loads it with ToolSearch when a task needs it."""


class ToolsConfig(BaseModel):
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolsConfig = Field(default_factory=ExecToolsConfig)
    results: ResultToolsConfig = Field(default_factory=ResultToolsConfig)
    groups: dict[str, ToolGroupConfig] = Field(default_factory=dict)
    """Only the groups the operator changed; the rest keep the default their declaration carries
    (``daedalus.tools.TOOL_GROUPS``), so a better default reaches every installation that never chose."""

    @field_validator("groups", mode="before")
    @classmethod
    def _known_groups(cls, value: Any) -> Any:
        """Drop the entries for groups this version does not have, with a warning.

        Refused, a group renamed or removed in an update failed the whole configuration: the
        installation could not load its settings at all over a choice about something that no longer
        exists. Dropped before the entry itself is read, so whatever it held cannot fail either."""
        if not isinstance(value, dict):
            return value
        from daedalus.tools import TOOL_GROUPS  # Lazy: config is a leaf the launcher reads without the core

        unknown = sorted(str(name) for name in value if name not in TOOL_GROUPS)
        if unknown:
            logger.warning("ignoring [tools.groups] for unknown tool group(s) %s; known: %s", ", ".join(unknown), ", ".join(sorted(TOOL_GROUPS)))
        return {name: entry for name, entry in value.items() if name in TOOL_GROUPS}


class McpOAuthConfig(BaseModel):
    """OAuth 2.1 (authorization code + PKCE) for remote MCP servers that require it.

    The flow is interactive: ``McpOAuthBegin`` returns an authorization URL the owner
    opens, the server redirects back to ``redirect_uri`` (loopback; the owner pastes the
    final URL back via ``McpOAuthFinish``), and the client exchanges the code for tokens.
    Tokens live on the state volume (0600), never in chat or tool arguments.
    """

    issuer: str = ""
    """Discovery base for ``/.well-known/oauth-authorization-server``; empty = origin of the server URL."""
    scopes: list[str] = Field(default_factory=list)
    """OAuth scopes requested, e.g. ``["board:read", "board:write"]``."""
    redirect_uri: str = ""
    """Loopback redirect registered with the server; default ``http://127.0.0.1:8931/callback``."""
    client_name: str = ""
    """Client name sent during dynamic client registration (default: ``"<server> MCP client"``)."""


class McpServerConfig(BaseModel):
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    timeout_seconds: float = 120.0
    oauth: McpOAuthConfig | None = None
    """When set, the HTTP transport authenticates with OAuth bearer tokens instead of static headers."""


class McpConfig(BaseModel):
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    """Configured servers; every session starts with all of them switched off."""


class SelfChangeConfig(BaseModel):
    mode: Literal["auto", "off", "local", "server"] = "auto"
    """Whether this installation can change its own code, and how. ``server`` = worktree, pull
    request, approval, merge, rebuild; ``local`` = a checkout edited in place and applied by a
    restart; ``off`` = not at all. ``auto`` resolves it at startup from the prerequisites that are
    really present (see ``daedalus.host.capabilities``); an explicit value wins, and the doctor says
    so when what it needs is missing. A change takes effect on the next restart."""
    approval: ApprovalMode = "manual"
    auto_rebuild: bool = True
    rebuild_wait_minutes: int = Field(default=30, ge=1)
    """How long an automatic rebuild waits for the active runs to finish before restarting anyway."""
    """Trigger a rebuild automatically after a merge."""


class LimitsConfig(BaseModel):
    max_iterations: int = 200
    tool_timeout_seconds: float = 900.0
    usd_per_run: float = Field(default=5.0, ge=0)
    """Spend cap for one run; the run is stopped after the model call that crosses it. 0 = no cap.
    Calls without a known price (self-hosted models) cannot count towards it."""
    usd_total: float = Field(default=0.0, ge=0)
    """Cap on priced spend across every session and provider since ``total_since``; 0 = none."""
    usd_total_per_provider: dict[str, float] = Field(default_factory=dict)
    """The same kind of cap per provider id (e.g. ``{"deepseek": 20}``); 0 or absent = none."""
    total_since: str = ""
    """ISO timestamp the total counters start from (Settings → reset); empty = every recorded call."""
    max_run_minutes: int = Field(default=0, ge=0)
    """Wall-clock cap for one run; the run is stopped after the model call that crosses it. 0 = no cap."""
    max_run_tokens: int = Field(default=0, ge=0)
    """Cap on tokens (input + output, every call) for one run; a runaway loop stops here even on a free model. 0 = no cap."""


class BalanceConfig(BaseModel):
    enabled: bool = True
    poll_seconds: int = 60
    thresholds_usd: list[float] = Field(default_factory=lambda: [5.0, 2.0, 0.5])


class PolicyRuleConfig(BaseModel):
    """One operator rule: a regular expression over a tool's canonical text (a shell command, a URL, or the
    arguments as JSON) and what to do on a match. Rules can deny or ask; ``allow`` only lifts an ``ask``."""

    id: str = ""
    tool: str = "*"
    pattern: str = ""
    action: Literal["allow", "ask", "deny"] = "ask"
    note: str = ""


class PolicyConfig(BaseModel):
    """Tool policy beyond the built-in rules (see ``daedalus.host.policy``)."""

    rules: list[PolicyRuleConfig] = Field(default_factory=list)
    egress_allow: list[str] = Field(default_factory=list)
    """Hosts the agent may reach without asking (``example.com`` or ``*.example.com``). Empty = every host, logged."""


class HooksConfig(BaseModel):
    """Operator scripts around the agent's actions: JSON on stdin, verdict by exit code.

    ``pre_tool`` gets ``{"tool_name", "arguments", "session_id", "run_id"}``; exit 2 denies the call with
    the script's output as the reason, and a JSON object on stdout with ``"arguments"`` replaces them.
    ``post_tool`` gets the result too and may replace ``"tool_output"``. ``run_finished`` gets the run's
    status and is not waited for. Scripts run on the bot's host with the shell environment of tools.
    """

    pre_tool: str = ""
    post_tool: str = ""
    run_finished: str = ""
    timeout_seconds: float = Field(default=20.0, gt=0, le=300)


class MemoryConfig(BaseModel):
    """What the host does with memory beyond the Remember/Recall tools."""

    extract_after_run: bool = False
    """After a run of at least ``extract_min_messages`` messages, ask the model for the durable facts it learned
    (decisions, preferences, identifiers, how-tos) and store them as memories. A paid call per run."""
    extract_min_messages: int = Field(default=12, ge=2)
    extract_preset: str = ""
    """Preset id for the extraction call; empty = the session's own model."""


class CompactionConfig(BaseModel):
    """Whole-history compaction the host performs between runs (the core's tiers stay as the mid-run fallback).

    When the last prompt reached ``auto_ratio`` of the window, everything but the most recent
    messages is replaced by one structured summary: fixed English headings (the carrier that
    kept the most identifiers in the compaction-representation study), operator messages quoted
    verbatim by code rather than paraphrased by the model, and an identifier list.
    """

    auto_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    """Prompt size, as a share of the window, above which a finished run triggers compaction. 0 = manual only."""
    keep_recent_messages: int = Field(default=6, ge=0, le=60)
    """Messages at the end of the history kept as they are (cut at a turn boundary, never inside a tool exchange)."""
    max_words: int = Field(default=1200, ge=200, le=6000)
    """Length budget for the summary body, without the quoted operator messages."""
    chunk_tokens: int = Field(default=30_000, ge=5_000)
    """Longer transcripts are summarised in parallel chunks first, then merged."""
    min_messages: int = Field(default=12, ge=2)
    """Fewer messages than this are never compacted automatically."""
    call_timeout_seconds: float = Field(default=90.0, ge=10)
    """One summariser call may take this long; one that runs out is retried once with twice the time, then the automatic compaction stands down until the history has grown."""
    core_trigger_ratio: float = Field(default=0.85, gt=0.0, lt=1.0)
    """Where the core's own mid-run compaction starts; above the host's ratio so runs boundaries compact first."""
    preset: str = ""
    """Preset id the summariser runs on; empty = the session's own model. A cheaper model here saves on every long session."""


class SchedulerConfig(BaseModel):
    topic_mode: ScheduleTopicMode = "per_task"
    catch_up_missed: bool = True
    max_failures: int = Field(default=3, ge=1)
    """A recurring task that failed this many times in a row is switched off (an inbox entry says so)."""
    question_timeout_minutes: int = Field(default=120, ge=1)
    """An unattended run (schedule, heartbeat) that asks a question waits this long, then continues on its own judgement."""
    lazy_ttl_hours: int = Field(default=24, ge=1)
    """A lazy reminder not yet seen by the operator after this long becomes an agent task."""


NotifyCell = Literal["on", "off", "urgent"]
"""One cell of the notification matrix: always, never, or only for urgent notifications."""

NOTIFICATION_CATEGORIES = (
    "run_finished",
    "question",
    "permission",
    "run_failed",
    "staff_turn",
    "staff_review",
    "orchestrator_report",
    "agent_notify",
    "reminder",
    "spend",
    "system",
)
"""Every kind of thing that may want the operator. ``spend`` (a balance under its threshold, the day's
budget gone) is its own row because it is the one installation notice the operator wants in the
General topic, while a service that is down or a change waiting for review stays off Telegram."""


class NotifyCells(BaseModel):
    in_app: NotifyCell = "on"
    push: NotifyCell = "on"
    desktop: NotifyCell = "on"
    telegram: NotifyCell = "off"


def _default_matrix() -> dict[str, NotifyCells]:
    quiet_outside_app = NotifyCells(push="off", desktop="off")
    return {
        "run_finished": NotifyCells(),
        "question": NotifyCells(telegram="on"),
        "permission": NotifyCells(telegram="on"),
        "run_failed": NotifyCells(telegram="on"),
        "staff_turn": quiet_outside_app,
        "staff_review": NotifyCells(),
        # The orchestrator posts its reports to its project's topic itself; the router never does.
        "orchestrator_report": NotifyCells(telegram="on"),
        "agent_notify": NotifyCells(telegram="urgent"),
        "reminder": NotifyCells(telegram="on"),
        "spend": NotifyCells(telegram="on"),
        "system": NotifyCells(),
    }


class NotificationsConfig(BaseModel):
    """What happens to the things that want the operator's attention, and where each one goes."""

    matrix: dict[str, NotifyCells] = Field(default_factory=_default_matrix)
    """Per category, which channels carry it. A category the file does not name keeps its default."""
    finished_min_seconds: int = Field(default=30, ge=0)
    """A finished run is announced only when it took at least this long: a quick answer the
    operator waited for in front of the screen needs no notification."""
    quiet_hours: str = ""
    """``HH:MM-HH:MM`` in the operator's time zone, which may wrap midnight; empty means none.
    Inside it nothing below urgent pushes, raises a desktop notification or goes to Telegram."""
    muted_projects: dict[str, str] = Field(default_factory=dict)
    """Project id → the ISO moment the mute ends, or "" for until it is lifted."""
    quick_actions: bool = True
    """Allow, deny and answer from a notification without opening the app, where the request allows it."""
    telegram_covers_push: bool = True
    """Nothing is pushed or raised on the desktop that Telegram already delivered: one phone, one buzz."""
    keep_days: int = Field(default=30, ge=1)
    """Seen (or quiet) notifications older than this are pruned; one still waiting for an answer never is."""
    push_per_session: int = Field(default=6, ge=1)
    push_window_minutes: int = Field(default=10, ge=1)
    push_per_hour: int = Field(default=60, ge=1)
    """The ceiling across every session, against a loop that has gone wrong."""
    notify_tool_per_session: int = Field(default=5, ge=1)
    """How many times one session may call ``Notify`` within ``notify_tool_window_minutes``. Stricter
    than the push budget, because an agent that decides to talk to the operator outside its chat is
    the one producer that can decide to do it in a loop."""
    notify_tool_window_minutes: int = Field(default=10, ge=1)
    notify_tool_urgent_per_hour: int = Field(default=2, ge=0)
    """Urgent breaks through quiet hours, so one session gets few of them; 0 takes urgent away from agents."""
    orchestrator_hold_seconds: int = Field(default=60, ge=0)
    """How long a staff member's question waits for its orchestrator before it reaches the operator."""

    @field_validator("matrix")
    @classmethod
    def _complete_matrix(cls, value: dict[str, NotifyCells]) -> dict[str, NotifyCells]:
        unknown = sorted(set(value) - set(NOTIFICATION_CATEGORIES))
        if unknown:
            raise ValueError(f"unknown notification categories: {', '.join(unknown)}")
        return {**_default_matrix(), **value}

    @field_validator("quiet_hours")
    @classmethod
    def _quiet_hours_shape(cls, value: str) -> str:
        value = value.strip()
        if value and parse_quiet_hours(value) is None:
            raise ValueError("quiet hours are HH:MM-HH:MM, for example 23:00-07:30")
        return value

    def cells(self, category: str) -> NotifyCells:
        return self.matrix.get(category) or _default_matrix().get(category) or NotifyCells()


def parse_quiet_hours(spec: str) -> tuple[int, int] | None:
    """``"23:00-07:30"`` → minutes after midnight of the start and the end; ``None`` when malformed."""
    start, sep, end = spec.strip().partition("-")
    if not sep:
        return None
    minutes = []
    for part in (start, end):
        hours, colon, mins = part.strip().partition(":")
        if not colon or not hours.isdigit() or not mins.isdigit() or len(mins) != 2:
            return None
        h, m = int(hours), int(mins)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        minutes.append(h * 60 + m)
    return minutes[0], minutes[1]


class AsrConfig(BaseModel):
    """Speech-to-text for voice notes: any OpenAI-compatible ``/audio/transcriptions`` endpoint."""

    provider: str = ""
    """A configured provider id (``openrouter``, a ``vllm`` endpoint with a Whisper model, …): its base URL and
    key are used, so the key stays where the provider keeps it; empty = ``url``/``api_key`` below."""
    url: str = ""
    """Base URL (``https://api.openai.com/v1``, a local faster-whisper server, …); empty = voice notes arrive as files only."""
    api_key: str = ""
    model: str = "whisper-1"
    language: str = ""
    """ISO code hint; empty = auto."""
    timeout_seconds: float = Field(default=120.0, ge=5)
    max_seconds: int = Field(default=600, ge=5)
    """Longer voice notes are not transcribed."""
    autosend: bool = False
    """Send the transcript to the agent without the confirm step (dictation is error-prone; off by default)."""
    transcriber: str = "cloud"
    """What turns a voice note into words: ``cloud`` is the endpoint above, anything else is the id of a
    local model installed on the Voice page. With ``cloud`` and no endpoint set, the Voice page's own
    model is used if one is installed, which is what an installation without a key always had."""
    fallback: str = ""
    """What is tried when the transcriber fails: empty for nothing, ``cloud``, or a local model id. A
    recording is never thrown away on a failure either way; this decides whether one is retried by itself."""
    chunk_seconds: int = Field(default=180, ge=20, le=290)
    """A recording longer than this goes to the endpoint in pieces of at most this length, cut in the
    pauses. The hosted endpoint in use refuses anything over five minutes, and anything over about ten
    megabytes, with a bare HTTP 400; three minutes of the site's 16 kHz recording is under six."""


class SttConfig(BaseModel):
    """Speech recognition that runs here, with no endpoint and no key.

    Empty ``local_model`` is the default and means nothing changes: the voice page uses the
    browser's own recognition, or the configured ``[asr]`` endpoint for a recorded utterance.
    Naming a catalog model is the voice page's live listener. Voice notes in the composer and
    Telegram follow ``[asr].transcriber`` and ``[asr].fallback``, which may name this model or
    another installed one.
    """

    local_model: str = ""
    """A catalog id (``gigaam-ru``, ``parakeet-unified-en``, …). Empty = no local recognition."""
    local_language: str = "auto"
    """ISO code the model is loaded for. "auto" lets a multilingual model decide and pins a
    Whisper-class one to English, which is what it does with no hint anyway."""
    local_threads: int = Field(default=2, ge=1, le=16)
    """Decoding threads. Two is enough to run many times faster than real time on an ordinary CPU;
    more buys little and takes cores away from the rest of the process."""


class TtsConfig(BaseModel):
    """How the voice page speaks, in the order it tries: a voice that runs here, then an endpoint.

    ``local_voice`` names an entry from :mod:`daedalus.speech.tts_catalog` — a Piper, Kokoro or
    KittenTTS voice downloaded into the state directory — and is the whole switch: set it and the
    answer is synthesised on this machine's processor, clear it and nothing changes anywhere else.
    Past that, an OpenAI-compatible ``/audio/speech`` endpoint if one is configured, and past that the
    browser's own synthesiser, which every modern browser has and none of them do well.
    """

    local_voice: str = ""
    """A catalog id (``ru-dmitri``, ``en-amy``, …). Empty = no local synthesis."""
    local_speaker: str = ""
    """Which voice inside a multi-speaker archive (``af_sarah``, ``expr-voice-4-f``). Empty = the first.
    Ignored by the single-voice models, which is most of them."""
    local_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    """How fast it talks. 1.0 is the voice as trained; the bounds are what stays intelligible."""
    local_threads: int = Field(default=2, ge=1, le=16)
    """Synthesis threads. Two is enough for every voice in the catalog that keeps up with speech at
    all, and the two that do not are not rescued by more."""

    provider: str = ""
    """A configured provider id whose base URL and key are used, exactly as ``[asr]`` does; empty = ``url``/``api_key``."""
    url: str = ""
    """Base URL (``https://api.openai.com/v1``, a local speech server, …); empty = the browser's own synthesiser."""
    api_key: str = ""
    model: str = "gpt-4o-mini-tts"
    voice: str = "alloy"
    format: Literal["mp3", "opus", "pcm"] = "mp3"
    timeout_seconds: float = Field(default=60.0, ge=5)


class VoiceConfig(BaseModel):
    """The voice page: a small fast model the operator talks to, which hands real work to agent sessions.

    ``preset`` should name a model that answers in a second or two — a conversation stalls where a
    thinking model would merely be slow. Empty falls back to the default preset.
    """

    enabled: bool = True
    preset: str = ""
    """The concierge's model preset; pick a fast one (no thinking, or low effort). Empty = the default preset."""
    progress: bool = True
    """Whether an agent's mid-run words are relayed to the concierge at all. Off leaves the final answer
    and the questions, which is the quieter conversation and the cheaper one."""
    progress_window_seconds: float = Field(default=20.0, ge=0)
    """How long an agent's newest interim waits before it is relayed. An agent writes a line, calls a tool,
    writes another: the window lets the later line replace the earlier one instead of spending a turn on each."""
    progress_min_gap_seconds: float = Field(default=60.0, ge=0)
    """The floor between two interims of the same agent. A busy agent narrates every few seconds; a
    conversation interrupted that often is unusable, so the newest line waits for the gap and the rest are
    superseded. Finals and questions do not pass through here and are never held back by it."""
    progress_max_per_minute: int = Field(default=3, ge=0)
    """Interims relayed in any one minute across *all* the agents together. The gap above is per agent, and
    twenty agents each obeying it is still twenty spoken turns a minute — twenty model calls, each one
    growing the concierge's context. Past this cap the line is dropped rather than queued: an interim is
    superseded by the agent's next paragraph and by its final answer, so a late one is worth nothing.
    ``0`` relays no interims at all, which is what ``progress = false`` says more plainly."""
    tts: TtsConfig = Field(default_factory=TtsConfig)


class ModeConfig(BaseModel):
    """A named bundle of run limits and behaviour a session can switch to."""

    max_iterations: int | None = Field(default=None, ge=1)
    usd_per_run: float | None = Field(default=None, ge=0)
    """``0`` means spend nothing (the run stops at its first priced call); ``None`` inherits ``limits.usd_per_run``."""
    tool_timeout_seconds: float | None = Field(default=None, gt=0)
    verbosity: int | None = Field(default=None, ge=0, le=2)
    prompt: str = ""
    """Extra rules appended to the system prompt while the mode is active."""
    tools_off: list[str] = Field(default_factory=list)
    """Tools the session does not get while the mode is active; a trailing ``*`` matches a prefix (``Mcp_*``)."""
    tools_only: list[str] = Field(default_factory=list)
    """When set, the only tools the session gets while the mode is active: everything else, including tools added
    later and every MCP tool, is off. A plan mode is an allow-list, so a new tool is blocked until someone decides."""
    description: str = ""


PLAN_MODE_TOOLS_ONLY = ["Read", "Find", "Search", "WebFetch", "WebSearch", "HistorySearch", "HistoryExpand", "Recall", "Remember", "Skill", "ImageView", "BoardList", "BoardGet", "ScheduleList", "LoopStatus", "JobOutput", "JobList", "ServiceList", "ServiceLogs", "TerminalRead", "McpList", "PeerList", "SubAgentList", "IntentList", "LearningReport", "AskUser", "StaySilent", "ToolSearch"]
"""What a plan may do: read, search, look, remember and ask. Everything else — files, commands, Verify, sending,
starting, peers, MCP tools — is off until the operator switches the mode."""

DEFAULT_MODES: dict[str, ModeConfig] = {
    "plan": ModeConfig(
        max_iterations=60,
        usd_per_run=1.0,
        tools_only=PLAN_MODE_TOOLS_ONLY,
        description="read and plan only; nothing is changed until the operator switches the mode",
        prompt=(
            "Mode: plan. You may read files, search, fetch and think; the tools that change anything are off. "
            "Investigate, then reply with a plan the operator can approve: the goal, the steps in order with the files each touches, "
            "what you will verify and how, the risks, and the open questions. End by asking the operator to switch the mode "
            "(/mode default or another mode) to carry it out; do not attempt work-arounds for the blocked tools."
        ),
    ),
    "quick": ModeConfig(max_iterations=25, usd_per_run=0.5, description="short answers, few tool calls, cheap", prompt="Mode: quick. Answer briefly, prefer a direct answer over investigation, at most a handful of tool calls."),
    "deep": ModeConfig(max_iterations=400, usd_per_run=15.0, description="long autonomous work with a high budget", prompt="Mode: deep. Work autonomously to completion; verify with Verify; ask only when a choice is genuinely the operator's."),
    "careful": ModeConfig(max_iterations=100, description="ask before anything irreversible", prompt="Mode: careful. Before any irreversible action (deleting, pushing, sending, paying, changing configuration) ask with AskUser and wait."),
}


VOICE_TOOLS = ["Delegate", "Agents", "AgentResult", "StopAgent", "Projects", "WebSearch"]
"""Everything the concierge may call: hand work to an agent, look at the agents, stop one, and answer a
quick factual question itself. Nothing that reads, writes or runs anything — that is what the agents are for."""

VOICE_ONLY_TOOLS = ["Delegate", "Agents", "AgentResult", "StopAgent", "Projects"]
"""The tools that exist for the concierge alone; every other session is blocked from them (it has SpawnAgent)."""

STAFF_BLOCKED_TOOLS = [
    "AskUser",
    "SpawnAgent",
    "ScheduleCreate",
    "ScheduleList",
    "ScheduleDelete",
    "LoopNext",
    "LoopPause",
    "LoopResume",
    "LoopStatus",
    "LoopStop",
    "IntentCreate",
    "IntentDelete",
    "IntentList",
    "SelfPropose",
    "SelfApply",
    "SelfRebuild",
    "SelfRollback",
    "SelfWorkspace",
    "Notify",
]
"""What a staff member of a project may not call. It asks the orchestrator, not the operator
(``AskOrchestrator``), reports to the orchestrator rather than notifying the operator itself, works
on the task it was given rather than starting agents, schedules, loops or intents of its own, and
never changes the installation. Enforced by the host beside the voice rule,
not through a mode, so editing a mode cannot hand a staff member any of them."""

STAFF_ONLY_TOOLS = ["Report", "AskOrchestrator"]
"""The tools that exist for staff alone; every other session is blocked from them."""

SUBAGENT_BLOCKED_TOOLS = ["Notify"]
"""What a subagent may not call, whoever its leader is. Its work reaches the operator through the
agent that started it, which decides what is worth the operator's attention; a subagent that could
notify on its own would put a dozen workers' progress on the operator's phone. Enforced by the host
beside the staff rule, so no mode or leader can hand it back."""

ORCHESTRATOR_ONLY_TOOLS = [
    "Brief",
    "Folders",
    "Journal",
    "Team",
    "Hire",
    "StaffEdit",
    "Dismiss",
    "Assign",
    "Require",
    "ReviewResult",
    "Decide",
    "Tell",
    "ReadStaff",
    "StaffSessions",
    "Answer",
    "Interrupt",
    "Pause",
    "Release",
    "Peek",
    "Tasks",
    "WakeMe",
    "Watch",
    "Unwatch",
    "AskOperator",
    "WithdrawQuestions",
    "ProjectReport",
    "ProjectExtension",
    "Harnesses",
]
"""The tools that exist for a project's orchestrator alone; every other session is blocked from them.
Names that are not registered on this installation yet are simply absent from every list. ``Harnesses``
is here too: only the orchestrator hires, and an ordinary session would pay for its description on
every turn."""

ORCHESTRATOR_TOOLS = [*ORCHESTRATOR_ONLY_TOOLS, "Notify", "StaySilent", "HistorySearch", "HistoryExpand", "Recall"]
"""Everything an orchestrator may call. It runs the team and never does the work itself: no shell, no
file writes, no subagents. An allowlist rather than a list of refusals, so a tool added later is not
an orchestrator's until someone decides it should be."""


DISPATCHER_TOOLS = ["Projects", "Delegate", "Progress", "Cancel", "CreateProject", "Answer", "Files", "Notify", "StaySilent", "HistorySearch", "HistoryExpand", "Recall"]
"""Everything the main orchestrator may call. It routes the operator's words to projects and follows
what it handed over; it never touches files, staff or a board itself, and it never blocks on a
question (no AskUser: a main orchestrator paused on a question could not hear its projects report).
Its own tools share names with other roles' tools (the voice concierge's Delegate and Projects, the
project orchestrator's Answer), so they live in a registry of their own and this list is the whole of it."""


class WebhookConfig(BaseModel):
    """One inbound webhook provider: how it is authenticated and where its events run."""

    secret: str = ""
    """HMAC-SHA256 secret (GitHub style ``X-Hub-Signature-256``) or bearer token; empty = the endpoint refuses."""
    scheme: Literal["github", "bearer"] = "bearer"
    session: str = ""
    """Session id (or title) the events run in; empty = a standing session named after the provider."""
    prompt: str = ""
    """What to do with an event; the flattened payload follows it."""
    enabled: bool = True
    deliver: Literal["session", "events"] = "session"
    """``session`` runs the event in its session as before; ``events`` only publishes it, for the
    projects' watches (a pull request, a CI result) to act on, and starts no run."""


class BoardConfig(BaseModel):
    wip_limit: int = Field(default=3, ge=1)
    """How many tasks one agent (a session with its subagents) may hold in 'doing' at once."""
    stale_hours: int = Field(default=6, ge=1)
    """A 'doing' task whose session has been quiet this long is handed back to 'todo'."""


class PeersConfig(BaseModel):
    max_depth: int = Field(default=3, ge=1)
    """How many AskPeer hops may chain (a peer asking a peer …) before the call is refused."""
    wait_timeout_minutes: int = Field(default=30, ge=1)


class SubagentsConfig(BaseModel):
    max_depth: int = Field(default=2, ge=1)
    """How deep SubAgent may nest (a subagent starting a subagent …) before the call is refused."""
    max_active: int = Field(default=4, ge=1)
    """Running subagents one leader may have at a time."""
    wait_timeout_minutes: int = Field(default=30, ge=1)


class StaffConfig(BaseModel):
    """The named members of a project's team: how much of them is kept, and how their signals are paced."""

    notes_max_chars: int = Field(default=8000, ge=500)
    """What a staff member carries from one session to the next. Past it the oldest lines go: notes
    are read at the start of every session, so an unbounded file would slowly become the whole prompt."""
    launch_stagger_seconds: int = Field(default=5, ge=0)
    """The gap between two launches of one project, so a queue that frees six slots at once does not
    start six processes in the same second."""
    silence_minutes: int = Field(default=10, ge=1)
    """A working staff member with no signal for this long is shown as silent rather than working."""
    ask_escalate_minutes: int = Field(default=10, ge=1)
    """A request left with the orchestrator this long goes to the operator: a blocked worker must not
    wait on a model that is itself failing."""
    read_default_chars: int = Field(default=4000, ge=200)
    read_max_chars: int = Field(default=16000, ge=1000)
    report_summary_chars: int = Field(default=300, ge=40)
    signal_write_seconds: int = Field(default=5, ge=0)
    """The least time between two writes of a staff session's last signal. Every event of a busy
    session is a signal, and a row rewritten on each one is write load that tells nobody anything new."""


class DispatcherConfig(BaseModel):
    """The main orchestrator: the one session that hands the operator's requests to projects."""

    preset: str = ""
    """Its model preset; empty is a mid-tier preset (:meth:`RuntimeConfig.middle_preset`). It routes and
    follows, which a mid-tier model does as well as the strongest, and it runs on every report."""
    batch_seconds: int = Field(default=10, ge=1, le=600)
    """Reports on dispatches arriving together are told in one turn."""
    max_wakes_per_hour: int = Field(default=20, ge=1)
    stalled_minutes: int = Field(default=30, ge=5, le=24 * 60)
    """An open dispatch with nothing happening on it this long, while nobody in its project works, is stalled."""
    state_max_chars: int = Field(default=4000, ge=1000, le=20_000)
    max_iterations: int = Field(default=20, ge=5)
    """Model calls in one of its turns: it routes and ends its turn."""
    usd_per_run: float | None = Field(default=None, ge=0)
    container_roots: list[str] = Field(default_factory=list)
    """Where ``CreateProject`` may find or make a container folder. Empty: the folders that already
    hold container projects (the parents of their folders), which are the mounts the operator set up."""


class OrchestratorConfig(BaseModel):
    """A project's orchestrator: which model it runs by default, how its wake-ups are batched, and what bounds a turn."""

    preset: str = ""
    """The model preset of a project orchestrator whose project names none; empty is the strongest
    preset (:meth:`RuntimeConfig.strongest_preset`). A project's own choice overrides it."""
    batch_seconds: int = Field(default=20, ge=1, le=600)
    """Routine events wait this long after the first of them, so one wake-up carries several."""
    batch_max_lines: int = Field(default=30, ge=5, le=200)
    """Lines of one wake-up batch; the rest is counted, and the team and board say what they were."""
    state_max_chars: int = Field(default=8000, ge=1000, le=40_000)
    """The project state re-sent at every turn. It is paid for on every turn, so it is bounded."""
    max_iterations: int = Field(default=40, ge=5)
    """Model calls in one orchestrator turn. It hands work over and ends its turn; forty is a lot of that."""
    usd_per_run: float | None = Field(default=None, ge=0)
    """Spend cap of one orchestrator turn; ``None`` is ``limits.usd_per_run``."""
    max_wakes_per_hour: int = Field(default=30, ge=1)
    """Past this, routine batches wait for the next hour and the operator is told; urgent ones still go."""
    default_concurrency: int = Field(default=6, ge=1)
    """How many staff of a project work at once when the operator first switches its orchestrator on."""
    default_concurrency_cap: int = Field(default=10, ge=1)
    """The ceiling the orchestrator may raise that to, when first switched on; the operator moves it."""
    max_concurrency_cap: int = Field(default=32, ge=1)
    """The most any project's ceiling may be set to."""
    wakeups_max: int = Field(default=20, ge=1, le=200)
    """Wake-ups one project may hold at once, whoever set them."""
    wake_cron_min_minutes: int = Field(default=10, ge=1, le=1440)
    """The shortest gap a recurring wake-up may have. A fired wake-up is urgent and passes the hourly
    cap, so this is what keeps a cron from buying a turn every minute."""


class WatchesConfig(BaseModel):
    """What bounds a project's watches ("when X, do Y"), so none can cost more than it is worth."""

    tick_seconds: int = Field(default=30, ge=5, le=600)
    """How often silences are measured and the watched folders' branches are due for a look."""
    git_poll_seconds: int = Field(default=60, ge=15, le=3600)
    """How often a folder named by a commit watch has its branch heads read; only those folders are read."""
    min_cooldown_seconds: int = Field(default=60, ge=10, le=86_400)
    """The shortest time a watch may be set to wait between two fires."""
    max_fires_per_hour: int = Field(default=12, ge=1, le=120)
    """Past this a watch switches itself off and the journal says so: a watch whose action causes its
    own event would otherwise go round for as long as the cooldown lets it."""
    max_per_project: int = Field(default=50, ge=1, le=500)
    regex_max_chars: int = Field(default=200, ge=10, le=1000)


class HarnessConfig(BaseModel):
    """Command-line agents as staff: how long to wait on them, and how carefully to type into them."""

    no_signal_after_s: int = Field(default=300, ge=30, le=3600)
    """A working session with neither a structured signal nor terminal output for this long has its
    screen checked; what the screen cannot settle is shown as silent, never as failed."""
    reconcile_gap_ms: int = Field(default=1500, ge=200, le=10_000)
    """Between the two screen readings that must agree before a turn end is inferred from the screen."""
    idle_settle_s: int = Field(default=60, ge=5, le=3600)
    """A session shown working whose CLI has sent no signal for this long has its screen read, output
    or not; an idle prompt read twice the same ends the turn. A missed turn end otherwise waited for
    ``no_signal_after_s`` of a silent terminal, which a status line that redraws never gives, and a
    member sat "working" at an empty prompt with its next task queued behind a turn long over."""
    ready_timeout_s: float = Field(default=30.0, ge=5, le=600)
    """How long a launched CLI may take to say it is ready before the session is shown as failed,
    with the screen it was stuck on; the terminal is left running for the operator to look at."""
    ready_poll_ms: int = Field(default=300, ge=50, le=5000)
    """How often the readiness gate reads the screen for a dialog it answers."""
    ack_timeout_s: float = Field(default=8.0, ge=1, le=120)
    """How long a submitted message may go unacknowledged before it is looked for on screen and in
    the transcript."""
    enter_retries: int = Field(default=3, ge=0, le=5)
    """Enters sent again when the composer still holds the message. Never more: a message that is
    neither in the composer nor in the transcript is reported failed, not resent."""
    submit_settle_ms: int = Field(default=1000, ge=100, le=10_000)
    """How long after an Enter the composer may still hold the message before that Enter counts as
    lost and another is sent. Each retry waits twice as long as the one before it: a TUI that is
    slow to take a prompt is given longer, never flooded with Enters."""
    paste_confirm_s: float = Field(default=30.0, ge=1, le=300)
    """How long a pasted message may take to show in the composer before it is reported as not
    arrived. A TUI that has sat idle for a while, or runs on a loaded machine, has drawn a large
    paste seconds late; the delivery gave up at five, pressed no Enter, and the message sat unsent in
    the prompt with its receipt saying it never arrived."""
    enter_delay_base_ms: int = Field(default=400, ge=0, le=5000)
    enter_delay_per_kib_ms: int = Field(default=60, ge=0, le=1000)
    enter_delay_max_ms: int = Field(default=2000, ge=0, le=10_000)
    """The pause between a paste and its Enter grows with the paste, up to this: a TUI still reading
    a large paste takes an early Enter as part of it."""
    paste_chunk_bytes: int = Field(default=4096, ge=256, le=65_536)
    pointer_threshold_bytes: int = Field(default=16_384, ge=1024, le=1 << 20)
    """A message longer than this goes as a file in the launch directory with a one-line pointer:
    TUIs collapse, truncate or choke on large pastes."""
    interrupt_timeout_s: float = Field(default=15.0, ge=1, le=300)
    answer_confirm_s: float = Field(default=5.0, ge=0.5, le=60)
    """How long an answer typed into a dialog may take to show its effect before it counts as not
    delivered and the operator is asked to answer in the terminal."""
    ask_hold_s: int = Field(default=300, ge=10, le=3600)
    """How long a staff member's question to the orchestrator is held open before the tool returns
    "pending" and the answer, when it comes, is delivered as a message instead."""
    report_hold_s: int = Field(default=15, ge=1, le=120)
    """How long a Report waits for the team's word on it ("commit first" is one). A CLI's own timeout
    on a tool call is set above this and above ``ask_hold_s``."""
    permission_hold_s: int = Field(default=300, ge=0, le=3600)
    """How long a CLI's own permission or question is held open at its hook for an answer from the
    app or the orchestrator. The CLI's dialog is on screen meanwhile where it draws one, so the
    operator can answer there as well; 0 answers only on screen."""
    team_hello_s: float = Field(default=30.0, ge=1, le=600)
    """How long after a CLI is ready its team tools may take to say they loaded before the member is
    shown with its team tools not connected."""
    stop_grace_s: float = Field(default=10.0, ge=0, le=120)
    """Between asking a CLI to exit and killing its terminal."""
    catalog_ttl_s: int = Field(default=21_600, ge=300)
    """How often each environment is looked at again for what is installed, signed in, and which
    agents and models each CLI offers. Never an update: a version changes only when the operator asks."""
    latest_ttl_s: int = Field(default=21_600, ge=300)
    """How long a known latest version is trusted before the release feeds are asked again."""
    update_timeout_s: int = Field(default=600, ge=60, le=3600)
    """A CLI's own updater, or npm installing a newer version, is given this long."""
    install_timeout_s: int = Field(default=900, ge=60, le=3600)
    """An install runs in a terminal the operator can watch; past this it is reported as failed and
    the terminal is left for the operator to read."""
    allow_untested: bool = True
    """Launch a CLI whose version is outside the adapter's tested range, when its self-check passed."""
    self_check_model_turn: bool = True
    """The self-check after an update sends one tiny prompt on the cheapest model, which costs a few
    tokens of the subscription and is the only proof that a prompt still gets through."""
    opencode_port_range: str = Field(default="18300-18399", pattern=r"^\d{4,5}-\d{4,5}$")
    """Loopback ports OpenCode's embedded server may take, one per launch."""

    @field_validator("opencode_port_range")
    @classmethod
    def _outside_reserved_ports(cls, value: str) -> str:
        # The agent hands 8100–8119 to its own preview servers and 8120–8139 to container terminals'
        # servers; an OpenCode server on one of those would take a port someone else was promised.
        low, high = (int(part) for part in value.split("-"))
        if not 1024 <= low <= high <= 65535:
            raise ValueError("a port range is low-high within 1024-65535")
        if low <= 8139 and high >= 8100:
            raise ValueError("the range must stay clear of 8100-8139")
        return value


class LoopsConfig(BaseModel):
    """Loop agents: a session woken up for one standing task, on an interval or when it asks."""

    min_interval_seconds: int = Field(default=60, ge=10)
    """The shortest cadence a loop may run at, and the floor for a delay the agent picks."""
    max_delay_seconds: int = Field(default=7 * 24 * 3600, ge=60)
    """The longest delay a dynamically paced loop may ask for."""
    tick_seconds: int = Field(default=20, ge=5)
    """How often due loops are looked for."""


class OpsConfig(BaseModel):
    """Operational thresholds: boot-loop guard, delivery ledger, doctor."""

    boot_loop_window_minutes: int = Field(default=10, ge=1)
    boot_loop_threshold: int = Field(default=3, ge=2)
    """Unclean boots inside the window after which boot recovery is skipped once."""
    delivery_max_attempts: int = Field(default=3, ge=1)
    delivery_max_age_hours: int = Field(default=24, ge=1)
    delivery_keep_days: int = Field(default=7, ge=1)
    events_keep_per_run: int = Field(default=300, ge=10)
    """Core events kept per finished run (the transcript is the record; events feed the live view)."""
    events_keep_days: int = Field(default=7, ge=1)
    """Age past which a finished run's events are dropped. Per-run trimming bounds a run, not the table:
    runs accumulate for as long as the installation does."""
    events_max_rows: int = Field(default=100_000, ge=1000)
    """Hard ceiling on the event log, whatever the age sweep left."""
    db_maintenance_minutes: int = Field(default=60, ge=5)
    """How often the event sweep and the incremental vacuum run."""
    doctor_min_free_gb: float = Field(default=1.0, ge=0)
    doctor_workspaces_warn_gb: float = Field(default=20.0, ge=0)
    doctor_stale_snapshot_hours: int = Field(default=24, ge=1)
    doctor_probe_timeout_seconds: float = Field(default=6.0, ge=1)
    checkpoint_max_gb: float = Field(default=2.0, ge=0)
    """Workspaces larger than this are not snapshotted (revert then restores the history only)."""
    checkpoint_keep_days: int = Field(default=30, ge=1)
    """Age past which a workspace snapshot is dropped. Nothing bounded the stores before: they grew
    for as long as the installation did."""
    checkpoint_total_max_gb: float = Field(default=2.0, ge=0)
    """Ceiling on every snapshot store together; over it, the oldest go first. 0 = no size bound."""
    checkpoint_keep_last: int = Field(default=50, ge=1)
    """Snapshots every session keeps whatever the two bounds say, so the undo the operator reaches
    for cannot be taken away by a store somebody else filled."""
    learning_digest_days: int = Field(default=7, ge=1)
    learning_repeat_threshold: int = Field(default=3, ge=2)
    """A failure or an ask seen this many times in a digest window becomes an improvement candidate."""
    provider_retry_max_attempts: int = Field(default=6, ge=0)
    """Runs driven again after the model provider failed one, in a row; 0 leaves a failed run failed."""
    provider_retry_base_seconds: float = Field(default=30.0, ge=1)
    """The wait before the first of those attempts; it doubles each time up to ``provider_retry_max_seconds``."""
    provider_retry_max_seconds: float = Field(default=600.0, ge=1)
    settle_wait_seconds: float = Field(default=180.0, ge=1)
    """How long anything that rewrites a session waits for the last turn's snapshot before it gives
    up and says so. A snapshot is a commit into the workspace; a network mount can make it slow, but
    a wait with no bound is a session wedged with nothing on the screen to say why."""
    shutdown_grace_seconds: float = Field(default=20.0, ge=0)
    """How long a shutdown gives the housekeeping of a run that has just ended — the snapshot and the
    handover to the other fronts — before it is cancelled. 0 cancels at once, which loses them."""
    app_events_keep_days: int = Field(default=7, ge=1)
    """Age past which an event on the bus is dropped from its ring. A client or subscriber whose
    cursor is older than that is told to re-read its lists rather than replayed."""
    app_events_max_rows: int = Field(default=200_000, ge=1000)
    """Hard ceiling on the bus's ring, whatever the age sweep left."""
    event_subscriber_queue: int = Field(default=1024, ge=4)
    """Live events one subscriber may leave unread before it stops receiving them and reads what it
    missed from the ring instead. It bounds memory per subscriber; it never makes a publisher wait."""
    event_replay_max: int = Field(default=5000, ge=100)
    """Events a reconnecting client may be behind and still be caught up. Further behind, it is told
    to re-read its lists, which is cheaper than streaming it a day of history."""
    presence_ttl_seconds: float = Field(default=60.0, ge=5)
    """How long one window's report of what it shows counts. The app re-sends it every 20 s while
    visible, so a window that stopped reporting (a phone locked without a word) is away after this."""
    presence_grace_seconds: float = Field(default=5.0, ge=0)
    """How long a window still counts after its event stream dropped: long enough for a reconnect,
    far shorter than the report's own lifetime."""


class TerminalsConfig(BaseModel):
    """Terminals: real shells and CLI programs run by a terminal daemon per environment."""

    running_cap: int = Field(default=20, ge=1)
    """Terminals that may run at once across the whole machine, both environments together. It has
    no upper bound on purpose: the load estimate warns when a value is more than the machine can
    carry, and the operator decides. An agent's launch waits for a free place; the operator may go
    past it after confirming."""
    ring_bytes: int = Field(default=8 << 20, ge=1 << 20, le=64 << 20)
    """Output each terminal keeps for a late reader or a reattaching browser."""
    snapshot_scrollback_lines: int = Field(default=2000, ge=0, le=10_000)
    input_idle_ms: int = Field(default=10_000, ge=0, le=600_000)
    """How long an agent's write waits after the last human keystroke, so it never lands mid-word."""
    kill_grace_ms: int = Field(default=2000, ge=0, le=30_000)
    """Between the hang-up and the kill when a terminal is ended."""
    exited_retention_hours: int = Field(default=72, ge=1)
    """How long an ended terminal's row, with its last screen, stays listed."""
    audit_retention_days: int = Field(default=90, ge=1)
    ticket_ttl_seconds: int = Field(default=30, ge=5, le=300)
    shell: str = ""
    """The program a shell terminal runs; empty = the environment's login shell."""
    preview_poll_ms: int = Field(default=3000, ge=500)
    """How often the Terminals screen refreshes its previews while it is visible."""
    agent_launch_wait_seconds: float = Field(default=600.0, ge=0)
    """How long an agent's launch waits in line for a free place under the cap before it gives up."""
    agent_reads_host: bool = False
    """Whether a session's agent may read the host terminals of its own session. Off by default: a
    host terminal is the operator's own machine, where a screen can hold what was never meant for a
    model — a password prompt's surroundings, another project's secrets."""


class BrowserRuleConfig(BaseModel):
    """An operator's rule about sensitive browser actions on one site: ``allow`` lets the kinds named
    through without asking, ``deny`` refuses them, ``ask`` asks where nothing else would."""

    domain: str = Field(min_length=1, max_length=253)
    """A host (``shop.example.com``) or every host under one (``*.example.com``)."""
    kinds: list[Literal["credentials", "purchase", "send", "destroy", "accept", "upload", "cross_origin_post"]] = Field(default_factory=list)
    """Empty = every kind."""
    action: Literal["allow", "ask", "deny"] = "deny"
    note: str = ""


class BrowserConfig(BaseModel):
    """The agent's browser: Chromium run by a browser daemon per environment."""

    env: Literal["auto", "container", "host"] = "auto"
    """Where an agent's browser runs: ``auto`` is the container's browser service where there is one
    and the operator's machine otherwise (a native installation)."""
    running_cap: int = Field(default=2, ge=1, le=32)
    """Browsers that may run at once in one environment. The host gives it to each daemon on every
    connection and whenever it changes, so it is the daemon's own limit; the load bar projects it."""
    idle_close_minutes: int = Field(default=10, ge=0, le=24 * 60)
    """A browser with no agent call, no person's input and nobody watching for this long is closed;
    its profile, and so its logins, stay on disk. 0 = never."""
    agent_wait_seconds: float = Field(default=60.0, ge=0, le=600)
    """How long an agent's BrowserOpen waits in line when every browser is busy before it gives up."""
    control_wait_seconds: float = Field(default=20.0, ge=0, le=60)
    """How long an agent's call waits for a person who holds the browser to give it back."""
    ticket_ttl_seconds: int = Field(default=30, ge=5, le=300)
    audit_retention_days: int = Field(default=90, ge=1)
    closed_retention_hours: int = Field(default=72, ge=1)
    """How long a closed group's row stays listed."""
    local_sites: Literal["services", "ask", "allow"] = "ask"
    """The other ports of the machine the agent runs on — the operator's local apps and the dev
    servers coding members start: ``services`` opens only the agent's own services ranges, ``ask``
    asks about each port, ``allow`` opens them all. The installation's own doors (the API, the key
    proxy, the daemons, the launcher) stay shut whatever it says."""
    lan_allow: list[str] = Field(default_factory=list)
    """Addresses or prefixes on the local network the browser may reach after the operator's yes
    (the network wall asks about each; it never lets metadata addresses through)."""
    rules: list[BrowserRuleConfig] = Field(default_factory=list)
    """The operator's rules about sensitive actions by site. A rule never allows ``credentials``:
    typing into a sign-in is the operator's, however the site is trusted."""
    record_frames: bool = False
    """Whether a new browser records keyframes (one after every action, one every few seconds while
    the page changes) for the action log's replay. Each browser can be switched from its panel; the
    agent cannot switch it. The action log itself is always kept."""
    record_takeover: bool = False
    """Whether a recording goes on while the operator drives. Off: what a person does in the browser
    is not pictured, as what they type is never recorded."""
    record_retention_days: int = Field(default=7, ge=1, le=365)
    record_max_mb: int = Field(default=500, ge=10, le=100_000)
    """All the recorded keyframes of one environment together; past it the oldest go first."""
    watch_mode: bool = False
    """Watch mode: on the sites in ``watch_domains`` the agent acts only while the operator has the
    browser open and in view. Off by default; the list is there to switch on."""
    watch_domains: list[str] = Field(
        default_factory=lambda: [
            "mail.google.com",
            "outlook.live.com",
            "outlook.office.com",
            "*.mail.yahoo.com",
            "mail.yandex.ru",
            "e.mail.ru",
            "*.paypal.com",
            "online.sberbank.ru",
            "*.tbank.ru",
            "*.gosuslugi.ru",
            "*.gov.uk",
            "*.irs.gov",
        ],
        max_length=200,
    )
    """Hosts (``mail.example.com``) or everything under a domain (``*.example.com``) watch mode covers."""
    injection_monitor: bool = False
    """A small model reads the text of every page the agent opens on a site new to its session, before
    the agent does, and pauses the browser and asks the operator when the page looks like it is
    talking to the agent. Off by default: it costs a model call per new site."""
    injection_monitor_preset: str = ""
    """The model preset the monitor asks; empty = a middle one of the table (the weaker of two), which
    is what reading one page for one word needs."""
    extract_preset: str = ""
    """The model preset ``BrowserText(query=…)`` reads a page with, part by part, to hand the agent
    only what it asked for; empty = a middle one of the table, as for the monitor. Its answer is
    fenced as page content: the model read the page, so it speaks for the page, not the operator."""
    point_clicks: bool = False
    """Whether the agent may click, hover or double-click at a point of the viewport rather than on a
    ref, for what the outline does not name (a canvas, a map). Off by default: a point is harder for
    the operator to read in the action log. The element found at the point is hit-tested and
    classified as a ref's would be, so a sensitive one is still asked about."""


class HeartbeatConfig(BaseModel):
    """A periodic unattended check driven by the HEARTBEAT.md file on the state volume."""

    enabled: bool = False
    interval_minutes: int = Field(default=60, ge=5)
    active_hours: str = Field(default="08:00-23:00", pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    """UTC window ``HH:MM-HH:MM`` in which heartbeats run; may wrap past midnight."""
    preset: str = ""
    """Model preset for heartbeat runs; empty = the default preset."""
    max_runs_per_day: int = Field(default=24, ge=1)


class TelegramConfig(BaseModel):
    mode: TelegramMode | None = None
    """Where sessions live in Telegram. ``topics``: one forum topic per session in the bound
    supergroup. ``private``: every session lives in the operator's private chat, which is a window
    onto one of them at a time (/sessions, /use). Unset means the shape follows the installation —
    topics once a group was bound, the private chat otherwise."""
    forum_chat_id: int = 0
    """Supergroup with topics; 0 means "not bound yet"."""
    general_topic_id: int = 0
    status_edit_interval_seconds: float = 1.0
    status_edit_tiers: list[tuple[float, float]] = Field(default_factory=lambda: [(60, 1), (300, 2), (900, 5), (0, 10)])
    """``[[run age in seconds, multiplier], …]``: the status message is edited less often as a run ages
    (the last entry, age 0, applies beyond the previous one). Multiplies ``status_edit_interval_seconds``."""
    slow_tool_seconds: int = Field(default=60, ge=5)
    """A tool call running longer than this is marked as slow in the status message."""
    inbound_merge_window_seconds: float = 1.5
    photo_caption_wait_seconds: float = 8.0
    """A photo without a caption waits this long for the message that usually follows it (a voice note, the text)."""
    stale_after_seconds: int = Field(default=300, ge=0)
    """Messages older than this when received (queued while the bot was down) are acknowledged, not executed. 0 = off."""
    max_inbound_file_mb: int = Field(default=1500, ge=1)
    """Files larger than this are refused before download."""
    forward_unknown_commands: bool = True
    """``/anything`` that is not a bot command goes to the agent as text."""
    reactions: bool = True
    """React to the operator's messages with the run's state (👀 received, 🔥 done, 💔 failed)."""
    topic_status_emoji: bool = True
    """Prefix a session's topic name with its state (🟢 running, ❓ waiting, ✅ done, 💥 failed)."""
    verbosity: int = 1
    """0 = final answers only, 1 = tool summaries, 2 = everything."""
    streaming: bool = True
    """Stream the answer as a live draft (sendMessageDraft) while it is generated; private chats only."""
    draft_interval_seconds: float = 0.35

    def session_mode(self) -> TelegramMode:
        """The mode in force: the explicit choice, or topics for an installation that bound a group."""
        return self.mode or ("topics" if self.forum_chat_id else "private")


OPENCODE_GO_PRICING: dict[str, dict[str, float]] = {
    # OpenCode Go list prices (USD per 1M tokens, opencode.ai/docs/go): the subscription is prepaid, the metered
    # spend is what counts against its $12 / 5 h, $30 / week and $60 / month allowance.
    "deepseek-v4.1-flash": {"input": 0.30, "output": 1.20, "cache_hit": 0.006},
    "deepseek-v4-flash": {"input": 0.30, "output": 1.20, "cache_hit": 0.006},
    "deepseek-flash": {"input": 0.30, "output": 1.20, "cache_hit": 0.006},
    "deepseek-v4-pro": {"input": 1.32, "output": 3.96, "cache_hit": 0.044},
    "glm-5.3-flash": {"input": 0.15, "output": 0.50, "cache_hit": 0.03},
    "glm-5": {"input": 1.40, "output": 4.40, "cache_hit": 0.26},
    "kimi-k3": {"input": 3.00, "output": 15.00, "cache_hit": 0.30},
    "kimi-k2.7-code": {"input": 0.95, "output": 4.00, "cache_hit": 0.19},
    "kimi-k2.6": {"input": 0.95, "output": 4.00, "cache_hit": 0.16},
    "qwen3.8-max": {"input": 2.00, "output": 6.00, "cache_hit": 0.25},
    "qwen3.8-flash": {"input": 0.15, "output": 0.47, "cache_hit": 0.016},
    "qwen3.7-max": {"input": 2.50, "output": 7.50, "cache_hit": 0.50},
    "qwen3.7-plus": {"input": 0.40, "output": 1.60, "cache_hit": 0.04},
    "qwen3.6-plus": {"input": 0.50, "output": 3.00, "cache_hit": 0.05},
    "minimax-m3": {"input": 0.30, "output": 1.20, "cache_hit": 0.06},
    "minimax-m2.7": {"input": 0.30, "output": 1.20, "cache_hit": 0.06},
    "mimo-v2.5-pro": {"input": 0.435, "output": 0.87, "cache_hit": 0.003625},
    "mimo-v2.5": {"input": 0.14, "output": 0.28, "cache_hit": 0.0028},
    "longcat-2.0": {"input": 0.30, "output": 1.20, "cache_hit": 0.006},
    "hy4-preview": {"input": 0.834, "output": 2.501, "cache_hit": 0.042},
    "hy3": {"input": 0.14, "output": 0.58, "cache_hit": 0.035},
    "grok-4.6": {"input": 4.00, "output": 12.00, "cache_hit": 1.00},
    "gpt-5.6-luna": {"input": 0.40, "output": 1.80, "cache_hit": 0.04},
    "muse-spark": {"input": 0.10, "output": 0.20, "cache_hit": 0.002},
}


class RuntimeConfig(BaseModel):
    seeded: list[str] = Field(default_factory=list)
    """Seeds already applied to this file (``claude-subscription`` …). A seed adds a provider or presets once;
    listed here it is never applied again, so what the operator removes stays removed."""
    model: ModelConfig = Field(default_factory=ModelConfig)
    presets: dict[str, ModelPresetConfig] = Field(default_factory=dict)
    """Named models keyed by id. Empty on a fresh install: a provider endpoint is not a model, and
    which model to run — and pay for — is the operator's to pick. The app asks for one at first login
    (Settings → Models → Add a model); ``deploy/config.example.toml`` shows the shape of an entry."""
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {
            "deepseek": ProviderConfig(kind="deepseek", base_url=keyproxy_base() + "/deepseek"),
            "openrouter": ProviderConfig(kind="openrouter", base_url=keyproxy_base() + "/openrouter"),
            "vllm": ProviderConfig(kind="vllm", base_url=""),
            "grok": ProviderConfig(kind="openai_compat", base_url=keyproxy_base() + "/grok/v1", timeout_seconds=900.0, pricing={"grok": {"input": 0.0, "output": 0.0, "cache_hit": 0.0}}),
            "codex": ProviderConfig(kind="openai_compat", base_url=keyproxy_base() + "/codex/v1", timeout_seconds=900.0, pricing={"gpt": {"input": 0.0, "output": 0.0, "cache_hit": 0.0}}),
            "claude": ProviderConfig(kind="openai_compat", base_url=keyproxy_base() + "/claude/v1", timeout_seconds=900.0, pricing={"claude": {"input": 0.0, "output": 0.0, "cache_hit": 0.0}}),
            "opencode": ProviderConfig(kind="opencode", base_url=keyproxy_base() + "/opencode", timeout_seconds=900.0, pricing=OPENCODE_GO_PRICING),
            "openai": ProviderConfig(kind="openai_compat", name="OpenAI API", base_url=keyproxy_base() + "/openai"),
            "anthropic": ProviderConfig(kind="openai_compat", base_url=keyproxy_base() + "/anthropic"),
            "zai": ProviderConfig(name="Z.AI API", base_url=keyproxy_base() + "/zai"),
            "zai_coding": ProviderConfig(name="Z.AI Coding Plan", base_url=keyproxy_base() + "/zai_coding"),
            "minimax": ProviderConfig(name="MiniMax API", base_url=keyproxy_base() + "/minimax"),
            "minimax_plan": ProviderConfig(name="MiniMax Token Plan", base_url=keyproxy_base() + "/minimax_plan"),
            "moonshot": ProviderConfig(name="Moonshot AI API", base_url=keyproxy_base() + "/moonshot"),
            "kimi_coding": ProviderConfig(name="Kimi Code", base_url=keyproxy_base() + "/kimi_coding"),
        }
    )
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    self_change: SelfChangeConfig = Field(default_factory=SelfChangeConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    balance: BalanceConfig = Field(default_factory=BalanceConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    ops: OpsConfig = Field(default_factory=OpsConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    board: BoardConfig = Field(default_factory=BoardConfig)
    peers: PeersConfig = Field(default_factory=PeersConfig)
    subagents: SubagentsConfig = Field(default_factory=SubagentsConfig)
    staff: StaffConfig = Field(default_factory=StaffConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    dispatcher: DispatcherConfig = Field(default_factory=DispatcherConfig)
    watches: WatchesConfig = Field(default_factory=WatchesConfig)
    harness: HarnessConfig = Field(default_factory=HarnessConfig)
    loops: LoopsConfig = Field(default_factory=LoopsConfig)
    terminals: TerminalsConfig = Field(default_factory=TerminalsConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    modes: dict[str, ModeConfig] = Field(default_factory=lambda: {k: v.model_copy() for k, v in DEFAULT_MODES.items()})
    webhooks: dict[str, WebhookConfig] = Field(default_factory=dict)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    answer_language: str = "auto"
    """"auto" answers in the language of the request; otherwise a language name."""

    @property
    def has_model(self) -> bool:
        """Whether anything can run at all. False on a fresh install until the operator adds a model."""
        return bool(self.presets)

    def default_preset(self, preset_id: str | None = None) -> tuple[str, ModelPresetConfig] | None:
        """The named preset, the default one, or None when the table is empty.

        The form for everything that only shows a model — a list, a settings line, a status chip.
        Use :meth:`preset` where the caller genuinely needs one and cannot go on without it.
        """
        if preset_id and preset_id in self.presets:
            return preset_id, self.presets[preset_id]
        if self.model.preset in self.presets:
            return self.model.preset, self.presets[self.model.preset]
        if self.presets:
            first = next(iter(self.presets))
            return first, self.presets[first]
        return None

    def preset(self, preset_id: str | None = None) -> tuple[str, ModelPresetConfig]:
        """The named preset, or the default one when the id is empty/unknown.

        Raises :class:`NoModelConfigured` when there is no model at all; its message is the one the
        operator reads, so it may be handed straight to a chat reply or an HTTP body.
        """
        found = self.default_preset(preset_id)
        if found is None:
            raise NoModelConfigured
        return found

    def strongest_preset(self) -> str | None:
        """The preset most likely to be the strongest model, for work that is judgement rather than typing.

        The host cannot know which model is stronger, and prices say little when several models are
        on a subscription. What a preset does say is how hard it is asked to think and how much it
        may hold: the highest reasoning effort with thinking on wins, then the larger window, then the
        longer reply, and a tie goes to the default preset, then to the order of the table. It is a
        guess the operator corrects once, in Settings → Models.
        """
        if not self.presets:
            return None
        effort = {name: rank for rank, name in enumerate(REASONING_EFFORTS)}
        order = list(self.presets)

        def strength(pid: str) -> tuple[int, int, int, int, int, int]:
            p = self.presets[pid]
            return (int(p.thinking), effort.get(p.reasoning_effort, 0), p.context_window, p.max_output_tokens, int(pid == self.model.preset), -order.index(pid))

        return max(order, key=strength)

    def middle_preset(self) -> str | None:
        """A preset in the middle of the table by the same measure as :meth:`strongest_preset`: for
        work that is routing and following rather than judgement, where the strongest is waste. With
        two presets the weaker one; with one, that one."""
        if not self.presets:
            return None
        strongest = self.strongest_preset()
        effort = {name: rank for rank, name in enumerate(REASONING_EFFORTS)}
        order = list(self.presets)

        def strength(pid: str) -> tuple[int, int, int, int, int]:
            p = self.presets[pid]
            return (int(p.thinking), effort.get(p.reasoning_effort, 0), p.context_window, p.max_output_tokens, -order.index(pid))

        ranked = sorted(order, key=strength)
        if len(ranked) > 1 and ranked[-1] != strongest:
            ranked.remove(strongest)  # type: ignore[arg-type]
            ranked.append(strongest)  # type: ignore[arg-type]
        return ranked[(len(ranked) - 1) // 2]

    def dispatcher_preset(self) -> str | None:
        """The preset the main orchestrator runs: the one chosen in Settings, else a mid-tier one."""
        if self.dispatcher.preset and self.dispatcher.preset in self.presets:
            return self.dispatcher.preset
        return self.middle_preset()

    def orchestrator_preset(self, project_choice: str = "") -> str | None:
        """The preset a project orchestrator runs: its project's choice, else the Settings default, else the strongest."""
        for pid in (project_choice, self.orchestrator.preset):
            if pid and pid in self.presets:
                return pid
        return self.strongest_preset()

    def vision_preset(self) -> tuple[str, ModelPresetConfig] | None:
        if self.vision.preset and self.vision.preset in self.presets and self.presets[self.vision.preset].images:
            return self.vision.preset, self.presets[self.vision.preset]
        for pid, preset in self.presets.items():
            if preset.images:
                return pid, preset
        return None

    @classmethod
    def load(cls, path: Path) -> RuntimeConfig:
        if not path.exists():
            # A seed adds presets to a config written before that seed existed. A file created now
            # is not one of those: every seed counts as applied, so a fresh install stays empty.
            config = cls(seeded=list(SEEDS))
            # What the setup wizard wrote to the environment is not an upgrade seed: it applies to a
            # new file too, which is the file the wizard's first run creates.
            raw = config.model_dump(mode="json")
            if _seed_from_setup(raw):
                config = cls.model_validate(raw)
            config.save(path)
            return config
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        if _migrate(raw):
            config = cls.model_validate(raw)
            config.save(path)
            return config
        return cls.model_validate(raw)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".toml.tmp")
        with tmp.open("wb") as fh:
            tomli_w.dump(_without_none(self.model_dump(mode="json")), fh)
        tmp.replace(path)


def _without_none(value: Any) -> Any:
    """TOML has no null: optional sections that are unset are simply omitted."""
    if isinstance(value, dict):
        return {k: _without_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_none(v) for v in value if v is not None]
    return value


def preset_id_for(provider_id: str, model: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in model).strip(".-") or "model"
    return f"{provider_id}.{slug}"


LEGACY_PROVIDER_KEYS = ("default_model", "supports_images", "supports_thinking")
LEGACY_MODEL_KEYS = ("provider", "name", "thinking", "reasoning_effort", "context_window", "max_output_tokens")


def _seed_presets(raw: dict[str, Any]) -> bool:
    """Convert configs written before presets carried the model settings.

    Provider ``default_model``/``supports_*`` and the global ``[model]`` thinking/window
    fields become presets (one per provider default model, plus the active pair), the
    chain of provider ids becomes a chain of preset ids, ``[vision]`` points at a preset,
    and the legacy keys are dropped. Returns True when anything changed.
    """
    model = dict(raw.get("model") or {})
    providers = raw.get("providers") or {}
    legacy = any(k in model for k in LEGACY_MODEL_KEYS) or any(
        isinstance(p, dict) and any(k in p for k in LEGACY_PROVIDER_KEYS) for p in providers.values()
    ) or any(k in (raw.get("vision") or {}) for k in ("provider", "model"))
    if not legacy:
        return False
    presets: dict[str, Any] = {pid: dict(p) for pid, p in (raw.get("presets") or {}).items() if isinstance(p, dict)}
    base = {
        "thinking": bool(model.get("thinking", True)),
        "reasoning_effort": model.get("reasoning_effort") or "medium",
        "context_window": int(model.get("context_window") or 128_000),
        "max_output_tokens": int(model.get("max_output_tokens") or 32_000),
    }
    by_provider: dict[str, str] = {}
    for provider_id, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        default_model = str(provider.pop("default_model", "") or "").strip()
        images = bool(provider.pop("supports_images", False))
        provider.pop("supports_thinking", None)
        if default_model:
            pid = next((k for k, v in presets.items() if v.get("provider") == provider_id and v.get("model") == default_model), None)
            if pid is None:
                pid = preset_id_for(provider_id, default_model)
                presets[pid] = {"provider": provider_id, "model": default_model, "label": ""}
            presets[pid].setdefault("images", images)
            by_provider.setdefault(provider_id, pid)
    active_provider, active_name = model.get("provider") or "", (model.get("name") or "").strip()
    default_pid = model.get("preset") or ""
    if active_provider and active_name:
        default_pid = next((k for k, v in presets.items() if v["provider"] == active_provider and v["model"] == active_name), "")
        if not default_pid:
            default_pid = preset_id_for(active_provider, active_name)
            presets[default_pid] = {"provider": active_provider, "model": active_name, "label": ""}
        by_provider.setdefault(active_provider, default_pid)
    vision = dict(raw.get("vision") or {})
    v_provider, v_model = vision.pop("provider", ""), vision.pop("model", "")
    if v_provider and v_model:
        vpid = next((k for k, v in presets.items() if v["provider"] == v_provider and v["model"] == v_model), None)
        if vpid is None:
            vpid = preset_id_for(v_provider, v_model)
            presets[vpid] = {"provider": v_provider, "model": v_model, "label": "", "thinking": False}
        presets[vpid]["images"] = True
        vision["preset"] = vpid
    for preset in presets.values():
        for key, value in base.items():
            preset.setdefault(key, value)
        preset.setdefault("images", False)
    chain = [by_provider[p] for p in (model.get("chain") or []) if isinstance(p, str) and p in by_provider and by_provider[p] != default_pid]
    chain += [c for c in (model.get("chain") or []) if isinstance(c, str) and c in presets and c not in chain and c != default_pid]
    raw["model"] = {"preset": default_pid or (next(iter(presets)) if presets else ""), "chain": chain}
    raw["vision"] = vision
    raw["presets"] = presets
    return True


SEEDS = ("claude-subscription", "openai-anthropic-keys", "more-provider-endpoints")
"""Every seed a config can have had applied, in the order they were introduced. The seeds that follow
the setup wizard's environment (``voice-cloud``, ``local-model:<id>``) are not listed: a new file
should get them, because the wizard's answers are about this installation."""


def _seed_claude_subscription(raw: dict[str, Any]) -> bool:
    """Add the Claude Code subscription endpoint and presets to a config that has never had them.

    Once: the seed is recorded in ``seeded`` and never applied again, so an operator who removes the
    presets (or the provider) keeps them removed across restarts.
    """
    seeded = raw.setdefault("seeded", [])
    if not isinstance(seeded, list):
        seeded = raw["seeded"] = []
    if "claude-subscription" in seeded:
        return False
    seeded.append("claude-subscription")
    changed = True
    providers = raw.setdefault("providers", {})
    presets = raw.setdefault("presets", {})
    if isinstance(providers, dict) and "claude" not in providers:
        providers["claude"] = {
            "kind": "openai_compat",
            "base_url": keyproxy_base() + "/claude/v1",
            "timeout_seconds": 900.0,
            "pricing": {"claude": {"input": 0.0, "output": 0.0, "cache_hit": 0.0}},
        }
        changed = True
    seeds = {
        "claude.sonnet-5": {"provider": "claude", "model": "claude-sonnet-5", "label": "Claude Sonnet 5 (Claude Code subscription)", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 200_000},
        "claude.opus-5": {"provider": "claude", "model": "claude-opus-5", "label": "Claude Opus 5 (Claude Code subscription)", "thinking": True, "reasoning_effort": "medium", "images": True, "context_window": 1_000_000},
        "claude.fable-5.1": {"provider": "claude", "model": "claude-fable-5-1", "label": "Claude Fable 5.1 (Claude Code subscription)", "thinking": True, "reasoning_effort": "medium", "images": True, "context_window": 1_000_000},
    }
    if isinstance(presets, dict):
        for pid, spec in seeds.items():
            if pid not in presets:
                presets[pid] = spec
                changed = True
    return changed


def _seed_openai_anthropic_keys(raw: dict[str, Any]) -> bool:
    """Add the ``openai`` and ``anthropic`` providers, whose keys the key proxy holds, once.

    A provider here is only an address on the proxy: it is usable the moment a key is written to the
    proxy's environment, and shows "no key" until then. Once, like the Claude seed, so a provider the
    operator deletes stays deleted.
    """
    seeded = raw.setdefault("seeded", [])
    if not isinstance(seeded, list):
        seeded = raw["seeded"] = []
    if "openai-anthropic-keys" in seeded:
        return False
    seeded.append("openai-anthropic-keys")
    providers = raw.setdefault("providers", {})
    if isinstance(providers, dict):
        for name in ("openai", "anthropic"):
            providers.setdefault(name, {"kind": "openai_compat", "base_url": keyproxy_base() + "/" + name})
    return True


def _seed_from_setup(raw: dict[str, Any]) -> bool:
    """Apply the first-run wizard's answers, which the launcher writes to the app's environment.

    ``DAEDALUS_LOCAL_MODEL``: a model served by the endpoint the wizard registered with the key proxy
    as ``local``. ``DAEDALUS_VOICE``: ``cloud`` transcribes through the ``openai`` provider; ``local``
    needs nothing here (the launcher installs the speech extra and the Voice page's own model is used
    when no endpoint is configured), and ``off`` is the empty default.

    Each answer is applied once and recorded in ``seeded``: deleting the preset or clearing the ASR
    provider afterwards sticks, while choosing another model in a later setup run adds that one.
    """
    changed = False
    seeded = raw.setdefault("seeded", [])
    if not isinstance(seeded, list):
        seeded = raw["seeded"] = []
    model = os.environ.get("DAEDALUS_LOCAL_MODEL", "").strip()
    marker = f"local-model:{model}"
    if model and marker not in seeded:
        seeded.append(marker)
        changed = True
        providers = raw.setdefault("providers", {})
        presets = raw.setdefault("presets", {})
        if isinstance(providers, dict):
            local = providers.get("local")
            if local is None:
                local = providers["local"] = {"kind": "openai_compat", "name": "Local endpoint", "base_url": keyproxy_base() + "/local", "timeout_seconds": 600.0, "pricing": {}}
            # A model on the operator's own machine costs nothing; priced per model id because a
            # later setup run may add another, and a provider that already exists keeps its own table.
            if isinstance(local, dict) and isinstance(local.get("pricing"), dict):
                local["pricing"].setdefault(model, {"input": 0.0, "output": 0.0, "cache_hit": 0.0})
        if isinstance(presets, dict):
            pid = preset_id_for("local", model)
            if pid not in presets:
                # 32k, not the 128k default: a local server usually loads a small context, and a run
                # compacts against this number.
                presets[pid] = {"provider": "local", "model": model, "label": f"{model} (local)", "thinking": False, "context_window": 32_000}
            chosen = raw.get("model")
            if not isinstance(chosen, dict):
                chosen = raw["model"] = {}
            if not chosen.get("preset"):
                chosen["preset"] = pid
    if os.environ.get("DAEDALUS_VOICE", "").strip().lower() == "cloud" and "voice-cloud" not in seeded:
        seeded.append("voice-cloud")
        changed = True
        asr = raw.get("asr")
        if not isinstance(asr, dict):
            asr = raw["asr"] = {}
        if not asr.get("provider") and not asr.get("url"):
            asr["provider"] = "openai"
    return changed


def _seed_provider_endpoints(raw: dict[str, Any]) -> bool:
    """Offer the new endpoints once, leaving later operator removals alone."""
    seeded = raw.setdefault("seeded", [])
    if not isinstance(seeded, list):
        seeded = raw["seeded"] = []
    if "more-provider-endpoints" in seeded:
        return False
    seeded.append("more-provider-endpoints")
    providers = raw.setdefault("providers", {})
    if isinstance(providers, dict):
        for provider_id, name in (
            ("openai", "OpenAI API"),
            ("zai", "Z.AI API"),
            ("zai_coding", "Z.AI Coding Plan"),
            ("minimax", "MiniMax API"),
            ("minimax_plan", "MiniMax Token Plan"),
            ("moonshot", "Moonshot AI API"),
            ("kimi_coding", "Kimi Code"),
        ):
            providers.setdefault(provider_id, {"kind": "openai_compat", "name": name, "base_url": keyproxy_base() + "/" + provider_id})
    return True


def _migrate_web_search(raw: dict[str, Any]) -> bool:
    """The DuckDuckGo-only fields of ``[tools.web]`` became the ``search`` section with a backend choice."""
    web = (raw.get("tools") or {}).get("web")
    if not isinstance(web, dict) or not any(k in web for k in ("search_url", "search_region", "search_results", "search_timeout_seconds")):
        return False
    search = web.get("search")
    if not isinstance(search, dict):
        search = web["search"] = {}
    ddg = search.get("duckduckgo")
    if not isinstance(ddg, dict):
        ddg = search["duckduckgo"] = {}
    if "search_url" in web:
        ddg["url"] = web.pop("search_url")
    if "search_region" in web:
        ddg["region"] = web.pop("search_region")
    if "search_results" in web:
        search["results"] = web.pop("search_results")
    if "search_timeout_seconds" in web:
        search["timeout_seconds"] = web.pop("search_timeout_seconds")
    return True


def _migrate_keyproxy_base(raw: dict[str, Any], base: str = "") -> bool:
    """Move a container's key-proxy address to this machine's, wherever it was written down.

    ``keyproxy_base()`` only decides a *default*, and the first run persists what it decided. An
    installation whose ``config.toml`` was written against a compose project — or written by a
    launcher whose checkout still held the container default — keeps eleven base URLs pointing at a
    host that does not resolve here, and every provider and every search backend answers
    ConnectError with a remedy about the network. Rewriting the prefix is idempotent: after it there
    is no container address left to find.
    """
    base = base or keyproxy_base()
    if not native_mode() or base == CONTAINER_KEYPROXY_BASE:
        return False
    changed = False

    def walk(node: Any) -> Any:
        nonlocal changed
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str) and node.startswith(CONTAINER_KEYPROXY_BASE):
            changed = True
            return base + node[len(CONTAINER_KEYPROXY_BASE) :]
        return node

    raw.update(walk(raw))
    return changed


def _migrate_inbox_retention(raw: dict[str, Any]) -> bool:
    """``[scheduler] inbox_keep_days`` became ``[notifications] keep_days`` when the inbox did."""
    scheduler = raw.get("scheduler")
    if not isinstance(scheduler, dict) or "inbox_keep_days" not in scheduler:
        return False
    days = scheduler.pop("inbox_keep_days")
    notifications = raw.get("notifications")
    if not isinstance(notifications, dict):
        notifications = raw["notifications"] = {}
    notifications.setdefault("keep_days", days)
    return True


def _migrate(raw: dict[str, Any]) -> bool:
    """Rewrite config shapes older versions wrote; returns True when something changed."""
    changed = _seed_presets(raw)
    changed = _migrate_inbox_retention(raw) or changed
    changed = _migrate_keyproxy_base(raw) or changed
    changed = _seed_claude_subscription(raw) or changed
    changed = _seed_openai_anthropic_keys(raw) or changed
    changed = _seed_provider_endpoints(raw) or changed
    changed = _seed_from_setup(raw) or changed
    changed = _migrate_web_search(raw) or changed
    for provider in (raw.get("providers") or {}).values():
        pricing = provider.get("pricing") if isinstance(provider, dict) else None
        if not isinstance(pricing, dict):
            continue
        for model, entry in list(pricing.items()):
            if isinstance(entry, dict) and "off_peak_utc" in entry and "peak_utc" not in entry:
                # The seeded DeepSeek tables used an off-peak window; the built-in schedule replaces them.
                del pricing[model]
                changed = True
    return changed


__all__ = [
    "NO_MODEL_MESSAGE",
    "NoModelConfigured",
    "preset_id_for",
    "ApprovalMode",
    "BalanceConfig",
    "LimitsConfig",
    "McpConfig",
    "McpOAuthConfig",
    "McpServerConfig",
    "ModelConfig",
    "NotificationsConfig",
    "ModelPresetConfig",
    "KNOWN_TOOL_LIMITS",
    "ON_DEMAND_GROUPS_OFF",
    "PROVIDER_KINDS",
    "PromptConfig",
    "ProviderConfig",
    "ProviderKind",
    "ReasoningEffort",
    "RuntimeConfig",
    "SchedulerConfig",
    "SelfChangeConfig",
    "Settings",
    "TelegramConfig",
    "ToolsConfig",
    "WebToolsConfig",
    "WebSearchConfig",
    "ExecToolsConfig",
    "AsrConfig",
    "SttConfig",
    "BoardConfig",
    "PeersConfig",
    "StaffConfig",
    "OrchestratorConfig",
    "DispatcherConfig",
    "DISPATCHER_TOOLS",
    "HarnessConfig",
    "DEFAULT_MODES",
    "HeartbeatConfig",
    "ModeConfig",
    "OpsConfig",
    "WebhookConfig",
    "VisionConfig",
    "VoiceConfig",
    "TtsConfig",
    "VOICE_TOOLS",
    "VOICE_ONLY_TOOLS",
    "STAFF_BLOCKED_TOOLS",
    "STAFF_ONLY_TOOLS",
    "SUBAGENT_BLOCKED_TOOLS",
    "ORCHESTRATOR_TOOLS",
    "ORCHESTRATOR_ONLY_TOOLS",
    "max_advertised_tools_for",
    "on_demand_tool_groups_for",
]
