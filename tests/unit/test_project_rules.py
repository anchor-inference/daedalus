"""The operator's standing rules: written once, shown whole to the orchestrator in every turn's state
block and to every member with every task, told to the members at work when one is made or lifted,
and bounded where they are written so that all of them always fit.

The instruction these exist for was appended to the brief's notes, a section already three times
longer than the part of it the state block showed; no later turn of the orchestrator saw it, and no
member ever did."""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.config import Settings
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.stores.database import Database
from daedalus.stores.projects import RULE_TEXT_MAX, RULES_CHARS, RULES_MAX
from tests.unit.test_orchestrator import rig
from tests.unit.test_orchestrator_team import fake, office, working
from tests.unit.test_staff_runtime import BRIEF, board_task

NOTES = "Earlier notes of the project, kept for the record. " * 30
RULE = (
    "Whenever a task is running and a member has reported, and there is a member free to take the next "
    "step, hand that step out at once: do not stop at a report to me, do not wait to be asked, and never "
    "leave a free member idle while work that could be theirs waits on the board. If nobody fits, say so "
    "in one line and hire. This holds for every piece of work in this project, the updater and the mail "
    "server alike, until I say otherwise; a blocker is a reason to reassign, not to pause."
)


def test_the_rule_is_longer_than_the_part_of_a_section_the_state_block_used_to_show() -> None:
    assert 400 < len(RULE) <= RULE_TEXT_MAX and len(NOTES) > 1500


async def test_a_rule_is_shown_whole_every_turn_and_given_to_every_member_with_the_task(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        await r.manager.projects.set_brief(r.project.id, "notes", NOTES, "orchestrator")
        await r.manager.projects.set_brief(r.project.id, "constraints", "Never push to the shared remote.", "operator")
        ada, ada_live = await working(r, "Ada", "Menu page")
        ben = await r.manager.staff.hire(r.project.id, name="Ben", role="Photos", isolation="shared")
        await r.team.assign(ben, await board_task(r.manager, r.project, "Photos"), by="operator")
        ben_live = await r.team.live_of(ben)
        assert ben_live is not None
        # Ben handed his task in and sits idle: he is not woken for the rule, his next brief has it.
        await r.manager.db.execute("UPDATE board_tasks SET status = 'done' WHERE id = ?", (ben_live.session.task_id,))
        await r.team.ingress.status(ben_live, "idle")

        said = await r.call(sid, "journal", kind="rule", text=RULE)
        rule_id = int(said.split("#")[1].split()[0])
        assert said.endswith("every member gets it with each task; Ada is told when their current turn ends")
        [told] = [m for m in await r.manager.staff.messages(ada.id) if RULE in m.text]
        assert told.mode == "after_turn" and told.text.startswith("[a rule of the operator's for this project, in force from now on")
        assert not [m for m in await r.manager.staff.messages(ben.id) if RULE in m.text]

        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert RULE in state, "whole, not cut at the old 400 characters"
        assert f"  #{rule_id} (" in state and "The operator's rules in force" in state
        assert f"notes: {NOTES.strip()[:100]}" in state and f"({len(NOTES.strip())} characters, changed " in state and "Brief(section='notes') shows it whole" in state

        # The next brief of any member carries it, with the project's constraints.
        await r.call(sid, "assign", staff="Ben", title="Photos of the new menu", **BRIEF)
        first = runtime.started[-1].first_message
        assert RULE in first and "The operator's rules for this project, in force for all work here:" in first
        assert "- The project's constraints: Never push to the shared remote." in first
        assert first.index(RULE) < first.index("Folder:")

        # Lifted, it leaves the state block and the briefs, and the members at work hear of it.
        said = await r.call(sid, "journal", op="lift", rule=rule_id, why="the team is two people now")
        assert said.startswith(f"rule #{rule_id} lifted") and "Ada, Ben are told" in said
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert RULE not in state and "rules in force" not in state
        assert "(lifted)" in await r.call(sid, "journal", op="read", kind="rule")
        with pytest.raises(Refused, match="was lifted already"):
            await r.call(sid, "journal", op="lift", rule=rule_id)
        with pytest.raises(Refused, match="needs rule"):
            await r.call(sid, "journal", op="lift")
        assert ada_live is not None
    finally:
        await r.manager.close()


async def test_rules_are_bounded_where_they_are_written_so_all_of_them_always_show(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        with pytest.raises(Refused, match=f"a rule is at most {RULE_TEXT_MAX} characters"):
            await r.call(sid, "journal", kind="rule", text="x" * (RULE_TEXT_MAX + 1))
        with pytest.raises(Refused, match="a rule needs its text"):
            await r.call(sid, "journal", kind="rule", text="  ")
        for n in range(RULES_MAX):
            await r.call(sid, "journal", kind="rule", text=f"Rule number {n}: keep the changelog in step with every release.")
        with pytest.raises(Refused, match=f"{RULES_MAX} rules are in force already"):
            await r.call(sid, "journal", kind="rule", text="One more.")
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert all(f"Rule number {n}:" in state for n in range(RULES_MAX))

        for rule in (await r.manager.projects.rules(r.project.id))[:8]:
            await r.call(sid, "journal", op="lift", rule=rule.id)
        await r.call(sid, "journal", kind="rule", text="y" * 500)
        await r.call(sid, "journal", kind="rule", text="z" * 500)
        with pytest.raises(Refused, match=f"more than {RULES_CHARS} characters"):
            await r.call(sid, "journal", kind="rule", text="w" * 500)

        # An ordinary entry is what it was: a decision unless said otherwise, and never a rule.
        assert (await r.call(sid, "journal", text="Chose the smaller model for the scouts")).startswith("journal entry #")
        assert (await r.manager.projects.journal(r.project.id, limit=1))[0].kind == "decision"
    finally:
        await r.manager.close()
