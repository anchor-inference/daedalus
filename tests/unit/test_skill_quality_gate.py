"""A skill becomes visible only after a reviewed, exact-digest activation."""

from __future__ import annotations

import pytest
from protocore.contracts.skills import SkillNotFoundError

from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.skill_quality import SkillQuality, SkillRefused, assess
from daedalus.host.skills import DirectorySkillStore
from daedalus.stores.control import Principal
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore

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
    assert not assess("inspect-project", MARKDOWN.replace("description:", "requires: browser, unknown\ndescription:"), [])["valid"]
    assert not assess("inspect-project", MARKDOWN.replace("description:", "tools: Board, Board\ndescription:"), [])["valid"]


async def test_required_tools_and_components_are_checked_again_before_publication(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        directory = DirectorySkillStore(tmp_path / "skills")
        dispatcher = EffectDispatcher(OutboxStore(db))
        available = {"Board"}
        component = {"browser": "ok"}
        service = SkillQuality(
            db, directory, dispatcher, available_tools=lambda: available,
            requirement_status=lambda name: component[name],
        )
        dispatcher.register("skill.publish", service)
        principal = Principal.operator({"via": "token", "user_id": 1})
        markdown = MARKDOWN.replace("description:", "requires: browser\ntools: Board\ndescription:")
        candidate = await service.submit_command(
            principal, "inspect-project", "1.0.0", markdown, [],
            expected_collection_revision=1, client_operation_id="candidate-with-tools",
        )
        available.clear()
        with pytest.raises(SkillRefused, match="required tools are unavailable"):
            await service.activate_command(
                principal, "inspect-project", "1.0.0", candidate["digest"],
                expected_collection_revision=2, client_operation_id="activation-missing-tool",
            )
        assert (await db.fetchone("SELECT status FROM skill_manifests"))["status"] == "candidate"
        assert not await db.fetchall("SELECT id FROM effect_outbox WHERE kind = 'skill.publish'")
        available.add("Board")
        await service.activate_command(
            principal, "inspect-project", "1.0.0", candidate["digest"],
            expected_collection_revision=2, client_operation_id="activation-with-tool",
        )
        component["browser"] = "missing"
        assert await dispatcher.step()
        assert not await directory.list("tenant")
        assert not (directory.root / "inspect-project").exists()
        assert (await db.fetchone("SELECT state FROM effect_outbox WHERE kind = 'skill.publish'"))["state"] == "failed"
    finally:
        await db.close()


async def test_required_tool_change_after_claim_does_not_publish(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        available = {"Board"}
        service = SkillQuality(
            db, DirectorySkillStore(tmp_path / "skills"),
            available_tools=lambda: available,
        )
        principal = Principal.operator({"via": "token", "user_id": 1})
        markdown = MARKDOWN.replace("description:", "tools: Board\ndescription:")
        candidate = await service.submit_command(
            principal, "inspect-project", "1.0.0", markdown, [],
            expected_collection_revision=1, client_operation_id="candidate-changing-tool",
        )
        await service.activate_command(
            principal, "inspect-project", "1.0.0", candidate["digest"],
            expected_collection_revision=2, client_operation_id="activation-changing-tool",
        )
        claim = await OutboxStore(db).claim(("skill.publish",))
        assert claim is not None

        async def interleave(_claim) -> None:
            available.clear()

        outcome = await service.run(claim, interleave)
        assert outcome.state == "failed"
        assert not (tmp_path / "skills" / "inspect-project").exists()
    finally:
        await db.close()


async def test_restart_hides_published_skill_when_required_tool_disappears(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        directory = DirectorySkillStore(tmp_path / "skills")
        dispatcher = EffectDispatcher(OutboxStore(db))
        available = {"Board"}
        service = SkillQuality(db, directory, dispatcher, available_tools=lambda: available)
        dispatcher.register("skill.publish", service)
        principal = Principal.operator({"via": "token", "user_id": 1})
        markdown = MARKDOWN.replace("description:", "tools: Board\ndescription:")
        candidate = await service.submit_command(
            principal, "inspect-project", "1.0.0", markdown, [],
            expected_collection_revision=1, client_operation_id="candidate-before-restart",
        )
        await service.activate_command(
            principal, "inspect-project", "1.0.0", candidate["digest"],
            expected_collection_revision=2, client_operation_id="activate-before-restart",
        )
        assert await dispatcher.step()
        assert await directory.list("tenant")
        available.clear()
        await service.load_active()
        assert not await directory.list("tenant")
        with pytest.raises(SkillNotFoundError, match="inspect-project"):
            await directory.load("tenant", "inspect-project")
        available.add("Board")
        await service.load_active()
        assert await directory.list("tenant")
    finally:
        await db.close()


async def test_submission_and_activation_are_receipted(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        directory = DirectorySkillStore(tmp_path / "skills")
        dispatcher = EffectDispatcher(OutboxStore(db))
        service = SkillQuality(db, directory, dispatcher)
        dispatcher.register("skill.publish", service)
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
        assert await dispatcher.step()
        assert [item.id for item in await directory.list("tenant")] == ["inspect-project"]
        rows = await db.fetchall("SELECT state FROM effect_outbox WHERE kind = 'skill.publish'")
        assert [row["state"] for row in rows] == ["completed"]
    finally:
        await db.close()


async def test_skill_publication_rechecks_staged_state_after_claim(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        service = SkillQuality(db, DirectorySkillStore(tmp_path / "skills"))
        principal = Principal.operator({"via": "token", "user_id": 1})
        candidate = await service.submit_command(
            principal, "inspect-project", "1.0.0", MARKDOWN, [],
            expected_collection_revision=1, client_operation_id="candidate-before-change",
        )
        await service.activate_command(
            principal, "inspect-project", "1.0.0", candidate["digest"],
            expected_collection_revision=2, client_operation_id="activate-before-change",
        )
        claim = await OutboxStore(db).claim(("skill.publish",))
        assert claim is not None

        async def interleave(_claim) -> None:
            await db.execute("UPDATE skill_manifests SET status = 'revoked' WHERE id = 'inspect-project'")

        outcome = await service.run(claim, interleave)
        assert outcome.state == "failed"
        assert not (tmp_path / "skills" / "inspect-project").exists()
    finally:
        await db.close()


async def test_skill_publication_keeps_late_revocation_disabled(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        directory = DirectorySkillStore(tmp_path / "skills")
        service = SkillQuality(db, directory)
        principal = Principal.operator({"via": "token", "user_id": 1})
        candidate = await service.submit_command(
            principal, "inspect-project", "1.0.0", MARKDOWN, [],
            expected_collection_revision=1, client_operation_id="late-candidate",
        )
        await service.activate_command(
            principal, "inspect-project", "1.0.0", candidate["digest"],
            expected_collection_revision=2, client_operation_id="late-activation",
        )
        claim = await OutboxStore(db).claim(("skill.publish",))
        assert claim is not None
        checks = 0

        async def interleave(_claim) -> None:
            nonlocal checks
            checks += 1
            if checks == 2:
                await db.execute("UPDATE skill_manifests SET status = 'revoked' WHERE id = 'inspect-project'")

        outcome = await service.run(claim, interleave)
        assert outcome.state == "unknown"
        assert (tmp_path / "skills" / "inspect-project" / ".disabled").exists()
        assert await directory.list("tenant") == []
    finally:
        await db.close()
