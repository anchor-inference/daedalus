"""CreateProject: the folder checks, the confirmation card, and what the operator's answer makes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from daedalus.config import Settings
from daedalus.extensions.dispatcher_projects import ProjectMaker
from daedalus.extensions.notifications import ActionRequest
from daedalus.extensions.staff import AlreadyAnswered
from daedalus.stores.database import Database
from daedalus.stores.projects import ProjectError
from daedalus.stores.staff import StaffError
from daedalus.terminals.bridge import FolderCheck
from tests.unit.test_dispatcher import Main, main_rig
from tests.unit.test_orchestrator import events


@dataclass
class FakeBridge:
    """The host through its terminal daemon, as far as a new project needs it."""

    present: set[str] = field(default_factory=set)
    calls: list[tuple[str, bool]] = field(default_factory=list)
    up: bool = True

    def available(self) -> bool:
        return self.up

    async def check_folder(self, path: str, *, create_missing: bool = False, actor: str = "operator") -> FolderCheck:
        self.calls.append((path, create_missing))
        if path.rstrip("/") == "/home/someone":
            return FolderCheck(path=path, exists=True, is_dir=True, problem="the home directory itself cannot be a project folder")
        if path in self.present:
            return FolderCheck(path=path, exists=True, is_dir=True, writable=True, is_git=True)
        if create_missing:
            self.present.add(path)
            return FolderCheck(path=path, exists=True, is_dir=True, writable=True, created=True)
        return FolderCheck(path=path, exists=False, problem="the folder does not exist on the host")


async def maker_rig(settings: Settings, db: Database, tmp_path: Path) -> tuple[Main, ProjectMaker, str]:
    m = await main_rig(settings, db, tmp_path)
    maker = ProjectMaker(m.r.team.app, m.main)
    m.r.team.app.extensions["dispatcher_projects"] = maker
    maker.attach()
    return m, maker, await m.main.ensure()


async def test_a_container_folder_must_lie_under_the_allowed_roots(settings: Settings, db: Database, tmp_path: Path) -> None:
    m, _maker, sid = await maker_rig(settings, db, tmp_path)
    try:
        roots = tmp_path / "projects"
        roots.mkdir()
        m.r.manager.config.dispatcher.container_roots = [str(roots)]
        with pytest.raises(ValueError, match="outside the folders new projects may use"):
            await m.call(sid, "create_project", name="Garden", folders=[{"path": "/etc/garden", "env": "container"}])
        with pytest.raises(ValueError, match="does not exist; pass create_missing"):
            await m.call(sid, "create_project", name="Garden", folders=[{"path": str(roots / "garden"), "env": "container"}])
        with pytest.raises(ValueError, match="already a project called Bakery"):
            await m.call(sid, "create_project", name="bakery")
        assert await m.r.manager.asks.of_origin("dispatcher") == [], "a refused call asks nothing"
    finally:
        await m.r.manager.close()


async def test_a_folder_another_project_has_is_offered_with_that_said_and_shared_on_create(settings: Settings, db: Database, tmp_path: Path) -> None:
    m, _maker, sid = await maker_rig(settings, db, tmp_path)
    try:
        roots = tmp_path / "projects"
        (roots / "shed").mkdir(parents=True)
        m.r.manager.config.dispatcher.container_roots = [str(roots)]
        shed = await m.r.manager.projects.create("Shed", [str(roots / "shed")])
        await m.call(sid, "create_project", name="Tools", folders=[{"path": str(roots / "shed"), "env": "container"}], start_orchestrator=False)
        [card] = await m.r.manager.asks.of_origin("dispatcher")
        assert f"use the container folder {roots / 'shed'}, also in the project Shed" in card.text
        assert (await m.r.team.answer(card.id, selected=["Create"], by="operator", via="app"))["delivered"]
        [tools] = [p for p in await m.r.manager.projects.list() if p.name == "Tools"]
        assert [str(f.path) for f in tools.folders] == [str(roots / "shed")]
        kept = await m.r.manager.projects.get(shed.id)
        assert kept is not None and [str(f.path) for f in kept.folders] == [str(roots / "shed")]
    finally:
        await m.r.manager.close()


async def test_nothing_is_created_until_the_operator_confirms_and_then_the_setup_starts(settings: Settings, db: Database, tmp_path: Path) -> None:
    m, maker, sid = await maker_rig(settings, db, tmp_path)
    try:
        roots = tmp_path / "projects"
        roots.mkdir()
        m.r.manager.config.dispatcher.container_roots = [str(roots)]
        target = roots / "garden"
        said = await m.call(sid, "create_project", name="Garden", folders=[{"path": str(target), "env": "container"}], goal="A planting calendar", create_missing=True)
        assert said.startswith("asked the operator to confirm as [q")
        assert not target.exists(), "the folder waits for the confirmation"
        assert [p.name for p in await m.r.manager.projects.list() if p.name == "Garden"] == []
        [card] = await m.r.manager.asks.of_origin("dispatcher")
        assert card.kind == "project" and card.project_id is None and card.detail["options"] == ["Create", "Don't create"]
        with pytest.raises(ProjectError, match="no operator confirmation"):
            await m.r.manager.projects.create("Garden", [str(target)], initial_goal="A planting calendar",
                                              confirmation_ask_id=card.id)
        assert [p.name for p in await m.r.manager.projects.list() if p.name == "Garden"] == []
        assert f"make the container folder {target}" in card.text and "Goal: A planting calendar" in card.text
        [pending] = [e for e in await events(m.r.manager, "ask.pending") if e.payload.get("kind") == "project"]
        assert pending.session_id == sid and [o["label"] for o in pending.payload["questions"][0]["options"]] == ["Create", "Don't create"]
        view = await m.main.view()
        assert [a["id"] for a in view["asks"]] == [card.id] and view["questions"] == 1

        answered = await m.r.team.answer(card.id, selected=["Create"], by="operator", via="main")
        assert answered["delivered"]
        with pytest.raises(AlreadyAnswered):
            await m.r.team.answer(card.id, selected=["Don't create"], by="operator", via="app")
        assert target.is_dir()
        [garden] = [p for p in await m.r.manager.projects.list() if p.name == "Garden"]
        assert garden.setup_by == "dispatcher" and garden.settings.orchestrator.enabled
        assert (await m.r.manager.projects.brief(garden.id))["goals"].body == "A planting calendar"
        goal = await db.fetchone("SELECT goal_revision,body,origin_kind,origin_ref"
                                 " FROM project_goal_revisions WHERE project_id = ?", (garden.id,))
        assert goal is not None and tuple(goal) == (1, "A planting calendar", "operator", card.id)
        assert "handed it dispatch" in await maker._create(card)
        assert len(await m.r.manager.dispatches.open_for(garden.id)) == 1
        [survey] = await m.r.manager.dispatches.open_for(garden.id)
        assert survey.seq == 1 and survey.kind == "setup" and "write its brief" in survey.text and "A planting calendar" in survey.text
        settled = await m.r.manager.asks.get(card.id)
        assert settled is not None and "switched its orchestrator on" in settled.resolution["outcome"] and settled.resolution["via"] == "main"
        [told] = [e for e in await events(m.r.manager, "ask.answered") if e.payload.get("request_id") == card.id]
        wake = await m.main.classify(told)
        assert wake is not None and wake.urgent, "the main orchestrator learns what its confirmation made"
        assert "switched its orchestrator on" in await m.main.line(told)
    finally:
        await m.r.manager.close()


async def test_confirmed_project_cannot_change_its_folder_before_creation(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    m, _maker, sid = await maker_rig(settings, db, tmp_path)
    try:
        roots = tmp_path / "projects"
        roots.mkdir()
        m.r.manager.config.dispatcher.container_roots = [str(roots)]
        await m.call(sid, "create_project", name="Garden", goal="A planting calendar",
                     folders=[{"path": str(roots / "garden"), "env": "container"}], create_missing=True)
        [card] = await m.r.manager.asks.of_origin("dispatcher")
        assert await m.r.manager.asks.resolve(card.id, "operator", {"allow": True})
        with pytest.raises(ProjectError, match="folders differ"):
            await m.r.manager.projects.create("Garden", [str(roots / "other")],
                                              initial_goal="A planting calendar", confirmation_ask_id=card.id)
        assert not [project for project in await m.r.manager.projects.list() if project.name == "Garden"]
    finally:
        await m.r.manager.close()


async def test_a_declined_confirmation_makes_nothing(settings: Settings, db: Database, tmp_path: Path) -> None:
    m, _maker, sid = await maker_rig(settings, db, tmp_path)
    try:
        await m.call(sid, "create_project", name="Garden")
        [card] = await m.r.manager.asks.of_origin("dispatcher")
        assert "a new folder of the installation's own" in card.text
        await m.r.team.answer(card.id, selected=["Don't create"], by="operator", via="app")
        assert [p for p in await m.r.manager.projects.list() if p.name == "Garden"] == []
        settled = await m.r.manager.asks.get(card.id)
        assert settled is not None and settled.resolution["outcome"].startswith("declined")
    finally:
        await m.r.manager.close()


async def test_a_host_folder_is_checked_on_the_host_and_confirmed_in_the_app_alone(settings: Settings, db: Database, tmp_path: Path) -> None:
    m, maker, sid = await maker_rig(settings, db, tmp_path)
    try:
        if m.r.manager.projects.local_env != "container":
            pytest.skip("a host folder is the other environment only in a container installation")
        with pytest.raises(ValueError, match="needs the host terminal bridge"):
            await m.call(sid, "create_project", name="Shop", folders=[{"path": "/home/someone/shop", "env": "host"}])
        bridge = FakeBridge(present={"/home/someone/code"})
        m.r.team.app.extensions["host_bridge"] = bridge
        with pytest.raises(ValueError, match="home directory itself"):
            await m.call(sid, "create_project", name="Shop", folders=[{"path": "/home/someone", "env": "host"}])
        with pytest.raises(ValueError, match="does not exist on the host"):
            await m.call(sid, "create_project", name="Shop", folders=[{"path": "/home/someone/shop", "env": "host"}])
        await m.call(sid, "create_project", name="Shop", folders=[{"path": "/home/someone/shop", "env": "host"}], create_missing=True)
        assert ("/home/someone/shop", True) not in bridge.calls, "nothing is made on the host before the confirmation"
        [card] = await m.r.manager.asks.of_origin("dispatcher")
        assert await m.main.host_level(card)
        [pending] = [e for e in await events(m.r.manager, "ask.pending") if e.payload.get("kind") == "project"]
        assert pending.payload["questions"][0]["options"] == [], "no notification action can create a host project"
        with pytest.raises(StaffError, match="confirmed in the app"):
            await m.r.team.answer(card.id, selected=["Create"], by="operator", via="telegram")
        still = await m.r.manager.asks.get(card.id)
        assert still is not None and still.open
        await m.r.team.answer(card.id, selected=["Create"], by="operator", via="main")
        assert ("/home/someone/shop", True) in bridge.calls
        [shop] = [p for p in await m.r.manager.projects.list() if p.name == "Shop"]
        assert shop.primary.env == "host" and str(shop.primary.path) == "/home/someone/shop"
        assert maker is m.r.team.dispatcher_requests
    finally:
        await m.r.manager.close()


async def test_the_confirmation_is_answered_from_a_notification_too(settings: Settings, db: Database, tmp_path: Path) -> None:
    m, maker, sid = await maker_rig(settings, db, tmp_path)
    try:
        await m.call(sid, "create_project", name="Garden", start_orchestrator=False)
        [card] = await m.r.manager.asks.of_origin("dispatcher")
        ref = str(card.detail["event_ref"])
        outcome = await maker.resolve_action(ActionRequest(request_ref=ref, kind="dispatcher", scope="main", target=card.id, action="answer:0", value=None, via="push", notification=None))  # type: ignore[arg-type]
        assert outcome.resolution == "answered"
        [garden] = [p for p in await m.r.manager.projects.list() if p.name == "Garden"]
        assert not garden.settings.orchestrator.enabled and garden.setup_by == ""
    finally:
        await m.r.manager.close()


async def test_a_goal_the_model_escaped_twice_keeps_its_cyrillic(settings: Settings, db: Database, tmp_path: Path) -> None:
    m, _maker, sid = await maker_rig(settings, db, tmp_path)
    try:
        roots = tmp_path / "projects"
        roots.mkdir()
        m.r.manager.config.dispatcher.container_roots = [str(roots)]
        # As a model sent it: valid JSON whose string holds the escapes themselves, not the letters.
        goal = "\\u041f\\u0440\\u043e\\u0435\\u043a\\u0442 labs. \\u041f\\u0430\\u043f\\u043a\\u0430 \\u043d\\u0430 \\u0445\\u043e\\u0441\\u0442\\u0435"
        await m.call(sid, "create_project", name="Labs", folders=[{"path": str(roots / "labs"), "env": "container", "label": "\\u043b\\u0430\\u0431"}], goal=goal, create_missing=True)
        [card] = await m.r.manager.asks.of_origin("dispatcher")
        assert "Goal: Проект labs. Папка на хосте" in card.text and "\\u" not in card.text
        await m.r.team.answer(card.id, selected=["Create"], by="operator", via="main")
        [labs] = [p for p in await m.r.manager.projects.list() if p.name == "Labs"]
        assert labs.primary.label == "лаб"
        assert (await m.r.manager.projects.brief(labs.id))["goals"].body == "Проект labs. Папка на хосте"
        [survey] = await m.r.manager.dispatches.open_for(labs.id)
        assert "The operator's goal for it: Проект labs. Папка на хосте" in survey.text and "\\u" not in survey.text
    finally:
        await m.r.manager.close()
