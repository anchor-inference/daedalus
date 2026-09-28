"""The proposal path without the GitHub CLI: same answers, one API, no new dependency.

`gh` is driven in `selfdev.gh` for six operations — list, create, view, edit, merge, close — and the
proposals endpoint in `daedalus.extensions.api` asks for a seventh, the diff. Each test below names
the operation, the request it must produce and the shape it must answer in, because the point of the
fallback is that the code *above* it cannot tell which one ran.

The last test reads the calls out of the tree instead: a hand-kept list of the shapes this module
answers is a sentence about what existed when it was written, and the diff was missing from it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest

from daedalus.extensions.selfdev import SelfDevelopment
from daedalus.host.forge import RestForge, parse_gh_args, remote_slug
from daedalus.host.gitrun import GitError

SLUG = ("anchor-inference", "daedalus")
API = "https://api.github.test"
TOKEN = "test-token-not-a-credential"
ROOT = Path(__file__).resolve().parents[2]


def forge(handler) -> RestForge:
    return RestForge(TOKEN, api=API, transport=httpx.MockTransport(handler))


def seen(request: httpx.Request) -> tuple[str, str, dict]:
    body = json.loads(request.content) if request.content else {}
    return request.method, request.url.path, {"query": dict(request.url.params), "json": body}


def test_remote_slug_reads_the_three_shapes_git_writes() -> None:
    assert remote_slug("https://github.com/anchor-inference/daedalus.git\n") == SLUG
    assert remote_slug("git@github.com:anchor-inference/daedalus") == SLUG
    assert remote_slug("https://github.com/anchor-inference/daedalus/") == SLUG


def test_remote_slug_refuses_a_remote_it_cannot_name() -> None:
    with pytest.raises(GitError):
        remote_slug("https://gitlab.example.invalid/team/repo.git")


def test_the_cli_arguments_the_proposal_path_uses_are_understood() -> None:
    sub, opts, positional = parse_gh_args(("pr", "list", "--head", "b", "--json", "number,url"))
    assert (sub, opts["head"], opts["json"], positional) == ("list", "b", "number,url", [])
    sub, opts, positional = parse_gh_args(("pr", "edit", "12", "--title", "t", "--body", "b"))
    assert (sub, positional, opts["title"]) == ("edit", ["12"], "t")


def test_an_option_the_fallback_does_not_implement_is_refused_not_ignored() -> None:
    with pytest.raises(GitError, match="unsupported option --draft"):
        parse_gh_args(("pr", "create", "--head", "b", "--draft"))


@pytest.mark.asyncio
async def test_list_answers_the_json_the_caller_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method, path, extra = seen(request)
        assert (method, path) == ("GET", "/repos/anchor-inference/daedalus/pulls")
        assert extra["query"]["head"] == "anchor-inference:agent/x"
        return httpx.Response(200, json=[{"number": 7, "html_url": "https://github.com/o/r/pull/7", "state": "open"}])

    out = await forge(handler).run(("pr", "list", "--head", "agent/x", "--json", "number,url"), SLUG)
    assert json.loads(out) == [{"number": 7, "url": "https://github.com/o/r/pull/7"}]


@pytest.mark.asyncio
async def test_list_keeps_only_the_fields_asked_for() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"number": 7, "html_url": "u", "state": "open"}])

    out = await forge(handler).run(("pr", "list", "--head", "b", "--json", "number,state"), SLUG)
    assert json.loads(out) == [{"number": 7, "state": "open"}]


@pytest.mark.asyncio
async def test_a_field_the_fallback_cannot_answer_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"number": 7, "html_url": "u"}])

    with pytest.raises(GitError, match="not a field this REST path answers"):
        await forge(handler).run(("pr", "list", "--head", "b", "--json", "number,mergedAt"), SLUG)


@pytest.mark.asyncio
async def test_create_posts_the_branch_and_the_public_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method, path, extra = seen(request)
        assert (method, path) == ("POST", "/repos/anchor-inference/daedalus/pulls")
        assert extra["json"] == {"title": "T", "body": "B", "head": "agent/x", "base": "main"}
        return httpx.Response(201, json={"number": 8, "html_url": "https://github.com/o/r/pull/8"})

    out = await forge(handler).run(
        ("pr", "create", "--base", "main", "--head", "agent/x", "--title", "T", "--body", "B"), SLUG
    )
    assert out.strip() == "https://github.com/o/r/pull/8"


@pytest.mark.asyncio
async def test_view_answers_one_object_and_refuses_to_invent_one() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"number": 8, "html_url": "https://github.com/o/r/pull/8"}])

    out = await forge(handler).run(("pr", "view", "agent/x", "--json", "number,url"), SLUG)
    assert json.loads(out) == {"number": 8, "url": "https://github.com/o/r/pull/8"}

    empty = forge(lambda request: httpx.Response(200, json=[]))
    with pytest.raises(GitError, match="no pull request found"):
        await empty.run(("pr", "view", "agent/x", "--json", "number,url"), SLUG)


@pytest.mark.asyncio
async def test_edit_patches_only_what_was_given() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method, path, extra = seen(request)
        assert (method, path) == ("PATCH", "/repos/anchor-inference/daedalus/pulls/12")
        assert extra["json"] == {"title": "T2", "body": "B2"}
        return httpx.Response(200, json={"html_url": "u"})

    await forge(handler).run(("pr", "edit", "12", "--title", "T2", "--body", "B2"), SLUG)


@pytest.mark.asyncio
async def test_merge_and_close_do_what_the_approval_path_asks_for() -> None:
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(seen(request))
        return httpx.Response(200, json={"message": "Pull Request successfully merged"})

    await forge(handler).run(("pr", "merge", "8", "--squash"), SLUG)
    assert calls[-1][0:2] == ("PUT", "/repos/anchor-inference/daedalus/pulls/8/merge")
    assert calls[-1][2]["json"] == {"merge_method": "squash"}

    await forge(handler).run(("pr", "close", "8", "--comment", "Rejected."), SLUG)
    assert calls[-2][0:2] == ("PATCH", "/repos/anchor-inference/daedalus/pulls/8")
    assert calls[-2][2]["json"] == {"state": "closed"}
    assert calls[-1][0:2] == ("POST", "/repos/anchor-inference/daedalus/issues/8/comments")
    assert calls[-1][2]["json"] == {"body": "Rejected."}


@pytest.mark.asyncio
async def test_a_refused_call_names_the_permission_the_api_asked_for() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "Resource not accessible by personal access token"},
            headers={"x-accepted-github-permissions": "pull_requests=write"},
        )

    with pytest.raises(GitError) as err:
        await forge(handler).run(("pr", "create", "--head", "agent/x", "--title", "T", "--body", "B"), SLUG)
    message = str(err.value)
    assert "403" in message
    assert "pull_requests: write" in message
    assert "pull_requests=write" in message


@pytest.mark.asyncio
async def test_the_token_never_reaches_the_error_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    with pytest.raises(GitError) as err:
        await forge(handler).run(("pr", "view", "agent/x", "--json", "number,url"), SLUG)
    assert TOKEN not in str(err.value)


class _Recorder(SelfDevelopment):
    """`SelfDevelopment.gh` with the subprocess and the network taken out, keeping its routing."""

    def __init__(self, *, token: str) -> None:  # noqa: D107
        self.app = type("App", (), {"settings": type("S", (), {"github_token": token})()})()
        self.forge_calls: list[tuple[str, ...]] = []

    def _git_env(self) -> dict[str, str]:
        return {}

    async def forge(self, *args: str, cwd: Path) -> str:
        self.forge_calls.append(args)
        return "[]"


@pytest.mark.asyncio
async def test_gh_is_used_when_it_is_installed(monkeypatch) -> None:
    monkeypatch.setattr("daedalus.extensions.selfdev.shutil.which", lambda name: "/usr/bin/gh")
    calls: list[list[str]] = []

    async def fake_run_command(cmd, **kwargs):  # noqa: ANN001, ANN003
        calls.append(list(cmd))
        return "[]"

    monkeypatch.setattr("daedalus.extensions.selfdev.run_command", fake_run_command)
    dev = _Recorder(token="t")
    await SelfDevelopment.gh(dev, "pr", "list", "--head", "b", "--json", "number,url", cwd=Path("/tmp"))
    assert calls == [["gh", "pr", "list", "--head", "b", "--json", "number,url"]]
    assert dev.forge_calls == []


@pytest.mark.asyncio
async def test_the_fallback_is_used_when_it_is_not(monkeypatch) -> None:
    monkeypatch.setattr("daedalus.extensions.selfdev.shutil.which", lambda name: None)
    dev = _Recorder(token="t")
    await SelfDevelopment.gh(dev, "pr", "list", "--head", "b", "--json", "number,url", cwd=Path("/tmp"))
    assert dev.forge_calls == [("pr", "list", "--head", "b", "--json", "number,url")]


@pytest.mark.asyncio
async def test_without_a_token_the_fallback_says_so_rather_than_failing_obscurely() -> None:
    dev = _Recorder(token="")
    with pytest.raises(GitError, match="no GitHub token is configured"):
        await SelfDevelopment.forge(dev, "pr", "list", "--head", "b", "--json", "number,url", cwd=Path("/tmp"))


@pytest.mark.asyncio
async def test_view_of_a_number_asks_for_that_pull_request_by_number() -> None:
    """`gh pr view 8` and `gh pr view my-branch` are different questions and must not share an answer."""
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(seen(request))
        return httpx.Response(200, json={"number": 8, "html_url": "https://github.com/o/r/pull/8"})

    out = await forge(handler).run(("pr", "view", "8", "--json", "number,url"), SLUG)
    assert json.loads(out) == {"number": 8, "url": "https://github.com/o/r/pull/8"}
    assert calls == [("GET", "/repos/anchor-inference/daedalus/pulls/8", {"query": {}, "json": {}})]


@pytest.mark.asyncio
async def test_view_of_a_branch_searches_by_head_and_still_refuses_to_invent_one() -> None:
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(seen(request))
        return httpx.Response(200, json=[{"number": 8, "html_url": "u"}])

    await forge(handler).run(("pr", "view", "agent/x", "--json", "number,url"), SLUG)
    assert calls[0][1] == "/repos/anchor-inference/daedalus/pulls"
    assert calls[0][2]["query"]["head"] == "anchor-inference:agent/x"


def test_the_short_flags_gh_accepts_reach_the_option_table() -> None:
    sub, opts, positional = parse_gh_args(("pr", "create", "-t", "T", "-b", "B", "-B", "main", "-H", "agent/x"))
    assert (sub, positional) == ("create", [])
    assert opts == {"title": "T", "body": "B", "base": "main", "head": "agent/x"}


def test_a_flag_carrying_its_value_after_an_equals_is_read() -> None:
    sub, opts, positional = parse_gh_args(("pr", "create", "--title=a=b", "--body=B"))
    assert (sub, positional) == ("create", [])
    assert opts == {"title": "a=b", "body": "B"}


def test_a_short_flag_it_does_not_know_is_refused_rather_than_read_as_a_positional() -> None:
    with pytest.raises(GitError, match="unsupported option -F"):
        parse_gh_args(("pr", "create", "-F", "body.md"))


@pytest.mark.asyncio
async def test_a_head_the_caller_already_qualified_is_not_qualified_twice() -> None:
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(seen(request))
        return httpx.Response(200, json=[])

    await forge(handler).run(("pr", "list", "--head", "someone-else:fix-thing", "--json", "number,url"), SLUG)
    assert calls[0][2]["query"]["head"] == "someone-else:fix-thing"


@pytest.mark.asyncio
async def test_diff_prints_the_diff_the_proposals_endpoint_shows() -> None:
    calls: list[tuple[str, str, dict]] = []
    accepts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(seen(request))
        accepts.append(request.headers.get("accept", ""))
        return httpx.Response(200, text="diff --git a/x b/x\n--- a/x\n+++ b/x\n")

    out = await forge(handler).run(("pr", "diff", "8"), SLUG)
    assert out.startswith("diff --git a/x b/x")
    assert calls[0][0:2] == ("GET", "/repos/anchor-inference/daedalus/pulls/8")
    assert "v3.diff" in accepts[0]


@pytest.mark.asyncio
async def test_diff_without_a_number_is_refused() -> None:
    with pytest.raises(GitError, match="diff needs the pull-request number"):
        await forge(lambda request: httpx.Response(200, text="")).run(("pr", "diff"), SLUG)


def the_gh_calls_the_tree_makes() -> list[tuple[str, tuple[str, ...]]]:
    """Every ``gh(...)`` call in this repository's own sources, as the shape of its arguments.

    A computed argument — a branch name, a number, a reason — cannot be named by a reading of the
    source, so it stands in as ``<value>``: what is read is the subcommand and the flags, which is
    what decides whether this module can answer the call at all.
    """
    shapes: list[tuple[str, tuple[str, ...]]] = []
    for path in sorted((ROOT / "daedalus").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
            if name != "gh":
                continue
            literals = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if not literals or literals[0] != "pr":
                continue  # a call whose subcommand is itself computed names no shape
            shape = tuple(
                a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else "<value>" for a in node.args
            )
            shapes.append((str(path.relative_to(ROOT)), shape))
    return shapes


def the_api(request: httpx.Request) -> httpx.Response:
    """One answer per route, so a call that reaches the API at all comes back with something."""
    if "v3.diff" in request.headers.get("accept", ""):
        return httpx.Response(200, text="diff --git a/x b/x\n")
    if request.method == "GET" and request.url.path.endswith("/pulls"):
        return httpx.Response(200, json=[{"number": 8, "html_url": "u", "state": "open", "head": {"ref": "b"}}])
    if request.method == "GET":
        return httpx.Response(200, json={"number": 8, "html_url": "u", "state": "open", "head": {"ref": "b"}})
    return httpx.Response(200, json={"html_url": "u", "message": "merged", "state": "closed"})


@pytest.mark.asyncio
async def test_every_call_the_tree_makes_is_a_route_this_module_answers() -> None:
    """The calls are read out of the tree, not kept in a list here.

    `pr diff` was missing from this module while `daedalus/extensions/api.py` asked for it, and a list
    of the shapes the module answers would have said it was complete.
    """
    shapes = the_gh_calls_the_tree_makes()
    assert shapes, "no gh() call was found -- this reading is not looking at the tree"
    subs = set()
    for where, args in shapes:
        out = await forge(the_api).run(args, SLUG)
        assert isinstance(out, str) and out, f"{where}: gh {' '.join(args)} was answered with nothing"
        subs.add(parse_gh_args(args)[0])
    assert {"list", "create", "view", "edit", "merge", "close", "diff"} <= subs, f"the tree drives {sorted(subs)}"
