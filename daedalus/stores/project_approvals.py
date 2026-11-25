"""The calls the operator allowed for every chat of a project ("Allow similar in this project").

A rule is exactly the family a session's "Allow similar" grant holds (``policy.similar_grant``): the
same tool, the same question, the same command prefix, folder or host, matched by the same
``policy.similar_covers``. Nothing here widens it. What differs is only who it answers: not one chat,
but whichever chat of the project is asking, until the operator takes it back.

The policy gate is synchronous and cannot wait on a query, so the rules are read into memory at start
and kept in step by :meth:`add`, :meth:`revoke` and :meth:`forget`; the table is what survives a restart.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

MAX_RULES_PER_PROJECT = 50
"""Past this the operator revokes one first. Dropping the oldest silently would hand a question back to
the agent that the operator had answered for good."""

SCOPE = "project"
"""Marks a grant as a project's, for the words the decision gives and for the listing."""


def rule_id(family: dict[str, Any]) -> str:
    """A rule's name: a digest of the family it allows, so allowing the same family twice is one rule."""
    body = {k: v for k, v in family.items() if k not in ("id", "at", "scope", "until")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:12]


def public(row: dict[str, Any]) -> dict[str, Any]:
    """What the app is shown of a rule."""
    family = row["family"]
    return {"id": row["id"], "tool": family.get("tool"), "rule": family.get("rule"), "kind": family.get("kind"),
            "value": family.get("value"), "hosts": family.get("hosts") or [], "label": family.get("label"),
            "server": family.get("server") or None, "created_at": row["created_at"], "session_id": row["session_id"] or None,
            "via": row["via"] or None}


class ProjectApprovals:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._held: dict[str, list[dict[str, Any]]] = {}

    async def load(self) -> None:
        """Read every rule into memory; a row whose family cannot be read is left out and reported."""
        held: dict[str, list[dict[str, Any]]] = {}
        for row in await self.db.fetchall("SELECT project_id, id, family, created_at, session_id, via FROM project_approval_rules ORDER BY created_at, id"):
            try:
                family = json.loads(row["family"])
            except ValueError:
                logger.warning("project %s: the approval rule %s cannot be read and is ignored", row["project_id"], row["id"])
                continue
            if isinstance(family, dict):
                held.setdefault(row["project_id"], []).append({**dict(row), "family": family})
        self._held = held

    def standing(self, project_id: str) -> list[dict[str, Any]]:
        """The project's rules as grants the policy reads, for one call of the gate."""
        return [{**row["family"], "id": row["id"], "scope": SCOPE} for row in self._held.get(project_id, ())]

    def rows(self, project_id: str) -> list[dict[str, Any]]:
        return [public(row) for row in self._held.get(project_id, ())]

    async def add(self, project_id: str, family: dict[str, Any], *, session_id: str, via: str) -> dict[str, Any]:
        """Keep the family as the project's rule and return it; allowing one already held changes nothing."""
        ident = rule_id(family)
        held = self._held.setdefault(project_id, [])
        for row in held:
            if row["id"] == ident:
                return public(row)
        if len(held) >= MAX_RULES_PER_PROJECT:
            raise ValueError(f"this project already has {MAX_RULES_PER_PROJECT} standing approvals; take one back before adding another")
        stored = {k: v for k, v in family.items() if k not in ("at", "until", "scope", "id")}
        created = datetime.fromtimestamp(time.time(), UTC).isoformat()
        await self.db.execute(
            "INSERT INTO project_approval_rules(project_id, id, family, created_at, session_id, via) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, ident, json.dumps(stored, sort_keys=True), created, session_id, via),
        )
        row = {"project_id": project_id, "id": ident, "family": stored, "created_at": created, "session_id": session_id, "via": via}
        held.append(row)
        return public(row)

    async def revoke(self, project_id: str, ident: str) -> bool:
        """Take one rule back: what it allowed is asked about again, in every chat of the project."""
        held = self._held.get(project_id, [])
        if not any(row["id"] == ident for row in held):
            return False
        await self.db.execute("DELETE FROM project_approval_rules WHERE project_id = ? AND id = ?", (project_id, ident))
        self._held[project_id] = [row for row in held if row["id"] != ident]
        return True

    def forget(self, project_id: str) -> None:
        """The project is gone and the database removed its rows with it; stop answering for it here too."""
        self._held.pop(project_id, None)


__all__ = ["MAX_RULES_PER_PROJECT", "SCOPE", "ProjectApprovals", "public", "rule_id"]
