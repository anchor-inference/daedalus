"""Opening a pull request when the GitHub CLI is not installed.

The proposal path in :mod:`daedalus.extensions.selfdev` drives ``gh`` for four things: list the pull
requests already open on a head branch, create one, read it back, and edit its title and body; the
approval path merges or closes one; and the proposals endpoint in
:mod:`daedalus.extensions.api` asks for a pull request's diff. Those are HTTP calls to one API with
the token that already authenticates the push, so ``gh`` is a convenience rather than a dependency —
and when it is absent, the proposal used to die *after* the branch had been pushed, leaving a branch
whose whole purpose was a pull request that does not exist.

This module is the same operations over the REST API. It answers in the shape ``gh`` answers in
(``--json number,url`` is a JSON array of objects with those keys; ``pr create`` prints the URL;
``pr diff`` prints the diff), so the caller does not branch on which one ran: the boundary is here,
and everything above it is unchanged.

Three properties matter more than the calls themselves:

* it is used **only** when ``gh`` cannot be found, so a working installation behaves exactly as before;
* anything it does not understand is refused with the command in the message, rather than guessed at —
  a wrong pull request is worse than a missing one;
* every call the tree itself makes is a shape this module answers, and a test reads those calls out
  of the tree rather than out of a list kept here, because a list is a sentence about what existed
  when it was written.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from daedalus.host.gitrun import GitError, mask_credentials

API = "https://api.github.com"

_REMOTE_RE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$")
"""``https://github.com/o/r.git``, ``git@github.com:o/r`` and the same with a trailing slash."""

_VALUE_FLAGS = {
    "--json": "json",
    "--base": "base",
    "--head": "head",
    "--title": "title",
    "--body": "body",
    "--comment": "comment",
}

_SHORT_FLAGS = {"-B": "--base", "-H": "--head", "-t": "--title", "-b": "--body"}
"""The short spellings ``gh`` accepts for the flags this module understands."""

_BOOL_FLAGS = {"--squash": "squash", "--merge": "merge", "--rebase": "rebase"}
"""Flags that carry no value: ``pr merge <n> --squash`` names the merge method by being present."""

_SUBCOMMANDS = {"list", "create", "view", "edit", "merge", "close", "diff"}

_DIFF_ACCEPT = "application/vnd.github.v3.diff"
"""What the API answers a diff in, and what ``gh pr diff`` prints."""


def remote_slug(url: str) -> tuple[str, str]:
    """The ``(owner, repo)`` a git remote URL names; refuses anything that is not GitHub."""
    match = _REMOTE_RE.search(url.strip())
    if not match:
        raise GitError(f"not a GitHub remote: {mask_credentials(url.strip())[:200]}")
    return match.group("owner"), match.group("repo")


def _option(item: str) -> str:
    """The flag ``item`` names, in its long spelling, whatever spelling was used."""
    return _SHORT_FLAGS.get(item, item)


def parse_gh_args(args: tuple[str, ...]) -> tuple[str, dict[str, Any], list[str]]:
    """Split a ``gh`` argument list into ``(subcommand, options, positionals)``.

    Only the shapes this module answers are understood: ``pr list --head x --json number,url`` and
    ``pr view <branch|number> --json number,url`` carry a positional, ``pr create`` and ``pr edit``
    carry flags, and ``merge``, ``close`` and ``diff`` carry the pull-request number.
    """
    if len(args) < 2 or args[0] != "pr":
        raise GitError(
            f"the GitHub CLI is not installed, and this command has no REST equivalent here: gh {' '.join(args)}"
        )
    sub, rest = args[1], list(args[2:])
    opts: dict[str, Any] = {}
    positional: list[str] = []
    while rest:
        item = _option(rest.pop(0))
        if item in _BOOL_FLAGS:
            opts[_BOOL_FLAGS[item]] = True
            continue
        if item in _VALUE_FLAGS:
            if not rest:
                raise GitError(f"gh {' '.join(args)}: {item} needs a value")
            opts[_VALUE_FLAGS[item]] = rest.pop(0)
            continue
        if item.startswith("--") and "=" in item:
            # `gh pr create --title=T`, which the CLI accepts; the value is everything after the first
            # `=`, so a title may itself carry one.
            name, _, value = item.partition("=")
            key = _VALUE_FLAGS.get(name)
            if key is None:
                raise GitError(f"gh {' '.join(args)}: unsupported option {name}")
            if not value:
                raise GitError(f"gh {' '.join(args)}: {name} needs a value")
            opts[key] = value
            continue
        if item.startswith("-"):
            raise GitError(f"gh {' '.join(args)}: unsupported option {item}")
        positional.append(item)
    if sub not in _SUBCOMMANDS:
        raise GitError(f"gh {' '.join(args)}: unsupported subcommand {sub!r}")
    return sub, opts, positional


_FIELD_OF = {"number": "number", "url": "html_url", "state": "state", "title": "title", "headRefName": "head.ref"}


def _fields(opts: dict[str, Any]) -> list[str]:
    return [name.strip() for name in str(opts.get("json", "number,url")).split(",") if name.strip()]


def _project(row: dict[str, Any], fields: list[str], asked: str) -> dict[str, Any]:
    """``gh --json`` semantics: the keys asked for, and nothing else.

    A field this module cannot answer is refused rather than dropped: a caller reading a key that is
    silently absent would take it for a pull request without that property.
    """
    out: dict[str, Any] = {}
    for name in fields:
        path = _FIELD_OF.get(name)
        if path is None:
            raise GitError(f"gh --json {asked}: {name} is not a field this REST path answers")
        value: Any = row
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        out[name] = value
    return out


def _is_a_number(text: str) -> bool:
    """Whether a positional names a pull request rather than a head branch.

    ``gh pr view 8`` and ``gh pr view my-branch`` are different questions, and answering the second
    with the first would report a pull request found under a branch that happens to be named ``8``.
    """
    return bool(text) and text.isdigit()


class RestForge:
    """The pull-request operations the host drives, over the REST API, with the token the push uses."""

    def __init__(self, token: str, *, api: str = API, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token = token
        self._api = api.rstrip("/")
        self._transport = transport

    def _headers(self, accept: str) -> dict[str, str]:
        return {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _send(self, method: str, path: str, accept: str, **kwargs: Any) -> httpx.Response:
        headers = self._headers(accept)
        async with httpx.AsyncClient(
            base_url=self._api, headers=headers, timeout=30, transport=self._transport
        ) as client:
            response = await client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise GitError(self._refusal(method, path, response))
        return response

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._send(method, path, "application/vnd.github+json", **kwargs)
        return response.json() if response.content else {}

    async def _call_text(self, method: str, path: str, accept: str, **kwargs: Any) -> str:
        response = await self._send(method, path, accept, **kwargs)
        return response.text

    def _refusal(self, method: str, path: str, response: httpx.Response) -> str:
        """A failure a reader can act on: the call, the status, what the API asked for."""
        body = response.text[:600]
        try:
            body = str(json.loads(response.text).get("message", body))
        except (json.JSONDecodeError, AttributeError):
            pass
        accepted = response.headers.get("x-accepted-github-permissions", "")
        hint = ""
        if response.status_code in (403, 404) and method != "GET":
            hint = (
                " The token needs `pull_requests: write` on this repository"
                + (f" (the API named: {accepted})" if accepted else "")
                + "."
            )
        return mask_credentials(f"{method} {path} -> {response.status_code}: {body}{hint}")

    async def pr_list(self, slug: tuple[str, str], head: str, fields: list[str], asked: str) -> list[dict[str, Any]]:
        owner, repo = slug
        # `--head` matches a branch name only within this repository's own forks; the qualified form is
        # what gh sends and what makes a head branch unambiguous. A head the caller has already
        # qualified (`owner:branch`, which is how a pull request from a fork is named) is left alone:
        # qualifying it twice asks about a branch literally called `owner:branch`.
        qualified = head if ":" in head else f"{owner}:{head}"
        params = {"head": qualified, "state": "all", "per_page": "10"}
        rows = await self._call("GET", f"/repos/{owner}/{repo}/pulls", params=params)
        return [_project(row, fields, asked) for row in rows]

    async def pr_number(self, slug: tuple[str, str], number: str, fields: list[str], asked: str) -> dict[str, Any]:
        """``pr view <number>``: that pull request, asked for by number rather than by head."""
        owner, repo = slug
        row = await self._call("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        return _project(row, fields, asked)

    async def pr_diff(self, slug: tuple[str, str], number: str) -> str:
        """``pr diff <number>``: the diff itself, which the proposals endpoint shows the operator."""
        owner, repo = slug
        return await self._call_text("GET", f"/repos/{owner}/{repo}/pulls/{number}", _DIFF_ACCEPT)

    async def pr_create(self, slug: tuple[str, str], opts: dict[str, Any]) -> str:
        owner, repo = slug
        payload = {
            "title": opts.get("title", ""),
            "body": opts.get("body", ""),
            "head": opts.get("head", ""),
            "base": opts.get("base", "main"),
        }
        data = await self._call("POST", f"/repos/{owner}/{repo}/pulls", json=payload)
        return str(data.get("html_url", ""))

    async def pr_edit(self, slug: tuple[str, str], number: str, opts: dict[str, Any]) -> str:
        owner, repo = slug
        payload = {k: v for k, v in (("title", opts.get("title")), ("body", opts.get("body"))) if v is not None}
        data = await self._call("PATCH", f"/repos/{owner}/{repo}/pulls/{number}", json=payload)
        return str(data.get("html_url", ""))

    async def pr_merge(self, slug: tuple[str, str], number: str, opts: dict[str, Any]) -> str:
        """``pr merge <n> --squash``: the same merge the CLI performs, with the same method."""
        owner, repo = slug
        method = next((m for m in ("squash", "merge", "rebase") if m in opts), "merge")
        data = await self._call("PUT", f"/repos/{owner}/{repo}/pulls/{number}/merge", json={"merge_method": method})
        return str(data.get("message", "merged"))

    async def pr_close(self, slug: tuple[str, str], number: str, opts: dict[str, Any]) -> str:
        """``pr close <n> --comment X``: the state change and the comment, in that order."""
        owner, repo = slug
        await self._call("PATCH", f"/repos/{owner}/{repo}/pulls/{number}", json={"state": "closed"})
        comment = opts.get("comment")
        if comment:
            await self._call("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": comment})
        return f"closed #{number}"

    async def run(self, args: tuple[str, ...], slug: tuple[str, str]) -> str:
        """Answer in the shape ``gh`` answers in, so the caller above does not branch."""
        sub, opts, positional = parse_gh_args(args)
        asked = str(opts.get("json", "number,url"))
        if sub in {"list", "view"}:
            head = str(opts.get("head") or (positional[0] if positional else ""))
            if sub == "view" and _is_a_number(head):
                return json.dumps(await self.pr_number(slug, head, _fields(opts), asked))
            rows = await self.pr_list(slug, head, _fields(opts), asked)
            if sub == "view" and not rows:
                raise GitError(f"no pull request found for head {head}")
            return json.dumps(rows if sub == "list" else rows[0])
        if sub == "create":
            return await self.pr_create(slug, opts)
        if not positional:
            raise GitError(f"gh {' '.join(args)}: {sub} needs the pull-request number")
        if sub == "diff":
            return await self.pr_diff(slug, positional[0])
        if sub == "merge":
            return await self.pr_merge(slug, positional[0], opts)
        if sub == "close":
            return await self.pr_close(slug, positional[0], opts)
        return await self.pr_edit(slug, positional[0], opts)
