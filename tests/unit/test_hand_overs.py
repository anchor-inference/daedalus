"""Who does a piece of work, and on which card: rework goes back to whoever made it unless a reason says
otherwise; a model the operator names does the work itself; a card someone is working on is not taken
to other work; work already on the board is handed on rather than opened twice; a question still
waiting is changed in place rather than asked again; and what the operator allowed is not narrowed
without the operator hearing of it."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.extensions.runtime_observations import admit_native_run, observe_exit
from daedalus.harness.contract import Catalog
from daedalus.stores.database import Database
from daedalus.stores.staff import Staff
from tests.support.authorized_launch import operator_task
from tests.support.authorized_results import return_reviewed_result
from tests.support.authorized_stop import bind_native_run, finish_native_stop, stop_native_task
from tests.support.waiting import until_await
from tests.unit.test_board_rounds import task_of
from tests.unit.test_orchestrator import Rig, events, rig
from tests.unit.test_orchestrator_team import fake, office
from tests.unit.test_staff_runtime import Capacity, close_team, task_row
from tests.unit.test_task_contract import SCRIPT, apply_requirement

VIDEO = {
    "objective": "Render the 3D video of the product, version H, in English and Russian cuts",
    "deliverable": "video-h-en.mp4 and video-h-ru.mp4 with their sources",
    "boundaries": "The renders folder only; upload nothing",
    "done_when": "Both cuts play and match the storyboard",
}


async def hand_in(r: Rig, who: Staff, note: str = "both cuts rendered") -> None:
    live = await r.team.live_of(who)
    assert live is not None
    _folder, cwd = await r.team.cwd_of(live)
    (Path(cwd) / "video-h-en.mp4").write_bytes(b"fixture render")
    source = await r.manager.db.fetchone("SELECT tenant_id FROM sessions WHERE id = ?",
                                         (live.session.session_id,))
    assert source is not None
    run_id = uuid.uuid4().hex
    at = datetime.now(UTC).isoformat()
    await r.manager.db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                               " VALUES (?,?,?,'running',?,?)",
                               (run_id, source["tenant_id"], live.session.session_id, at, at))
    await admit_native_run(r.team.app, live.id, live.session.session_id, run_id)
    await r.team.ingress.report(live, "done", note, artifacts=["video-h-en.mp4"],
                                call_id=f"fixture-report:{uuid.uuid4().hex}")
    await r.manager.db.execute("UPDATE runs SET status = 'completed' WHERE id = ?", (run_id,))
    assert await observe_exit(r.team.app, staff_session_id=live.id, runtime_ref=run_id,
                              observed_status="completed")
    await r.team.ingress.status((await r.team.live_of(who)) or live, "idle")


async def made_by_ira(r: Rig, sid: str) -> tuple[str, Staff]:
    ira = await r.manager.staff.hire(r.project.id, name="Ira", role="3D video", isolation="shared")
    task_id = task_of(await r.call(sid, "assign", staff="Ira", title="3D video, version H", **VIDEO))
    await hand_in(r, ira)
    return task_id, ira


# -- the previous owner ------------------------------------------------------------------------------------------


async def test_rework_goes_back_to_who_made_it_and_elsewhere_only_with_a_reason(settings: Settings, db: Database, tmp_path: Path) -> None:
    """Two small fixes to a video went to a newly hired helper while the member who had made it sat
    free; the helper died at its start, and it took nine failed calls to give the work back."""
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        task_id, _ = await made_by_ira(r, sid)
        await r.manager.staff.hire(r.project.id, name="Gleb", role="Reviews", isolation="shared")
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        with pytest.raises(Refused, match=f"task {task_id} is Ira's work, and rework goes to whoever made it"):
            await r.call(sid, "assign", staff="Gleb", task_id=task_id, objective="Fix the caption at the start and the labels in the last shot")
        await return_reviewed_result(r.team, task_id, reason="the caption and labels need another pass")
        said = await r.call(sid, "assign", task_id=task_id, objective="Fix the caption at the start and the labels in the last shot")
        assert said.startswith(f"Ira will start {task_id}")
        from tests.unit.test_orchestrator_team import admitted

        await until_await(lambda: admitted(r, task_id), "Ira's return round launched")
        assert "Fix the caption at the start" in runtime.started[-1].first_message
        await hand_in(r, (await r.manager.staff.find(r.project.id, "Ira")))  # type: ignore[arg-type]

        await return_reviewed_result(r.team, task_id, reason="the two cuts need independent inspection")
        said = await r.call(sid, "assign", staff="Gleb", task_id=task_id, objective="Check both cuts frame by frame", reason="Ira is on leave today and the operator wants it before noon")
        assert said.startswith(f"Gleb will start {task_id}")
        await until_await(lambda: admitted(r, task_id), "Gleb's reviewed handover launched")
        notes = (await task_row(r.manager, task_id))["notes"]
        assert "reassigned from Ira: Ira is on leave today and the operator wants it before noon" in notes
        journal = [(e.kind, e.text) for e in await r.manager.projects.journal(r.project.id, limit=10)]
        assert ("reassignment", f"Task {task_id} \"3D video, version H\" passed from Ira to Gleb: Ira is on leave today and the operator wants it before noon") in journal
        assert "Waiting for your decision" not in state or task_id not in state.split("Waiting for your decision")[0]
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_card_nobody_holds_says_whose_it_was_and_a_one_off_hire_names_who_made_what(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        task_id, ira = await made_by_ira(r, sid)
        await return_reviewed_result(r.team, task_id, reason="fix the opening captions")
        await r.call(sid, "assign", task_id=task_id, objective="Fix the caption at the start of both cuts")
        await r.call(sid, "release", staff="Ira")
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert f"{task_id} 3D video, version H (unassigned, todo) — was Ira's" in state
        said = await r.call(sid, "hire", name="Fixer", role="Small video fixes", one_off=True)
        assert f"Ira is free and worked on \"3D video, version H\" ({task_id})" in said and "Assign(task_id=…) without staff gives a card back" in said
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- a model the operator names --------------------------------------------------------------------------------------


@dataclass
class Harnesses:
    """The harness manager as far as hiring and the state block ask it: a Codex CLI offering two of
    its three models (the operator's pick) and a Claude Code CLI."""

    offered: dict[str, list[str]] = field(default_factory=lambda: {"codex": ["gpt-6-luna", "gpt-6-sol"], "claude": ["claude-sonnet-5"]})
    listed: dict[str, list[str]] = field(default_factory=lambda: {"codex": ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"], "claude": ["claude-sonnet-5", "claude-opus-5"]})

    async def harnesses(self, env: str) -> list[dict[str, Any]]:
        labels = {"codex": "Codex", "claude": "Claude Code"}
        return [
            {"harness": h, "label": labels[h], "installed": True, "unavailable": "", "models": self.offered[h], "all_models": self.listed[h], "models_chosen": self.offered[h] != self.listed[h]}
            for h in ("codex", "claude")
        ]

    async def catalog(self, env: str, harness: str, folder_id: str | None = None) -> Catalog:
        modes = ("read-only", "workspace-write", "danger-full-access") if harness == "codex" else ("default", "acceptEdits", "plan")
        return Catalog(agents=(), models=tuple(self.listed[harness]), modes=modes, efforts=("low", "medium", "high"))

    async def hire_problem(self, env: str, harness: str) -> str:
        return ""

    async def hire_warning(self, env: str, harness: str) -> str:
        return ""


def with_clis(r: Rig) -> None:
    runtime = r.team.runtimes["daedalus"]
    r.team.runtimes["codex"] = runtime
    r.team.runtimes["claude"] = runtime
    r.team._capacity = Capacity()
    r.team.app.extensions["harness"] = Harnesses()


async def test_the_state_block_lists_what_can_be_hired_with_the_operators_pick_first(settings: Settings, db: Database, tmp_path: Path) -> None:
    """Asked for a named model at a named effort, the orchestrator's view held no word of what could be
    hired, and it told another member to arrange one "if available"."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        with_clis(r)
        sid = await office(r)
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert "Can be hired here" in state
        assert "Codex (container): models gpt-6-luna, gpt-6-sol (+1 more the operator did not pick; any works when named) · efforts low, medium, high" in state
        assert "modes read-only (reads only: no file writes and no network, whatever the task allows), workspace-write (writes in its own folder; the network stays off)" in state
        assert "Daedalus (presets): " in state
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_work_that_names_a_model_goes_to_a_member_that_runs_it(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        with_clis(r)
        sid = await office(r)
        await r.manager.staff.hire(r.project.id, name="Ada", role="Scripts", harness="claude", model="claude-sonnet-5", isolation="shared")
        task_of(await r.call(sid, "assign", staff="Ada", title="Post drafts", **SCRIPT))
        check = {**SCRIPT, "objective": "Have gpt-6-luna at high effort check the mail relay after its production approval and fix what needs fixing"}
        with pytest.raises(Refused, match="the brief names gpt-6-luna, and Ada runs claude-sonnet-5: .*\\(Codex can\\) — Hire\\(harness=…, model='gpt-6-luna'"):
            await r.call(sid, "assign", staff="Ada", title="Mail relay check", **check)
        said = await r.call(sid, "hire", name="Luna", role="Mail relay review", harness="codex", model="gpt-6-luna", effort="high", one_off=True)
        assert said.startswith("hired Luna")
        assert task_of(await r.call(sid, "assign", staff="Luna", title="Mail relay check", **check))
        told = await r.call(sid, "tell", staff="Ada", text="If you can, raise a one-off reviewer on gpt-6-luna for the relay")
        assert "the message names gpt-6-luna, which Ada does not run and cannot hire" in told
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- the card ---------------------------------------------------------------------------------------------------------


async def test_a_card_someone_works_on_is_not_taken_to_other_work(settings: Settings, db: Database,
                                                                  tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A card a member was at work on was renamed into an unrelated check of a mail service."""
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        await r.manager.staff.hire(r.project.id, name="Ada", role="Posts", isolation="shared")
        posts = {**SCRIPT, "objective": "Update the local post drafts with the 30-second clips"}
        task_id = task_of(await r.call(sid, "assign", staff="Ada", title="Update the post drafts with the short clips", **posts))
        with pytest.raises(Refused, match=f"task {task_id} is \"Update the post drafts with the short clips\", and Ada is at work on it: .* is other work, which is a card of its own"):
            await r.call(sid, "assign", staff="Ada", task_id=task_id, title="Check the mail relay after its approval", objective="Check the mail relay's settings after its production approval")
        assert (await task_row(r.manager, task_id))["title"] == "Update the post drafts with the short clips"
        with pytest.raises(Refused, match="stop or reconcile the current execution"):
            await r.call(sid, "assign", staff="Ada", task_id=task_id,
                         title="Update the post drafts with the short and long clips",
                         reason="the operator asked for both clip lengths in the same drafts")
        assert (await task_row(r.manager, task_id))["title"] == "Update the post drafts with the short clips"
        ada = await r.manager.staff.find(r.project.id, "Ada")
        assert ada is not None
        live = await r.team.live_of(ada)
        assert live is not None
        await stop_native_task(r.team, task_id, live, monkeypatch)
        said = await r.call(sid, "assign", staff="Ada", task_id=task_id,
                            title="Update the post drafts with the short and long clips",
                            reason="the operator asked for both clip lengths in the same drafts")
        assert "The card was renamed" in said
        from tests.unit.test_orchestrator_team import admitted

        await until_await(lambda: admitted(r, task_id), "renamed work launched")
        assert "Update the post drafts with the short and long clips" in runtime.started[-1].first_message
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_work_already_on_the_board_is_handed_on_rather_than_opened_twice(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A read-only audit of a mail service got a second card under a new title when it was given to a
    newly hired member instead of being handed over; a translation had two cards, one dropped later."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        await r.manager.staff.hire(r.project.id, name="Webops", role="Mail", isolation="shared")
        await r.manager.staff.hire(r.project.id, name="Mailops", role="Mail", isolation="shared")
        audit = {**SCRIPT, "objective": "Audit the mail relay and the mail server after the relay left its sandbox, read-only"}
        first = task_of(await r.call(sid, "assign", staff="Webops", title="Read-only audit of the mail relay after the sandbox", **audit))
        with pytest.raises(Refused, match=f"is work already on the board: {first} \"Read-only audit of the mail relay after the sandbox\" \\(doing, Webops\\)"):
            await r.call(sid, "assign", staff="Mailops", title="Read-only check of the mail relay after the sandbox", **audit)
        with pytest.raises(Refused, match="is work already on the board"):
            await r.call(sid, "tasks", op="create", title="Read-only check of the mail relay after the sandbox", **audit)
        with pytest.raises(Refused, match="is work already on the board"):
            await r.call(sid, "assign", staff="Mailops", title="Read-only check of the mail relay after the sandbox", **audit, new=True)
        said = await r.call(sid, "assign", staff="Mailops", title="Read-only check of the mail relay after the sandbox", **audit, new=True, reason="a second opinion from another member, on purpose")
        second = task_of(said)
        assert second != first
        # Finished a while ago it no longer counts; finished an hour ago it still does.
        for card in (first, second):
            await r.board.update(card, status="dropped", note="replaced")
        with pytest.raises(Refused, match="is work already on the board"):
            await r.call(sid, "tasks", op="create", title="Read-only audit of the mail relay after the sandbox", **audit)
        await r.manager.db.execute("UPDATE board_tasks SET updated_at = ? WHERE project_id = ?", ((datetime.now(UTC) - timedelta(hours=8)).isoformat(), r.project.id))
        assert "Read-only audit" in await r.call(sid, "tasks", op="create", title="Read-only audit of the mail relay after the sandbox", **audit)
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- the operator's questions -----------------------------------------------------------------------------------------


QUESTIONS = [
    {"title": "README and the new video", "text": "The roadmap says the README does not link the public video yet. May I prepare a local README patch with a short clip and show it to you, or do you allow commit and push to the public repository?", "options": ["Prepare locally and show me", "Commit and push after checks", "Leave the README"]},
    {"title": "Which company accounts exist?", "text": "The roadmap starts with a Russian Telegram channel and later English X and LinkedIn accounts, but nobody confirmed they exist. Which of these channels are created already?", "options": ["Russian Telegram exists", "English X and LinkedIn exist", "None yet"]},
]
AGAIN = {"title": "Company channels", "text": "Please tell me which official company accounts are created already: the Russian Telegram, English X, English LinkedIn? We will not create any without you.", "options": ["Russian Telegram exists", "English X exists", "English LinkedIn exists", "None yet"]}


async def test_a_question_still_waiting_is_changed_in_place_never_asked_again(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The operator found six questions in their list, three of them the first three reworded while the
    first ones had waited eight minutes and the state block said so."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        await r.call(sid, "ask_operator", questions=QUESTIONS)
        first = [a for a in await r.manager.asks.open_for(r.project.id) if a.origin == "orchestrator"]
        channels = next(a for a in first if a.title == "Which company accounts exist?")
        with pytest.raises(Refused, match=f"nothing was asked: \"Company channels\" asks what \\[{channels.short_id}\\] \"Which company accounts exist\\?\" already asks"):
            await r.call(sid, "ask_operator", questions=[AGAIN, {"title": "Release date", "text": "When should the next release go out: this week or after the video is published?"}])
        assert len([a for a in await r.manager.asks.open_for(r.project.id) if a.origin == "orchestrator"]) == 2, "nothing of the refused call was asked"

        said = await r.call(sid, "ask_operator", op="update", id=channels.short_id, title=AGAIN["title"], text=AGAIN["text"], options=AGAIN["options"])
        assert said == f"[{channels.short_id}] is updated in place (revision 1); the operator sees it changed, and an answer they began to write stays"
        changed = await r.manager.asks.get(channels.id)
        assert changed is not None and changed.id == channels.id and changed.title == "Company channels" and changed.detail["options"][2] == "English LinkedIn exists"
        assert changed.detail["revision"] == 1 and changed.detail["updated_at"]
        [event] = await events(r.manager, "ask.updated")
        assert event.payload["request_id"] == channels.id

        said = await r.call(sid, "ask_operator", op="update", id=channels.short_id, text="Which official company accounts exist already?")
        assert (await r.manager.asks.get(channels.id)).detail["options"] == AGAIN["options"], "words changed alone keep the options"  # type: ignore[union-attr]
        await r.call(sid, "ask_operator", op="withdraw", ids=[channels.short_id], reason="the operator told me in chat")
        assert not (await r.manager.asks.get(channels.id)).open  # type: ignore[union-attr]
        with pytest.raises(Refused, match="already answered by the system"):
            await r.call(sid, "ask_operator", op="update", id=channels.id, text="again")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_twins_already_asked_are_pointed_out_in_the_state_block(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        project = await r.refreshed()
        older = await r.orch.open_request(project, sid, kind="question", title=QUESTIONS[1]["title"], text=QUESTIONS[1]["text"], options=QUESTIONS[1]["options"], detail={})
        await r.orch.open_request(project, sid, kind="question", title=AGAIN["title"], text=AGAIN["text"], options=AGAIN["options"], detail={})
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert f"asks again what [{older.short_id}] asks: withdraw one" in state
        assert "AskOperator(op='update', id=…), never asked again" in state
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- what the operator allowed --------------------------------------------------------------------------------------


async def test_a_condition_that_narrows_what_the_operator_allowed_needs_a_reason_and_reaches_them(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"If something needs fixing, fix it" became "read-only, no configuration changes, nothing until a
    report and a permission" in the brief, and the operator learnt it only from the result."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        await r.manager.staff.hire(r.project.id, name="Webops", role="Mail", isolation="shared")
        grant = {"text": "If something needs fixing after the approval, fix it", "kind": "scope"}
        narrowing = {"text": "Read-only: no configuration changes before a report", "kind": "constraint", "source": "orchestrator"}
        with pytest.raises(Refused, match="the operator allowed this work \"If something needs fixing after the approval, fix it\"; a condition of yours that narrows it takes its reason"):
            await r.call(sid, "assign", staff="Webops", title="Mail relay after approval", **SCRIPT, requirements=[grant, narrowing])
        task_id = task_of(await r.call(sid, "assign", staff="Webops", title="Mail relay after approval", **SCRIPT, requirements=[grant]))
        with pytest.raises(Refused, match="takes its reason"):
            await r.call(sid, "require", task_id=task_id, text="Send no real mail", kind="constraint")
        webops = await r.manager.staff.find(r.project.id, "Webops")
        assert webops is not None
        live = await r.team.live_of(webops)
        assert live is not None
        run_id, stops = await bind_native_run(r.team, live, monkeypatch)
        staged = json.loads(await r.call(sid, "require", task_id=task_id, text="Send no real mail",
                                         kind="constraint", why="a test message would reach real customers"))
        assert staged["state"] == "pending_physical_exit"
        await finish_native_stop(r.team, live, run_id, staged["stop_effect_id"], stops)
        applied = json.loads(await r.call(sid, "require", op="apply", task_id=task_id,
                                          intent_id=staged["intent_id"]))
        assert applied["state"] == "applied"
        posted = r.team.app.notifications.posted[-1]
        assert posted.title.endswith("a condition on what you allowed") and "Send no real mail — a test message would reach real customers" in posted.body
        assert any(e.kind == "narrowing" for e in await r.manager.projects.journal(r.project.id, limit=5))
        # The operator's own condition is theirs to add, and nobody's narrowing.
        await apply_requirement(r, sid, task_id, text="Do not touch DNS", kind="constraint", source="operator")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_member_restricted_to_reading_is_widened_rather_than_the_operator_asked_again(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A reviewer was hired read-only for a check the operator had said to fix as well; when the operator
    allowed a test message, it could not send one, and the orchestrator asked the operator to lift a
    restriction it had set itself."""
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        with_clis(r)
        sid = await office(r)
        said = await r.call(sid, "hire", name="Luna", role="Mail relay review", harness="codex", model="gpt-6-luna", effort="high", permission_mode="read-only", one_off=True)
        assert "Its mode read-only: reads only: no file writes and no network" in said and "needs another mode (StaffEdit)" in said
        grant = {"text": "Check the relay after its approval and fix what needs fixing", "kind": "scope"}
        with pytest.raises(Refused, match="Luna runs read-only: reads only: no file writes and no network.*StaffEdit\\(staff='Luna', permission_mode=…\\)"):
            await r.call(sid, "assign", staff="Luna", title="Mail relay after approval", **SCRIPT, requirements=[grant])
        task_id = await operator_task(db, r.project.id, "Mail relay after approval", brief=SCRIPT)
        await apply_requirement(r, sid, task_id,
                                text="One test message to the operator's own address is allowed",
                                kind="scope", source="operator")
        with pytest.raises(Refused, match="Luna runs read-only"):
            await r.call(sid, "assign", staff="Luna", task_id=task_id)
        await r.call(sid, "staff_edit", staff="Luna", permission_mode="workspace-write")
        assert (await r.call(sid, "assign", staff="Luna", task_id=task_id)).startswith(f"Luna will start {task_id}")
        from tests.unit.test_orchestrator_team import admitted

        await until_await(lambda: admitted(r, task_id), "the widened member launched")
    finally:
        await close_team(r.manager)
        await r.manager.close()
