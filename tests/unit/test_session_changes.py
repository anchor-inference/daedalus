"""A chat's Changes tab: the files changed in the folder it works in, one file's diff, a revert and a
commit, read and done by real git in a temporary repository.

git runs through the same ``run(argv, max_output)`` the routes hand the terminal daemon's
``exec.run``, here as a local process, so what is checked is what git really does with the argv
this host builds. Every repository is made in the test's own temporary directory; nothing here
reads or writes a repository of the machine it runs on.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from daedalus.extensions import api_session_changes
from daedalus.host.session_runner import SessionManager
from daedalus.terminals.changes import ChangesBlocked, ChangesRefused, commit, file_diff, listing, revert

IDENTITY = ("-c", "user.name=someone", "-c", "user.email=someone@example.invalid")


def _clean_env() -> dict[str, str]:
    """This process's environment without any git identity or configuration of the machine: the
    repository's own configuration is all the git under test may see."""
    kept = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "EMAIL"))}
    return {**kept, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *IDENTITY, "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_clean_env()).stdout


@dataclass
class Ran:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool


SEEN: list[list[str]] = []


async def local_run(argv: list[str], max_output: int) -> Ran:
    """What the daemon's exec.run answers, run here: the exit code, the output cut at ``max_output``."""
    SEEN.append(argv)
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL, env=_clean_env())
    out, err = await process.communicate()
    return Ran(process.returncode or 0, out[:max_output].decode("utf-8", "replace"), err.decode("utf-8", "replace"), len(out) > max_output)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    (root / "order").mkdir(parents=True)
    git(root, "init", "-q", "-b", "order-form")
    git(root, "config", "user.name", "someone")
    git(root, "config", "user.email", "someone@example.invalid")
    (root / "order" / "form.js").write_text("one\ntwo\nthree\n")
    (root / "README.md").write_text("shop\n")
    (root / "old.txt").write_text("bye\n")
    (root / "logo.png").write_bytes(b"\x89PNG\x00\x01\x02")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "start")
    return root


def paths(answer: dict[str, Any]) -> list[tuple[str, str]]:
    return [(f["path"], f["status"]) for f in answer["files"]]


# -- the list -------------------------------------------------------------------------------


async def test_a_clean_tree_lists_nothing(repo: Path) -> None:
    answer = await listing(local_run, str(repo))
    assert answer["repository"] is True and answer["files"] == [] and answer["truncated"] is False
    assert answer["branch"] == "order-form" and len(answer["head"]) == 40 and answer["root"] == str(repo)
    assert answer["upstream"] is None and answer["ahead"] is None and answer["behind"] is None, "no upstream is not an error"


async def test_modified_added_deleted_untracked_renamed_and_binary_files(repo: Path) -> None:
    (repo / "order" / "form.js").write_text("one\n2\nthree\nfour\n")
    (repo / "staged.txt").write_text("a\nb\n")
    git(repo, "add", "staged.txt")
    (repo / "old.txt").unlink()
    (repo / "order" / "sheet.js").write_text("x\n" * 7)
    git(repo, "mv", "README.md", "READ.md")
    (repo / "logo.png").write_bytes(b"\x89PNG\x00\x09\x09\x09")
    (repo / "icon.bin").write_bytes(b"\x00\x01\x02")
    answer = await listing(local_run, str(repo), count_new=50)
    rows = {f["path"]: f for f in answer["files"]}
    assert paths(answer) == [
        ("READ.md", "A"), ("README.md", "D"), ("icon.bin", "?"), ("logo.png", "M"), ("old.txt", "D"),
        ("order/form.js", "M"), ("order/sheet.js", "?"), ("staged.txt", "A"),
    ], "a rename is the old file deleted and the new one added: two rows, each revertible"
    assert (rows["order/form.js"]["added"], rows["order/form.js"]["removed"]) == (2, 1)
    assert (rows["order/sheet.js"]["added"], rows["order/sheet.js"]["removed"]) == (7, 0), "a new file's lines are counted too"
    assert rows["logo.png"]["binary"] is True and rows["logo.png"]["added"] is None
    assert rows["icon.bin"]["binary"] is True
    assert rows["old.txt"]["removed"] == 1 and rows["order/form.js"]["binary"] is False
    assert answer["added"] == 2 + 7 + 2 + 1 and answer["removed"] == 1 + 1 + 1


async def test_a_folder_outside_any_repository_is_no_repository_not_an_error(tmp_path: Path) -> None:
    answer = await listing(local_run, str(tmp_path))
    assert answer["repository"] is False and answer["files"] == [] and answer["reason"] == "not_git" and answer["detail"]


async def test_a_chat_in_a_sub_folder_sees_and_touches_only_that_folder(repo: Path) -> None:
    (repo / "order" / "form.js").write_text("changed\n")
    (repo / "README.md").write_text("changed too\n")
    (repo / "order" / "new.js").write_text("n\n")
    answer = await listing(local_run, str(repo / "order"))
    assert answer["prefix"] == "order/" and paths(answer) == [("order/form.js", "M"), ("order/new.js", "?")]
    with pytest.raises(ChangesRefused):
        await file_diff(local_run, str(repo / "order"), "README.md", scoped=True)
    with pytest.raises(ChangesRefused):
        await revert(local_run, str(repo / "order"), ["README.md"])
    await revert(local_run, str(repo / "order"), None)
    assert (repo / "README.md").read_text() == "changed too\n", "a revert of everything is everything in the chat's folder"
    assert not (repo / "order" / "new.js").exists() and (repo / "order" / "form.js").read_text() == "one\ntwo\nthree\n"


async def test_the_branch_is_counted_against_its_upstream(repo: Path, tmp_path: Path) -> None:
    upstream = tmp_path / "upstream.git"
    git(tmp_path, "init", "-q", "--bare", str(upstream))
    git(repo, "remote", "add", "origin", str(upstream))
    git(repo, "push", "-q", "-u", "origin", "order-form")
    (repo / "README.md").write_text("one more\n")
    git(repo, "commit", "-qam", "ahead")
    answer = await listing(local_run, str(repo))
    assert (answer["upstream"], answer["ahead"], answer["behind"]) == ("origin/order-form", 1, 0)


async def test_a_nested_repository_is_listed_but_never_acted_on(repo: Path) -> None:
    nested = repo / ".agents" / "worktrees" / "member"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    (nested / "work.txt").write_text("the member's\n")
    (repo / "old.txt").write_text("changed\n")
    answer = await listing(local_run, str(repo))
    assert (".agents/worktrees/member/", "?") in paths(answer)
    with pytest.raises(ChangesRefused):
        await revert(local_run, str(repo), [".agents/worktrees/member/"])
    done = await revert(local_run, str(repo), None)
    assert done["reverted"] == ["old.txt"] and (nested / "work.txt").exists()


# -- one file's diff -------------------------------------------------------------------------


async def test_one_files_diff_tracked_untracked_and_binary(repo: Path) -> None:
    (repo / "order" / "form.js").write_text("one\n2\nthree\n")
    (repo / "notes.md").write_text("first\nsecond\n")
    (repo / "logo.png").write_bytes(b"\x89PNG\x00\x09")
    tracked = await file_diff(local_run, str(repo), "order/form.js", scoped=True)
    assert tracked["untracked"] is False and tracked["binary"] is False and "-two" in tracked["diff"] and "+2" in tracked["diff"]
    fresh = await file_diff(local_run, str(repo), "notes.md", scoped=True)
    assert fresh["untracked"] is True and "+first" in fresh["diff"]
    picture = await file_diff(local_run, str(repo), "logo.png", scoped=True)
    assert picture["binary"] is True and "Binary files" in picture["diff"]


async def test_a_file_behind_a_symbolic_link_is_never_read(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("the operator's key\n")
    (repo / "elsewhere").symlink_to(outside)
    answer = await file_diff(local_run, str(repo), "elsewhere/secret", scoped=True)
    assert answer["diff"] == "" and "key" not in str(answer)
    with pytest.raises(ChangesRefused):
        await revert(local_run, str(repo), ["elsewhere/secret"])
    with pytest.raises(ChangesRefused):
        await commit(local_run, str(repo), "take it", ["elsewhere/secret"])
    assert (outside / "secret").read_text() == "the operator's key\n"


@pytest.mark.parametrize("path", ["", "/etc/passwd", "../outside", "order/../../x", "a\0b"])
async def test_a_path_that_leaves_the_repository_is_refused(repo: Path, path: str) -> None:
    with pytest.raises(ChangesRefused):
        await file_diff(local_run, str(repo), path, scoped=True)
    with pytest.raises(ChangesRefused):
        await revert(local_run, str(repo), [path])


async def test_a_pattern_is_a_name_never_a_pattern(repo: Path) -> None:
    """``*`` matched every file before ``--literal-pathspecs``; now it names a file called ``*`` only."""
    (repo / "order" / "form.js").write_text("keep this\n")
    (repo / "old.txt").write_text("and this\n")
    for pattern in ("*", ":/", "order/*", ":(glob)**"):
        with pytest.raises(ChangesRefused):
            await revert(local_run, str(repo), [pattern])
    assert (repo / "order" / "form.js").read_text() == "keep this\n" and (repo / "old.txt").read_text() == "and this\n"
    assert all("--literal-pathspecs" in argv for argv in SEEN if argv[0] == "git")


# -- revert ---------------------------------------------------------------------------------


async def test_reverting_one_file_leaves_the_others(repo: Path) -> None:
    (repo / "order" / "form.js").write_text("changed\n")
    (repo / "README.md").write_text("changed too\n")
    done = await revert(local_run, str(repo), ["order/form.js"])
    assert done["reverted"] == ["order/form.js"] and done["restored"] == ["order/form.js"] and done["deleted"] == []
    assert (repo / "order" / "form.js").read_text() == "one\ntwo\nthree\n" and (repo / "README.md").read_text() == "changed too\n"


async def test_reverting_everything_says_which_files_are_gone_for_good(repo: Path) -> None:
    (repo / ".gitignore").write_text("*.log\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore logs")
    (repo / "order" / "form.js").write_text("changed\n")
    (repo / "old.txt").unlink()
    (repo / "order" / "sheet.js").write_text("new\n")
    (repo / "staged.txt").write_text("staged\n")
    git(repo, "add", "staged.txt")
    (repo / "build.log").write_text("ignored, so not the chat's to delete\n")
    done = await revert(local_run, str(repo), None)
    assert sorted(done["restored"]) == ["old.txt", "order/form.js"]
    assert done["deleted"] == ["order/sheet.js", "staged.txt"], "a new file, staged or not, is deleted, and the answer says so"
    assert git(repo, "status", "--porcelain", "--ignored") == "!! build.log\n"
    assert (repo / "old.txt").read_text() == "bye\n"


async def test_a_revert_names_only_changed_files(repo: Path) -> None:
    with pytest.raises(ChangesBlocked):
        await revert(local_run, str(repo), None)
    with pytest.raises(ChangesRefused):
        await revert(local_run, str(repo), ["README.md"])
    (repo / "README.md").write_text("x\n")
    with pytest.raises(ChangesRefused):
        await revert(local_run, str(repo), [" README.md"])


async def test_a_repository_with_no_commit_has_nothing_to_revert_to(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    git(root, "init", "-q")
    (root / "a.txt").write_text("a\n")
    with pytest.raises(ChangesBlocked):
        await revert(local_run, str(root), None)
    assert (root / "a.txt").exists()


# -- commit ---------------------------------------------------------------------------------


async def test_committing_a_subset_leaves_the_rest_as_it_was(repo: Path) -> None:
    (repo / "order" / "form.js").write_text("changed\n")
    (repo / "order" / "sheet.js").write_text("new\n")
    (repo / "old.txt").unlink()
    (repo / "README.md").write_text("staged by hand\n")
    git(repo, "add", "README.md")
    done = await commit(local_run, str(repo), "Send orders to the sheet\n\nWith the new form.", ["order/form.js", "order/sheet.js", "old.txt"])
    assert done["commit"] == git(repo, "rev-parse", "HEAD").strip() and done["branch"] == "order-form"
    assert git(repo, "show", "--name-status", "--format=%an|%s", "HEAD").split() == ["someone|Send", "orders", "to", "the", "sheet", "D", "old.txt", "M", "order/form.js", "A", "order/sheet.js"]
    assert git(repo, "status", "--porcelain") == "M  README.md\n", "what was staged and not named stays staged and uncommitted"


async def test_committing_everything_with_no_hook_and_no_signature(repo: Path) -> None:
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-commit").write_text("#!/bin/sh\ntouch \"$GIT_DIR/../hooked\"\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", str(repo / "no-such-signer"))
    (repo / "order" / "form.js").write_text("changed\n")
    (repo / "fresh.txt").write_text("new\n")
    done = await commit(local_run, str(repo), "Everything", None)
    assert sorted(done["files"]) == ["fresh.txt", "order/form.js"] and git(repo, "status", "--porcelain") == ""
    assert not (repo / "hooked").exists(), "the repository's hook did not run"


async def test_a_commit_needs_an_identity_the_configuration_names(tmp_path: Path) -> None:
    root = tmp_path / "anonymous"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=_clean_env())
    (root / "a.txt").write_text("a\n")
    with pytest.raises(ChangesBlocked, match="identity"):
        await commit(local_run, str(root), "first", None)


async def test_a_commit_in_a_repository_with_no_commit_yet_is_its_first(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "someone")
    git(root, "config", "user.email", "someone@example.invalid")
    (root / "a.txt").write_text("a\n")
    done = await commit(local_run, str(root), "first", None)
    assert git(root, "log", "--format=%H").strip() == done["commit"]


async def test_a_commit_message_must_say_something(repo: Path) -> None:
    (repo / "README.md").write_text("x\n")
    with pytest.raises(ChangesRefused):
        await commit(local_run, str(repo), "   \n", None)


# -- the routes -----------------------------------------------------------------------------


@dataclass
class State:
    session: Any
    project: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    pending: Any = None
    task: Any = None
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    submit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.settled.set()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


class Manager:
    """The part of the session manager the routes ask, with the real refusal of a busy session."""

    def __init__(self, folders: dict[str, Path]) -> None:
        self.states = {sid: State(SimpleNamespace(id=sid, title=f"chat {sid}"), SimpleNamespace(id="p1")) for sid in folders}
        self.folders = folders
        self.projects = SimpleNamespace(local_env="container")
        self.config = SimpleNamespace(ops=SimpleNamespace(settle_wait_seconds=1.0))
        self.devices: set[str] = set()
        self.published: list[tuple[str, dict[str, Any], str | None]] = []
        self.bus = SimpleNamespace(publish=self._publish)

    async def _publish(self, event_type: str, payload: dict[str, Any], **ids: Any) -> None:
        self.published.append((event_type, payload, ids.get("session_id")))

    async def get_state(self, session_id: str) -> State | None:
        return self.states.get(session_id)

    def live_state(self, session_id: str) -> State | None:
        return self.states.get(session_id)

    def env_of(self, session_id: str, metadata: dict[str, Any], project: Any) -> str:
        return f"device:{session_id}" if session_id in self.devices else "container"

    def work_dir(self, state: State) -> Path:
        return self.folders[state.session.id]

    def busy_sessions(self) -> set[str]:
        return {sid for sid, s in self.states.items() if s.running or s.pending is not None}

    _settling = staticmethod(SessionManager._settling)
    _wait_until_quiet = SessionManager._wait_until_quiet
    held_quiet = SessionManager.held_quiet


class Terminals:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def configured(self, env: str) -> bool:
        return env == "container"

    async def exec_run(self, env: str, argv: list[str], *, env_vars: dict[str, str], timeout: float, max_output: int, actor: str, audit: bool) -> Ran:
        assert env_vars["GIT_TERMINAL_PROMPT"] == "0" and timeout > 0
        self.calls.append((argv[argv.index("-C") + 2] if "-C" in argv else "", audit))
        return await local_run(argv, max_output)


def mount(manager: Manager, terminals: Terminals) -> FastAPI:
    api = FastAPI()
    app = SimpleNamespace(manager=manager, extensions={"terminals": terminals})

    async def auth() -> dict[str, Any]:
        return {"user_id": 1}

    api_session_changes.register(api, app, auth)  # type: ignore[arg-type]
    return api


async def ask(api: FastAPI, method: str, path: str, body: Any = None) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        return await client.request(method, path, json=body)


@pytest.fixture
def served(repo: Path, tmp_path: Path) -> Any:
    plain = tmp_path / "plain"
    plain.mkdir()
    manager = Manager({"s1": repo, "s2": repo, "s3": plain})
    terminals = Terminals()
    return SimpleNamespace(api=mount(manager, terminals), manager=manager, terminals=terminals, repo=repo)


async def test_the_routes_list_diff_revert_and_commit(served: Any) -> None:
    repo = served.repo
    (repo / "order" / "form.js").write_text("one\n2\nthree\n")
    (repo / "order" / "sheet.js").write_text("a\nb\n")
    listed = (await ask(served.api, "GET", "/api/sessions/s1/changes")).json()
    assert listed["repository"] is True and listed["env"] == "container" and listed["directory"] == str(repo) and listed["busy"] == ""
    assert paths(listed) == [("order/form.js", "M"), ("order/sheet.js", "?")]
    diff = (await ask(served.api, "GET", "/api/sessions/s1/changes/diff?path=order/form.js")).json()
    assert "+2" in diff["diff"]
    assert {audit for _, audit in served.terminals.calls} == {False}, "reads are not audited"

    unconfirmed = await ask(served.api, "POST", "/api/sessions/s1/changes/revert", {"all": True})
    assert unconfirmed.status_code == 400 and (repo / "order" / "sheet.js").exists()
    both = await ask(served.api, "POST", "/api/sessions/s1/changes/revert", {"confirm": True, "all": True, "paths": ["order/form.js"]})
    assert both.status_code == 400
    reverted = (await ask(served.api, "POST", "/api/sessions/s1/changes/revert", {"confirm": True, "paths": ["order/sheet.js"]})).json()
    assert reverted["deleted"] == ["order/sheet.js"] and not (repo / "order" / "sheet.js").exists()

    made = await ask(served.api, "POST", "/api/sessions/s1/commit", {"message": "Two for one"})
    assert made.status_code == 200 and made.json()["commit"] == git(repo, "rev-parse", "HEAD").strip()
    assert True in {audit for _, audit in served.terminals.calls}, "a write is audited"
    assert [(t, p["change"], p["files"], s) for t, p, s in served.manager.published] == [("session.changes", "reverted", 1, "s1"), ("session.changes", "committed", 1, "s1")]
    assert served.manager.published[1][1]["commit"] == made.json()["commit"]


async def test_nothing_changes_while_a_run_is_in_progress(served: Any) -> None:
    repo = served.repo
    (repo / "order" / "form.js").write_text("the agent is writing this\n")
    forever = asyncio.get_running_loop().create_future()
    served.manager.states["s1"].task = forever
    try:
        assert (await ask(served.api, "GET", "/api/sessions/s1/changes")).json()["busy"]
        for path, body in (("/api/sessions/s1/changes/revert", {"confirm": True, "all": True}), ("/api/sessions/s1/commit", {"message": "now"})):
            refused = await ask(served.api, "POST", path, body)
            assert refused.status_code == 409 and "busy" in refused.json()["detail"]
        # Another chat working in the same folder is refused too: the run is in its folder as well.
        beside = await ask(served.api, "POST", "/api/sessions/s2/changes/revert", {"confirm": True, "all": True})
        assert beside.status_code == 409 and "chat s1" in beside.json()["detail"]
    finally:
        forever.cancel()
    assert (repo / "order" / "form.js").read_text() == "the agent is writing this\n"
    served.manager.states["s1"].pending = object()
    waiting = await ask(served.api, "POST", "/api/sessions/s1/commit", {"message": "now"})
    assert waiting.status_code == 409, "a question outstanding is a run that goes on after the answer"
    assert served.manager.published == []


async def test_a_folder_without_a_repository_and_a_folder_out_of_reach(served: Any) -> None:
    plain = (await ask(served.api, "GET", "/api/sessions/s3/changes")).json()
    assert plain["repository"] is False and plain["reason"] == "not_git"
    assert (await ask(served.api, "POST", "/api/sessions/s3/commit", {"message": "x"})).status_code == 409
    served.manager.devices.add("s1")
    remote = (await ask(served.api, "GET", "/api/sessions/s1/changes")).json()
    assert remote["repository"] is False and remote["reason"] == "out_of_reach" and "daedalus-cli" in remote["detail"]
    assert (await ask(served.api, "POST", "/api/sessions/s1/changes/revert", {"confirm": True, "all": True})).status_code == 409
    assert (await ask(served.api, "GET", "/api/sessions/nobody/changes")).status_code == 404


async def test_the_routes_refuse_a_path_out_of_the_folder(served: Any) -> None:
    (served.repo / "README.md").write_text("x\n")
    for path in ("../etc/passwd", "/etc/passwd"):
        assert (await ask(served.api, "GET", f"/api/sessions/s1/changes/diff?path={path}")).status_code == 400
        assert (await ask(served.api, "POST", "/api/sessions/s1/changes/revert", {"confirm": True, "paths": [path]})).status_code == 400
        assert (await ask(served.api, "POST", "/api/sessions/s1/commit", {"message": "x", "paths": [path]})).status_code == 400
    assert (served.repo / "README.md").read_text() == "x\n"


def test_overlapping_folders() -> None:
    from daedalus.extensions.api_session_changes import overlaps

    assert overlaps("/work/shop", "/work/shop/") and overlaps("/work/shop", "/work/shop/order") and overlaps("/work/shop/order", "/work/shop")
    assert not overlaps("/work/shop", "/work/shopping") and not overlaps("/work/a", "/work/b")
