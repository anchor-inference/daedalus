"""A stand-in for the notifications service, for the applications tests build from a namespace.

It keeps every draft it was given and answers the reads from them, so a producer's test can say
what was posted without a database or a bus behind it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from daedalus.extensions.notifications import (
    DEFAULT_LEVEL,
    Draft,
    NotificationPage,
    NotificationSummary,
    NotificationView,
)


class RecordingNotifications:
    def __init__(self) -> None:
        self.drafts: list[Draft] = []
        self.seen: set[int] = set()
        self.locale = ""

    def language(self) -> str:
        return self.locale

    async def post(self, draft: Draft) -> NotificationView:
        self.drafts.append(draft)
        return self._view(len(self.drafts), draft)

    def _view(self, number: int, draft: Draft) -> NotificationView:
        now = datetime.now(UTC).isoformat()
        return NotificationView(
            id=number, at=now, updated_at=now, category=draft.category, kind=draft.kind or draft.category,
            level=draft.level or DEFAULT_LEVEL.get(draft.category, "normal"), tone=draft.tone, title=draft.title, body=draft.body,
            link=draft.link, session_id=draft.session_id, run_id=draft.run_id, project_id=draft.project_id, staff_id=draft.staff_id,
            terminal_id=draft.terminal_id, source=draft.source, dedupe_key=draft.dedupe_key, request_ref=draft.request_ref, count=1,
            actions=[a.as_dict() for a in draft.actions], seen=number in self.seen, resolved=None,
            needs_you=draft.request_ref is not None, delivered={c: "handled" for c in draft.handled},
        )

    def kinds(self) -> list[str]:
        return [d.kind or d.category for d in self.drafts]

    def titles(self) -> list[str]:
        return [d.title for d in self.drafts]

    async def summary(self) -> NotificationSummary:
        views = [self._view(i, d) for i, d in enumerate(self.drafts, start=1)]
        return NotificationSummary(
            unseen=sum(1 for v in views if not v["seen"] and v["level"] != "quiet"),
            needs_you=sum(1 for v in views if v["needs_you"]),
        )

    async def list(self, view: str = "all", **_: Any) -> NotificationPage:
        views = [self._view(i, d) for i, d in reversed(list(enumerate(self.drafts, start=1)))]
        if view == "unseen":
            views = [v for v in views if not v["seen"] and v["level"] != "quiet"]
        return NotificationPage(entries=views, next_before=None, summary=await self.summary())

    async def mark_seen(self, ids: Sequence[int] | None = None, *, everything: bool = False, session_id: str | None = None) -> int:
        wanted = set(range(1, len(self.drafts) + 1)) if everything else set(ids or ())
        if session_id is not None:
            wanted = {i for i, d in enumerate(self.drafts, start=1) if d.session_id == session_id}
        fresh = wanted - self.seen
        self.seen |= fresh
        return len(fresh)

    async def prune(self, keep_days: int) -> int:
        return 0


__all__ = ["RecordingNotifications"]
