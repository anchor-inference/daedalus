"""Secrets the operator hands the agent to use without reading: a router's password, an API key for one job.

The operator types a value once in the app (or sends ``/secret`` in Telegram) under a name such as
``router_admin``, for one chat or for a whole project. From then on the agent knows the name, the
operator's note and a placeholder, ``«secret:router_admin»`` — never the value. The value reaches only
the places that act on it:

* a tool argument: the host puts the value in place of the placeholder on the way out, in the browser's
  typing and in MCP calls (:meth:`OperatorSecrets.substitute`);
* a process: every command the agent runs, and every coding CLI that works for a project, finds it in
  ``$DAEDALUS_SECRET_ROUTER_ADMIN`` and in a file named by ``$DAEDALUS_SECRET_ROUTER_ADMIN_FILE``
  (:meth:`OperatorSecrets.environment`).

Everything that comes back is masked: the shared redactor learns each value and writes the placeholder
where the value was, so a password a page echoes, a command prints or a log line carries is back to
``«secret:router_admin»`` before the model, the transcript, the app or the log sees it.

At rest each value is sealed with AES-GCM under a key kept in the installation's secrets directory, which
the agent's own tools can neither read nor write; the row's identity is the associated data, so a sealed
value copied onto another row does not open. The database alone gives nothing away.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")
"""A secret's name: what the placeholder and the environment variable are made of, so nothing a shell or
a regular expression would have to quote."""
PLACEHOLDER_RE = re.compile(r"«secret:([a-z][a-z0-9_]{0,47})»")
LOOSE_PLACEHOLDER_RE = re.compile(r"\s*[\"'`]?\s*«secret:([a-z][a-z0-9_]{0,47})»\s*[\"'`]?\s*")
"""An argument that is the placeholder and nothing else, however the model quoted it."""
ENV_PREFIX = "DAEDALUS_SECRET_"
SCOPES = ("session", "project")
MAX_VALUE = 16_384
"""A password, a key, a short certificate. Anything larger is a file, and files go through the workspace."""
MAX_NOTE = 500
KEY_FILE = "operator-secrets.key"
LAUNCH_FILE_PREFIX = "secret-"
"""A staff launch's copy of a secret is the file ``secret-<name>`` in its launch directory."""


class SecretError(ValueError):
    """A request the store refuses, in words the operator can act on."""


def placeholder(name: str) -> str:
    return f"«secret:{name}»"


def is_whole_placeholder(text: str) -> bool:
    """Whether a value is one placeholder and nothing else, however quoted: what may go into a password field."""
    return bool(LOOSE_PLACEHOLDER_RE.fullmatch(text))


def env_name(name: str) -> str:
    return ENV_PREFIX + name.upper()


def normalise_name(raw: str) -> str:
    """The name as stored: lower case, spaces and dashes as underscores. Refused when nothing usable is left."""
    name = re.sub(r"[\s\-.]+", "_", str(raw or "").strip().lower())
    if not NAME_RE.fullmatch(name):
        raise SecretError("a name is a letter followed by up to 47 letters, digits or underscores, like router_admin")
    return name


@dataclass(slots=True)
class Secret:
    id: str
    name: str
    scope_kind: str
    scope_id: str
    note: str
    created_at: str
    updated_at: str
    last_used_at: str | None = None
    last_used_by: str = ""
    uses: int = 0
    value: str | None = field(default=None, repr=False)
    """None when the value could not be opened: the key file was replaced or the row was damaged."""

    @property
    def placeholder(self) -> str:
        return placeholder(self.name)

    @property
    def env(self) -> str:
        return env_name(self.name)

    def public(self) -> dict[str, Any]:
        """What the app is shown: everything but the value."""
        return {
            "id": self.id, "name": self.name, "scope": self.scope_kind, "scope_id": self.scope_id, "note": self.note,
            "placeholder": self.placeholder, "env": self.env, "created_at": self.created_at, "updated_at": self.updated_at,
            "last_used_at": self.last_used_at, "last_used_by": self.last_used_by, "uses": self.uses, "readable": self.value is not None,
        }


def load_key(path: Path) -> bytes:
    """The sealing key, made on first use. Written owner-only before a byte of it lands on disk."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b""
    if len(data) == 32:
        return data
    if data:
        raise SecretError(f"{path.name} is not a 32-byte key; move it aside to start a new one (the stored secrets go with it)")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:  # pragma: no cover — a filesystem without modes (Windows keeps the folder in the user's profile)
        pass
    key = AESGCM.generate_key(bit_length=256)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key)
        handle.flush()
        os.fsync(handle.fileno())
    return key


def _aad(secret_id: str, scope_kind: str, scope_id: str, name: str) -> bytes:
    return "\0".join((secret_id, scope_kind, scope_id, name)).encode("utf-8")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class NamedRedactor(Protocol):
    """What the store needs of :class:`daedalus.security.redact.Redactor`, named here rather than imported:
    nothing in the security package depends on another module of the host, the redactor included."""

    def replace_named(self, named: Iterable[tuple[str, str]]) -> None: ...


ProjectOf = Callable[[str], "str | None"]
"""``session id -> project id`` (None when the session is not known here)."""
StaffOf = Callable[[str], "str | None"]
"""``session id -> staff member id``, for a staff member's own session; None for every other session."""


class OperatorSecrets:
    """The store and the one in-memory copy of the values the host acts with.

    The values are opened once at start and kept in this process, which needs them to type, to call and to
    start processes with; the database only ever holds them sealed.
    """

    def __init__(self, db: Any, key_path: Path, redactor: NamedRedactor, *, files_dir: Path | None = None) -> None:
        self.db = db
        self.key_path = key_path
        self.redactor = redactor
        self.files_dir = files_dir
        """Where each value is also written as a file for commands that take a path (``curl --netrc-file``,
        ``--password-file``): one folder per scope, owner-only, outside every workspace."""
        self.project_of: ProjectOf = lambda _session_id: None
        self.staff_of: StaffOf = lambda _session_id: None
        self._aead: AESGCM | None = None
        self._by_id: dict[str, Secret] = {}
        self._grants: dict[str, set[str]] = {}
        """``staff id -> secret ids`` a coordinator handed that member by name."""
        self._retired: dict[str, str] = {}
        """Values of deleted secrets, still masked for the life of the process: deleting a secret takes it
        away from the agent, and must not also make it readable in what a command prints afterwards."""
        self._pending: set[asyncio.Task[Any]] = set()

    def _cipher(self) -> AESGCM:
        if self._aead is None:
            self._aead = AESGCM(load_key(self.key_path))
        return self._aead

    def seal(self, secret_id: str, scope_kind: str, scope_id: str, name: str, value: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._cipher().encrypt(nonce, value.encode("utf-8"), _aad(secret_id, scope_kind, scope_id, name))

    def open_sealed(self, secret_id: str, scope_kind: str, scope_id: str, name: str, sealed: bytes) -> str | None:
        try:
            return self._cipher().decrypt(sealed[:12], sealed[12:], _aad(secret_id, scope_kind, scope_id, name)).decode("utf-8")
        except Exception:  # noqa: BLE001 — a value that does not open is shown as unreadable, never as a crash
            logger.warning("operator secret %s could not be opened", name)
            return None

    async def load(self) -> None:
        """Open every stored value. A secret whose chat or project is gone goes with it."""
        await self.db.execute("DELETE FROM operator_secrets WHERE scope_kind = 'session' AND scope_id NOT IN (SELECT id FROM sessions)")
        await self.db.execute("DELETE FROM operator_secrets WHERE scope_kind = 'project' AND scope_id NOT IN (SELECT id FROM projects)")
        rows = await self.db.fetchall("SELECT * FROM operator_secrets")
        self._by_id = {}
        for row in rows:
            secret = Secret(
                id=row["id"], name=row["name"], scope_kind=row["scope_kind"], scope_id=row["scope_id"], note=row["note"],
                created_at=row["created_at"], updated_at=row["updated_at"], last_used_at=row["last_used_at"],
                last_used_by=row["last_used_by"], uses=int(row["uses"] or 0),
            )
            secret.value = self.open_sealed(secret.id, secret.scope_kind, secret.scope_id, secret.name, bytes(row["sealed"]))
            self._by_id[secret.id] = secret
        self._grants = {}
        for row in await self.db.fetchall("SELECT secret_id, staff_id FROM operator_secret_grants"):
            self._grants.setdefault(row["staff_id"], set()).add(row["secret_id"])
        self._refresh()

    def _refresh(self) -> None:
        """Teach the redactor the current values and rewrite the files commands read."""
        named = [(s.value, s.placeholder) for s in self._by_id.values() if s.value]
        named.extend(self._retired.items())
        self.redactor.replace_named(named)
        if self.files_dir is not None:
            self._write_files()

    def _write_files(self) -> None:
        assert self.files_dir is not None
        wanted: dict[Path, str] = {}
        for secret in self._by_id.values():
            if secret.value is not None:
                wanted[self._file_of(secret)] = secret.value
        try:
            self.files_dir.mkdir(parents=True, exist_ok=True)
            self.files_dir.chmod(0o700)
            for scope in list(self.files_dir.iterdir()):
                for item in list(scope.iterdir()) if scope.is_dir() else [scope]:
                    if item not in wanted:
                        item.unlink(missing_ok=True)
                if scope.is_dir() and not any(scope.iterdir()):
                    shutil.rmtree(scope, ignore_errors=True)
            for path, value in wanted.items():
                path.parent.mkdir(mode=0o700, exist_ok=True)
                temporary = path.with_name(f".{path.name}.tmp")
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(value)
                os.replace(temporary, path)
        except OSError:
            logger.warning("could not write the operator secrets' files", exc_info=True)

    def _file_of(self, secret: Secret) -> Path:
        assert self.files_dir is not None
        return self.files_dir / f"{secret.scope_kind}-{secret.scope_id}" / secret.name

    async def put(self, scope_kind: str, scope_id: str, name: str, value: str, note: str = "") -> Secret:
        """Store a value under a name in a scope. The same name in the same scope is replaced: that is how a
        changed password is handed over again."""
        if scope_kind not in SCOPES or not scope_id:
            raise SecretError("a secret belongs to a chat or to a project")
        name = normalise_name(name)
        if not isinstance(value, str) or not value.strip():
            raise SecretError("the value is empty")
        if len(value) > MAX_VALUE:
            raise SecretError(f"a value is at most {MAX_VALUE} characters; put larger material in a file")
        note = str(note or "").strip()[:MAX_NOTE]
        now = _now()
        existing = next((s for s in self._by_id.values() if (s.scope_kind, s.scope_id, s.name) == (scope_kind, scope_id, name)), None)
        secret_id = existing.id if existing is not None else secrets.token_hex(8)
        sealed = self.seal(secret_id, scope_kind, scope_id, name, value)
        if existing is not None:
            if existing.value and existing.value != value:
                self._retired[existing.value] = existing.placeholder
            await self.db.execute(
                "UPDATE operator_secrets SET sealed = ?, note = ?, updated_at = ? WHERE id = ?", (sealed, note, now, secret_id),
            )
            existing.value, existing.note, existing.updated_at = value, note, now
            secret = existing
        else:
            await self.db.execute(
                "INSERT INTO operator_secrets(id, name, scope_kind, scope_id, note, sealed, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (secret_id, name, scope_kind, scope_id, note, sealed, now, now),
            )
            secret = Secret(id=secret_id, name=name, scope_kind=scope_kind, scope_id=scope_id, note=note, created_at=now, updated_at=now, value=value)
            self._by_id[secret_id] = secret
        self._refresh()
        return secret

    async def delete(self, secret_id: str) -> bool:
        secret = self._by_id.pop(secret_id, None)
        if secret is None:
            return False
        await self.db.execute("DELETE FROM operator_secret_grants WHERE secret_id = ?", (secret_id,))
        await self.db.execute("DELETE FROM operator_secrets WHERE id = ?", (secret_id,))
        for granted in self._grants.values():
            granted.discard(secret_id)
        if secret.value:
            self._retired[secret.value] = secret.placeholder
        self._refresh()
        return True

    def get(self, secret_id: str) -> Secret | None:
        return self._by_id.get(secret_id)

    def listed(self, *, scope_kind: str | None = None, scope_id: str | None = None) -> list[Secret]:
        items = [s for s in self._by_id.values() if (scope_kind is None or s.scope_kind == scope_kind) and (scope_id is None or s.scope_id == scope_id)]
        return sorted(items, key=lambda s: (s.scope_kind, s.scope_id, s.name))

    def available(self, session_id: str, project_id: str | None = None) -> list[Secret]:
        """The secrets a session may use: its own, its project's, and — for a staff member's session — what its
        coordinator handed it. A chat's own secret wins over a project's of the same name: the operator said
        it closer to the work."""
        project = project_id if project_id is not None else self.project_of(session_id)
        staff = self.staff_of(session_id)
        return self._choose(session_id, project, self._grants.get(staff, set()) if staff else set())

    def for_staff(self, staff_id: str, project_id: str) -> list[Secret]:
        """What a staff member that is a coding CLI rather than a session may use: the project's secrets and
        the ones handed to it."""
        return self._choose(None, project_id, self._grants.get(staff_id, set()))

    def _choose(self, session_id: str | None, project_id: str | None, granted: set[str]) -> list[Secret]:
        chosen: dict[str, Secret] = {}
        for secret in self._by_id.values():
            if secret.value is None:
                continue
            if (secret.scope_kind == "session" and secret.scope_id == session_id) or secret.id in granted:
                chosen[secret.name] = secret
            elif secret.scope_kind == "project" and project_id and secret.scope_id == project_id:
                chosen.setdefault(secret.name, secret)
        return sorted(chosen.values(), key=lambda s: s.name)

    async def grant(self, names: Iterable[str], *, from_session: str, staff_id: str, project_id: str | None = None) -> tuple[list[Secret], list[str]]:
        """A coordinator hands secrets it may use to a staff member, by name. ``(handed, unknown names)``; with
        any name unknown nothing is handed, so a typo is a refusal rather than half a hand-over."""
        wanted = [normalise_name(n) for n in names if str(n or "").strip()]
        mine = {s.name: s for s in self.available(from_session, project_id)}
        handed = [mine[n] for n in dict.fromkeys(wanted) if n in mine]
        unknown = [n for n in dict.fromkeys(wanted) if n not in mine]
        if unknown:
            return [], unknown
        now = _now()
        for secret in handed:
            await self.db.execute(
                "INSERT OR IGNORE INTO operator_secret_grants(secret_id, staff_id, granted_by, granted_at) VALUES (?, ?, ?, ?)",
                (secret.id, staff_id, from_session, now),
            )
            self._grants.setdefault(staff_id, set()).add(secret.id)
        return handed, unknown

    def named(self, names: Iterable[str], session_id: str, project_id: str | None = None) -> list[Secret]:
        """The ones among ``names`` this session may use; the rest are left out, not an error."""
        mine = {s.name: s for s in self.available(session_id, project_id)}
        return [mine[n] for n in dict.fromkeys(names) if n in mine]

    def pool(self, *, session_id: str | None = None, staff_id: str | None = None, project_id: str | None = None) -> list[Secret]:
        """What a caller may use, whichever it is: a session, or a staff member that is a coding CLI."""
        if session_id:
            return self.available(session_id, project_id)
        if staff_id and project_id:
            return self.for_staff(staff_id, project_id)
        return []

    def substitute(
        self, value: Any, session_id: str, *, used_by: str, project_id: str | None = None, pool: list[Secret] | None = None,
    ) -> tuple[Any, list[Secret]]:
        """Arguments with every placeholder this session may use replaced by its value, and the secrets used.

        A placeholder the session may not use is left as it is, so the call fails on its own terms (a wrong
        password) rather than being handed another scope's value. ``pool`` overrides the session's own set
        (a staff member's, see :meth:`pool`).
        """
        used: dict[str, Secret] = {}
        mine: dict[str, Secret] | None = {s.name: s for s in pool} if pool is not None else None

        def one(item: Any) -> Any:
            nonlocal mine
            if isinstance(item, str):
                if "«secret:" not in item:
                    return item
                if mine is None:
                    mine = {s.name: s for s in self.available(session_id, project_id)}
                if (whole := LOOSE_PLACEHOLDER_RE.fullmatch(item)) and whole.group(1) in mine:
                    secret = mine[whole.group(1)]
                    used[secret.name] = secret
                    return secret.value

                def put_back(match: re.Match[str]) -> str:
                    secret = mine.get(match.group(1)) if mine is not None else None
                    if secret is None:
                        return match.group(0)
                    used[secret.name] = secret
                    return secret.value or match.group(0)

                return PLACEHOLDER_RE.sub(put_back, item)
            if isinstance(item, dict):
                return {k: one(v) for k, v in item.items()}
            if isinstance(item, list):
                return [one(v) for v in item]
            return item

        out = one(value)
        if used:
            self.record_use(used.values(), used_by)
        return out, list(used.values())

    def environment(self, session_id: str, project_id: str | None = None, *, names: Iterable[str] | None = None, files: bool = True) -> dict[str, str]:
        """Variables for a process this session starts: the value, and the path of the file holding it.

        ``names`` narrows it to those; None is every secret the session may use. ``files=False`` leaves the
        paths out, for a process on another machine, where the files are not.
        """
        chosen = self.available(session_id, project_id) if names is None else self.named(names, session_id, project_id)
        return self.variables(chosen, files=files)

    def variables(self, chosen: Iterable[Secret], *, files: bool = True) -> dict[str, str]:
        env: dict[str, str] = {}
        for secret in chosen:
            if secret.value is None:
                continue
            env[secret.env] = secret.value
            if files and self.files_dir is not None:
                env[secret.env + "_FILE"] = str(self._file_of(secret))
        return env

    def scope_dirs(self, session_id: str) -> list[Path]:
        """The file folders a session's sandbox may see: the folder of each scope it draws a secret from."""
        if self.files_dir is None:
            return []
        return sorted({self._file_of(s).parent for s in self.available(session_id)})

    def rewrite_command(self, command: str, session_id: str) -> tuple[str, list[Secret]]:
        """A shell command with each placeholder turned into its variable, and the secrets it names.

        The value is never written into the command: a command line is visible to every process on the
        machine and is echoed back in errors. ``${VAR}`` works bare and inside double quotes, which is how
        a model writes an argument; inside single quotes nothing expands, and the prompt says so.
        """
        mine = {s.name: s for s in self.available(session_id)}
        if not mine:
            return command, []
        rewritten = PLACEHOLDER_RE.sub(lambda m: "${" + mine[m.group(1)].env + "}" if m.group(1) in mine else m.group(0), command)
        named = [s for s in mine.values() if s.env in rewritten]
        return rewritten, named

    def record_use(self, used: Iterable[Secret], by: str) -> None:
        """Note who used a secret, without holding up the call that used it."""
        items = list(used)
        if not items:
            return
        now = _now()
        for secret in items:
            secret.last_used_at, secret.last_used_by, secret.uses = now, by[:200], secret.uses + 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._write_use([s.id for s in items], now, by[:200]), name="operator-secrets:used")
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _write_use(self, ids: list[str], when: str, by: str) -> None:
        try:
            for secret_id in ids:
                await self.db.execute(
                    "UPDATE operator_secrets SET last_used_at = ?, last_used_by = ?, uses = uses + 1 WHERE id = ?", (when, by, secret_id),
                )
        except Exception:  # noqa: BLE001 — the audit line is a courtesy; the use already happened
            logger.warning("could not record the use of an operator secret", exc_info=True)

    async def settle(self) -> None:
        """Wait for the audit writes in flight (tests, shutdown)."""
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)


def prompt_section(available: list[Secret]) -> str:
    """What the model is told about the secrets it may use: names, notes and how — never a value."""
    if not available:
        return ""
    lines = [
        "## Secrets the operator handed you",
        "The operator stored these values for you to use without seeing them. You know each by its name and "
        "placeholder only. Use a secret only for the purpose the operator gave. Never try to print, cat, echo, "
        "encode, write to a file or send anywhere the value itself; every output that contains it comes back "
        "with the placeholder instead, so trying only wastes a turn.",
    ]
    for secret in available:
        note = f" — {secret.note}" if secret.note else ""
        lines.append(f"- {secret.placeholder}{note} (shell: ${secret.env}, file: ${secret.env}_FILE)")
    lines.append(
        "How to use one: in BrowserAct type, put the placeholder as the text (it may go into a password field). "
        "In an MCP tool's arguments, write the placeholder where the value goes. In Exec, a terminal or a script, "
        "read the environment variable (\"$" + ENV_PREFIX + "NAME\") or the file it names; a placeholder written "
        "into an Exec command is turned into the variable. To give one to a staff member, name it in the "
        "assignment's secrets; never paste a value into a message."
    )
    return "\n".join(lines)


def staff_section(handed: list[Secret], *, mid_launch: bool = False) -> str:
    """What a staff member that is a coding CLI is told about the secrets its launch carries. ``mid_launch``:
    handed over while it runs, when its environment is already fixed and only the launch file is new."""
    if not handed:
        return ""
    lines = [
        "## Secrets the operator handed you",
        "Values the operator stored for this work. You know each by name only; use it only for the purpose given. "
        "Never print, cat, echo, encode, commit or send the value anywhere; output that contains it is masked.",
    ]
    for secret in handed:
        note = f" — {secret.note}" if secret.note else ""
        if mid_launch:
            where = f"file: \"$DAEDALUS_LAUNCH_DIR/{LAUNCH_FILE_PREFIX}{secret.name}\""
        else:
            where = f"shell: \"${secret.env}\", file: \"${secret.env}_FILE\" (also $DAEDALUS_LAUNCH_DIR/{LAUNCH_FILE_PREFIX}{secret.name})"
        lines.append(f"- {secret.placeholder}{note} ({where})")
    lines.append("Pass a value to a command from the variable or the file, never typed out. In the team browser tools, "
                 "write the placeholder as the text to type; the host types the value.")
    return "\n".join(lines)


ATTACHMENT_PREFIX = "[The operator attached a secret for "


def attachment_text(secret: Secret) -> str:
    """The line a message carries in place of a secret the operator attached to it. The app draws a chip
    from the message's names instead and leaves these lines out."""
    note = f" Note: {secret.note.rstrip('.')}." if secret.note else ""
    scope = "this chat" if secret.scope_kind == "session" else "this project"
    return f"{ATTACHMENT_PREFIX}{scope}: {secret.placeholder} (shell: ${secret.env}).{note} The value is not shown to you; use it by its placeholder.]"


def without_attachment_lines(text: str) -> str:
    return "\n".join(line for line in text.split("\n") if not line.startswith(ATTACHMENT_PREFIX)).rstrip()


_shared: OperatorSecrets | None = None


def install(store: OperatorSecrets | None) -> None:
    global _shared
    _shared = store


def shared() -> OperatorSecrets | None:
    """The process's store, once the host has opened it; None in a process that has none (a CLI, a test)."""
    return _shared


__all__ = [
    "ATTACHMENT_PREFIX", "ENV_PREFIX", "LAUNCH_FILE_PREFIX", "NAME_RE", "PLACEHOLDER_RE", "OperatorSecrets", "Secret", "SecretError", "attachment_text", "env_name",
    "install", "load_key", "normalise_name", "placeholder", "prompt_section", "shared", "staff_section", "without_attachment_lines",
]
