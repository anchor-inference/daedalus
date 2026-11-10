"""The fixed risk check: each rule fires on the calls that break it and stays quiet on their harmless
neighbours, calls that never ran do not count, and an allowed kind is left out."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from protocore.contracts.types import Message, MessageRole, ToolResultBlock, ToolUseBlock

from daedalus.host import turn_risk
from daedalus.host.turn_risk import Call, Scope

GIT_DATE = "2026-01-01T00:00:00+00:00"
"""Every commit a test makes is older than the goal, whatever second the test runs in."""


def turn(*calls: tuple[str, dict[str, Any]] | tuple[str, dict[str, Any], str, bool] | tuple[str, dict[str, Any], str, bool, dict[str, Any]]) -> list[Message]:
    """A run's messages: each call as the model's tool use and the tool's result after it."""
    messages: list[Message] = []
    for index, call in enumerate(calls):
        name, arguments = call[0], call[1]
        content, failed = (call[2], call[3]) if len(call) > 2 else ("ok", False)  # type: ignore[misc]
        metadata = call[4] if len(call) > 4 else {}  # type: ignore[misc]
        call_id = f"c{index}"
        messages.append(Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id=call_id, name=name, arguments_json=json.dumps(arguments))]))
        messages.append(Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id=call_id, content=content, is_error=failed, metadata=metadata)]))
    return messages


def shell(command: str, **extra: Any) -> tuple[str, dict[str, Any]]:
    return "Exec", {"command": command, **extra}


@pytest.fixture
def work(tmp_path: Path) -> Path:
    folder = tmp_path / "work"
    folder.mkdir()
    return folder


def scope_of(work: Path, **extra: Any) -> Scope:
    return Scope(work_dir=str(work), started=datetime.now(UTC), home=str(work.parent / "home"), **extra)


async def kinds(work: Path, *calls: Any, allowed: tuple[str, ...] = (), **extra: Any) -> list[str]:
    found = await turn_risk.check(turn_risk.turn_calls(turn(*calls)), scope_of(work, **extra), allowed=allowed)
    return [f.kind for f in found]


def git(folder: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_DATE": GIT_DATE, "GIT_COMMITTER_DATE": GIT_DATE, "GIT_CONFIG_GLOBAL": os.devnull}
    done = subprocess.run(["git", "-c", "user.name=someone", "-c", "user.email=someone@localhost", *args], cwd=folder, env=env,
                          capture_output=True, text=True, check=True)
    return done.stdout


def repo(folder: Path, files: dict[str, str], *, branch: str = "main") -> None:
    git(folder, "init", "-q", "-b", branch)
    for name, text in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "start")


# -- the shell rules ------------------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "docker compose -f deploy/compose.yaml --env-file .env up -d",
    "cd deploy && docker compose restart daedalus",
    "docker-compose down",
    "sudo docker restart deploy-daedalus-1",
    "docker rm -f old",
    "/usr/bin/docker stop web",
    "docker container prune -f",
    "docker volume rm data",
    "docker volume prune",
    "docker system prune -a",
    "podman kill web",
    "bash -c 'docker compose rm -f'",
])
async def test_container_mutations_stop(work: Path, command: str) -> None:
    assert await kinds(work, shell(command)) == ["containers"]


@pytest.mark.parametrize("command", [
    "docker ps", "docker logs deploy-daedalus-1", "docker compose -f up.yaml ps", "docker compose logs -f daedalus",
    "docker build -t x .", "docker images", "echo docker restart",
])
async def test_reading_containers_does_not_stop(work: Path, command: str) -> None:
    assert await kinds(work, shell(command)) == []


async def test_service_restarts_stop_and_status_does_not(work: Path) -> None:
    for command in ("systemctl --user restart worker", "sudo systemctl stop nginx", "service nginx restart"):
        assert await kinds(work, shell(command)) == ["services"], command
    for command in ("systemctl status nginx", "systemctl --user list-units", "service nginx status"):
        assert await kinds(work, shell(command)) == [], command


async def test_forced_pushes_stop_and_an_ordinary_push_does_not(work: Path) -> None:
    for command in ("git push --force origin feature", "git push -f", "git push origin +main", "git push --force-with-lease origin x", "git -C repo push -fu origin x"):
        assert await kinds(work, shell(command)) == ["git_history"], command
    assert await kinds(work, shell("git push origin feature"), shell("git push -u origin x")) == []


async def test_a_hard_reset_stops_on_a_shared_branch_only(work: Path) -> None:
    repo(work, {"a.txt": "a"})
    assert await kinds(work, shell("git reset --hard HEAD~1")) == ["git_history"]
    git(work, "checkout", "-q", "-b", "feature")
    assert await kinds(work, shell("git reset --hard origin/feature"), shell("git reset --soft HEAD~1")) == []
    # A folder git cannot read is not taken for an unshared one.
    assert await kinds(work, shell("git reset --hard", cwd=str(work.parent / "gone"))) == ["git_history"]


async def test_a_recursive_delete_outside_the_folder_stops(work: Path) -> None:
    for command in ("rm -rf /var/tmp/elsewhere", "rm -rf ../neighbour", "rm -r ~/projects", 'rm -rf "$TARGET"', "find /srv/data -name '*.log' -delete",
                    "cd .. && rm -rf work2"):
        assert await kinds(work, shell(command)) == ["delete_outside"], command
    for command in ("rm -rf build node_modules", "rm -rf ./dist/*", "rm ../one-file.txt", 'D=build; rm -rf "$D"'):
        assert await kinds(work, shell(command)) == [], command


async def test_dropping_data_stops_and_talking_about_it_does_not(work: Path) -> None:
    for command in ('sqlite3 state.db "DROP TABLE sessions"', "psql -c 'truncate table runs'", "dropdb prod", "redis-cli FLUSHALL",
                    "mysql -e 'drop database shop'", "python3 -c \"import sqlite3; sqlite3.connect('x').execute('DROP TABLE t')\""):
        assert await kinds(work, shell(command)) == ["database"], command
    for command in ('grep -rn "DROP TABLE" .', "truncate -s 0 app.log", 'sqlite3 state.db "select count(*) from sessions"', 'git commit -m "drop table cleanup"'):
        assert await kinds(work, shell(command)) == [], command
    # A statement handed to a database tool counts as much as one typed into a shell; a search does not.
    assert await kinds(work, ("postgres_query", {"query": "DROP TABLE orders"})) == ["database"]
    assert await kinds(work, ("mcp_run", {"sql": "truncate users"})) == ["database"]
    assert await kinds(work, ("WebSearch", {"query": "DROP TABLE syntax"})) == []


async def test_a_write_outside_the_folder_stops(work: Path) -> None:
    assert await kinds(work, ("Write", {"path": "/var/tmp/notes.md", "content": "x"})) == ["write_outside"]
    assert await kinds(work, ("Edit", {"path": "../sibling/a.py", "old_string": "a", "new_string": "b"})) == ["write_outside"]
    assert await kinds(work, ("Write", {"path": "notes.md", "content": "x"}), ("Edit", {"path": str(work / "a.py"), "edits": []})) == []


# -- migrations ------------------------------------------------------------------------------------


async def test_changing_a_migration_file_that_existed_stops_and_a_new_one_does_not(work: Path) -> None:
    repo(work, {"alembic/versions/0001_start.py": "up = 1\n", "app/migrations/0001_initial.py": "x = 1\n"})
    edit_old = ("Edit", {"path": "alembic/versions/0001_start.py", "old_string": "up = 1", "new_string": "up = 2"})
    assert await kinds(work, edit_old) == ["migration"]
    assert await kinds(work, ("Write", {"path": "app/migrations/0001_initial.py", "content": "x = 2\n"})) == ["migration"]
    # A migration the goal itself created, written and then corrected, is the goal's own work.
    (work / "alembic/versions/0002_more.py").write_text("up = 3\n")
    assert await kinds(work, ("Write", {"path": "alembic/versions/0002_more.py", "content": "up = 3\n"}),
                       ("Edit", {"path": "alembic/versions/0002_more.py", "old_string": "3", "new_string": "4"})) == []
    # A path nobody configured is an ordinary file.
    assert await kinds(work, ("Edit", {"path": "app/models.py", "old_string": "a", "new_string": "b"})) == []


LIST_FILE = '''\
"""The database."""

MIGRATIONS: list[str] = [
    """CREATE TABLE a (id INTEGER);""",
]

MIGRATIONS.append("""
CREATE TABLE b (id INTEGER);
""")

CACHE_PAGES = 1
'''


async def test_a_migration_list_may_grow_but_not_change(work: Path) -> None:
    repo(work, {"daedalus/stores/database.py": LIST_FILE})
    target = work / "daedalus/stores/database.py"
    edit = ("Edit", {"path": "daedalus/stores/database.py", "old_string": "x", "new_string": "y"})
    # Appended after the last entry: the region the goal began with is still its first lines.
    target.write_text(LIST_FILE.replace("CACHE_PAGES = 1", 'MIGRATIONS.append("""\nCREATE TABLE c (id INTEGER);\n""")\n\nCACHE_PAGES = 2'))
    assert await kinds(work, edit) == []
    # A column added to a table an applied migration created.
    target.write_text(LIST_FILE.replace("CREATE TABLE b (id INTEGER);", "CREATE TABLE b (id INTEGER, name TEXT);"))
    assert await kinds(work, edit) == ["migration"]
    # A line slipped into the middle of an entry, nothing removed.
    target.write_text(LIST_FILE.replace("CREATE TABLE b (id INTEGER);", "CREATE TABLE b (id INTEGER);\nCREATE INDEX b_id ON b(id);"))
    assert await kinds(work, edit) == ["migration"]
    # Code after the list is not a migration.
    target.write_text(LIST_FILE.replace("CACHE_PAGES = 1", "CACHE_PAGES = 9"))
    assert await kinds(work, edit) == []


async def test_without_git_the_edits_themselves_are_read(work: Path) -> None:
    (work / "daedalus/stores").mkdir(parents=True)
    (work / "daedalus/stores/database.py").write_text(LIST_FILE)
    appended = ("Edit", {"path": "daedalus/stores/database.py", "old_string": '""")\n\nCACHE_PAGES', "new_string": '""")\n\nMIGRATIONS.append("x")\n\nCACHE_PAGES'})
    changed = ("Edit", {"path": "daedalus/stores/database.py", "old_string": "b (id INTEGER)", "new_string": "b (id INTEGER, name TEXT)"})
    assert await kinds(work, appended) == []
    assert await kinds(work, changed) == ["migration"]
    assert await kinds(work, ("Edit", {"path": "migrations/0001.sql", "old_string": "a", "new_string": "b"})) == ["migration"]
    assert await kinds(work, ("Write", {"path": "migrations/0002.sql", "content": "create table c (id int);"})) == []


async def test_the_migration_patterns_are_configured(work: Path) -> None:
    from daedalus.config import GoalRiskConfig, MigrationListConfig

    custom = GoalRiskConfig(migration_paths=["schema/changes/*.sql"], migration_lists=[MigrationListConfig(path="db.py", start="^STEPS")])
    edit = ("Edit", {"path": "schema/changes/001.sql", "old_string": "a", "new_string": "b"})
    assert await kinds(work, edit, config=custom) == ["migration"]
    assert await kinds(work, ("Edit", {"path": "alembic/versions/1.py", "old_string": "a", "new_string": "b"}), config=custom) == []


# -- what counts ----------------------------------------------------------------------------------


async def test_a_call_that_never_ran_does_not_count_and_a_failed_one_does(work: Path) -> None:
    refused = shell("docker restart web") + ("refused by policy: no (rule x). Do it another way.", True, {"tool_dispatch.replay_error_kind": "permission"})
    asked = shell("docker restart web") + ("needs the operator's approval: x (rule y). Approval key: k.", True)
    failed = shell("docker compose down") + ("exit code 1", True, {"tool_dispatch.replay_error_kind": "execution"})
    assert await kinds(work, refused, asked) == []
    assert await kinds(work, failed) == ["containers"]


async def test_an_allowed_kind_is_left_out_and_the_rest_still_stop(work: Path) -> None:
    calls = (shell("docker compose up -d"), ("Write", {"path": "/var/tmp/x", "content": ""}))
    assert sorted(await kinds(work, *calls)) == ["containers", "write_outside"]
    assert await kinds(work, *calls, allowed=("containers",)) == ["write_outside"]


async def test_one_finding_per_command_and_the_digest_counts_the_turn(work: Path) -> None:
    calls = turn_risk.turn_calls(turn(
        shell("docker restart a && docker restart a"), shell("pytest -q", cwd="."), ("Write", {"path": "src/a.py", "content": ""}),
        ("Edit", {"path": "src/a.py", "old_string": "a", "new_string": "b"}), shell("false") + ("exit 1", True),
    ))
    found = await turn_risk.check(calls, scope_of(work))
    assert [(f.kind, f.what) for f in found] == [("containers", "docker restart a && docker restart a")]
    digest = turn_risk.digest(calls, scope_of(work))
    assert digest.as_dict() == {"files": ["src/a.py"], "commands": 3, "failed": 1}
    text = turn_risk.stop_text(found, digest, "en")
    assert "started, stopped or removed containers: `docker restart a && docker restart a`" in text
    assert "Files changed: 1 — src/a.py" in text and "Commands run: 3, failed: 1" in text


def test_a_call_without_a_result_counts_as_ran() -> None:
    message = Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="x", name="Exec", arguments_json='{"command": "docker stop a"}')])
    assert turn_risk.turn_calls([message]) == [Call("Exec", {"command": "docker stop a"})]
