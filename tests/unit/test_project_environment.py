"""A project's environment: declared once, prepared once, and holding members back until it is ready.

The clones here are of a bare repository made in the test's own directory: the suite never touches
the network, and a URL that would is refused by the policy before git is run.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import project_environment as environment_module
from daedalus.extensions.api import build_app
from daedalus.extensions.project_environment import (
    SKILL_LAUNCH_FILES,
    PrepareFailed,
    ProjectEnvironments,
    _verdict,
    _write_absent,
)
from daedalus.extensions.task_launch import queue_launch
from daedalus.harness.claude import ClaudeCodeAdapter
from daedalus.harness.codex import CodexAdapter
from daedalus.harness.contract import HandedSkill, LaunchSpec
from daedalus.harness.opencode import OpenCodeAdapter
from daedalus.harness.pi import PiAdapter
from daedalus.host.session_runner import SessionManager
from daedalus.host.skills import DirectorySkillStore, ScopedSkillStore
from daedalus.host.worktrees import StaffWorktrees
from daedalus.stores.database import Database
from daedalus.stores.project_environment import EnvironmentSpec, EnvironmentStore, SpecError, url_host
from daedalus.stores.projects import held_work
from tests.unit.test_launch_controls import OPERATOR
from tests.unit.test_task_launch import queued_fixture

HEADERS = {"X-Daedalus-Token": "tok"}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.name=someone", "-c", "user.email=someone@example.org", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repository with one commit on ``main`` and a ``docs`` branch."""
    bare = tmp_path / "origin.git"
    work = tmp_path / "origin-work"
    work.mkdir()
    _git("init", "--quiet", "--bare", "--initial-branch=main", str(bare), cwd=tmp_path)
    _git("init", "--quiet", "--initial-branch=main", cwd=work)
    (work / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=work)
    _git("commit", "--quiet", "-m", "first", cwd=work)
    _git("branch", "docs", cwd=work)
    _git("push", "--quiet", str(bare), "main", "docs", cwd=work)
    return bare


@pytest.fixture
async def envs(settings: Settings, config: RuntimeConfig, db: Database) -> Any:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    app = SimpleNamespace(db=db, manager=manager, settings=settings, config=config,
                          extensions={"staff": SimpleNamespace(worktrees=StaffWorktrees(manager.projects.local_env))})
    environments = ProjectEnvironments(app)  # type: ignore[arg-type]
    app.extensions["project_environment"] = environments
    try:
        yield environments
    finally:
        await manager.close()


# -- the spec -------------------------------------------------------------------------------------


def test_a_spec_refuses_what_would_run_a_program_or_climb_out() -> None:
    for url in ("ext::sh -c touch% /tmp/x", "-uhttps://example.org/x", "ftp://example.org/x.git", "https://exa mple.org/x"):
        with pytest.raises(SpecError):
            EnvironmentSpec.parse({"repos": [{"url": url}]})
    for path in ("../AGENTS.md", "/etc/passwd", "a/../../b", ".git/hooks/pre-commit", "~/x"):
        with pytest.raises(SpecError):
            EnvironmentSpec.parse({"seeds": [{"path": path, "content": "x"}]})
    with pytest.raises(SpecError, match="one folder"):
        EnvironmentSpec.parse({"repos": [{"url": "https://example.org/a/api.git"}, {"url": "git@example.org:b/api.git"}]})
    spec = EnvironmentSpec.parse({"repos": [{"url": "git@example.org:team/api.git", "branch": "main"}]})
    assert spec.repos[0].folder == "api" and url_host(spec.repos[0].url) == "example.org"
    assert url_host("/srv/repos/api.git") == "" and url_host("https://Example.org/x") == "example.org"


def test_the_visibility_subsets_do_not_make_a_prepared_environment_stale() -> None:
    base = EnvironmentSpec.parse({"seeds": [{"path": "AGENTS.md", "content": "Be brief."}]})
    narrowed = EnvironmentSpec.parse({**base.dump(), "mcp": ["github"], "skills": ["review"]})
    assert base.digest() == narrowed.digest()
    assert base.digest() != EnvironmentSpec.parse({"seeds": [{"path": "AGENTS.md", "content": "Be long."}]}).digest()
    assert not EnvironmentSpec.parse({"mcp": ["github"]}).needs_preparation


async def test_one_preparation_runs_at_a_time_and_a_stale_end_is_dropped(db: Database) -> None:
    await db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p1', 'Shop', '2026-10-15T00:00:00+00:00')")
    store = EnvironmentStore(db)
    await store.set_spec("p1", EnvironmentSpec.parse({"goal": "install"}))
    first = await store.begin("p1", first_only=True)
    assert first is not None and first.state == "preparing"
    assert await store.begin("p1", first_only=False) is None
    assert await store.interrupt_all() == ["p1"]
    failed = await store.get("p1")
    assert failed.state == "failed" and "restarted" in failed.reason
    assert await store.begin("p1", first_only=True) is None, "a launch never runs a preparation again by itself"
    second = await store.begin("p1", first_only=False)
    assert second is not None and second.attempt == first.attempt + 1
    assert not await store.finish("p1", first.attempt, ready=True, reason="", log="")
    assert await store.finish("p1", second.attempt, ready=True, reason="", log="done")
    assert (await store.get("p1")).state == "ready"
    assert "an environment" in await held_work(db.conn, "p1")


# -- preparing -----------------------------------------------------------------------------------------


async def test_preparing_clones_beside_the_primary_folder_and_seeds_without_overwriting(envs: ProjectEnvironments, origin: Path) -> None:
    manager = envs.app.manager
    project = await manager.projects.create("Shop")
    primary = project.primary.path
    (primary / "CLAUDE.md").write_text("the operator's own\n")
    await envs.save(project.id, {
        "repos": [{"url": str(origin), "branch": "docs", "folder": "api"}],
        "seeds": [{"path": "CLAUDE.md", "content": "seeded"}, {"path": "AGENTS.md", "content": "Run the tests."},
                  {"path": "api/AGENTS.md", "content": "The API's own."}],
    })
    assert "first launch" in await envs.hold(project.id, start=False)
    held = await envs.hold(project.id, start=True)
    assert held.startswith("environment not ready: it is being prepared")
    await envs.wait(project.id)
    record = await envs.store.get(project.id)
    assert record.state == "ready", record.log
    assert await envs.hold(project.id, start=True) == ""
    clone = primary.parent / f"{primary.name}-api"
    assert (clone / "README.md").read_text() == "hello\n"
    assert subprocess.run(["git", "branch", "--show-current"], cwd=clone, capture_output=True, text=True).stdout.strip() == "docs"
    assert (primary / "CLAUDE.md").read_text() == "the operator's own\n"
    assert (primary / "AGENTS.md").read_text() == "Run the tests."
    assert (clone / "AGENTS.md").read_text() == "The API's own."
    folders = (await manager.projects.get(project.id)).folders
    assert [f.path for f in folders] == [primary, clone] and folders[1].label == "api"

    # Prepared again by the operator: nothing is cloned over the folder, and what is there stays.
    (clone / "local.txt").write_text("mine")
    (primary / "AGENTS.md").write_text("edited by the operator")
    await envs.prepare(project.id, by="operator")
    await envs.wait(project.id)
    again = await envs.store.get(project.id)
    assert again.state == "ready" and again.attempt == 2
    assert "nothing was cloned over it" in again.log
    assert (clone / "local.txt").read_text() == "mine" and (primary / "AGENTS.md").read_text() == "edited by the operator"
    assert len((await manager.projects.get(project.id)).folders) == 2


async def test_a_failed_preparation_holds_launches_with_its_reason(envs: ProjectEnvironments, tmp_path: Path) -> None:
    project = await envs.app.manager.projects.create("Shop")
    await envs.save(project.id, {"repos": [{"url": str(tmp_path / "missing.git"), "folder": "api"}]})
    await envs.hold(project.id, start=True)
    await envs.wait(project.id)
    held = await envs.hold(project.id, start=True)
    assert held.startswith("environment not ready: preparation failed: git clone")
    assert "Prepare" in held
    record = await envs.store.get(project.id)
    assert record.state == "failed" and record.attempt == 1, "a failure is not retried by the next launch"
    journal = [entry.text for entry in await envs.app.manager.projects.journal(project.id, kind="environment")]
    assert any("could not be prepared" in text for text in journal)


async def test_a_host_outside_the_egress_allowlist_is_refused_before_git_runs(envs: ProjectEnvironments, monkeypatch: pytest.MonkeyPatch) -> None:
    envs.app.manager.config.policy.egress_allow = ["registry.example.org"]
    ran: list[Any] = []

    async def refuse_to_run(*args: Any, **kwargs: Any) -> Any:
        ran.append(args)
        raise AssertionError("git must not run for a host the policy asks about")

    monkeypatch.setattr(StaffWorktrees, "run_in", refuse_to_run)
    project = await envs.app.manager.projects.create("Shop")
    await envs.save(project.id, {"repos": [{"url": "https://github.com/someone/api.git"}]})
    await envs.prepare(project.id, by="operator")
    await envs.wait(project.id)
    record = await envs.store.get(project.id)
    assert record.state == "failed" and "github.com" in record.reason and "egress allowlist" in record.reason
    assert ran == []


async def test_the_goal_verdict_decides_the_outcome(envs: ProjectEnvironments, monkeypatch: pytest.MonkeyPatch) -> None:
    project = await envs.app.manager.projects.create("Shop")

    async def goal_fails(self: ProjectEnvironments, project: Any, record: Any, log: Any) -> None:
        raise PrepareFailed("the setup goal could not be met: npm is not installed")

    monkeypatch.setattr(ProjectEnvironments, "_goal", goal_fails)
    await envs.save(project.id, {"goal": "install dependencies and make the tests runnable"})
    await envs.prepare(project.id, by="operator")
    await envs.wait(project.id)
    assert (await envs.store.get(project.id)).reason == "the setup goal could not be met: npm is not installed"
    assert _verdict("Installed.\n\n**ENVIRONMENT READY**") == ""
    assert _verdict("Tried.\nENVIRONMENT FAILED: no network to the registry") == "no network to the registry"
    assert _verdict("I installed everything.") is None


def test_a_seed_is_never_written_through_a_link(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "docs").symlink_to(outside)
    with pytest.raises(PrepareFailed):
        _write_absent(root, root / "docs" / "AGENTS.md", "x")
    (root / "AGENTS.md").symlink_to(outside / "target")
    assert not _write_absent(root, root / "AGENTS.md", "x")
    assert not (outside / "target").exists()


# -- what sessions and launches see -------------------------------------------------------------------


def _skills(manager: SessionManager, tmp_path: Path, *names: str) -> None:
    """Skills in a store of the test's own: the configured skills folder is the repository's."""
    manager.skills = DirectorySkillStore(tmp_path / "skills")
    for name in names:
        (manager.skills.root / name).mkdir(parents=True, exist_ok=True)
        (manager.skills.root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: r\n---\nRead the diff.")


async def test_a_session_sees_the_subsets_from_its_next_run(envs: ProjectEnvironments, tmp_path: Path) -> None:
    manager = envs.app.manager
    _skills(manager, tmp_path, "review", "deploy")
    manager.mcp._configs = {"github": SimpleNamespace(description=""), "notion": SimpleNamespace(description="")}  # type: ignore[assignment]
    project = await manager.projects.create("Shop")
    state = await manager.create_session("chat", project_id=project.id)
    await envs.save(project.id, {"mcp": ["github"], "skills": ["review"]})
    assert manager.mcp_visible(state) == ["github", "notion"], "nothing changes until the next run"
    state.environment_scope = await manager._environment_scope(state)
    assert manager.mcp_visible(state) == ["github"]
    with pytest.raises(KeyError, match="not part of this project's environment"):
        await manager.set_mcp(state.session.id, "notion", True)
    skills = manager.skills_for(state)
    assert [e.name for e in await skills.list("t")] == ["review"]
    with pytest.raises(Exception, match="deploy"):
        await skills.load("t", "deploy")
    assert (await skills.load("t", "review")).manifest.name == "review"


async def test_an_unscoped_store_is_the_store(tmp_path: Path) -> None:
    store = DirectorySkillStore(tmp_path / "skills")
    (store.root / "a").mkdir()
    (store.root / "a" / "SKILL.md").write_text("---\nname: a\ndescription: x\n---\n")
    assert [e.name for e in await ScopedSkillStore(store, lambda: None).list("t")] == ["a"]
    assert await ScopedSkillStore(store, lambda: frozenset({"b"})).list("t") == []


async def test_a_command_line_launch_gets_the_project_skills(envs: ProjectEnvironments, tmp_path: Path) -> None:
    _skills(envs.app.manager, tmp_path, "review")
    project = await envs.app.manager.projects.create("Shop")
    await envs.save(project.id, {"skills": ["review"]})
    extras = await envs.launch_extras(SimpleNamespace(project=project), "l1", 1000)  # type: ignore[arg-type]
    assert extras.tool_sets == () and extras.skills == (HandedSkill("review", (("SKILL.md", b"---\nname: review\ndescription: r\n---\nRead the diff."),)),)
    spec = LaunchSpec(harness="claude", env="container", cwd="/w", launch_id="l1", first_prompt=None, skills=extras.skills)
    plan = ClaudeCodeAdapter().launch_plan(spec)
    assert plan.files[".claude/skills/review/SKILL.md"].endswith(b"Read the diff.")


async def test_a_command_line_launch_gets_the_whole_skill_directory(envs: ProjectEnvironments, tmp_path: Path) -> None:
    """A skill that says "run scripts/check.py" went to a CLI without the script: only SKILL.md was
    handed over. Its files go with it now, without links, without junk, and under its directory's name."""
    manager = envs.app.manager
    _skills(manager, tmp_path)
    root = manager.skills.root / "Daedalus Pages"
    (root / "scripts").mkdir(parents=True)
    (root / "references" / "deep").mkdir(parents=True)
    (root / "__pycache__").mkdir()
    (root / "SKILL.md").write_text("---\nname: Daedalus pages\ndescription: build a page\n---\nRun scripts/check.py, then read references/api.md.\n")
    (root / "scripts" / "check.py").write_text("print('ok')\n")
    (root / "references" / "api.md").write_text("the api\n")
    (root / "references" / "deep" / "more.md").write_text("more\n")
    (root / "__pycache__" / "check.cpython-312.pyc").write_bytes(b"junk")
    (root / ".DS_Store").write_bytes(b"junk")
    secret = tmp_path / "id_ed25519"
    secret.write_text("PRIVATE KEY")
    (root / "references" / "key.md").symlink_to(secret)
    (root / "linked").symlink_to(tmp_path)
    project = await manager.projects.create("Shop")
    await envs.save(project.id, {"skills": ["Daedalus Pages"]})
    extras = await envs.launch_extras(SimpleNamespace(project=project), "l1", 1000)  # type: ignore[arg-type]
    [skill] = extras.skills
    assert skill.name == "daedalus-pages"
    assert [path for path, _ in skill.files] == ["SKILL.md", "scripts/check.py", "references/api.md", "references/deep/more.md"]
    for harness, adapter, prefix in (
        ("claude", ClaudeCodeAdapter(), ".claude/skills"),
        ("opencode", OpenCodeAdapter(), "skills"),
        ("codex", CodexAdapter(), "skills"),
        ("pi", PiAdapter(), "skills"),
    ):
        plan = adapter.launch_plan(LaunchSpec(harness=harness, env="container", cwd="/w", launch_id="l1", first_prompt=None, skills=extras.skills))
        assert plan.files[f"{prefix}/daedalus-pages/scripts/check.py"] == b"print('ok')\n", harness
        handed = plan.files[f"{prefix}/daedalus-pages/SKILL.md"].decode()
        # OpenCode drops a skill whose name is not its directory's.
        assert handed.startswith("---\nname: daedalus-pages\n") and "Daedalus pages" not in handed, harness
        assert not any(b"PRIVATE KEY" in data for data in plan.files.values()), harness
        assert not any("pycache" in name or "DS_Store" in name or "linked" in name for name in plan.files), harness


async def test_a_skill_too_big_for_one_launch_says_what_it_left_behind(envs: ProjectEnvironments, tmp_path: Path) -> None:
    manager = envs.app.manager
    _skills(manager, tmp_path)
    for name in ("big", "small"):
        (manager.skills.root / name / "references").mkdir(parents=True)
        (manager.skills.root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nRead references/f29.md first.\n")
    for index in range(40):
        (manager.skills.root / "big" / "references" / f"f{index:02d}.md").write_text(f"part {index}\n")
    project = await manager.projects.create("Shop")
    await envs.save(project.id, {"skills": ["big", "small"]})
    extras = await envs.launch_extras(SimpleNamespace(project=project), "l1", 1000)  # type: ignore[arg-type]
    big, small = extras.skills
    assert sum(len(skill.files) for skill in extras.skills) <= SKILL_LAUNCH_FILES
    assert [path for path, _ in small.files] == ["SKILL.md"], "every skill keeps room for its own SKILL.md"
    names = [path for path, _ in big.files]
    assert names[:2] == ["SKILL.md", "references/f29.md"], "a file the text names goes before the rest"
    note = dict(big.files)["SKILL.md"].decode()
    assert "Not handed over with this launch" in note and "references/f39.md" in note


# -- the routes -----------------------------------------------------------------------------------------


async def test_the_routes_save_refuse_and_prepare(envs: ProjectEnvironments, settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = envs.app.manager
    project = await manager.projects.create("Shop")
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions=envs.app.extensions, guard=None)
    api = build_app(app, "tok")  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        path = f"/api/projects/{project.id}/environment"
        empty = (await client.get(path, headers=HEADERS)).json()
        assert empty["configured"] is False and empty["hold"] == "" and "mcp" in empty["choices"]
        refused = await client.put(path, headers=HEADERS, json={"spec": {"mcp": ["nowhere"]}})
        assert refused.status_code == 422 and "nowhere" in refused.json()["detail"]
        nothing = await client.post(f"{path}/prepare", headers=HEADERS)
        assert nothing.status_code == 409
        saved = await client.put(path, headers=HEADERS, json={"spec": {"seeds": [{"path": "AGENTS.md", "content": "hi"}]}})
        assert saved.status_code == 200 and saved.json()["state"] == "unprepared"
        started = await client.post(f"{path}/prepare", headers=HEADERS)
        assert started.status_code == 200 and started.json()["state"] in ("preparing", "ready")
        await envs.wait(project.id)
        done = (await client.get(path, headers=HEADERS)).json()
        assert done["state"] == "ready" and not done["stale"] and "wrote" in done["log"]
        changed = await client.put(path, headers=HEADERS, json={"spec": {"seeds": [{"path": "AGENTS.md", "content": "bye"}]}})
        assert changed.json()["stale"] is True and changed.json()["hold"] == "", "a stale environment holds nobody back"
        assert json.loads(json.dumps(changed.json()["spec"]))["seeds"][0]["content"] == "bye"
    assert environment_module.TOOL_SET == "project_mcp"


async def test_a_launch_waits_for_the_environment_and_starts_once_it_is_ready(db: Database) -> None:
    app, dispatcher, team, starts, revision = await queued_fixture(db)
    asked: list[tuple[str, bool]] = []
    reason = ["environment not ready: it is being prepared (since 2026-10-15 08:00 UTC); members start when it is ready"]

    async def hold(project_id: str, *, start: bool) -> str:
        asked.append((project_id, start))
        return reason[0]

    app.extensions["project_environment"] = SimpleNamespace(hold=hold)
    try:
        result = await queue_launch(app, "task", OPERATOR, staff_id="worker", client_operation_id="launch", expected_entity_revision=revision)
        assert await dispatcher.step()
        waiting = await dispatcher.store.view(result["effect_id"])
        assert waiting["state"] == "pending" and waiting["wait_reason"] == "environment"
        assert waiting["wait_detail"].startswith("environment not ready")
        assert starts == [] and asked == [("project", True)]
        reason[0] = ""
        dispatcher.notify()
        assert await dispatcher.step()
        await team.queue.pump()
        assert len(starts) == 1
    finally:
        team.queue.close()
        app.executions.release()
