"""Project-scoped GitHub issue sync with reviewed diffs and conservative remote effects."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

import aiosqlite
import httpx

from daedalus.extensions.board_commands import insert_task
from daedalus.extensions.effects import EffectOutcome, EffectResolution
from daedalus.extensions.orchestrator_domain import capture_contract_change
from daedalus.host.forge import remote_slug
from daedalus.host.gitrun import GitError
from daedalus.stores.control import (
    ControlConflict,
    ControlDenied,
    ControlStore,
    Entity,
    Mutation,
    Principal,
    Scope,
    canonical,
    digest,
    now,
    one,
)
from daedalus.stores.database import Database
from daedalus.stores.outbox import Claim, OutboxStore

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
ORIGIN = re.compile(r"\n?<!-- daedalus-issue-origin:([0-9a-f]{32}) -->\s*\Z")
LABEL = re.compile(r"[^,\x00-\x1f]{1,50}\Z")
LIST_LIMIT = 100
"""One page of open issues. A repository with more is narrowed with a label, rather than the host
walking pages of an API that answers slowly and counts every request against the token."""
TITLE_LIMIT, BODY_LIMIT = 200, 2000
"""A task's title and contract limits; an issue past them cannot become a card unedited."""
IMPORT_OPERATION, UPDATE_OPERATION = "board.task.create", "board.task.update"
"""An import makes a card and a field resolution rewrites one, so they are authorized as exactly
that. A coordinator's planning grant carries these two names; no grant was ever issued with a name of
the issue sync's own, so one would have refused every coordinator import."""


class IssueSyncRefused(ValueError):
    pass


class _NoPriorReceipt(Exception):
    pass


def _remote_id(repository: str, number: int) -> str:
    if (not REPOSITORY.fullmatch(repository) or any(part in (".", "..") for part in repository.split("/"))
            or type(number) is not int or number < 1):
        raise IssueSyncRefused("a GitHub repository and positive issue number are required")
    return f"{repository}#{number}"


def _snapshot(issue: dict[str, Any]) -> dict[str, Any]:
    if "pull_request" in issue:
        raise IssueSyncRefused("pull requests are outside issue sync")
    title, body, state, version = issue.get("title"), issue.get("body"), issue.get("state"), issue.get("updated_at")
    if not isinstance(title, str) or not isinstance(body, (str, type(None))) or state not in ("open", "closed") or not isinstance(version, str):
        raise IssueSyncRefused("the provider returned an incomplete issue snapshot")
    body = body or ""
    marker = ORIGIN.search(body)
    plain = ORIGIN.sub("", body).rstrip()
    return {"title": title, "body": plain, "state": state, "version": version,
            "origin_token": marker.group(1) if marker else None}


class GitHubIssues:
    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.token = token
        self.transport = transport

    async def request(self, method: str, repository: str, number: int, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        _remote_id(repository, number)
        if not self.token.strip():
            raise IssueSyncRefused("GitHub is not configured")
        async with httpx.AsyncClient(timeout=10, transport=self.transport) as client:
            try:
                response = await client.request(method, f"https://api.github.com/repos/{repository}/issues/{number}",
                                                headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}"},
                                                json=body)
            except httpx.HTTPError as exc:
                raise IssueSyncRefused("GitHub issue state is unavailable") from exc
        if response.status_code != 200:
            raise IssueSyncRefused(f"GitHub issue request returned {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise IssueSyncRefused("GitHub returned an invalid issue") from exc
        if not isinstance(payload, dict):
            raise IssueSyncRefused("GitHub returned an invalid issue")
        return _snapshot(payload)

    async def read(self, repository: str, number: int) -> dict[str, Any]:
        return await self.request("GET", repository, number)

    async def list(self, repository: str, label: str | None = None) -> list[dict[str, Any]]:
        """Open issues, newest first, each as the snapshot :meth:`read` returns plus what a list shows.

        The snapshot fields are the ones a single read answers with, so a digest computed from this
        listing is the one an apply recomputes from its own fresh read."""
        _remote_id(repository, 1)
        if label is not None and not LABEL.fullmatch(label):
            raise IssueSyncRefused("a label is one name of at most fifty characters, without commas")
        if not self.token.strip():
            raise IssueSyncRefused("GitHub is not configured")
        params: dict[str, Any] = {"state": "open", "per_page": LIST_LIMIT, "sort": "created", "direction": "desc"}
        if label:
            params["labels"] = label
        async with httpx.AsyncClient(timeout=10, transport=self.transport) as client:
            try:
                response = await client.get(f"https://api.github.com/repos/{repository}/issues", params=params,
                                            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}"})
            except httpx.HTTPError as exc:
                raise IssueSyncRefused("GitHub issues are unavailable") from exc
        if response.status_code == 404:
            raise IssueSyncRefused(f"GitHub has no repository {repository} this token can read")
        if response.status_code != 200:
            raise IssueSyncRefused(f"GitHub issue listing returned {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise IssueSyncRefused("GitHub returned an invalid issue list") from exc
        if not isinstance(payload, list):
            raise IssueSyncRefused("GitHub returned an invalid issue list")
        issues = []
        for item in payload:
            # The issues endpoint lists pull requests too; they are outside issue sync.
            if not isinstance(item, dict) or "pull_request" in item or type(item.get("number")) is not int:
                continue
            labels = [entry.get("name") for entry in item.get("labels") or [] if isinstance(entry, dict)]
            url = item.get("html_url")
            issues.append({"number": item["number"], "remote": _snapshot(item),
                           "labels": [name for name in labels if isinstance(name, str)],
                           "url": url if isinstance(url, str) else f"https://github.com/{repository}/issues/{item['number']}"})
        return issues


def guess_repository(folder: Path) -> str | None:
    """The ``owner/repo`` a folder's git remote names, read from its git config on disk.

    It reads the file rather than running git: no network, and no refusal over a folder owned by
    another user, which git answers with a safe-directory error instead of the remote. ``origin``
    is preferred; otherwise the first GitHub remote. A worktree's ``.git`` is a file naming its
    git directory, whose ``commondir`` holds the shared config."""
    try:
        git = folder / ".git"
        if git.is_file():
            pointer = git.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            git = (folder / pointer.partition(":")[2].strip()).resolve()
        common = git / "commondir"
        if common.is_file():
            git = (git / common.read_text(encoding="utf-8").strip()).resolve()
        text = (git / "config").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    remotes: dict[str, str] = {}
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        header = re.fullmatch(r'\[remote\s+"([^"]+)"\]', stripped)
        if header:
            section = header.group(1)
            continue
        if stripped.startswith("["):
            section = None
            continue
        key, _, value = stripped.partition("=")
        if section is not None and key.strip() == "url" and section not in remotes:
            remotes[section] = value.strip()
    for name in ("origin", *remotes):
        url = remotes.get(name)
        if url is None:
            continue
        try:
            owner, repo = remote_slug(url)
        except GitError:
            continue
        if REPOSITORY.fullmatch(f"{owner}/{repo}"):
            return f"{owner}/{repo}"
    return None


def repository_of(folders: Any, local_env: str) -> str | None:
    """The repository a project's own folders point at: the first local, reachable folder whose git
    remote is on GitHub, the primary first because the store keeps it first."""
    for folder in folders:
        if folder.local(local_env) and folder.reachable:
            found = guess_repository(folder.path)
            if found:
                return found
    return None


class IssueSync:
    def __init__(self, db: Database, github: GitHubIssues) -> None:
        self.db, self.github = db, github
        self.control = ControlStore(db)
        self._write_lock = asyncio.Lock()

    async def _replay(self, principal: Principal, scope: Scope, operation: str,
                      client_operation_id: str, expected_revision: int, entity: Entity,
                      payload: dict[str, Any], *, effects: tuple[str, ...] = ()) -> dict[str, Any] | None:
        async def missing(_: aiosqlite.Connection, __: Mutation) -> dict[str, Any]:
            raise _NoPriorReceipt

        try:
            return await self.control.mutate(principal, scope, operation, client_operation_id,
                                             expected_revision, entity, payload, missing, effects=effects)
        except _NoPriorReceipt:
            return None

    async def record_webhook(self, event: str, payload: Any) -> str:
        """Retain a signed issue observation; a delayed echo cannot change a newer link."""
        async with self.db.transaction() as conn:
            return await self.record_webhook_in(conn, event, payload)

    async def record_webhook_in(self, conn: aiosqlite.Connection, event: str, payload: Any) -> str:
        """Observe an issue in the transaction that accepted its exact signed delivery."""
        if event != "issues" or not isinstance(payload, dict):
            return "ignored"
        issue = payload.get("issue")
        repo = payload.get("repository")
        repository = repo.get("full_name") if isinstance(repo, dict) else None
        if not isinstance(issue, dict) or not isinstance(repository, str) or type(issue.get("number")) is not int:
            return "ignored"
        try:
            remote_id = _remote_id(repository, issue["number"])
            remote = _snapshot(issue)
        except IssueSyncRefused:
            return "invalid"
        link = await one(conn, "SELECT * FROM issue_links WHERE provider = 'github' AND remote_id = ?", (remote_id,))
        if link is None:
            return "unlinked"
        await self._observe(conn, link["project_id"], remote_id, remote, link["id"], link["local_revision"])
        if remote["version"] < link["remote_version"]:
            return "out_of_order"
        if remote["version"] == link["remote_version"] and digest(remote) == link["last_digest"]:
            return "echo" if remote["origin_token"] == link["origin_token"] else "unchanged"
        if remote["origin_token"]:
            own = await one(conn, "SELECT desired_json FROM issue_sync_outbound WHERE link_id = ?"
                            " AND origin_token = ? AND state IN ('queued','unknown','verified')",
                            (link["id"], remote["origin_token"]))
            if own is not None:
                desired = json.loads(own["desired_json"])
                if remote["title"] == desired["title"] and remote["body"] == desired["body"]:
                    return "echo_pending_readback"
        await conn.execute("UPDATE issue_links SET state = 'conflict' WHERE id = ? AND state = 'linked'", (link["id"],))
        return "conflict"

    async def _local(self, project_id: str, remote_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        link = await self.db.fetchone("SELECT * FROM issue_links WHERE provider = 'github' AND remote_id = ?", (remote_id,))
        if link is not None and link["project_id"] != project_id:
            raise IssueSyncRefused("the issue is linked to another project")
        task = await self.db.fetchone("SELECT * FROM board_tasks WHERE id = ?", (link["task_id"],)) if link else None
        return dict(link) if link else None, dict(task) if task else None

    async def preview(self, project_id: str, repository: str, number: int) -> dict[str, Any]:
        remote_id = _remote_id(repository, number)
        project = await self.db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
        if project is None:
            raise KeyError(project_id)
        remote = await self.github.read(repository, number)
        return await self._preview_of(project_id, remote_id, remote)

    async def _preview_of(self, project_id: str, remote_id: str, remote: dict[str, Any]) -> dict[str, Any]:
        link, task = await self._local(project_id, remote_id)
        local = {"title": task["title"], "body": task["acceptance"], "state": task["status"],
                 "entity_revision": task["entity_revision"]} if task else None
        conflicts = []
        if link and task:
            if int(link["local_revision"]) != int(task["entity_revision"]):
                conflicts.append("local_changed")
            if link["remote_version"] != remote["version"] or link["last_digest"] != digest(remote):
                conflicts.append("remote_changed")
        version = digest({"project_id": project_id, "remote_id": remote_id, "remote": remote,
                          "link": link, "local": local})
        actions = ["import"] if link is None else ["resolve_fields"] if "remote_changed" in conflicts else ["push_title_body"]
        return {"project_id": project_id, "provider": "github", "remote_id": remote_id,
                "remote": remote, "local": local, "link": link, "conflicts": conflicts,
                "preview_digest": version, "actions": actions,
                "status_mapping": "review_required"}

    @staticmethod
    async def _observe(conn: aiosqlite.Connection, project_id: str, remote_id: str,
                       remote: dict[str, Any], link_id: str | None, local_revision: int | None) -> str:
        previous = await one(conn, "SELECT id FROM issue_sync_observations WHERE project_id = ? AND provider = 'github'"
                             " AND remote_id = ? ORDER BY observed_at DESC,id DESC LIMIT 1", (project_id, remote_id))
        observation_id = uuid.uuid4().hex
        remote_digest = digest(remote)
        await conn.execute("INSERT OR IGNORE INTO issue_sync_observations"
                           "(id,link_id,project_id,provider,remote_id,remote_version,remote_digest,remote_json,local_revision,origin_token,predecessor_id,observed_at)"
                           " VALUES (?,? ,?,'github',?,?,?,?,?,?,?,?)",
                           (observation_id, link_id, project_id, remote_id, remote["version"], remote_digest,
                            canonical(remote), local_revision, remote["origin_token"], previous["id"] if previous else None, now()))
        observed = await one(conn, "SELECT id FROM issue_sync_observations WHERE project_id = ? AND provider = 'github'"
                             " AND remote_id = ? AND remote_version = ? AND remote_digest = ?",
                             (project_id, remote_id, remote["version"], remote_digest))
        assert observed is not None
        return observed["id"]

    async def apply_import(self, principal: Principal, project_id: str, repository: str, number: int, *,
                           preview_digest: str, expected_collection_revision: int,
                           client_operation_id: str) -> dict[str, Any]:
        remote_id = _remote_id(repository, number)
        scope = Scope("project", project_id)
        payload = {"remote_id": remote_id, "preview_digest": preview_digest}
        replay = await self._replay(principal, scope, IMPORT_OPERATION, client_operation_id,
                                    expected_collection_revision, Entity("collection", project_id), payload)
        if replay is not None:
            return replay
        preview = await self.preview(project_id, repository, number)
        if preview["preview_digest"] != preview_digest or preview["link"] is not None:
            raise ControlConflict("the issue preview changed; inspect the current issue")
        remote = preview["remote"]
        if len(remote["title"]) > TITLE_LIMIT or len(remote["body"]) > BODY_LIMIT:
            raise IssueSyncRefused("the issue exceeds the task contract limits")

        async def effect(conn: aiosqlite.Connection, mutation: Mutation) -> dict[str, Any]:

            if await one(conn, "SELECT id FROM issue_links WHERE provider = 'github' AND remote_id = ?", (preview["remote_id"],)):
                raise ControlConflict("the issue was linked while the preview was open")
            task = await insert_task(conn, mutation, scope=scope, title=remote["title"], acceptance=remote["body"],
                                     checklist=[], dependencies=[], priority=3, brief={}, notes="",
                                     source_session_id=None, origin_kind="operator")
            link_id = uuid.uuid5(uuid.NAMESPACE_URL, f"github:{preview['remote_id']}").hex
            await conn.execute("INSERT INTO issue_links(id,project_id,task_id,provider,remote_id,remote_version,local_revision,origin_token,last_digest,state)"
                               " VALUES (?,?,?,'github',?,?,?,?,?,'linked')",
                               (link_id, project_id, task["task_id"], preview["remote_id"], remote["version"], 1,
                                remote["origin_token"] or "", digest(remote)))
            await self._observe(conn, project_id, preview["remote_id"], remote, link_id, 1)
            return {**task, "link_id": link_id, "remote_id": preview["remote_id"]}

        return await self.control.mutate(principal, scope, IMPORT_OPERATION, client_operation_id,
                                         expected_collection_revision, Entity("collection", project_id), payload, effect)

    async def queue_push(self, principal: Principal, project_id: str, repository: str, number: int, *,
                         preview_digest: str, expected_entity_revision: int,
                         client_operation_id: str) -> dict[str, Any]:
        remote_id = _remote_id(repository, number)
        scope = Scope("project", project_id)
        payload = {"remote_id": remote_id, "preview_digest": preview_digest}
        existing, _ = await self._local(project_id, remote_id)
        if existing is not None:
            replay = await self._replay(principal, scope, "issues.sync.push", client_operation_id,
                                        expected_entity_revision, Entity("task", existing["task_id"]), payload,
                                        effects=("issue.github.push",))
            if replay is not None:
                return replay
        preview = await self.preview(project_id, repository, number)
        if preview["preview_digest"] != preview_digest or preview["link"] is None or preview["local"] is None:
            raise ControlConflict("the issue preview changed; inspect the current issue")
        if "remote_changed" in preview["conflicts"]:
            raise ControlConflict("the remote issue changed; review its fields before a push")
        link, local, remote = preview["link"], preview["local"], preview["remote"]
        if len(local["body"]) > 2000:
            raise IssueSyncRefused("the task contract is too large for issue sync")

        async def effect(conn: aiosqlite.Connection, mutation: Mutation) -> dict[str, Any]:
            current = await one(conn, "SELECT * FROM issue_links WHERE id = ? AND state = 'linked'", (link["id"],))
            if current is None or current["remote_version"] != remote["version"] or current["last_digest"] != digest(remote):
                raise ControlConflict("the issue link changed")
            pending = await one(conn, "SELECT effect_id FROM issue_sync_outbound WHERE link_id = ?"
                                " AND state IN ('queued','unknown')", (link["id"],))
            if pending is not None:
                raise ControlConflict("a previous issue push still needs a proven outcome")
            observation_id = await self._observe(conn, project_id, preview["remote_id"], remote,
                                                 link["id"], expected_entity_revision)
            token = uuid.uuid5(uuid.NAMESPACE_URL, f"issue-origin:{mutation.receipt_id}").hex
            desired = {"title": local["title"], "body": local["body"], "origin_token": token}
            effect_id = await OutboxStore.enqueue(conn, mutation, principal, kind="issue.github.push",
                                                  operation="issues.sync.push", payload={"repository": repository,
                                                  "number": number, "link_id": link["id"], "observation_id": observation_id,
                                                  "local_revision": mutation.entity_revision},
                                                  effects=("issue.github.push",), task_id=link["task_id"])
            await conn.execute("INSERT INTO issue_sync_outbound(effect_id,link_id,before_observation_id,origin_token,desired_digest,desired_json,state,updated_at)"
                               " VALUES (?,?,?,?,?,?,'queued',?)",
                               (effect_id, link["id"], observation_id, token, digest(desired), canonical(desired), now()))
            return {"link_id": link["id"], "effect_id": effect_id, "state": "queued"}

        return await self.control.mutate(principal, scope, "issues.sync.push", client_operation_id,
                                         expected_entity_revision, Entity("task", link["task_id"]), payload, effect,
                                         effects=("issue.github.push",))

    async def resolve_remote(self, principal: Principal, project_id: str, repository: str, number: int, *,
                             preview_digest: str, expected_entity_revision: int,
                             client_operation_id: str, fields: dict[str, str]) -> dict[str, Any]:
        if set(fields) != {"title", "body"} or set(fields.values()) - {"local", "remote"}:
            raise IssueSyncRefused("choose local or remote for each issue field")
        remote_id = _remote_id(repository, number)
        scope = Scope("project", project_id)
        payload = {"remote_id": remote_id, "preview_digest": preview_digest, "fields": fields}
        existing, _ = await self._local(project_id, remote_id)
        if existing is None:
            raise IssueSyncRefused("the issue has no linked task")
        replay = await self._replay(principal, scope, UPDATE_OPERATION, client_operation_id,
                                    expected_entity_revision, Entity("task", existing["task_id"]), payload)
        if replay is not None:
            return replay
        preview = await self.preview(project_id, repository, number)
        if preview["preview_digest"] != preview_digest or preview["link"] is None:
            raise ControlConflict("the issue preview changed; inspect the current fields")
        remote, link = preview["remote"], preview["link"]
        if remote["state"] == "closed":
            # A closed remote issue does not silently complete a task. The operator resolves
            # title and body only; status remains a separate task review decision.
            status_mapping = "closed_remote_unmapped"
        else:
            status_mapping = "unchanged"

        async def effect(conn: aiosqlite.Connection, mutation: Mutation) -> dict[str, Any]:
            current_link = await one(conn, "SELECT * FROM issue_links WHERE id = ?", (link["id"],))
            task = await one(conn, "SELECT * FROM board_tasks WHERE id = ? AND project_id = ?",
                             (link["task_id"], project_id))
            if current_link is None or task is None or current_link["remote_version"] != link["remote_version"]:
                raise ControlConflict("the issue link changed")
            if fields["body"] == "remote" and remote["body"] != task["acceptance"]:
                if len(remote["body"]) > 2000 or task["status"] in ("review", "done"):
                    raise IssueSyncRefused("the task contract cannot be replaced in its current state")
                if task["current_attempt_id"]:
                    attempt = await one(conn, "SELECT state FROM execution_attempts WHERE id = ?",
                                        (task["current_attempt_id"],))
                    if attempt is not None and attempt["state"] in ("queued", "starting", "running", "waiting", "recovering"):
                        raise IssueSyncRefused("stop or reconcile the active attempt before replacing the contract")
                await conn.execute("UPDATE board_tasks SET acceptance = ? WHERE id = ?", (remote["body"], task["id"]))
                await capture_contract_change(conn, task["id"], origin_kind=principal.origin_class,
                                              origin_ref=f"github:{remote_id}@{remote['version']}")
            if fields["title"] == "remote" and remote["title"] != task["title"]:
                if not remote["title"].strip() or len(remote["title"]) > 200:
                    raise IssueSyncRefused("the remote title exceeds the task title limit")
                await conn.execute("UPDATE board_tasks SET title = ? WHERE id = ?", (remote["title"], task["id"]))
            await conn.execute("UPDATE board_tasks SET updated_at = ? WHERE id = ?", (now(), task["id"]))
            await conn.execute("UPDATE issue_links SET remote_version = ?,last_digest = ?,local_revision = ?,"
                               " state = 'linked' WHERE id = ?", (remote["version"], digest(remote), mutation.entity_revision, link["id"]))
            await self._observe(conn, project_id, remote_id, remote, link["id"], mutation.entity_revision)
            return {"task_id": task["id"], "link_id": link["id"], "field_sources": fields,
                    "status_mapping": status_mapping}

        return await self.control.mutate(principal, scope, UPDATE_OPERATION, client_operation_id,
                                         expected_entity_revision, Entity("task", link["task_id"]), payload, effect)

    async def list_issues(self, project_id: str, repository: str, label: str | None = None) -> dict[str, Any]:
        """Preview a repository's open issues against the project's board, one entry per issue.

        Each entry carries the per-issue preview digest :meth:`preview` would compute, so applying a
        ticked entry goes through the same reviewed path as a single issue. ``action`` is what the
        entry offers: ``import`` (no card yet), ``update`` (the issue changed since its card was made
        and the card did not), or nothing to do with a reason: ``linked`` (up to date), ``conflict``
        (both sides changed: the fields are chosen one by one), ``too_large`` (past the task limits)
        or ``elsewhere`` (linked to another project)."""
        if await self.db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,)) is None:
            raise KeyError(project_id)
        listed = await self.github.list(repository, label or None)
        revision = await self.control.revision(Scope("project", project_id), Entity("collection", project_id))
        entries = []
        for item in listed:
            remote = item["remote"]
            remote_id = _remote_id(repository, item["number"])
            entry = {"number": item["number"], "title": remote["title"], "url": item["url"],
                     "labels": item["labels"], "body_length": len(remote["body"]),
                     "task_id": None, "task_title": None, "preview_digest": None,
                     "expected_entity_revision": None}
            try:
                preview = await self._preview_of(project_id, remote_id, remote)
            except IssueSyncRefused:
                entries.append({**entry, "action": "elsewhere"})
                continue
            local = preview["local"]
            entry["preview_digest"] = preview["preview_digest"]
            if preview["link"] is not None:
                entry["task_id"] = preview["link"]["task_id"]
                entry["task_title"] = local["title"] if local else None
                entry["expected_entity_revision"] = local["entity_revision"] if local else None
            too_large = len(remote["title"]) > TITLE_LIMIT or len(remote["body"]) > BODY_LIMIT or not remote["title"].strip()
            if preview["link"] is None:
                action = "too_large" if too_large else "import"
            elif "remote_changed" not in preview["conflicts"]:
                action = "linked"
            elif "local_changed" in preview["conflicts"] or local is None:
                action = "conflict"
            else:
                action = "too_large" if too_large else "update"
            entries.append({**entry, "action": action})
        return {"project_id": project_id, "repository": repository, "label": label or None,
                "collection_revision": revision, "limit": LIST_LIMIT, "issues": entries}

    async def apply_selection(self, principal: Principal, project_id: str, repository: str,
                              items: list[dict[str, Any]], *, expected_collection_revision: int,
                              client_operation_id: str) -> dict[str, Any]:
        """Import or update the ticked entries of a listing, in order, each through its own receipt.

        The listing was read at one collection revision, and each import advances it by one; the
        next import expects the revision the previous one published, so anything else that changed
        the board meanwhile refuses the rest instead of being overrun. Each entry's command identity
        is derived from the batch's, so a retried batch replays its receipts rather than making
        twins. One entry refused for its own reason (it changed on GitHub, it was linked meanwhile)
        is reported and the others go on; a refused authority stops the batch."""
        revision = expected_collection_revision
        done: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for item in items:
            number, action = item["issue_number"], item["action"]
            operation_id = f"{client_operation_id}:{number}"[:160]
            try:
                if action == "import":
                    result = await self.apply_import(principal, project_id, repository, number,
                                                     preview_digest=item["preview_digest"],
                                                     expected_collection_revision=revision,
                                                     client_operation_id=operation_id)
                    revision = int(result["entity_revision"])
                    done.append({"issue_number": number, "action": "import", "task_id": result["task_id"]})
                elif action == "update":
                    expected = item.get("expected_entity_revision")
                    if type(expected) is not int:
                        raise IssueSyncRefused("an update needs the card's entity revision from the listing")
                    result = await self.resolve_remote(principal, project_id, repository, number,
                                                       preview_digest=item["preview_digest"],
                                                       expected_entity_revision=expected,
                                                       client_operation_id=operation_id,
                                                       fields={"title": "remote", "body": "remote"})
                    done.append({"issue_number": number, "action": "update", "task_id": result["task_id"]})
                else:
                    raise IssueSyncRefused(f"{action!r} is not an import or an update")
            except (ControlConflict, IssueSyncRefused) as exc:
                skipped.append({"issue_number": number, "reason": str(exc)})
            except KeyError:
                skipped.append({"issue_number": number, "reason": "the project or the card is gone"})
        return {"project_id": project_id, "repository": repository, "applied": done, "skipped": skipped,
                "collection_revision": revision}

    async def run(self, claim: Claim, check: Any) -> EffectOutcome:
        # GitHub does not offer a conditional issue PATCH, so host writes to this adapter
        # are serialized while the remote snapshot and observed response are compared.
        async with self._write_lock:
            return await self._run_locked(claim, check)

    async def _finalize(self, claim: Claim, outbound: aiosqlite.Row, observed: dict[str, Any]) -> bool:
        before = json.loads(outbound["remote_json"])
        desired = json.loads(outbound["desired_json"])
        async with self.db.transaction() as conn:
            link = await one(conn, "SELECT remote_version,last_digest,origin_token FROM issue_links WHERE id = ?",
                             (outbound["link_id"],))
            if link is None:
                return False
            already = (link["remote_version"] == observed["version"] and link["last_digest"] == digest(observed)
                       and link["origin_token"] == desired["origin_token"])
            if not already:
                if link["remote_version"] != before["version"] or link["last_digest"] != digest(before):
                    return False
                changed = await conn.execute("UPDATE issue_links SET remote_version = ?,last_digest = ?,origin_token = ?,"
                                             " local_revision = ?,state = 'linked' WHERE id = ? AND remote_version = ? AND last_digest = ?",
                                             (observed["version"], digest(observed), desired["origin_token"],
                                              claim.payload["local_revision"], outbound["link_id"],
                                              before["version"], digest(before)))
                if changed.rowcount != 1:
                    return False
            await conn.execute("UPDATE issue_sync_outbound SET state = 'verified',observed_version = ?,observed_digest = ?,updated_at = ?"
                               " WHERE effect_id = ? AND state IN ('queued','unknown','verified')",
                               (observed["version"], digest(observed), now(), claim.id))
            await self._observe(conn, claim.scope.id, outbound["remote_id"], observed,
                                outbound["link_id"], claim.payload["local_revision"])
        return True

    async def _run_locked(self, claim: Claim, check: Any) -> EffectOutcome:
        outbound = await self.db.fetchone("SELECT o.*,s.remote_json,s.remote_id FROM issue_sync_outbound o"
                                          " JOIN issue_sync_observations s ON s.id=o.before_observation_id WHERE o.effect_id = ?", (claim.id,))
        if outbound is None or outbound["state"] != "queued":
            return EffectOutcome("failed", "issue push is no longer queued")
        repository, number = claim.payload["repository"], int(claim.payload["number"])
        before = json.loads(outbound["remote_json"])
        try:
            current = await self.github.read(repository, number)
        except IssueSyncRefused as exc:
            return EffectOutcome("unknown", str(exc))
        if current != before:
            await self.db.execute("UPDATE issue_sync_outbound SET state = 'preflight_conflict',observed_version = ?,"
                                  "observed_digest = ?,updated_at = ? WHERE effect_id = ? AND state = 'queued'",
                                  (current["version"], digest(current), now(), claim.id))
            return EffectOutcome("failed", "remote issue changed before the push")
        desired = json.loads(outbound["desired_json"])
        try:
            await check(claim)
        except ControlDenied as exc:
            await self.db.execute("UPDATE issue_sync_outbound SET state = 'preflight_conflict',updated_at = ?"
                                  " WHERE effect_id = ? AND state = 'queued'", (now(), claim.id))
            return EffectOutcome("failed", str(exc))
        link = await self.db.fetchone("SELECT l.state,l.remote_version,l.last_digest,t.entity_revision"
                                      " FROM issue_links l JOIN board_tasks t ON t.id=l.task_id WHERE l.id = ?",
                                      (outbound["link_id"],))
        if (link is None or link["state"] != "linked" or link["remote_version"] != before["version"]
                or link["last_digest"] != digest(before)
                or int(link["entity_revision"]) != int(claim.payload["local_revision"])):
            await self.db.execute("UPDATE issue_sync_outbound SET state = 'preflight_conflict',updated_at = ?"
                                  " WHERE effect_id = ? AND state = 'queued'", (now(), claim.id))
            return EffectOutcome("failed", "the linked task or issue changed before the remote write")
        body = desired["body"].rstrip() + f"\n\n<!-- daedalus-issue-origin:{desired['origin_token']} -->"
        try:
            sent = await self.github.request("PATCH", repository, number, body={"title": desired["title"], "body": body})
            observed = await self.github.read(repository, number)
        except IssueSyncRefused:
            await self.db.execute("UPDATE issue_sync_outbound SET state = 'unknown',updated_at = ? WHERE effect_id = ?",
                                  (now(), claim.id))
            return EffectOutcome("unknown", "remote write outcome requires readback")
        if sent != observed or observed["origin_token"] != desired["origin_token"] or observed["title"] != desired["title"] or observed["body"] != desired["body"]:
            await self.db.execute("UPDATE issue_sync_outbound SET state = 'unknown',observed_version = ?,"
                                  "observed_digest = ?,updated_at = ? WHERE effect_id = ?",
                                  (observed["version"], digest(observed), now(), claim.id))
            return EffectOutcome("unknown", "remote readback does not prove the intended state")
        if not await self._finalize(claim, outbound, observed):
            return EffectOutcome("unknown", "local issue link changed before readback was committed")
        return EffectOutcome("completed")

    async def reconcile(self, claim: Claim) -> EffectResolution | None:
        outbound = await self.db.fetchone("SELECT o.*,s.remote_json,s.remote_id FROM issue_sync_outbound o"
                                          " JOIN issue_sync_observations s ON s.id=o.before_observation_id WHERE o.effect_id = ?", (claim.id,))
        if outbound is None:
            return EffectResolution("failed", {"observed": "missing_outbound_intent"})
        if outbound["state"] == "verified":
            return EffectResolution("completed", {"observed": "verified_remote_readback"})
        if outbound["state"] == "preflight_conflict":
            return EffectResolution("failed", {"observed": "preflight_conflict"})
        try:
            remote = await self.github.read(claim.payload["repository"], int(claim.payload["number"]))
        except IssueSyncRefused:
            return None
        desired = json.loads(outbound["desired_json"])
        if remote["origin_token"] != desired["origin_token"]:
            return None
        if remote["title"] != desired["title"] or remote["body"] != desired["body"]:
            return None
        if not await self._finalize(claim, outbound, remote):
            return None
        return EffectResolution("completed", {"observed": "matching_remote_origin_and_fields", "digest": digest(remote)})


async def install(app: Any) -> list[Any]:
    sync = IssueSync(app.db, GitHubIssues(app.settings.github_token))
    app.extensions["issue_sync"] = sync
    app.extensions["effects"].register("issue.github.push", sync)
    return []
