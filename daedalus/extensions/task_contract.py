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

import fnmatch
import json
import re
import unicodedata
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
            "INSERT INTO requirement_deliveries(requirement_id, staff_session_id, staff_id, via, message_id, path, delivered_at, task_id, contract_revision)"
            " SELECT ?, ?, ?, ?, ?, ?, ?, t.id, t.contract_revision FROM board_tasks t WHERE t.id = ?"
            " ON CONFLICT(requirement_id, staff_session_id) DO UPDATE SET via = excluded.via, message_id = excluded.message_id,"
            " path = CASE WHEN excluded.path != '' THEN excluded.path ELSE requirement_deliveries.path END,"
            " acknowledged_at = CASE WHEN requirement_deliveries.contract_revision = excluded.contract_revision"
            " THEN requirement_deliveries.acknowledged_at ELSE NULL END,"
            " opened_at = CASE WHEN requirement_deliveries.contract_revision = excluded.contract_revision"
            " THEN requirement_deliveries.opened_at ELSE NULL END,"
            " delivered_at = excluded.delivered_at, task_id = excluded.task_id, contract_revision = excluded.contract_revision",
            (requirement.id, staff_session_id, staff_id, via, message_id, path, _now(), requirement.task_id),
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
            delivered = await self.db.fetchone(
                "SELECT 1 FROM requirement_deliveries d JOIN board_tasks t ON t.id = d.task_id"
                " WHERE d.requirement_id = ? AND d.staff_session_id = ? AND d.contract_revision = t.contract_revision",
                (requirement.id, staff_session_id),
            )
            if delivered is None:
                unknown.append(ref)
                continue
            await self.db.execute("UPDATE requirement_deliveries SET acknowledged_at = COALESCE(acknowledged_at, ?)"
                                  " WHERE requirement_id = ? AND staff_session_id = ?",
                                  (now, requirement.id, staff_session_id))
            confirmed.append(requirement.label)
        return confirmed, unknown

    async def opened(self, staff_session_id: str, arguments: Any) -> list[str]:
        """Mark the input files a tool call of the session points at as opened; their requirements' ids.
        ``arguments`` are the call's arguments as the model wrote them (a mapping, or its JSON)."""
        rows = await self.db.fetchall(
            "SELECT d.requirement_id, d.path FROM requirement_deliveries d JOIN board_tasks t ON t.id = d.task_id"
            " WHERE d.staff_session_id = ? AND d.path != '' AND d.opened_at IS NULL"
            " AND d.contract_revision = t.contract_revision", (staff_session_id,)
        )
        hit = [row["requirement_id"] for row in rows if names_input(arguments, row["path"])]
        for requirement_id in hit:
            await self.db.execute("UPDATE requirement_deliveries SET opened_at = ? WHERE requirement_id = ? AND staff_session_id = ?", (_now(), requirement_id, staff_session_id))
        return hit

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
            " WHERE r.project_id = ? AND r.state = 'active' AND d.via = 'message' AND d.acknowledged_at IS NULL"
            " AND d.contract_revision = t.contract_revision AND t.status = 'doing'"
            " ORDER BY d.delivered_at",
            (project_id,),
        )
        return [dict(row) for row in rows]

    # -- checks and acceptance --------------------------------------------------------------------

    async def checks(self, task_id: str) -> list[dict[str, Any]]:
        row = await self.db.fetchone("SELECT checklist FROM board_tasks WHERE id = ?", (task_id,))
        return check_items(row["checklist"]) if row is not None else []

    async def set_checks(self, task_id: str, items: list[dict[str, Any]]) -> None:
        from daedalus.extensions.orchestrator_domain import (
            capture_contract_change,  # Lazy: the domain imports checklist parsing from this module.
        )

        async with self.db.transaction() as conn:
            await conn.execute("UPDATE board_tasks SET checklist = ?, entity_revision = entity_revision + 1 WHERE id = ?",
                               (json.dumps(items, ensure_ascii=False), task_id))
            await capture_contract_change(conn, task_id, origin_kind="checklist")

    async def acceptance(self, task_id: str) -> str:
        row = await self.db.fetchone("SELECT acceptance_state FROM board_tasks WHERE id = ?", (task_id,))
        return str(row["acceptance_state"] or "") if row is not None else ""

    async def set_acceptance(self, task_id: str, state: str) -> None:
        if state not in ACCEPTANCE_STATES:
            raise ValueError(f"acceptance is one of {', '.join(s or "''" for s in ACCEPTANCE_STATES)}")
        if state in ("accepted", "operator_approved"):
            raise ValueError("approval needs an exact result and review verdict")
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


GLOB_TOKEN_RE = re.compile(r"[^\s\"'`(),;|&<>=]*[*?][^\s\"'`(),;|&<>=]*")
"""A word of a command that is a glob: ``*.docx``, ``inbox/t/*``, ``glob('*.docx')``'s argument."""


def _strings(value: Any) -> list[str]:
    """Every string in a tool call's arguments, decoded: a command, a path, a working directory.
    Arguments handed over as their JSON are read as the mapping they spell."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        return _leaves(parsed) if isinstance(parsed, (dict, list)) else [value]
    return _leaves(value)


def _leaves(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _leaves(item)]
    if isinstance(value, list):
        return [s for item in value for s in _leaves(item)]
    return []


def _from_inbox(path: str) -> str:
    """The part of a path from the inbox on, which is how a member's relative path to it reads."""
    return path[path.rfind("/inbox/") + 1:] if "/inbox/" in path else ""


def _names_folder(text: str, folder: str) -> bool:
    """Whether the text names the folder, by its whole path or from the inbox on, as a whole word:
    ``inbox/5a9`` must not be read in ``inbox/5a9b``."""
    for spelling in dict.fromkeys(s for s in (folder, _from_inbox(folder)) if s):
        if re.search(re.escape(spelling) + r"(?![\w.-])", text):
            return True
    return False


def _runs_in(arguments: Any, folder: str) -> bool:
    """Whether a command runs with the folder as its working directory (``cwd``, whole or relative)."""
    if not isinstance(arguments, dict):
        return False
    cwd = unicodedata.normalize("NFC", str(arguments.get("cwd") or "")).rstrip("/")
    if not cwd:
        return False
    tail = _from_inbox(folder)
    return cwd == folder or bool(tail) and (cwd == tail or cwd.endswith("/" + tail))


def names_input(arguments: Any, path: str) -> bool:
    """Whether a tool call's arguments point at a delivered input file.

    Its path whole, or from the inbox on, counts anywhere. A member that read the inputs with a
    command — ``cd .agents/inbox/<task> && python3 …glob('*.docx')…`` — never wrote a path, and the
    hand-in was refused for files it had read; so a command also counts when it places itself in
    the file's box (names that folder, or runs with it as its ``cwd``) and names the file there, by
    its name or by a glob that matches it. Listing the box alone does not: a member must point at the
    file, not merely see that it is there.
    """
    if not path:
        return False
    # Compared composed: a name kept decomposed (NFD, as macOS writes it) never matched the composed
    # spelling every model uses, and the input stayed unopened however the member read it.
    path = unicodedata.normalize("NFC", path)
    pieces = [unicodedata.normalize("NFC", s) for s in _strings(arguments)]
    text = "\n".join(pieces)
    tail = _from_inbox(path) or path.rsplit("/", 1)[-1]
    if path in text or (tail and tail in text):
        return True
    folder, _, name = path.rpartition("/")
    if not folder or not name:
        return False
    if not (_names_folder(text, folder) or _runs_in(arguments, folder)):
        return False
    if re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", text):
        return True
    for glob in GLOB_TOKEN_RE.findall(text):
        pattern = glob.strip("'\"")
        if not pattern:
            continue
        # A glob with a folder in it is matched against the path's end, any other against the name.
        if "/" in pattern:
            relative = pattern.removeprefix("./")
            if fnmatch.fnmatchcase(path, relative if relative.startswith("/") else "*/" + relative):
                return True
        elif fnmatch.fnmatchcase(name, pattern):
            return True
    return False


__all__ = [
    "ACCEPTANCE_STATES", "CHECKS_MAX", "REQUIREMENTS_MAX", "REQUIREMENT_KINDS", "RETURNED", "Contracts", "Requirement", "check_items", "names_input", "split_checks",
]
