"""``Skill`` — load a skill body (and its file list) into the conversation."""

from __future__ import annotations

import re
from typing import Any

from protocore.contracts.skills import SkillNotFoundError
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.host.services import locator
from daedalus.host.skills import assess
from daedalus.host.toolchain import status as toolchain_status
from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import error, ok


@search_hint(
    "load skill playbook recipe procedure how-to instructions guide "
    "скилл навык плейбук рецепт методичка загрузить загрузи подгрузить подгрузи инструкция"
)
@tool(
    name="Skill",
    description=(
        "Load a skill by name. The skill's instructions are returned so you can follow "
        "them; supporting files are listed with their paths. A skill that declares parameters is a recipe: "
        "pass them in args ({name: value}) and its {{name}} placeholders are filled."
    ),
)
async def load_skill(context: ToolContext, skill: str, args: dict[str, Any] | None = None) -> ToolResult:
    store = locator.get(context.session_id).extra.get("skill_store")
    if store is None:
        return error(context, "skills are not available in this session")
    try:
        bundle = await store.load(context.tenant_id, skill)
    except (SkillNotFoundError, KeyError):
        return error(context, f"no skill named {skill!r}")
    params = store.params_of(bundle.manifest.id) if hasattr(store, "params_of") else []
    body = bundle.body
    if params:
        given = {k: str(v) for k, v in (args or {}).items()}
        missing = [p for p in params if p not in given]
        for name, value in given.items():
            body = body.replace("{{" + name + "}}", value)
        if missing:
            body = f"[This skill takes parameters: {', '.join(params)}. Not given: {', '.join(missing)} — their {{{{placeholders}}}} are left as they are.]\n\n" + body
    for note in _unmet(store, bundle.manifest.id):
        body = f"[{note}]\n\n" + body
    files = [f for f in await store.list_files(context.tenant_id, bundle.manifest.id) if f.path != "SKILL.md"]
    listing = _listing(files)
    text = f"# Skill: {bundle.manifest.name}\n{bundle.manifest.description}\n\n{body}"
    if listing:
        root = getattr(store, "root", None)
        where = f"{root}/{bundle.manifest.id}/" if root else f"skills/{bundle.manifest.id}/"
        text += f"\n\nFiles in this skill (under {where}; read or run them from there):\n{listing}"
    return ok(context, text, skill_id=bundle.manifest.id)


def _unmet(store: Any, skill_id: str) -> list[str]:
    """The requirements this installation does not meet, said before the skill body rather than
    discovered halfway through it by an import that fails."""
    requires = store.requires_of(skill_id) if hasattr(store, "requires_of") else []
    notes = []
    for requirement in requires:
        state = toolchain_status(requirement)
        if state != "ok":
            notes.append(f"This skill needs something this installation does not have: {state} Say so rather than following instructions that cannot work here.")
    return notes


LISTING_MAX_FILES = 40
"""Past this many files the listing folds directories: a gallery skill indexes its own files in SKILL.md."""


def _listing(files: list[Any]) -> str:
    if len(files) <= LISTING_MAX_FILES:
        return "\n".join(f"- {f.path} ({f.size_bytes} bytes)" for f in files)
    top: dict[str, list[Any]] = {}
    for f in files:
        top.setdefault(f.path.split("/", 1)[0] if "/" in f.path else "", []).append(f)
    lines = []
    for name, group in top.items():
        if name:
            lines.append(f"- {name}/ ({len(group)} files, {sum(g.size_bytes for g in group)} bytes; see SKILL.md for the index)")
        else:
            lines.extend(f"- {f.path} ({f.size_bytes} bytes)" for f in group)
    return "\n".join(lines)


@tool_group("learning")
@search_hint(
    "save new skill draft distill procedure write playbook "
    "сохранить сохрани навык скилл черновик оформить оформи рецепт запомнить как делать"
)
@tool(
    name="SkillDraft",
    description=(
        "Save a skill you distilled from this session as a draft: name (lowercase, hyphens), a one-line description "
        "that says when to use it, and the body (the procedure, the pitfalls, the commands that worked). The draft is "
        "written under the state directory; the result says how to make it a real skill on this installation. "
        "A skill earns its place with a case where it made the difference, so say which task produced it."
    ),
)
async def skill_draft(context: ToolContext, name: str, description: str, body: str, params: str | None = None) -> ToolResult:
    services = locator.get(context.session_id)
    manager = services.extra.get("manager")
    if manager is None:
        return error(context, "skill drafts are not available in this session")
    slug = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
    if not slug or len(slug) > 48:
        return error(context, "name must be 1–48 characters of lowercase letters, digits and hyphens")
    description = " ".join(description.split())
    if not (20 <= len(description) <= 320):
        return error(context, "description must be one line of 20–320 characters saying when to use the skill")
    if len(body.strip()) < 80:
        return error(context, "the body is too short to be a skill; write the procedure, not a note")
    directory = manager.settings.state_dir / "skill-drafts" / slug
    directory.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {slug}\ndescription: {description}\n" + (f"params: {params.strip()}\n" if params and params.strip() else "") + "---\n"
    body = manager.redactor.redact(body.strip())  # a draft may become public through a pull request: no secrets in it
    markdown = front + body + "\n"
    (directory / "SKILL.md").write_text(markdown, encoding="utf-8")
    # The same check a skill must pass to be activated, said now while the session still knows the
    # procedure; the draft is kept either way, since a draft is allowed to be unfinished.
    verdict = assess(slug, markdown, [])
    gaps = "" if verdict["valid"] else " Before it can become a skill: " + "; ".join(verdict["errors"]) + "."
    return ok(context, f"draft saved at {directory}/SKILL.md (session {context.session_id}).{gaps} {_how_to_ship(manager, slug)}",
              path=str(directory), valid=verdict["valid"])


def _how_to_ship(manager: Any, slug: str) -> str:
    """How a draft becomes a skill here — named in the tools this installation actually registered.

    The route differs by self-development mode, and a route through a tool that is not registered is
    an instruction the model cannot follow and does not know why.
    """
    mode = getattr(getattr(manager, "capabilities", None), "selfdev", None)
    checks = "Verify with `uv run pytest tests/unit/test_skill_catalogue.py -q` from that worktree (the catalogue test loads every skill)"
    if getattr(mode, "mode", "off") == "server":
        return f"To ship it: SelfWorkspace('bot', 'skill-{slug}'), copy the directory into skills/{slug}/ there, {checks}, commit, SelfPropose with execution_path='daedalus.host.skills'."
    if getattr(mode, "mode", "off") == "local":
        return f"To ship it: SelfWorkspace('bot', 'skill-{slug}'), copy the directory into skills/{slug}/ there, {checks}, commit, SelfApply — the change is live after the restart it asks for."
    return f"This installation does not change its own code, so the draft stays where it is: copy the directory into skills/{slug}/ of the host repository by hand to make it a skill."


TOOLS = [load_skill, skill_draft]

__all__ = ["TOOLS"]
