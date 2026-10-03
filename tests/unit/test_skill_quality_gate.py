"""A skill becomes visible only after a reviewed, exact-digest activation."""

from __future__ import annotations

import pytest

from daedalus.extensions.skill_quality import SkillQuality, SkillRefused, assess
from daedalus.host.skills import DirectorySkillStore
from daedalus.stores.control import Principal
from daedalus.stores.database import Database

MARKDOWN = """---
name: Inspect Project
description: Inspect task status in one named project.
---
## When to use
Use for a project status question.
## Procedure
Read the board for that project.
## Checks
Check the project identifier first.
"""


def test_quality_gate_requires_contract_and_exact_pins() -> None:
    assert assess("inspect-project", MARKDOWN, [])["valid"]
    bad = assess("inspect-project", MARKDOWN.replace("## Checks", "## Notes"), [])
    assert not bad["valid"]
    assert not assess("inspect-project", MARKDOWN, [{"id": "dep", "version": "1", "digest": "unknown"}])["valid"]


async def test_submission_and_activation_are_receipted(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        directory = DirectorySkillStore(tmp_path / "skills")
        service = SkillQuality(db, directory)
        principal = Principal.operator({"via": "token", "user_id": 1})
        first = await service.submit_command(
            principal, "inspect-project", "1.0.0", MARKDOWN, [],
            expected_collection_revision=1, client_operation_id="skill-submit-one",
        )
        assert first == await service.submit_command(
            principal, "inspect-project", "1.0.0", MARKDOWN, [],
            expected_collection_revision=1, client_operation_id="skill-submit-one",
        )
        assert await directory.list("tenant") == []
        with pytest.raises(SkillRefused, match="digest changed"):
            await service.activate_command(
                principal, "inspect-project", "1.0.0", "0" * 64,
                expected_collection_revision=2, client_operation_id="bad-activation",
            )
        active = await service.activate_command(
            principal, "inspect-project", "1.0.0", first["digest"],
            expected_collection_revision=2, client_operation_id="skill-activate-one",
        )
        assert active["status"] == "staged"
        assert [item.id for item in await directory.list("tenant")] == ["inspect-project"]
        rows = await db.fetchall("SELECT state FROM effect_outbox WHERE kind = 'skill.publish'")
        assert [row["state"] for row in rows] == ["completed"]
    finally:
        await db.close()
