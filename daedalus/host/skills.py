"""Directory-backed skill store.

A skill is a directory under ``skills/`` holding ``SKILL.md`` (YAML-ish front matter
with ``name`` and ``description``, then the body) plus any supporting files. The
agent adds a skill by writing a directory; nothing else needs registering.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
import shutil
from collections.abc import Sequence
from pathlib import Path

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
        for path in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
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
        if not str(target).startswith(str(skill_dir.resolve())) or not target.is_file():
            return None
        return target.read_bytes()


__all__ = ["DirectorySkillStore", "parse_skill_markdown", "render_skill_markdown"]
