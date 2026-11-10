"""The package caches every operator session shares: uv's, npm's and Playwright's browsers.

One folder outside every workspace (:func:`daedalus.config.package_cache_root`: ``/srv/cache`` in the
container, a volume that is not the state volume, whose backups would carry gigabytes of wheels), made by the host and handed to commands through the environment
(:func:`environment`) and, under the sandbox, as a writable bind (:func:`writable_dirs`). Without it a
command's caches were the home folder's, which the sandbox binds read-only: ``uv init`` failed with
"Failed to initialize cache at ~/.cache/uv: Read-only file system", and a model that had been told never
to move the cache to a private folder spent six steps doing exactly that.

Shared between the operator's own chats on purpose: they are one person's work, and a download made once
is a download every later chat skips. A guest's turn gets none of it. Its environment is
:data:`daedalus.tools.shell.GUEST_ENVIRONMENT` and nothing else, so its uv and npm write under its own
``/tmp``, inside the space its box caps and throws away; a writable shared cache there would let a stranger
leave a package for the operator's next ``uv add`` to install.

Playwright looks for a browser under one path. The browsers an image or the launcher already installed are
read-only, so the shared folder links to each of them instead of holding a copy, and ``playwright install``
of another revision writes beside the links.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from daedalus.config import installed_browsers, package_cache_root

logger = logging.getLogger(__name__)

LINKS = ".links"
"""Playwright's record of which installation uses which browser, written on every install."""

_prepared: Path | None = None


def folders(base: Path) -> dict[str, Path]:
    return {"UV_CACHE_DIR": base / "uv", "npm_config_cache": base / "npm", "PLAYWRIGHT_BROWSERS_PATH": base / "ms-playwright"}


def prepare(base: Path | None = None, *, installed: str | None = None) -> Path | None:
    """Make the folders and link the browsers already installed; returns the root, or None when it cannot be made.

    Idempotent and cheap after the first call. ``installed`` is the read-only browser folder to link from,
    by default the ``PLAYWRIGHT_BROWSERS_PATH`` this process was started with.
    """
    global _prepared
    base = base or package_cache_root()
    if _prepared == base:
        return base
    try:
        for folder in folders(base).values():
            folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("the shared package cache %s cannot be made (%s); commands keep their own caches", base, exc.strerror)
        return None
    source = installed if installed is not None else installed_browsers()
    if source and Path(source).is_dir() and Path(source).resolve() != folders(base)["PLAYWRIGHT_BROWSERS_PATH"].resolve():
        _link_browsers(Path(source), folders(base)["PLAYWRIGHT_BROWSERS_PATH"])
    _prepared = base
    return base


def _link_browsers(source: Path, target: Path) -> None:
    """Link every browser of ``source`` into ``target`` and copy its install records, which Playwright must write to."""
    for entry in sorted(source.iterdir()):
        here = target / entry.name
        try:
            if entry.name == LINKS:
                here.mkdir(exist_ok=True)
                for record in entry.iterdir():
                    if record.is_file() and not (here / record.name).exists():
                        shutil.copyfile(record, here / record.name)
            elif not here.exists() and not here.is_symlink():
                here.symlink_to(entry, target_is_directory=entry.is_dir())
        except OSError as exc:
            logger.warning("could not link the installed browser %s into the shared cache: %s", entry, exc.strerror)


def environment() -> dict[str, str]:
    """The variables that point an operator's command at the shared caches; empty when there are none."""
    base = prepare()
    return {} if base is None else {name: str(path) for name, path in folders(base).items()}


def writable_dirs() -> list[Path]:
    """What the sandbox binds writable for the caches."""
    base = prepare()
    return [] if base is None else list(folders(base).values())


__all__ = ["environment", "folders", "prepare", "writable_dirs"]
