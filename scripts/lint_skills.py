"""Check that every skill can be followed with what this installation registers.

A skill is instructions, and an instruction to call a tool that does not exist, or one the run does not
offer until something loads it, cost a session its next steps: the model searched for the tool, or did
the job some other way in the shell. So every tool a ``SKILL.md`` names must be a registered tool, every
tool of a group must be covered by the skill's ``requires:`` (the Skill tool loads what is listed there),
every program its commands run must be listed there too (the Skill tool says at once when one is
missing), and no skill may tell the model to install what the image already carries.

    python scripts/lint_skills.py [skills directory]

Prints one line per problem and exits 1 when there is any.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daedalus.host.skills import ENTRY, classify_requirement, parse_skill_markdown  # noqa: E402
from daedalus.tools import TOOL_GROUPS, discover_tools  # noqa: E402

FENCE = re.compile(r"```.*?```", re.DOTALL)
CALL = re.compile(r"(?<![\w.$#])(?<!new )([A-Z][a-z][A-Za-z0-9]*)(?=\()")
"""A tool written as a call, ``Recall("...")``: the form a skill gives a tool in, and never a class a skill
quotes (``new GLTFLoader()``, ``Image.save(...)``), which comes after ``new`` or a dot."""
TICKED = re.compile(r"`([A-Z][a-z][A-Za-z0-9]*)(?:\([^`]*\))?`")
COMMANDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsync_playwright\b|\basync_playwright\b|\bfrom playwright\b"), "playwright"),
    (re.compile(r"\bnpx\s"), "npx"),
    (re.compile(r"\bnpm\s+(?:install|run|ci|i|init|create)\b"), "npm"),
    (re.compile(r"\bfrom PIL\b|\bimport PIL\b"), "python:PIL"),
)
"""What a skill's text runs, and the ``requires:`` item that says the shell has it."""
FORBIDDEN = re.compile(r"uv add playwright|pip install playwright|playwright install")
"""The image carries Playwright and its Chromium for the shell's ``python3``; a skill that installs them
sends the model through a minute of downloads into the workspace, past a read-only cache, to get a
second copy of each. A line that warns against it ("never `playwright install`") is the cure, not the
fault, so a line with a negation in it passes."""
NEGATION = re.compile(r"\b(?:never|not|don't|no)\b", re.IGNORECASE)
CORE_TOOLS = ("Forget", "Recall", "Remember", "ToolSearch")
"""Registered beside the tool modules rather than by them: the memory tools and the core's search."""


def registered_tools() -> dict[str, str]:
    """Every tool the host registers, with its group ("" for a tool in none)."""
    from daedalus.tools.dispatcher import build as dispatcher_tools

    tools = {tool.name: str(getattr(tool, "tool_group", "") or "") for tool in discover_tools()}
    tools.update({tool.name: "" for tool in dispatcher_tools()})
    tools.update({name: "" for name in CORE_TOOLS})
    return tools


def lint_skill(path: Path, tools: dict[str, str]) -> list[str]:
    """The problems of one ``SKILL.md``, each a line naming the skill."""
    skill = path.parent.name
    markdown = path.read_text(encoding="utf-8")
    meta, body = parse_skill_markdown(markdown)
    problems: list[str] = []
    declared: set[str] = set()
    items = [item.strip() for item in meta.get("requires", "").split(",") if item.strip()]
    if len(set(items)) != len(items):
        problems.append(f"{skill}: requires names an item twice")
    for item in items:
        requirement = classify_requirement(item, TOOL_GROUPS)
        if requirement is None:
            problems.append(f"{skill}: requires {item!r} is not a tool, a tool group, a program or a python: module")
            continue
        if requirement.kind == "tool" and requirement.name not in tools:
            problems.append(f"{skill}: requires the tool {item}, which does not exist")
            continue
        declared.add(requirement.name)
    prose = FENCE.sub("", body)
    for match in CALL.finditer(prose):
        if match.group(1) not in tools:
            problems.append(f"{skill}: names the tool {match.group(1)}, which does not exist")
    named = {m.group(1) for m in (*CALL.finditer(prose), *TICKED.finditer(prose))} & tools.keys()
    uncovered: dict[str, list[str]] = {}
    for name in sorted(named):
        group = tools[name]
        if group and group not in declared and name not in declared:
            uncovered.setdefault(group, []).append(name)
    for group, names in sorted(uncovered.items()):
        problems.append(f"{skill}: names {', '.join(names)} without the group {group!r} in requires, so the skill does not load them")
    for pattern, requirement in COMMANDS:
        if pattern.search(body) and requirement not in declared:
            problems.append(f"{skill}: runs {requirement} without {requirement!r} in requires")
    for line in body.splitlines():
        if (found := FORBIDDEN.search(line)) and not NEGATION.search(line):
            problems.append(f"{skill}: says {found.group(0)!r}; the image carries Playwright and Chromium for the shell's python3 already")
    return problems


def lint(skills_dir: Path) -> list[str]:
    tools = registered_tools()
    problems: list[str] = []
    for path in sorted(skills_dir.glob(f"*/{ENTRY}")):
        problems.extend(lint_skill(path, tools))
    return problems


def main(argv: list[str]) -> int:
    skills_dir = Path(argv[1]) if len(argv) > 1 else ROOT / "skills"
    problems = lint(skills_dir)
    for line in problems:
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
