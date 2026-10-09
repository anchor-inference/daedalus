"""The folder browser of the new-project dialog: one directory level at a time, on either side.

A folder for a project lives in the container or on the operator's machine. The container's are read
here with ``os``; the machine's, from a Docker installation, through the host terminal daemon's
``fs.browse``, which lists directories and nothing else. Natively the machine is this process's own
filesystem and is read here as well. Both sides answer in one shape, so the app draws one browser.

What the two local cases may reach differs on purpose. In Docker the container's filesystem is the
image plus what is mounted into it, and only the places a project can sensibly live are offered as
roots: the home folder, the workspaces, the data volume and the folders projects already have. A
native installation is the operator's own user on their own machine, so it may browse anywhere the
host daemon would let it: everything but the installation's own sealed paths.

The listing says, for each folder, what the dialog needs to judge it without another request:
whether it is a git work tree, whether it can be written, when it changed, and whether a project
already owns it. A chat's scratch folder is named by its chat, and the installation's own projects
are marked as such, because a folder named by a hash is not something the operator can recognise.
Reads are not audited, as the file pane's are not; the one write, making a folder, is logged here
and, on the host, audited by the terminals service.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.api_host_pane import _mtime
from daedalus.host.policy import sealed_root
from daedalus.stores.projects import Project, ProjectError, folder_name
from daedalus.terminals.bridge import HostDaemonOutdated

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

Env = Literal["container", "host"]

LIST_MAX_ENTRIES = 250
"""The most folders one listing returns; a directory with more is cut and says so."""
LIST_BUDGET_SECONDS = 0.25
"""How long one local listing may spend reading entries before it stops and says it was cut."""
HOST_LIST_LIMIT = 500
RECENT_MAX = 5
FAVOURITES_MAX = 24
FAVOURITES_KEY = "folder_browser.favourites"
HOST_START = "systemctl --user start daedalus-ptyd"
HOST_INSTALL = "bash deploy/host-terminal.sh install"


class FavouriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: Env
    path: str = Field(min_length=1, max_length=4000)
    on: bool


class MkdirBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: Env
    path: str = Field(min_length=1, max_length=4000)
    """The folder the new one is made in."""
    name: str = Field(min_length=1, max_length=80)


def _is_windows_path(text: str) -> bool:
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


def crumbs(path: str, home: str, anchor: str = "") -> list[dict[str, str]]:
    """The breadcrumb of a path, as the browser draws it: from the anchor when there is one (a root a
    Docker container's listing may not climb above), else from the home folder shown as ``~`` when
    the path is inside it, else from the filesystem's root. Windows paths of a native machine or of
    the host daemon on Windows are split by their own rules."""
    flavour = PureWindowsPath if _is_windows_path(path) else PurePosixPath
    target = flavour(path)
    start = flavour(anchor) if anchor else None
    home_path = flavour(home) if home else None
    if start is None and home_path is not None and (target == home_path or home_path in target.parents):
        start = home_path
    out: list[dict[str, str]] = []
    current = target
    while True:
        if start is not None and current == start:
            label = "~" if not anchor and home_path is not None and current == home_path else (current.name or str(current))
            out.append({"name": label, "path": str(current)})
            break
        out.append({"name": current.name or str(current), "path": str(current)})
        if current.parent == current:
            break
        current = current.parent
    return list(reversed(out))


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    manager = app.manager
    assert manager is not None
    settings = app.settings

    def local_env() -> str:
        return manager.projects.local_env

    def protected() -> list[str]:
        return [str(p) for p in manager.protected_paths()]

    def is_protected(path: Path) -> bool:
        return sealed_root(str(path), protected()) is not None

    def terminals_configured() -> bool:
        terminals = app.extensions.get("terminals")
        return bool(terminals is not None and terminals.configured("host"))

    def env_of(raw: str) -> str:
        env = raw or local_env()
        if env == local_env():
            return env
        if env == "host" and local_env() == "container":
            return env
        raise HTTPException(400, "this installation runs on the host without a container; there is only the host to browse")

    # -- what is known about folders ------------------------------------------------------------

    async def owners(env: str) -> tuple[dict[str, Project], list[Project]]:
        """Every folder of ``env`` that a project has, by its path, and the projects in order of
        creation, newest first. A local folder is keyed by its resolved path as well, since the
        listing compares resolved paths."""
        projects = await manager.projects.list()
        by_path: dict[str, Project] = {}
        for project in projects:
            for folder in project.folders:
                if folder.env != env:
                    continue
                by_path[str(folder.path)] = project
                if env == local_env():
                    try:
                        by_path.setdefault(str(folder.path.resolve()), project)
                    except OSError:
                        pass
        return by_path, sorted(projects, key=lambda p: p.created_at, reverse=True)

    async def chat_titles(projects: Iterable[Project]) -> dict[str, str]:
        """The title of the chat each scratch project belongs to; a chat renamed after it started is
        known by its new name, which is the one the operator sees in the list."""
        titles: dict[str, str] = {}
        for project in projects:
            if not project.settings.ephemeral:
                continue
            sessions = await manager.projects.sessions_of(project.id)
            titles[project.id] = str(sessions[0].get("title") or project.name) if sessions else project.name
        return titles

    def describe(entry: dict[str, Any], project: Project | None, titles: dict[str, str]) -> dict[str, Any]:
        """One folder of a listing with what owns it: ``kind`` is ``folder``, ``chat`` (a chat's
        scratch folder, offered only through the chat's own "keep as a project") or ``service``
        (one of the installation's own projects); neither of the last two can be chosen."""
        kind = "folder"
        if project is not None and (project.settings.system or project.setup_by == "dispatcher"):
            kind = "service"
        elif project is not None and project.settings.ephemeral:
            kind = "chat"
        view = {**entry, "kind": kind, "project": None}
        if project is not None:
            view["project"] = {"id": project.id, "name": project.name}
            if kind == "chat":
                view["chat_title"] = titles.get(project.id, project.name)
        return view

    async def favourites(env: str | None = None) -> list[dict[str, str]]:
        stored = await app.db.kv_get(FAVOURITES_KEY)
        rows = [row for row in stored if isinstance(row, dict)] if isinstance(stored, list) else []
        return [{"env": str(row.get("env")), "path": str(row.get("path")), "name": _basename(str(row.get("path")))}
                for row in rows if env is None or row.get("env") == env]

    def recent(env: str, ordered: list[Project]) -> list[dict[str, str]]:
        """The folders of the newest projects: where the operator last pointed a project."""
        out: list[dict[str, str]] = []
        for project in ordered:
            if project.settings.ephemeral or project.settings.system or project.settings.archived:
                continue
            for folder in project.folders:
                if folder.env == env and len(out) < RECENT_MAX and not any(row["path"] == str(folder.path) for row in out):
                    out.append({"name": folder.label.strip() or folder.path.name or str(folder.path), "path": str(folder.path)})
        return out

    # -- this process's own filesystem ----------------------------------------------------------

    def local_roots() -> list[dict[str, str]]:
        """Where a local listing starts. In Docker these are also the only trees it may enter."""
        home = Path.home()
        candidates: list[tuple[Path, str]] = [(home, "home"), (settings.workspaces_dir, "workspaces")]
        if not settings.native:
            candidates.append((settings.state_dir.parent, "data"))
        else:
            candidates.extend((Path(drive), "volume") for drive in _volumes())
        roots: list[dict[str, str]] = []
        seen: set[Path] = set()
        for candidate, kind in candidates:
            try:
                real = candidate.resolve()
            except OSError:
                continue
            if real in seen or is_protected(real) or not real.is_dir() or not os.access(real, os.R_OK):
                continue
            seen.add(real)
            roots.append({"name": real.name or str(real), "path": str(real), "kind": kind})
        return roots

    async def offered(by_path: dict[str, Project]) -> list[Path]:
        """The trees a Docker container's listing may enter: the roots, and every project folder here."""
        trees = [Path(root["path"]) for root in local_roots()]
        projects = await manager.projects.list()
        for project in projects:
            for folder in project.local_folders(local_env()):
                if not folder.reachable:
                    continue
                try:
                    real = folder.path.resolve()
                except OSError:
                    continue
                if real not in trees and not is_protected(real):
                    trees.append(real)
        return trees

    def anchor_of(target: Path, trees: list[Path]) -> Path | None:
        """The widest offered tree that holds ``target``, so the breadcrumb climbs as far as allowed."""
        holding = [tree for tree in trees if tree == target or tree in target.parents]
        return min(holding, key=lambda tree: len(tree.parts)) if holding else None

    def local_entry(path: Path, name: str) -> dict[str, Any] | None:
        try:
            info = path.stat()
        except OSError:
            return None
        return {
            "name": name, "path": str(path), "mtime": info.st_mtime,
            "writable": os.access(path, os.W_OK), "readable": os.access(path, os.R_OK | os.X_OK),
            "is_git": os.path.lexists(path / ".git"), "link": False,
        }

    def scan(target: Path, anchor: Path | None) -> tuple[list[dict[str, Any]], bool]:
        """The folders directly in ``target``: no files, no symbolic links (one could lead out of the
        tree it was offered from), nothing hidden and nothing of the installation's own; at most
        ``LIST_MAX_ENTRIES`` of them, read for at most ``LIST_BUDGET_SECONDS``."""
        entries: list[dict[str, Any]] = []
        truncated = False
        deadline = time.monotonic() + LIST_BUDGET_SECONDS
        try:
            scanner = os.scandir(target)
        except OSError as exc:
            raise HTTPException(403, {"code": "refused", "message": f"{target} cannot be read: {exc.strerror or 'permission denied'}"}) from exc
        with scanner:
            for child in scanner:
                if len(entries) >= LIST_MAX_ENTRIES or time.monotonic() >= deadline:
                    truncated = True
                    break
                if child.name.startswith("."):
                    continue
                try:
                    if child.is_symlink() or not child.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                real = Path(child.path)
                if is_protected(real) or (anchor is not None and anchor not in real.parents):
                    continue
                entry = local_entry(real, child.name)
                if entry is not None:
                    entries.append(entry)
        entries.sort(key=lambda entry: entry["name"].lower())
        return entries, truncated

    async def local_target(raw: str, by_path: dict[str, Project]) -> tuple[Path, Path | None]:
        """The directory a local listing may show, and the tree it was offered from (Docker only)."""
        text = (raw or "").strip()
        if not text:
            text = str(Path.home())
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            raise HTTPException(400, {"code": "invalid", "message": "a folder to browse is a full path"})
        if ".." in candidate.parts:
            raise HTTPException(400, {"code": "invalid", "message": "a folder to browse names no parent with .."})
        try:
            target = candidate.resolve()
        except OSError as exc:
            raise HTTPException(404, {"code": "missing", "message": str(exc)}) from exc
        if is_protected(target) or is_protected(Path(os.path.normpath(candidate))):
            raise HTTPException(403, {"code": "refused", "message": f"{candidate} belongs to the installation itself"})
        anchor = None
        if not settings.native:
            # The listing never offers a link, so a path that goes through one was typed or crafted;
            # in the container it is refused rather than followed into whatever tree it names.
            if target.exists() and Path(os.path.normpath(candidate)) != target:
                raise HTTPException(403, {"code": "refused", "message": f"{candidate} goes through a symbolic link"})
            anchor = anchor_of(target, await offered(by_path))
            if anchor is None:
                if not target.exists():
                    raise HTTPException(404, await missing_here(str(candidate)))
                raise HTTPException(403, {"code": "refused", "message": f"{candidate} is outside the folders this browser offers in the container"})
        if not target.exists():
            raise HTTPException(404, await missing_here(str(candidate)))
        if not target.is_dir():
            raise HTTPException(400, {"code": "invalid", "message": f"{candidate} is not a folder"})
        return target, anchor

    async def missing_here(path: str) -> dict[str, Any]:
        """A local folder that is not there. In Docker that usually means it is on the machine but not
        mounted, which the host daemon can confirm when it answers; ``on_host`` is None when nobody
        could ask."""
        detail: dict[str, Any] = {"code": "missing", "message": f"{path} does not exist here", "mount": not settings.native, "on_host": None}
        bridge = manager.host_bridge
        if not settings.native and bridge is not None and bridge.available():
            try:
                stat = await bridge.stat_folder(path)
                detail["on_host"] = bool(stat.get("exists")) and stat.get("type") == "dir"
            except (OSError, ConnectionError):
                pass
        return detail

    async def local_listing(raw: str) -> dict[str, Any]:
        by_path, ordered = await owners(local_env())
        target, anchor = await local_target(raw, by_path)
        entries, truncated = await asyncio.to_thread(scan, target, anchor)
        here = local_entry(target, target.name or str(target)) or {}
        parent = target.parent if target.parent != target else None
        if parent is not None and anchor is not None and target == anchor:
            parent = None
        return await finish(local_env(), {
            "path": str(target), "parent": str(parent) if parent else None, "home": str(Path.home()),
            "writable": here.get("writable"), "is_git": here.get("is_git", False),
            "crumbs": crumbs(str(target), str(Path.home()), str(anchor) if anchor else ""),
            "entries": entries, "truncated": truncated, "roots": local_roots(),
        }, by_path, ordered, resolve=True)

    # -- the machine, through its terminal daemon -------------------------------------------------

    def host_failure(exc: BaseException | None) -> HTTPException:
        """The host side's errors in the shapes the browser tells apart: the daemon down or never
        installed (with the command that starts or installs it), the daemon too old for browsing,
        a folder that is not there, and a refusal in the daemon's own words."""
        if exc is None or isinstance(exc, ConnectionError):
            configured = terminals_configured()
            message = str(exc) if exc else "this installation has no host terminal bridge"
            return HTTPException(503, {"code": "host_down", "configured": configured, "message": message,
                                       "start": HOST_START, "install": HOST_INSTALL})
        if isinstance(exc, HostDaemonOutdated):
            return HTTPException(501, {"code": "host_outdated", "message": str(exc), "install": HOST_INSTALL})
        if isinstance(exc, FileNotFoundError):
            return HTTPException(404, {"code": "missing", "message": str(exc), "mount": False, "on_host": None})
        return HTTPException(403, {"code": "refused", "message": str(exc)})

    def bridge_or_fail() -> Any:
        bridge = manager.host_bridge
        if bridge is None:
            raise host_failure(None)
        return bridge

    def host_entry(raw: dict[str, Any]) -> dict[str, Any]:
        return {"name": str(raw.get("name") or ""), "path": str(raw.get("path") or ""), "mtime": _mtime(raw.get("mtime")),
                "writable": raw.get("writable"), "readable": bool(raw.get("readable", True)),
                "is_git": bool(raw.get("is_git")), "link": bool(raw.get("link"))}

    async def host_listing(raw: str) -> dict[str, Any]:
        bridge = bridge_or_fail()
        try:
            listed = await bridge.browse(raw.strip(), limit=HOST_LIST_LIMIT)
        except (OSError, ConnectionError) as exc:
            raise host_failure(exc) from None
        by_path, ordered = await owners("host")
        path, home = str(listed.get("path") or raw), str(listed.get("home") or "")
        places = [{"name": str(p.get("name") or p.get("path")), "path": str(p.get("path")), "kind": str(p.get("kind") or "volume")}
                  for p in listed.get("places") or [] if isinstance(p, dict)]
        return await finish("host", {
            "path": path, "parent": listed.get("parent") or None, "home": home,
            "writable": listed.get("writable"), "is_git": bool(listed.get("is_git")),
            "crumbs": crumbs(path, home), "entries": [host_entry(e) for e in listed.get("entries") or [] if isinstance(e, dict)],
            "truncated": bool(listed.get("truncated")), "roots": places,
        }, by_path, ordered, resolve=False)

    async def finish(env: str, listing: dict[str, Any], by_path: dict[str, Project], ordered: list[Project], *, resolve: bool) -> dict[str, Any]:
        """Name each folder's owner and add the places: the newest projects' folders and the favourites."""
        def owner(path: str) -> Project | None:
            return by_path.get(path)
        involved = [p for p in (owner(e["path"]) for e in listing["entries"]) if p is not None]
        here = owner(listing["path"])
        titles = await chat_titles([*involved, *([here] if here else [])])
        listing["entries"] = [describe(entry, owner(entry["path"]), titles) for entry in listing["entries"]]
        listing["here"] = describe({"path": listing["path"]}, here, titles)
        listing["env"] = env
        listing["recent"] = recent(env, ordered)
        listing["favourites"] = await favourites(env)
        return listing

    # -- routes ------------------------------------------------------------------------------------

    @api.get("/api/folders")
    async def browse(env: str = "", path: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """One directory level for the folder browser, with its breadcrumb and the places beside it."""
        side = env_of(env)
        if side == local_env():
            return await local_listing(path)
        return await host_listing(path)

    @api.get("/api/folders/check")
    async def check(env: str = "", path: str = "", _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """What the dialog needs to say about a typed folder before it is chosen: whether it is there,
        whether it can be written, whether it is a repository and whether a project owns it.

        It answers for any path, not only the browsable ones: a Docker folder outside the offered
        trees may still be the operator's to name, and the store holds a new project's folder to its
        own rules when the project is made.
        """
        side = env_of(env)
        text = path.strip()
        if not text:
            raise HTTPException(400, {"code": "invalid", "message": "name a folder to check"})
        by_path, _ = await owners(side)
        result: dict[str, Any] = {"env": side, "path": text, "exists": False, "is_dir": False, "writable": None,
                                  "is_git": False, "mount": False, "on_host": None}
        if side == local_env():
            candidate = Path(text).expanduser()
            if not candidate.is_absolute():
                raise HTTPException(400, {"code": "invalid", "message": "a folder is a full path"})
            normal = Path(os.path.normpath(candidate))
            if is_protected(normal):
                raise HTTPException(403, {"code": "refused", "message": f"{normal} belongs to the installation itself"})
            result["path"] = str(normal)
            if normal.is_dir():
                entry = local_entry(normal, normal.name) or {}
                result.update(exists=True, is_dir=True, writable=entry.get("writable"), is_git=entry.get("is_git", False))
            elif normal.exists():
                result.update(exists=True)
            else:
                missing = await missing_here(str(normal))
                result.update(mount=missing["mount"], on_host=missing["on_host"])
            owner = by_path.get(str(normal))
            if owner is None and normal.exists():
                owner = by_path.get(str(normal.resolve()))
        else:
            bridge = bridge_or_fail()
            try:
                listed = await bridge.browse(text, limit=1)
                result.update(path=str(listed.get("path") or text), exists=True, is_dir=True,
                              writable=listed.get("writable"), is_git=bool(listed.get("is_git")))
            except FileNotFoundError:
                pass
            except (OSError, ConnectionError) as exc:
                raise host_failure(exc) from None
            owner = by_path.get(result["path"])
        titles = await chat_titles([owner] if owner else [])
        return {**result, **{key: value for key, value in describe({}, owner, titles).items() if key != "path"}}

    @api.post("/api/folders/mkdir")
    async def mkdir(body: MkdirBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Make an empty folder inside the one being browsed: the browser's "New folder"."""
        side = env_of(body.env)
        try:
            name = folder_name(body.name)
        except ProjectError as exc:
            raise HTTPException(400, {"code": "invalid", "message": str(exc)}) from exc
        if side == local_env():
            by_path, _ = await owners(side)
            parent, _anchor = await local_target(body.path, by_path)
            child = parent / name
            if is_protected(child):
                raise HTTPException(403, {"code": "refused", "message": f"{child} belongs to the installation itself"})
            try:
                await asyncio.to_thread(child.mkdir)
            except FileExistsError as exc:
                raise HTTPException(409, {"code": "exists", "message": f"{child} already exists"}) from exc
            except OSError as exc:
                raise HTTPException(403, {"code": "refused", "message": f"{child} could not be made: {exc.strerror or exc}"}) from exc
            logger.info("folder browser made %s", child)
            return {"env": side, "path": str(child), "name": name}
        bridge = bridge_or_fail()
        flavour = PureWindowsPath if _is_windows_path(body.path) else PurePosixPath
        child_text = str(flavour(body.path) / name)
        try:
            made = await bridge.mkdir(child_text, actor="operator")
        except (OSError, ConnectionError) as exc:
            raise host_failure(exc) from None
        if not made.get("created"):
            raise HTTPException(409, {"code": "exists", "message": f"{child_text} already exists"})
        return {"env": side, "path": child_text, "name": name}

    @api.put("/api/folders/favourites")
    async def favourite(body: FavouriteBody, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        """Pin a folder to the browser's places, or unpin it. Kept with the installation rather than in
        the browser, so the desktop window, a phone and Telegram share one list."""
        env_of(body.env)
        rows = [row for row in await favourites() if not (row["env"] == body.env and row["path"] == body.path)]
        if body.on:
            rows.insert(0, {"env": body.env, "path": body.path, "name": _basename(body.path)})
        rows = rows[:FAVOURITES_MAX]
        await app.db.kv_set(FAVOURITES_KEY, [{"env": row["env"], "path": row["path"]} for row in rows])
        return {"favourites": [row for row in rows if row["env"] == body.env]}


def _basename(path: str) -> str:
    flavour = PureWindowsPath if _is_windows_path(path) else PurePosixPath
    pure = flavour(path)
    return pure.name or str(pure)


def _volumes() -> list[str]:
    """The filesystem roots of a native machine: ``/``, or each drive letter that is there."""
    if os.name != "nt":
        return ["/"]
    return [f"{letter}:\\" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{letter}:\\")]


__all__ = ["crumbs", "register"]
