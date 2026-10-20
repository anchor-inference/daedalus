"""A skill's files are its own directory's: no neighbour's, nothing a link points at, nothing it only names."""

from __future__ import annotations

from pathlib import Path

from daedalus.host.skills import DirectorySkillStore, assess, handed_markdown, referenced_files

MARKDOWN = """---
name: Inspect Project
description: Inspect task status in one named project.
---
## When to use
Use for a project status question.
## Procedure
Run scripts/status.py, then compare with references/board.md and fill templates/report.md.
Keep templates/components in mind (prose, not a file).
## Checks
Check the project identifier first.
"""


async def test_a_skill_cannot_read_a_neighbour_whose_name_starts_with_its_own(tmp_path: Path) -> None:
    store = DirectorySkillStore(tmp_path / "skills")
    (store.root / "a").mkdir()
    (store.root / "a" / "SKILL.md").write_text("---\nname: a\ndescription: x\n---\n")
    (store.root / "ab").mkdir()
    (store.root / "ab" / "secret.txt").write_text("not a's")
    assert await store.load_file("t", "a", "../ab/secret.txt") is None
    assert await store.load_file("t", "a", "SKILL.md") is not None


async def test_listing_a_skill_follows_no_link(tmp_path: Path) -> None:
    store = DirectorySkillStore(tmp_path / "skills")
    skill = store.root / "a"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: a\ndescription: x\n---\n")
    (skill / "references" / "real.md").write_text("mine")
    outside = tmp_path / "home"
    (outside / ".ssh").mkdir(parents=True)
    (outside / ".ssh" / "id_ed25519").write_text("PRIVATE KEY")
    (skill / "references" / "key.md").symlink_to(outside / ".ssh" / "id_ed25519")
    (skill / "home").symlink_to(outside)
    listed = [ref.path for ref in await store.list_files("t", "a")]
    assert listed == ["SKILL.md", "references/real.md"]
    assert await store.load_file("t", "a", "references/key.md") is None


def test_assess_checks_the_files_a_skill_names(tmp_path: Path) -> None:
    assert referenced_files(MARKDOWN) == ["scripts/status.py", "references/board.md", "templates/report.md"]
    assert assess("inspect-project", MARKDOWN, [])["valid"], "without a directory there is nothing to look in"
    skill = tmp_path / "inspect-project"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "status.py").write_text("print(1)\n")
    (skill / "references").mkdir()
    (tmp_path / "board.md").write_text("outside")
    (skill / "references" / "board.md").symlink_to(tmp_path / "board.md")
    missing = assess("inspect-project", MARKDOWN, [], directory=skill)
    assert not missing["valid"]
    assert missing["errors"] == ["files the skill names are missing: references/board.md, templates/report.md"]
    (skill / "references" / "board.md").unlink()
    (skill / "references" / "board.md").write_text("board")
    (skill / "templates").mkdir()
    (skill / "templates" / "report.md").write_text("report")
    assert assess("inspect-project", MARKDOWN, [], directory=skill)["valid"]


def test_the_handed_name_is_the_directory_s() -> None:
    handed = handed_markdown(MARKDOWN, "inspect-project")
    assert handed.startswith("---\nname: inspect-project\ndescription: Inspect task status")
    assert handed.endswith(MARKDOWN.split("---\n", 2)[2])
    assert handed_markdown("Just a body.\n", "plain") == "---\nname: plain\n---\nJust a body.\n"
