"""Importing a repository's issues as cards: the listing preview, the ticked apply, the repository
guess from the folder's git config, the HTTP routes and the coordinator's tool operation, all against
an offline GitHub transport."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from daedalus.config import Settings
from daedalus.extensions.api_issue_sync import register
from daedalus.extensions.coordinator_authority import approve_authority
from daedalus.extensions.issue_sync import GitHubIssues, IssueSync, guess_repository
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from tests.unit.test_orchestrator import git, rig


def issue(number: int, title: str, body: str = "Do it", **extra: Any) -> dict[str, Any]:
    return {"number": number, "title": title, "body": body, "state": "open",
            "updated_at": "2026-01-01T00:00:00Z", "html_url": f"https://github.com/owner/repo/issues/{number}",
            "labels": [{"name": "bug"}], **extra}


class FakeGitHub:
    """The two read endpoints issue import uses, served from one dict the test edits."""

    def __init__(self, repository: str = "owner/repo") -> None:
        self.repository = repository
        self.issues: dict[int, dict[str, Any]] = {}
        self.queries: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "issue import never writes to GitHub"
        base = f"/repos/{self.repository}/issues"
        if request.url.path == base:
            self.queries.append(dict(request.url.params))
            return httpx.Response(200, json=list(self.issues.values()))
        if request.url.path.startswith(base + "/"):
            found = self.issues.get(int(request.url.path.rsplit("/", 1)[1]))
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        return httpx.Response(404)

    def client(self) -> GitHubIssues:
        return GitHubIssues("test-token", transport=httpx.MockTransport(self.handler))


@pytest.fixture
async def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Research','2026-01-01')")
    await db.execute("INSERT INTO planning_budgets(project_id,max_depth,max_tasks,max_tokens) VALUES ('project',8,50,100000)")
    github = FakeGitHub()
    try:
        yield db, IssueSync(db, github.client()), github
    finally:
        await db.close()


def selection(listing: dict[str, Any], *numbers: int) -> list[dict[str, Any]]:
    by = {entry["number"]: entry for entry in listing["issues"]}
    return [{"issue_number": n, "action": by[n]["action"], "preview_digest": by[n]["preview_digest"],
             "expected_entity_revision": by[n]["expected_entity_revision"]} for n in numbers]


async def test_listing_previews_issues_and_the_ticked_ones_become_linked_cards(store) -> None:  # type: ignore[no-untyped-def]
    db, sync, github = store
    github.issues = {1: issue(1, "First"), 2: issue(2, "Second"), 3: issue(3, "Pull", pull_request={}),
                     4: issue(4, "Huge", body="x" * 2001)}
    listing = await sync.list_issues("project", "owner/repo", "bug")
    assert github.queries[-1]["labels"] == "bug" and github.queries[-1]["state"] == "open"
    assert {e["number"]: e["action"] for e in listing["issues"]} == {1: "import", 2: "import", 4: "too_large"}
    assert listing["issues"][0]["url"] == "https://github.com/owner/repo/issues/1"
    operator = Principal("operator:1", "operator")
    revision = listing["collection_revision"]
    applied = await sync.apply_selection(operator, "project", "owner/repo", selection(listing, 1, 2),
                                         expected_collection_revision=revision, client_operation_id="batch")
    assert [a["issue_number"] for a in applied["applied"]] == [1, 2] and applied["skipped"] == []
    assert applied["collection_revision"] == revision + 2
    # A retried batch replays its receipts instead of making twins.
    again = await sync.apply_selection(operator, "project", "owner/repo", selection(listing, 1, 2),
                                       expected_collection_revision=revision, client_operation_id="batch")
    assert again["applied"] == applied["applied"]
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM board_tasks"))["n"] == 2
    links = await db.fetchall("SELECT remote_id FROM issue_links ORDER BY remote_id")
    assert [row["remote_id"] for row in links] == ["owner/repo#1", "owner/repo#2"]
    relisted = await sync.list_issues("project", "owner/repo")
    first = next(e for e in relisted["issues"] if e["number"] == 1)
    assert first["action"] == "linked" and first["task_id"] == applied["applied"][0]["task_id"]


async def test_an_issue_changed_on_github_updates_its_card_and_a_stale_board_refuses(store) -> None:  # type: ignore[no-untyped-def]
    db, sync, github = store
    github.issues = {1: issue(1, "First"), 2: issue(2, "Second")}
    operator = Principal("operator:1", "operator")
    listing = await sync.list_issues("project", "owner/repo")
    await sync.apply_selection(operator, "project", "owner/repo", selection(listing, 1),
                               expected_collection_revision=listing["collection_revision"], client_operation_id="one")
    github.issues[1] = issue(1, "First, reworded", body="Do it better", updated_at="2026-01-02T00:00:00Z")
    relisted = await sync.list_issues("project", "owner/repo")
    assert {e["number"]: e["action"] for e in relisted["issues"]} == {1: "update", 2: "import"}
    # The listing that preceded the first import is stale: its revision refuses the import.
    stale = await sync.apply_selection(operator, "project", "owner/repo", selection(listing, 2),
                                       expected_collection_revision=listing["collection_revision"], client_operation_id="two")
    assert stale["applied"] == [] and stale["skipped"][0]["issue_number"] == 2
    result = await sync.apply_selection(operator, "project", "owner/repo", selection(relisted, 1),
                                        expected_collection_revision=relisted["collection_revision"], client_operation_id="three")
    assert result["applied"][0]["action"] == "update"
    task = await db.fetchone("SELECT title,acceptance FROM board_tasks WHERE id = ?", (result["applied"][0]["task_id"],))
    assert (task["title"], task["acceptance"]) == ("First, reworded", "Do it better")


def test_the_repository_is_guessed_from_the_folder_git_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text('[core]\n\tbare = false\n[remote "upstream"]\n\turl = https://github.com/up/stream.git\n'
                                          '[remote "origin"]\n\turl = git@github.com:owner/repo.git\n', encoding="utf-8")
    assert guess_repository(repo) == "owner/repo"
    # A worktree's .git is a file pointing at its git directory, whose commondir holds the config.
    worktree = tmp_path / "wt"
    gitdir = repo / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert guess_repository(worktree) == "owner/repo"
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    (other / ".git" / "config").write_text('[remote "origin"]\n\turl = https://gitlab.example/a/b.git\n', encoding="utf-8")
    assert guess_repository(other) is None
    assert guess_repository(tmp_path / "missing") is None


async def test_http_routes_guess_list_and_import(store, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    db, sync, github = store
    github.issues = {5: issue(5, "From the API")}
    repo = tmp_path / "folder"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/owner/repo\n', encoding="utf-8")
    folder = SimpleNamespace(path=repo, reachable=True, local=lambda env: env == "host")

    async def get(project_id: str) -> Any:
        return SimpleNamespace(id=project_id, folders=(folder,)) if project_id == "project" else None

    app = SimpleNamespace(db=db, extensions={"issue_sync": sync, "effects": SimpleNamespace(notify=lambda: None)},
                          settings=SimpleNamespace(github_token="test-token"),
                          manager=SimpleNamespace(projects=SimpleNamespace(get=get, local_env="host")))
    api = FastAPI()
    register(api, app, lambda: {"via": "cookie", "user_id": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        source = await client.get("/api/issues/sync/source", params={"project_id": "project"})
        assert source.json() == {"project_id": "project", "repository": "owner/repo", "configured": True}
        assert (await client.get("/api/issues/sync/source", params={"project_id": "nope"})).status_code == 404
        listed = await client.post("/api/issues/sync/list", json={"project_id": "project", "repository": "owner/repo"})
        assert listed.status_code == 200, listed.text
        assert (await client.post("/api/issues/sync/list", json={"project_id": "project", "repository": "bad repo"})).status_code == 409
        body = {"project_id": "project", "repository": "owner/repo", "items": selection(listed.json(), 5),
                "expected_collection_revision": listed.json()["collection_revision"], "client_operation_id": "sheet"}
        imported = await client.post("/api/issues/sync/import", json=body)
        assert imported.status_code == 200, imported.text
        assert imported.json()["applied"][0]["issue_number"] == 5
        assert (await client.post("/api/issues/sync/import", json={**body, "items": []})).status_code == 422


async def test_coordinator_previews_and_imports_under_its_planning_grant(settings: Settings, db: Database, tmp_path: Path) -> None:
    run = await rig(settings, db, tmp_path)
    try:
        git(run.repo, "remote", "add", "origin", "https://github.com/owner/repo.git")
        github = FakeGitHub()
        github.issues = {7: issue(7, "Investigate failure"), 8: issue(8, "Too long", body="x" * 2001)}
        run.orch.app.extensions["issue_sync"] = IssueSync(db, github.client())
        session_id = (await run.orch.enable(run.project.id, autonomy="ask")).settings.orchestrator.session_id
        preview = await run.call(session_id, "tasks", op="issues")
        assert "owner/repo: 2 open issue(s)" in preview and "#7 Investigate failure (would become a new card)" in preview
        scope = Scope("project", run.project.id)
        revision = await ControlStore(db).revision(scope, Entity("collection", scope.id))
        with pytest.raises(Refused, match="grant"):
            await run.call(session_id, "tasks", op="import_issues", issues=[7], client_operation_id="call-1",
                           expected_collection_revision=revision)
        issuer = Principal.operator({"via": "token", "user_id": 1})
        await approve_authority(run.orch.app, run.project.id, issuer, client_operation_id="approve-planning",
                                expected_entity_revision=await ControlStore(db).revision(scope, Entity("project", scope.id)),
                                expected_coordinator_session_id=session_id, bundle_id="planning",
                                expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
        said = await run.call(session_id, "tasks", op="import_issues", issues=[7, 8], client_operation_id="call-2",
                              expected_collection_revision=revision)
        assert "#7 imported as card" in said and "not applied: #8" in said
        board = await run.board.project_board(run.project.id)
        card = next(task for task in board["tasks"] if task["title"] == "Investigate failure")
        assert card["issue"] == {"repository": "owner/repo", "number": 7, "state": "linked",
                                 "url": "https://github.com/owner/repo/issues/7"}
    finally:
        await run.manager.close()
