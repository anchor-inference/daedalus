"""A task's contract and its acceptance: the requirements a card carries, who was given each of them,
and the checks its result is accepted against.

The four-part brief says what a task is. What the operator adds while the work goes on — a quality bar,
a file to start from, a thing not to do — used to live in the orchestrator's chat, and from there it
reached the member as a message or not at all: a list of eleven demands for a video stayed in the
conversation, two reference videos were promised to the scriptwriter and never sent, and a fallback
the operator had ruled out was allowed in a message nobody could later find. A requirement is a row
on the card instead, numbered (``R1`` …), with where it came from, and every member who works the card
is given it — in the brief at the start, as a message while at work — with the delivery recorded and,
once the member confirms it, acknowledged. An ``input`` requirement names a file the work starts from;
the host counts it opened when the member's tool touches it, and a member who never opened it cannot
hand the work in.

A card's checks (``C1`` …) are its done-when, one item each, kept in the board's checklist. A member
hands work in with evidence per item; the orchestrator accepts it with a mark per item and per
requirement, or returns it. Acceptance is a column of its own beside the board's status: ``done`` is
where the work is, ``accepted`` is that someone checked it.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from daedalus.stores.database import Database

REQUIREMENT_KINDS = ("quality", "scope", "input", "constraint")
REQUIREMENTS_MAX = 20
"""Active requirements on one card. More is a list nobody checks item by item; the refusal asks for
them to be merged, which is what a list that long needs anyway."""
REQUIREMENT_MAX_CHARS = 1000
CHECKS_MAX = 12
CHECK_MAX_CHARS = 200
ACCEPTANCE_STATES = ("", "handed_in", "accepted", "operator_approved", "returned")
RETURNED = "returned by the orchestrator: "
"""How a return is written into a card's notes; the next brief of the card starts from the latest."""
SOURCES = ("operator", "orchestrator")
LABEL_RE = re.compile(r"^\s*([RrCc])\s*(\d{1,3})\s*$")
LIST_MARK_RE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    task_id: str
    project_id: str
    number: int
    text: str
    kind: str
    source: str
    state: str
    replaces: str | None
    file_id: str | None
    evidence: dict[str, Any]
    mark: dict[str, Any]
    created_at: str
    updated_at: str

    @property
    def label(self) -> str:
        return f"R{self.number}"

    @property
    def from_operator(self) -> bool:
        """Whether the operator stated it: in their own words in the chat, or in an answer to a question."""
        return self.source == "operator" or self.source.startswith(("operator:", "answer:"))

    @property
    def message_seq(self) -> int | None:
        """The operator's message in the orchestrator's chat this came from, when it is known."""
        kind, _, ref = self.source.partition(":")
        return int(ref) if kind == "operator" and ref.isdigit() else None

    def origin(self) -> str:
        if self.source.startswith("answer:"):
            return f"the operator's answer [{self.source.split(':', 1)[1]}]"
        if self.source.startswith("rule:"):
            return f"the operator's rule #{self.source.split(':', 1)[1]}"
        return "the operator" if self.from_operator else "the orchestrator"

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "number": self.number, "text": self.text, "kind": self.kind, "source": self.source,
            "from_operator": self.from_operator, "state": self.state, "replaces": self.replaces, "file_id": self.file_id,
            "evidence": self.evidence or None, "mark": self.mark or None, "created_at": self.created_at, "message_seq": self.message_seq,
        }


def _requirement(row: Any) -> Requirement:
    return Requirement(
        id=row["id"], task_id=row["task_id"], project_id=row["project_id"], number=int(row["number"]), text=row["text"], kind=row["kind"],
        source=row["source"], state=row["state"], replaces=row["replaces"], file_id=row["file_id"], evidence=_json(row["evidence_json"]),
        mark=_json(row["mark_json"]), created_at=row["created_at"], updated_at=row["updated_at"],
    )


def split_checks(done_when: str) -> list[str]:
    """A done-when as separate checks: one per line or list item, else one per sentence joined by "; ".

    Nothing cleverer: a check the orchestrator marks is the words it wrote, and a split made on a
    guess about meaning would leave it marking halves of sentences.
    """
    lines = [LIST_MARK_RE.sub("", line).strip() for line in (done_when or "").splitlines()]
    parts = [line for line in lines if line]
    if len(parts) == 1 and ";" in parts[0]:
        pieces = [p.strip() for p in parts[0].split(";")]
        if all(len(p) >= 8 for p in pieces):
            parts = pieces
    return [" ".join(p.split())[:CHECK_MAX_CHARS] for p in parts][:CHECKS_MAX]


def check_items(raw: Any) -> list[dict[str, Any]]:
    """A card's checklist as the board keeps it: ``[{text, done, evidence?, mark?}]``."""
    try:
        items = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        return []
    return [dict(item) for item in items if isinstance(item, dict) and str(item.get("text") or "").strip()] if isinstance(items, list) else []


def _same(a: str, b: str) -> bool:
    x, y = " ".join(a.casefold().split()), " ".join(b.casefold().split())
    if not x or not y:
        return False
    return x == y or (min(len(x), len(y)) >= 8 and (x in y or y in x))


class Contracts:
    """The requirements, deliveries, checks and acceptance of the board's cards."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- requirements -----------------------------------------------------------------------------

    async def requirements(self, task_id: str, *, active: bool = True) -> list[Requirement]:
        where = " AND state = 'active'" if active else ""
        rows = await self.db.fetchall(f"SELECT * FROM task_requirements WHERE task_id = ?{where} ORDER BY number", (task_id,))  # noqa: S608 — a fixed clause
        return [_requirement(r) for r in rows]

    async def of_tasks(self, task_ids: list[str]) -> dict[str, list[Requirement]]:
        if not task_ids:
            return {}
        marks = ",".join("?" for _ in task_ids)
        rows = await self.db.fetchall(f"SELECT * FROM task_requirements WHERE task_id IN ({marks}) ORDER BY task_id, number", tuple(task_ids))  # noqa: S608
        out: dict[str, list[Requirement]] = {}
        for row in rows:
            out.setdefault(row["task_id"], []).append(_requirement(row))
        return out

    async def find(self, task_id: str, ref: str) -> Requirement | None:
        """A requirement of the card by its label (``R3``, ``3``) or its id, whatever its state."""
        text = (ref or "").strip()
        match = re.match(r"^[Rr]?\s*(\d{1,3})$", text)
        if match is not None:
            row = await self.db.fetchone("SELECT * FROM task_requirements WHERE task_id = ? AND number = ?", (task_id, int(match.group(1))))
        else:
            row = await self.db.fetchone("SELECT * FROM task_requirements WHERE task_id = ? AND id = ?", (task_id, text))
        return _requirement(row) if row is not None else None

    async def add(self, task_id: str, project_id: str, text: str, kind: str, source: str, *, replaces: Requirement | None = None, file_id: str | None = None) -> Requirement:
        """A new requirement on the card, or the one already there in the same words. ``ValueError``
        says what is wrong in words the orchestrator can act on."""
        body = " ".join((text or "").split()) if "\n" not in (text or "") else (text or "").strip()
        if not body:
            raise ValueError("a requirement needs its text")
        if len(body) > REQUIREMENT_MAX_CHARS:
            raise ValueError(f"a requirement is at most {REQUIREMENT_MAX_CHARS} characters; say it shorter or split it")
        if kind not in REQUIREMENT_KINDS:
            raise ValueError(f"kind is one of {', '.join(REQUIREMENT_KINDS)}")
        active = await self.requirements(task_id)
        same = next((r for r in active if r.text.casefold() == body.casefold() and r.file_id == file_id), None)
        if same is not None and replaces is None:
            return same
        if len(active) - (1 if replaces is not None else 0) >= REQUIREMENTS_MAX:
            raise ValueError(f"the card already has {REQUIREMENTS_MAX} requirements in force; merge some of them (Require(replaces=…)) before adding more")
        now = _now()
        async with self.db.transaction() as conn:
            cursor = await conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM task_requirements WHERE task_id = ?", (task_id,))
            number = int((await cursor.fetchone())[0])
            requirement_id = uuid.uuid4().hex[:10]
            await conn.execute(
                "INSERT INTO task_requirements(id, task_id, project_id, number, text, kind, source, state, replaces, file_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (requirement_id, task_id, project_id, number, body, kind, source, replaces.id if replaces is not None else None, file_id, now, now),
            )
            if replaces is not None:
                await conn.execute("UPDATE task_requirements SET state = 'superseded', updated_at = ? WHERE id = ?", (now, replaces.id))
        found = await self.find(task_id, requirement_id)
        assert found is not None
        return found

    async def withdraw(self, requirement: Requirement) -> None:
        await self.db.execute("UPDATE task_requirements SET state = 'withdrawn', updated_at = ? WHERE id = ?", (_now(), requirement.id))

    async def set_evidence(self, requirement: Requirement, evidence: dict[str, Any]) -> None:
        await self.db.execute("UPDATE task_requirements SET evidence_json = ?, updated_at = ? WHERE id = ?", (json.dumps(evidence, ensure_ascii=False), _now(), requirement.id))

    async def set_mark(self, requirement: Requirement, mark: dict[str, Any]) -> None:
        await self.db.execute("UPDATE task_requirements SET mark_json = ?, updated_at = ? WHERE id = ?", (json.dumps(mark, ensure_ascii=False), _now(), requirement.id))

    async def clear_marks(self, task_id: str) -> None:
        """A new round starts unmarked: last round's marks and evidence were about other work."""
        await self.db.execute("UPDATE task_requirements SET evidence_json = '{}', mark_json = '{}' WHERE task_id = ?", (task_id,))

    # -- deliveries -------------------------------------------------------------------------------

    async def delivered(self, requirement: Requirement, *, staff_session_id: str, staff_id: str, via: str, message_id: str = "", path: str = "") -> None:
        """That a member's session was given the requirement. A second delivery to the same session (a
        new round in a command-line member's session) keeps what the first one learnt: an
        acknowledgement or an opened file is not taken back by being told again."""
        await self.db.execute(
            "INSERT INTO requirement_deliveries(requirement_id, staff_session_id, staff_id, via, message_id, path, delivered_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(requirement_id, staff_session_id) DO UPDATE SET via = excluded.via, message_id = excluded.message_id,"
            " path = CASE WHEN excluded.path != '' THEN excluded.path ELSE requirement_deliveries.path END, delivered_at = excluded.delivered_at",
            (requirement.id, staff_session_id, staff_id, via, message_id, path, _now()),
        )

    async def acknowledge(self, task_id: str, staff_session_id: str, refs: list[str]) -> tuple[list[str], list[str]]:
        """The requirements a member confirmed, by label; ``(confirmed, not found)``."""
        confirmed: list[str] = []
        unknown: list[str] = []
        for ref in dict.fromkeys(str(r).strip() for r in refs if str(r or "").strip()):
            requirement = await self.find(task_id, ref)
            if requirement is None or requirement.state != "active":
                unknown.append(ref)
                continue
            now = _now()
            await self.db.execute(
                "INSERT INTO requirement_deliveries(requirement_id, staff_session_id, staff_id, via, delivered_at, acknowledged_at)"
                " SELECT ?, s.id, s.staff_id, 'brief', ?, ? FROM staff_sessions s WHERE s.id = ?"
                " ON CONFLICT(requirement_id, staff_session_id) DO UPDATE SET acknowledged_at = COALESCE(requirement_deliveries.acknowledged_at, excluded.acknowledged_at)",
                (requirement.id, now, now, staff_session_id),
            )
            confirmed.append(requirement.label)
        return confirmed, unknown

    async def opened(self, staff_session_id: str, text: str) -> list[str]:
        """Mark the input files a tool call of the session names as opened; their requirements' ids."""
        rows = await self.db.fetchall(
            "SELECT requirement_id, path FROM requirement_deliveries WHERE staff_session_id = ? AND path != '' AND opened_at IS NULL", (staff_session_id,)
        )
        hit = [row["requirement_id"] for row in rows if _names_file(text, row["path"])]
        for requirement_id in hit:
            await self.db.execute("UPDATE requirement_deliveries SET opened_at = ? WHERE requirement_id = ? AND staff_session_id = ?", (_now(), requirement_id, staff_session_id))
        return hit

    async def unmet_inputs(self, task_id: str, staff_id: str, *, cli: bool) -> list[tuple[Requirement, str]]:
        """The card's input files the member has not opened (a command-line member: not confirmed),
        in any of its sessions on the card, with the path it was given them at."""
        missing: list[tuple[Requirement, str]] = []
        for requirement in await self.requirements(task_id):
            if requirement.kind != "input":
                continue
            rows = await self.db.fetchall("SELECT path, acknowledged_at, opened_at FROM requirement_deliveries WHERE requirement_id = ? AND staff_id = ?", (requirement.id, staff_id))
            done = any((row["acknowledged_at"] if cli else row["opened_at"]) for row in rows)
            if not done:
                missing.append((requirement, next((row["path"] for row in rows if row["path"]), "")))
        return missing

    async def deliveries(self, task_id: str) -> dict[str, list[dict[str, Any]]]:
        rows = await self.db.fetchall(
            "SELECT d.*, m.name AS staff_name, m.harness FROM requirement_deliveries d JOIN task_requirements r ON r.id = d.requirement_id"
            " LEFT JOIN staff m ON m.id = d.staff_id WHERE r.task_id = ? ORDER BY d.delivered_at",
            (task_id,),
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(row["requirement_id"], []).append({
                "staff_id": row["staff_id"], "staff_name": row["staff_name"] or "", "via": row["via"], "message_id": row["message_id"],
                "delivered_at": row["delivered_at"], "acknowledged_at": row["acknowledged_at"], "opened_at": row["opened_at"],
                "cli": (row["harness"] or "daedalus") != "daedalus",
            })
        return out

    async def unconfirmed(self, project_id: str) -> list[dict[str, Any]]:
        """Requirements sent to a member at work that it has not confirmed, on cards still in doing."""
        rows = await self.db.fetchall(
            "SELECT r.task_id, r.number, r.text, d.delivered_at, m.name AS staff_name FROM requirement_deliveries d"
            " JOIN task_requirements r ON r.id = d.requirement_id JOIN board_tasks t ON t.id = r.task_id LEFT JOIN staff m ON m.id = d.staff_id"
            " WHERE r.project_id = ? AND r.state = 'active' AND d.via = 'message' AND d.acknowledged_at IS NULL AND t.status = 'doing'"
            " ORDER BY d.delivered_at",
            (project_id,),
        )
        return [dict(row) for row in rows]

    # -- checks and acceptance --------------------------------------------------------------------

    async def checks(self, task_id: str) -> list[dict[str, Any]]:
        row = await self.db.fetchone("SELECT checklist FROM board_tasks WHERE id = ?", (task_id,))
        return check_items(row["checklist"]) if row is not None else []

    async def set_checks(self, task_id: str, items: list[dict[str, Any]]) -> None:
        await self.db.execute("UPDATE board_tasks SET checklist = ? WHERE id = ?", (json.dumps(items, ensure_ascii=False), task_id))

    async def acceptance(self, task_id: str) -> str:
        row = await self.db.fetchone("SELECT acceptance_state FROM board_tasks WHERE id = ?", (task_id,))
        return str(row["acceptance_state"] or "") if row is not None else ""

    async def set_acceptance(self, task_id: str, state: str) -> None:
        if state not in ACCEPTANCE_STATES:
            raise ValueError(f"acceptance is one of {', '.join(s or "''" for s in ACCEPTANCE_STATES)}")
        await self.db.execute("UPDATE board_tasks SET acceptance_state = ? WHERE id = ?", (state, task_id))

    @staticmethod
    def match(ref: str, checks: list[dict[str, Any]], requirements: list[Requirement]) -> tuple[str, int | Requirement] | None:
        """What a piece of evidence or a mark is about: ``("C", index)`` or ``("R", requirement)``, by its
        label or by its words."""
        text = str(ref or "").strip()
        label = LABEL_RE.match(text)
        if label is not None:
            number = int(label.group(2))
            if label.group(1).upper() == "C":
                return ("C", number - 1) if 1 <= number <= len(checks) else None
            found = next((r for r in requirements if r.number == number), None)
            return ("R", found) if found is not None else None
        for index, item in enumerate(checks):
            if _same(text, str(item.get("text") or "")):
                return "C", index
        for requirement in requirements:
            if _same(text, requirement.text):
                return "R", requirement
        return None


def _names_file(text: str, path: str) -> bool:
    """Whether a tool call's arguments name a delivered file: its path whole, or the part from the
    inbox on, which is how a member's relative path to it reads."""
    if not path:
        return False
    if path in text:
        return True
    tail = path[path.rfind("/inbox/") + 1:] if "/inbox/" in path else path.rsplit("/", 1)[-1]
    return bool(tail) and tail in text


__all__ = [
    "ACCEPTANCE_STATES", "CHECKS_MAX", "REQUIREMENTS_MAX", "REQUIREMENT_KINDS", "RETURNED", "Contracts", "Requirement", "check_items", "split_checks",
]
