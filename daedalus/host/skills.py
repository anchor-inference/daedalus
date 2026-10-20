"""Directory-backed skill store.

A skill is a directory under ``skills/`` holding ``SKILL.md`` (YAML-ish front matter
with ``name`` and ``description``, then the body) plus any supporting files. The
agent adds a skill by writing a directory; nothing else needs registering.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from protocore.contracts.skills import (
    ISkillStore,
    SkillBundle,
    SkillFileRef,
    SkillIndexEntry,
    SkillNotFoundError,
    SkillUpsertInput,
)
from protocore.contracts.types import SkillManifest

ENTRY = "SKILL.md"
DISABLED_MARKER = ".disabled"
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_skill_markdown(text: str) -> tuple[dict[str, str], str]:
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, text[match.end() :]


def _csv(value: object) -> list[str]:
    return [item.strip() for item in str(value).split(",") if item.strip()]


def render_skill_markdown(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body.lstrip()}"


class DirectorySkillStore(ISkillStore):
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _dir(self, skill_id: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", skill_id)
        return self._root / safe

    def _manifest(self, skill_dir: Path) -> tuple[SkillManifest, str] | None:
        entry = skill_dir / ENTRY
        if not entry.is_file():
            return None
        meta, body = parse_skill_markdown(entry.read_text(encoding="utf-8"))
        return (
            SkillManifest(
                id=skill_dir.name,
                name=meta.get("name") or skill_dir.name,
                description=meta.get("description", ""),
            ),
            body,
        )

    def _entries(self, *, include_disabled: bool) -> list[tuple[SkillIndexEntry, Path]]:
        out: list[tuple[SkillIndexEntry, Path]] = []
        for skill_dir in sorted(p for p in self._root.iterdir() if p.is_dir()):
            loaded = self._manifest(skill_dir)
            if loaded is None:
                continue
            manifest, _ = loaded
            enabled = not (skill_dir / DISABLED_MARKER).exists()
            if not enabled and not include_disabled:
                continue
            out.append(
                (
                    SkillIndexEntry(
                        id=manifest.id,
                        name=manifest.name,
                        description=manifest.description,
                        enabled=enabled,
                    ),
                    skill_dir,
                )
            )
        return out

    async def list(self, tenant_id: str) -> Sequence[SkillIndexEntry]:
        return [entry for entry, _ in self._entries(include_disabled=False)]

    async def load(self, tenant_id: str, skill_id: str) -> SkillBundle:
        skill_dir = self._dir(skill_id)
        # Hiding a disabled skill from the catalogue is insufficient when its exact id is known.
        if (skill_dir / DISABLED_MARKER).exists():
            raise SkillNotFoundError(skill_id)
        loaded = self._manifest(skill_dir)
        if loaded is None:
            loaded = self._by_name(skill_id)
        if loaded is None:
            raise SkillNotFoundError(skill_id)
        manifest, body = loaded
        return SkillBundle(manifest=manifest, body=body)

    def _by_name(self, name: str) -> tuple[SkillManifest, str] | None:
        for _, skill_dir in self._entries(include_disabled=False):
            loaded = self._manifest(skill_dir)
            if loaded and loaded[0].name == name:
                return loaded
        return None

    async def upsert(self, tenant_id: str, manifest: SkillManifest, body: str) -> None:
        skill_dir = self._dir(manifest.id)
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / ENTRY).write_text(
            render_skill_markdown(manifest.name, manifest.description, body), encoding="utf-8"
        )

    async def create(self, tenant_id: str, payload: SkillUpsertInput) -> SkillIndexEntry:
        skill_id = re.sub(r"[^a-z0-9-]+", "-", payload.name.lower()).strip("-") or "skill"
        await self.upsert(
            tenant_id,
            SkillManifest(id=skill_id, name=payload.name, description=payload.description),
            payload.body_md,
        )
        await self.set_enabled(tenant_id, skill_id, enabled=payload.enabled)
        return SkillIndexEntry(
            id=skill_id, name=payload.name, description=payload.description, enabled=payload.enabled
        )

    async def update(self, tenant_id: str, skill_id: str, payload: SkillUpsertInput) -> SkillIndexEntry:
        if self._manifest(self._dir(skill_id)) is None:
            raise SkillNotFoundError(skill_id)
        await self.upsert(
            tenant_id,
            SkillManifest(id=skill_id, name=payload.name, description=payload.description),
            payload.body_md,
        )
        await self.set_enabled(tenant_id, skill_id, enabled=payload.enabled)
        return SkillIndexEntry(
            id=skill_id, name=payload.name, description=payload.description, enabled=payload.enabled
        )

    async def delete(self, tenant_id: str, skill_id: str) -> None:
        skill_dir = self._dir(skill_id)
        if not skill_dir.exists():
            raise SkillNotFoundError(skill_id)
        shutil.rmtree(skill_dir)

    async def set_enabled(self, tenant_id: str, skill_id: str, *, enabled: bool) -> None:
        marker = self._dir(skill_id) / DISABLED_MARKER
        if enabled:
            marker.unlink(missing_ok=True)
        else:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()

    async def list_subset(self, tenant_id: str, names: Sequence[str]) -> Sequence[SkillIndexEntry]:
        wanted = set(names)
        return [
            e for e, _ in self._entries(include_disabled=True) if e.name in wanted or e.id in wanted
        ]

    async def list_enabled_subset(
        self, tenant_id: str, names: Sequence[str]
    ) -> Sequence[SkillIndexEntry]:
        wanted = set(names)
        return [
            e for e, _ in self._entries(include_disabled=False) if e.name in wanted or e.id in wanted
        ]

    async def list_files(self, tenant_id: str, skill_id: str) -> Sequence[SkillFileRef]:
        skill_dir = self._dir(skill_id)
        if not skill_dir.is_dir():
            raise SkillNotFoundError(skill_id)
        refs: list[SkillFileRef] = []
        for path in _regular_files(skill_dir):
            if path.name in (DISABLED_MARKER, ".published"):
                continue
            data = path.read_bytes()
            refs.append(
                SkillFileRef(
                    path=str(path.relative_to(skill_dir)),
                    size_bytes=len(data),
                    mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    content_hash=hashlib.sha256(data).hexdigest(),
                )
            )
        return refs

    def _meta_of(self, skill_id: str) -> dict[str, str]:
        """The front matter of a skill named by id or by name; empty when there is none to read."""
        entry = self._dir(skill_id) / ENTRY
        if not entry.is_file():
            loaded = self._by_name(skill_id)
            if loaded is None:
                return {}
            entry = self._dir(loaded[0].id) / ENTRY
        try:
            meta, _ = parse_skill_markdown(entry.read_text(encoding="utf-8"))
        except OSError:
            return {}
        return meta

    def params_of(self, skill_id: str) -> list[str]:
        """The parameters a skill declares in its front matter (``params: name, count``): the ``{{name}}`` placeholders
        of its body, which the Skill tool fills from ``args`` so a skill can be a recipe rather than only advice."""
        return _csv(self._meta_of(skill_id).get("params", ""))

    def requires_of(self, skill_id: str) -> list[str]:
        """What a skill declares it cannot work without (``requires: browser``). The Skill tool turns an
        unmet requirement into the first line of the skill, so the model reads "not installed" before the
        instructions that would have it call a tool this installation does not have."""
        return _csv(self._meta_of(skill_id).get("requires", ""))

    async def load_file(self, tenant_id: str, skill_id: str, path: str) -> bytes | None:
        skill_dir = self._dir(skill_id)
        target = (skill_dir / path).resolve()
        # Compared as paths, not as strings: as a string prefix, ``skills/ab/x`` passed for skill
        # ``a`` (``skills/a`` is a prefix of it), so ``../ab/x`` read another skill's files.
        if not target.is_relative_to(skill_dir.resolve()) or not target.is_file():
            return None
        return target.read_bytes()


def _regular_files(root: Path) -> list[Path]:
    """Every regular file under ``root``, sorted, reached without following a symbolic link.

    A skill directory is something the agent can write (a draft, a skill it is improving), and a
    link in it to ``~/.ssh/id_ed25519`` or to the state directory was listed, hashed and read back
    as one of the skill's own files."""
    found: list[Path] = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(directory, d)))
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink() and path.is_file():
                found.append(path)
    return sorted(found)


HANDOVER_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
HANDOVER_SKIP_FILES = frozenset({DISABLED_MARKER, ".published", ".requires-unavailable", ".DS_Store", "Thumbs.db"})
HANDOVER_FILE_BYTES = 512 << 10
"""The largest file a launch takes: the terminal daemon refuses a launch with a bigger one."""
HANDOVER_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
"""A path part the terminal daemon writes into a launch directory; a file with any other name cannot go."""
HANDOVER_DEPTH = 3
"""Directories below the skill's own a handed file may sit in: the launch puts the skill two or three
levels down already (``.claude/skills/<name>/``), and the daemon takes six in all."""
_REFERENCED = re.compile(r"(?<![\w./-])((?:scripts|references|templates)/[A-Za-z0-9_][A-Za-z0-9._/-]*\.[A-Za-z0-9]+)")


def referenced_files(markdown: str) -> list[str]:
    """The ``scripts/…``, ``references/…`` and ``templates/…`` files a skill's text names, in order.

    Only names with an extension count: "keep templates/components in the content paths" is prose."""
    return list(dict.fromkeys(m.group(1).rstrip(".") for m in _REFERENCED.finditer(markdown)))


def handed_markdown(markdown: str, slug: str) -> str:
    """``SKILL.md`` as a command-line agent gets it: the front matter's ``name`` is the directory's.

    OpenCode drops a skill whose ``name`` differs from its directory without a word, and a skill
    written for Daedalus may be named in words ("Daedalus pages") while its directory is a slug."""
    match = _FRONT_MATTER.match(markdown)
    if not match:
        return f"---\nname: {slug}\n---\n{markdown}"
    lines = [line for line in match.group(1).splitlines() if line.partition(":")[0].strip() != "name"]
    return "---\n" + "\n".join([f"name: {slug}", *lines]) + "\n---\n" + markdown[match.end():]


def handover_files(skill_dir: Path, slug: str, *, max_files: int, max_bytes: int) -> tuple[list[tuple[str, bytes]], list[str]]:
    """A skill as it goes to a command-line agent: ``([(path, bytes)], [paths left out])``.

    The whole directory, not ``SKILL.md`` alone: a skill that says "run scripts/check.py" or "read
    references/api.md" was handed over without either, and the agent followed instructions to files
    that were not there. No link is followed, the junk a working directory collects stays behind,
    and the launch's limits decide the rest: ``SKILL.md`` first, then the files its text names, then
    the others by name. What does not fit is listed at the end of the handed ``SKILL.md``, so the
    agent knows the file exists rather than searching for it."""
    entry = skill_dir / ENTRY
    if entry.is_symlink():
        raise OSError(f"{entry} is a link")
    markdown = entry.read_text(encoding="utf-8")
    named = referenced_files(markdown)
    candidates: list[tuple[str, Path]] = []
    for path in _regular_files(skill_dir):
        relative = path.relative_to(skill_dir)
        parts = relative.parts
        if path == entry or path.name in HANDOVER_SKIP_FILES or path.suffix in (".pyc", ".pyo") or any(part in HANDOVER_SKIP_DIRS for part in parts):
            continue
        candidates.append((relative.as_posix(), path))
    order = {name: index for index, name in enumerate(named)}
    candidates.sort(key=lambda item: (order.get(item[0], len(order)), item[0]))
    kept: list[tuple[str, bytes]] = []
    left: list[str] = []
    total = len(markdown.encode("utf-8"))
    for relative, path in candidates:
        parts = relative.split("/")
        size = path.stat().st_size
        if (len(parts) > HANDOVER_DEPTH + 1 or not all(HANDOVER_NAME.fullmatch(part) for part in parts)
                or size > HANDOVER_FILE_BYTES or len(kept) + 1 >= max_files or total + size > max_bytes):
            left.append(relative)
            continue
        kept.append((relative, path.read_bytes()))
        total += size
    handed = handed_markdown(markdown, slug)
    if left:
        handed = handed.rstrip("\n") + "\n\n(Not handed over with this launch, past what one launch carries: " + ", ".join(left) + ".)\n"
    return [(ENTRY, handed.encode("utf-8")), *kept], left


class ScopedSkillStore:
    """The skill store as one session sees it: only the skills its project's environment names.

    ``allowed`` is read on every call and answers ``None`` for "every skill", which is what a session
    outside a project with a skills list gets. The session runner sets the answer when a run's engine
    is assembled, so the catalogue the core builds once per run, and caches in the prompt prefix, is
    the same list the Skill tool loads from for the rest of that run. Writes and everything else go
    to the store underneath unchanged: the subset hides skills, it does not own them.
    """

    def __init__(self, store: DirectorySkillStore, allowed: Callable[[], frozenset[str] | None]) -> None:
        self._store = store
        self._allowed = allowed

    def _visible(self, entry: SkillIndexEntry) -> bool:
        allowed = self._allowed()
        return allowed is None or entry.name in allowed or entry.id in allowed

    async def list(self, tenant_id: str) -> Sequence[SkillIndexEntry]:
        return [e for e in await self._store.list(tenant_id) if self._visible(e)]

    async def load(self, tenant_id: str, skill_id: str) -> SkillBundle:
        bundle = await self._store.load(tenant_id, skill_id)
        allowed = self._allowed()
        if allowed is not None and bundle.manifest.id not in allowed and bundle.manifest.name not in allowed:
            raise SkillNotFoundError(skill_id)
        return bundle

    async def list_subset(self, tenant_id: str, names: Sequence[str]) -> Sequence[SkillIndexEntry]:
        return [e for e in await self._store.list_subset(tenant_id, names) if self._visible(e)]

    async def list_enabled_subset(self, tenant_id: str, names: Sequence[str]) -> Sequence[SkillIndexEntry]:
        return [e for e in await self._store.list_enabled_subset(tenant_id, names) if self._visible(e)]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


# The static contract a skill must meet before the operator may activate it; also what SkillDraft
# reports on a draft, so it lives beside the parser both use.
SKILL_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")


def skill_declarations(markdown: str) -> tuple[list[str], list[str]]:
    meta, _ = parse_skill_markdown(markdown)
    return (
        [item.strip() for item in meta.get("requires", "").split(",") if item.strip()],
        [item.strip() for item in meta.get("tools", "").split(",") if item.strip()],
    )


def assess(skill_id: str, markdown: str, dependencies: list[dict[str, str]], *, directory: Path | None = None) -> dict[str, Any]:
    """The static check a skill must pass before it is activated. With the skill's ``directory``,
    the files its text names under ``scripts/``, ``references/`` and ``templates/`` must be there too:
    a skill pointing at a script it does not carry fails at the one moment it is needed."""
    errors: list[str] = []
    if not SKILL_ID.fullmatch(skill_id):
        errors.append("invalid skill id")
    if not 80 <= len(markdown) <= 60_000:
        errors.append("skill Markdown length is outside the supported range")
    meta, body = parse_skill_markdown(markdown)
    if not meta.get("name") or not 12 <= len(meta.get("description", "")) <= 500:
        errors.append("name and descriptive purpose are required")
    if "## When to use" not in body or "## Procedure" not in body or "## Checks" not in body:
        errors.append("usage, procedure and checks sections are required")
    requirements, tools = skill_declarations(markdown)
    if len(requirements) > 8 or len(set(requirements)) != len(requirements) or any(item not in {"browser", "node"} for item in requirements):
        errors.append("requires must name unique supported host components")
    if len(tools) > 32 or len(set(tools)) != len(tools) or any(not TOOL_NAME.fullmatch(item) for item in tools):
        errors.append("tools must name unique registered tool identifiers")
    if not isinstance(dependencies, list) or len(dependencies) > 16:
        errors.append("too many dependencies")
    else:
        for dependency in dependencies:
            if not isinstance(dependency, dict) or set(dependency) != {"id", "version", "digest"} or not all(isinstance(value, str) and value for value in dependency.values()) or not re.fullmatch(r"[0-9a-f]{64}", dependency["digest"]):
                errors.append("dependencies need exact id, version and digest pins")
                break
    if directory is not None:
        root = directory.resolve()
        missing = []
        for name in referenced_files(markdown):
            target = directory / name
            if target.is_symlink() or not target.resolve().is_relative_to(root) or not target.is_file():
                missing.append(name)
        if missing:
            errors.append("files the skill names are missing: " + ", ".join(missing))
    return {"valid": not errors, "errors": errors, "digest": hashlib.sha256(markdown.encode()).hexdigest()}


DRAFTS_DIR = "skill-drafts"
"""Where drafts are written, under the state directory: a draft is not a skill of the repository until
someone copies it there, and a recorded one, full of the operator's own sites, usually never is."""
SLUG = re.compile(r"[^a-z0-9-]+")


def draft_slug(name: str) -> str:
    """A skill's directory name from what someone called it; "" when nothing usable is left."""
    slug = SLUG.sub("-", name.strip().lower()).strip("-")
    return slug if len(slug) <= 48 else ""


def save_skill_draft(
    state_dir: Path, redact: Any, name: str, description: str, body: str, *,
    params: str | None = None, front: dict[str, str] | None = None, files: dict[str, str] | None = None,
) -> tuple[Path, str, dict[str, Any]]:
    """Write a skill draft: ``SKILL.md`` and any supporting files, every text through the redactor,
    since a draft may become public through a pull request. ``(directory, slug, verdict)``, the verdict
    being the check a skill must pass to be activated; a draft is kept either way, since a draft is
    allowed to be unfinished. ``ValueError`` says what is wrong with the name, description or body.
    This is the one writer of drafts: the agent's ``SkillDraft`` and a recording the operator saves
    both come here, so the two cannot drift apart in where or how they write."""
    slug = draft_slug(name)
    if not slug:
        raise ValueError("name must be 1–48 characters of lowercase letters, digits and hyphens")
    description = " ".join(description.split())
    if not (20 <= len(description) <= 320):
        raise ValueError("description must be one line of 20–320 characters saying when to use the skill")
    if len(body.strip()) < 80:
        raise ValueError("the body is too short to be a skill; write the procedure, not a note")
    for path in files or {}:
        if path == ENTRY or "/" in path or path.startswith(".") or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", path):
            raise ValueError(f"{path!r} is not a supporting file's name")
    directory = state_dir / DRAFTS_DIR / slug
    directory.mkdir(parents=True, exist_ok=True)
    head = f"---\nname: {slug}\ndescription: {description}\n" + (f"params: {params.strip()}\n" if params and params.strip() else "")
    head += "".join(f"{key}: {value}\n" for key, value in (front or {}).items() if value)
    markdown = head + "---\n" + redact(body.strip()) + "\n"
    (directory / ENTRY).write_text(markdown, encoding="utf-8")
    for path, text in (files or {}).items():
        (directory / path).write_text(redact(text), encoding="utf-8")
    return directory, slug, assess(slug, markdown, [], directory=directory)


__all__ = [
    "DRAFTS_DIR", "DirectorySkillStore", "assess", "draft_slug", "handed_markdown", "handover_files", "parse_skill_markdown",
    "referenced_files", "render_skill_markdown", "save_skill_draft", "skill_declarations",
]
