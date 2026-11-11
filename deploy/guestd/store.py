"""The door's own database: links, the one conversation of each, its messages, and what happened.

A SQLite file nothing else opens. Every change is one short transaction on one connection on the event
loop, which is what makes a redeem atomic: two POSTs of one invite cannot both find it unspent, because
the second one's read happens after the first one's commit.

An invite and a conversation token are kept as their hashes only. A copy of this file opens nothing,
and it also settles what a repeated redeem can do: there is no token here to hand out twice.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    invite_hash TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    label TEXT NOT NULL,
    topic TEXT NOT NULL,
    mode TEXT NOT NULL,
    briefing TEXT NOT NULL,
    model_path TEXT NOT NULL,
    model_name TEXT NOT NULL,
    price_in REAL NOT NULL,
    price_out REAL NOT NULL,
    approval INTEGER NOT NULL,
    usd_limit REAL NOT NULL,
    turns_per_day INTEGER NOT NULL,
    turns_total INTEGER NOT NULL,
    invite_expires_at REAL NOT NULL,
    conversation_hours REAL NOT NULL,
    idle_hours REAL NOT NULL,
    retention_days REAL NOT NULL,
    guest_tools TEXT NOT NULL,
    created_at REAL NOT NULL,
    closed_at REAL,
    spent_usd REAL NOT NULL DEFAULT 0,
    reserved_usd REAL NOT NULL DEFAULT 0,
    turns_used INTEGER NOT NULL DEFAULT 0,
    turns_today INTEGER NOT NULL DEFAULT 0,
    turns_day TEXT NOT NULL DEFAULT '',
    file_mb REAL NOT NULL DEFAULT 50,
    day_mb REAL NOT NULL DEFAULT 500
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL UNIQUE REFERENCES links(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    decide_by REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_guest_at REAL NOT NULL,
    closed_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    at REAL NOT NULL,
    state TEXT NOT NULL,
    reserved_usd REAL NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'text',
    task_id TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS messages_waiting ON messages(state, id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id TEXT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    at REAL NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS spend_days (
    day TEXT PRIMARY KEY,
    usd REAL NOT NULL DEFAULT 0,
    reserved REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    origin TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    received INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    touched_at REAL NOT NULL,
    fetched INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS files_by_conversation ON files(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_by_conversation ON tasks(conversation_id);
"""

SCHEMA_VERSION = 2
"""What :func:`migrate` brings a database to. 1 is the door before messages had kinds; nothing before it
counted, so a file with no version and no ``kind`` column is a 1."""

ADDED_COLUMNS = (
    ("links", "file_mb", "REAL NOT NULL DEFAULT 50"),
    ("links", "day_mb", "REAL NOT NULL DEFAULT 500"),
    ("messages", "kind", "TEXT NOT NULL DEFAULT 'text'"),
    ("messages", "task_id", "TEXT NOT NULL DEFAULT ''"),
    ("messages", "data", "TEXT NOT NULL DEFAULT ''"),
)
"""What version 2 added to tables that existed in version 1. ``CREATE TABLE IF NOT EXISTS`` leaves an old table
as it was, so each is added where it is missing; a link made before files existed gets the same defaults a new one
does."""


def migrate(conn: sqlite3.Connection) -> None:
    """Bring the door's database to :data:`SCHEMA_VERSION`, in one transaction, whatever version it is at.

    The door has no other copy of this file, so a step that fails rolls back and the door stops with the
    file as it was, rather than half a schema under a version number that says otherwise."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, name, kind in ADDED_COLUMNS:
            present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

STAND_IN = "stand_in"
DIRECT = "direct"

WAITING = "waiting"
"""A link whose invite has not been redeemed."""
SPENT = "spent"
REVOKED = "revoked"
EXPIRED = "expired"

PENDING = "pending"
"""A conversation the owner has not admitted yet."""
OPEN = "open"
CLOSED = "closed"

HELD = "held"
"""A first message waiting behind approval. Shown to the owner; no model has read it."""
QUEUED = "queued"
CLAIMED = "claimed"
ANSWERED = "answered"
FAILED = "failed"
SENT = "sent"

GUEST = "guest"
AGENT = "agent"
DOOR = "door"
"""The door's own words to the guest: a limit, a failure. Never the model's."""
OWNER = "owner"
"""A turn the host asked for itself, to carry an answer of the operator's to the guest. Its words never reach the
door: the message only holds a place in the queue, so the turn is counted and bounded as a guest's is."""

TEXT, FILE, TASK, QUESTION, STATUS = "text", "file", "task", "question", "status"
KINDS = (TEXT, FILE, TASK, QUESTION, STATUS)
"""What a message is. ``text`` is what a message always was; the others carry ids, so a status names its task and
a file message names its files."""

ACCEPTED, WORKING, NEEDS_INPUT, DONE, REFUSED_TASK, CANCELLED = "accepted", "working", "needs_input", "done", "refused", "cancelled"
TASK_STATES = (ACCEPTED, WORKING, NEEDS_INPUT, DONE, REFUSED_TASK, CANCELLED)
OPEN_TASK_STATES = (ACCEPTED, WORKING, NEEDS_INPUT)
"""A task that may still move. One that is done, refused or cancelled stays so."""

UPLOADING, READY, GONE = "uploading", "ready", "gone"
"""A file's body: arriving in pieces, whole and checked, or deleted (by retention, or once the host has its copy)."""

DEFAULT_USD_PER_DAY = 5.0
DEFAULT_FILE_MB = 50.0
DEFAULT_DAY_MB = 500.0
"""A link's file limits when the owner set none: a file, and a day's files both ways. 0 is no limit, as for every other."""

NO_LIMIT = 0
"""What every limit and lifetime of a link, and the day's budget, holds when the owner set none. A column
cannot hold infinity, and a zero limit had no use: a link that may never answer is one not made."""


def until(now: float, hours: float) -> float:
    """When a lifetime of ``hours`` from ``now`` ends: :data:`NO_LIMIT` for one that never does."""
    return now + float(hours) * 3600 if hours else NO_LIMIT


def ended(at: float, now: float) -> bool:
    return bool(at) and at < now


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_secret(prefix: str) -> str:
    """256 bits, so a guess is not a thing an attempt limit even has to stop."""
    return prefix + secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a limit stops a turn. ``until`` is when it lifts, ``None`` when it never will."""

    limit: str
    until: float | None


class Store:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        # One event loop uses this connection; the check would only trip a test client's own thread.
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # The tables first, then the columns an older file is missing.
        self.conn.executescript(SCHEMA)
        migrate(self.conn)

    def close(self) -> None:
        self.conn.close()

    def _one(self, sql: str, *params: Any) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def _all(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def _write(self, sql: str, *params: Any) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    # -- settings ---------------------------------------------------------------------

    def routed(self, link: Any) -> Any:
        """``link`` as today's table of model routes has it: limited to its providers and at their price.

        Read for each turn and not kept on the link, so a choice made after the link was made holds for it.
        A link's own price stands for a model the table does not name.
        """
        for route in json.loads(self.setting("model_routes", "[]")):
            if route["path"] == link["model_path"] and route["model"] == link["model_name"]:
                prices = {name: route[name] for name in ("price_in", "price_out") if route[name] is not None}
                return {**dict(link), "provider_only": route["only"], **prices}
        return link

    def setting(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM settings WHERE key = ?", key)
        return str(row["value"]) if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        self._write("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, value)

    def usd_per_day(self) -> float:
        return float(self.setting("usd_per_day", str(DEFAULT_USD_PER_DAY)))

    # -- links ------------------------------------------------------------------------

    def create_link(self, fields: dict[str, Any]) -> tuple[str, str]:
        """A new link and its invite. The invite is returned once and stored as a hash."""
        link_id = secrets.token_hex(8)
        invite = new_secret("")
        now = self.clock()
        self._write(
            "INSERT INTO links(id, invite_hash, state, label, topic, mode, briefing, model_path, model_name, price_in, price_out, approval,"
            " usd_limit, turns_per_day, turns_total, invite_expires_at, conversation_hours, idle_hours, retention_days, guest_tools, created_at, file_mb, day_mb)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            link_id, digest(invite), WAITING, fields["label"], fields["topic"], fields["mode"], fields["briefing"], fields["model_path"], fields["model_name"],
            float(fields["price_in"]), float(fields["price_out"]), int(bool(fields["approval"])), float(fields["usd_limit"]), int(fields["turns_per_day"]),
            int(fields["turns_total"]), until(now, fields["invite_hours"]), float(fields["conversation_hours"]), float(fields["idle_hours"]),
            float(fields["retention_days"]), fields["guest_tools"], now, float(fields.get("file_mb", DEFAULT_FILE_MB)), float(fields.get("day_mb", DEFAULT_DAY_MB)),
        )
        self.record(link_id, "created")
        return link_id, invite

    def update_link(self, link_id: str, fields: dict[str, Any]) -> bool:
        """The owner's edit of every setting of a link. What it spent and counted stays; lifetimes count from
        when the link and its conversation began, as they did, so an edit lengthens or shortens them rather
        than starting them again. An invite that ran out comes back if its new lifetime has not."""
        link = self.link(link_id)
        if link is None or link["state"] == REVOKED:
            return False
        now = self.clock()
        expires = until(link["created_at"], fields["invite_hours"])
        state = WAITING if link["state"] == EXPIRED and not ended(expires, now) else link["state"]
        self._write("BEGIN IMMEDIATE")
        try:
            self._write(
                "UPDATE links SET state = ?, closed_at = CASE WHEN ? = ? THEN NULL ELSE closed_at END, label = ?, topic = ?, mode = ?, briefing = ?, model_path = ?, model_name = ?,"
                " price_in = ?, price_out = ?, approval = ?, usd_limit = ?, turns_per_day = ?, turns_total = ?, invite_expires_at = ?, conversation_hours = ?,"
                " idle_hours = ?, retention_days = ?, guest_tools = ?, file_mb = ?, day_mb = ? WHERE id = ?",
                state, state, WAITING, fields["label"], fields["topic"], fields["mode"], fields["briefing"], fields["model_path"], fields["model_name"],
                float(fields["price_in"]), float(fields["price_out"]), int(bool(fields["approval"])), float(fields["usd_limit"]), int(fields["turns_per_day"]),
                int(fields["turns_total"]), expires, float(fields["conversation_hours"]), float(fields["idle_hours"]), float(fields["retention_days"]),
                fields["guest_tools"], float(fields.get("file_mb", link["file_mb"])), float(fields.get("day_mb", link["day_mb"])), link_id,
            )
            hours = float(fields["conversation_hours"])
            self._write("UPDATE conversations SET expires_at = CASE WHEN ? > 0 THEN created_at + ? ELSE 0 END WHERE link_id = ? AND state != ?", hours, hours * 3600, link_id, CLOSED)
            self._write("COMMIT")
        except BaseException:
            self._write("ROLLBACK")
            raise
        self.record(link_id, "edited")
        return True

    def link(self, link_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM links WHERE id = ?", link_id)

    def link_by_invite(self, invite: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM links WHERE invite_hash = ?", digest(invite))

    def links(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM links ORDER BY created_at DESC")

    def revoke(self, link_id: str) -> bool:
        """Close a link for good, the conversation with it. Every read of the token sees it at once."""
        link = self.link(link_id)
        if link is None or link["state"] == REVOKED:
            return False
        now = self.clock()
        self._write("BEGIN IMMEDIATE")
        try:
            self._write("UPDATE links SET state = ?, closed_at = ? WHERE id = ?", REVOKED, now, link_id)
            self._close_conversation_of(link_id, "revoked", now)
            self._write("COMMIT")
        except BaseException:
            self._write("ROLLBACK")
            raise
        self.record(link_id, "revoked")
        return True

    def _close_conversation_of(self, link_id: str, reason: str, now: float) -> None:
        """A turn already running stays claimed: its cost is still booked when it ends, its words go nowhere."""
        conversation = self._one("SELECT * FROM conversations WHERE link_id = ?", link_id)
        if conversation is None or conversation["state"] == CLOSED:
            return
        self._write("UPDATE conversations SET state = ?, reason = ?, closed_at = ? WHERE id = ?", CLOSED, reason, now, conversation["id"])
        self._write("UPDATE messages SET state = ? WHERE conversation_id = ? AND state IN (?, ?)", FAILED, conversation["id"], HELD, QUEUED)
        # A task nobody will finish is not left looking as if somebody might.
        self._write(
            f"UPDATE tasks SET state = ?, updated_at = ? WHERE conversation_id = ? AND state IN ({','.join('?' * len(OPEN_TASK_STATES))})",
            CANCELLED, now, conversation["id"], *OPEN_TASK_STATES,
        )

    # -- redeem and approval ----------------------------------------------------------

    def redeem(self, link: sqlite3.Row, address_note: str) -> tuple[str, sqlite3.Row] | None:
        """Spend the invite once: the conversation token and row, or ``None`` when it was spent already."""
        token = new_secret("gt_")
        conversation_id = secrets.token_hex(8)
        now = self.clock()
        self._write("BEGIN IMMEDIATE")
        try:
            # The state is read again inside the transaction, so a race has exactly one winner.
            cursor = self._write("UPDATE links SET state = ? WHERE id = ? AND state = ?", SPENT, link["id"], WAITING)
            if cursor.rowcount != 1:
                self._write("ROLLBACK")
                return None
            state = PENDING if link["approval"] else OPEN
            self._write(
                "INSERT INTO conversations(id, link_id, token_hash, state, created_at, decide_by, expires_at, last_guest_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                conversation_id, link["id"], digest(token), state, now, now + FIRST_CONTACT_HOURS * 3600, until(now, link["conversation_hours"]), now,
            )
            self._write("COMMIT")
        except BaseException:
            self._write("ROLLBACK")
            raise
        self.record(link["id"], "first_contact" if state == PENDING else "opened", address_note)
        row = self.conversation(conversation_id)
        assert row is not None
        return token, row

    def conversation(self, conversation_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM conversations WHERE id = ?", conversation_id)

    def conversation_of_link(self, link_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM conversations WHERE link_id = ?", link_id)

    def conversation_by_token(self, token: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM conversations WHERE token_hash = ?", digest(token))

    def admit(self, conversation_id: str) -> bool:
        """The owner's yes. What the guest already sent goes into the queue; nothing was spent before now."""
        conversation = self.conversation(conversation_id)
        if conversation is None or conversation["state"] != PENDING:
            return False
        now = self.clock()
        self._write("UPDATE conversations SET state = ?, last_guest_at = ? WHERE id = ?", OPEN, now, conversation_id)
        self._write("UPDATE messages SET state = ? WHERE conversation_id = ? AND state = ?", QUEUED, conversation_id, HELD)
        self.record(conversation["link_id"], "admitted")
        return True

    def refuse(self, conversation_id: str, reason: str = "refused") -> bool:
        conversation = self.conversation(conversation_id)
        if conversation is None or conversation["state"] != PENDING:
            return False
        self._close_conversation_of(conversation["link_id"], reason, self.clock())
        self.record(conversation["link_id"], reason)
        return True

    def end(self, conversation: sqlite3.Row) -> None:
        self._close_conversation_of(conversation["link_id"], "ended", self.clock())
        self.record(conversation["link_id"], "ended")

    # -- messages ---------------------------------------------------------------------

    def add_guest_message(self, conversation: sqlite3.Row, text: str, *, kind: str = TEXT, task_id: str = "", data: dict[str, Any] | None = None) -> int:
        now = self.clock()
        state = HELD if conversation["state"] == PENDING else QUEUED
        cursor = self._write(
            "INSERT INTO messages(conversation_id, role, text, at, state, kind, task_id, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            conversation["id"], GUEST, text, now, state, kind, task_id, json.dumps(data) if data else "",
        )
        self._write("UPDATE conversations SET last_guest_at = ? WHERE id = ?", now, conversation["id"])
        return int(cursor.lastrowid or 0)

    def add_reply(self, conversation_id: str, role: str, text: str, *, kind: str = TEXT, task_id: str = "", data: dict[str, Any] | None = None) -> int:
        cursor = self._write(
            "INSERT INTO messages(conversation_id, role, text, at, state, kind, task_id, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            conversation_id, role, text, self.clock(), SENT, kind, task_id, json.dumps(data) if data else "",
        )
        return int(cursor.lastrowid or 0)

    def add_resume(self, conversation_id: str, task_id: str, data: dict[str, Any]) -> int:
        """A turn the host asks for to carry the operator's answer on: queued as a guest's message is, so it is
        counted, bounded and answered in turn, and kept from the guest's own reads by its role."""
        cursor = self._write(
            "INSERT INTO messages(conversation_id, role, text, at, state, kind, task_id, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            conversation_id, OWNER, "", self.clock(), QUEUED, TEXT, task_id, json.dumps(data),
        )
        return int(cursor.lastrowid or 0)

    def held_count(self, conversation_id: str) -> int:
        row = self._one("SELECT count(*) c FROM messages WHERE conversation_id = ? AND state = ?", conversation_id, HELD)
        return int(row["c"]) if row else 0

    def replies_after(self, conversation_id: str, after: int) -> list[sqlite3.Row]:
        """What the guest has to read. Their own words are not echoed, so ``after`` is a plain cursor."""
        return self._all(
            "SELECT id, role, text, at, kind, task_id, data FROM messages WHERE conversation_id = ? AND id > ? AND role NOT IN (?, ?) ORDER BY id LIMIT 50",
            conversation_id, after, GUEST, OWNER,
        )

    def transcript(self, conversation_id: str, limit: int = 500) -> list[sqlite3.Row]:
        return list(reversed(self._all("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?", conversation_id, limit)))

    def oldest_unanswered(self, conversation_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM messages WHERE conversation_id = ? AND state IN (?, ?) ORDER BY id LIMIT 1", conversation_id, QUEUED, CLAIMED)

    def message(self, message_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM messages WHERE id = ?", message_id)

    def next_queued(self, mode: str) -> sqlite3.Row | None:
        """The oldest guest message of ``mode`` nobody is answering, one conversation at a time."""
        return self._one(
            "SELECT m.* FROM messages m JOIN conversations c ON c.id = m.conversation_id JOIN links l ON l.id = c.link_id"
            " WHERE m.state = ? AND c.state = ? AND l.mode = ?"
            " AND NOT EXISTS (SELECT 1 FROM messages b WHERE b.conversation_id = m.conversation_id AND b.state = ?) ORDER BY m.id LIMIT 1",
            QUEUED, OPEN, mode, CLAIMED,
        )

    def claimed_of(self, mode: str) -> list[sqlite3.Row]:
        return self._all(
            "SELECT m.* FROM messages m JOIN conversations c ON c.id = m.conversation_id JOIN links l ON l.id = c.link_id WHERE m.state = ? AND l.mode = ? ORDER BY m.id",
            CLAIMED, mode,
        )

    def unacknowledged(self, mode: str) -> list[sqlite3.Row]:
        """Handed out and never confirmed: the answer to the feed may have been lost on the way."""
        return [m for m in self.claimed_of(mode) if not m["acknowledged"]]

    def acknowledge(self, message_id: int) -> None:
        self._write("UPDATE messages SET acknowledged = 1 WHERE id = ?", message_id)

    def write_off(self, message_id: int) -> None:
        """A turn nobody will finish: it may have cost anything up to what was put aside, so it costs that."""
        message = self.message(message_id)
        if message is not None and message["state"] == CLAIMED:
            self.settle(message_id, cost=float(message["reserved_usd"]), state=FAILED)

    # -- money ------------------------------------------------------------------------

    def _day(self, now: float | None = None) -> str:
        return datetime.fromtimestamp(self.clock() if now is None else now, UTC).strftime("%Y-%m-%d")

    def _tomorrow(self) -> float:
        now = self.clock()
        return now - now % 86400 + 86400

    def spend_today(self) -> dict[str, float]:
        row = self._one("SELECT usd, reserved FROM spend_days WHERE day = ?", self._day())
        return {"usd": float(row["usd"]), "reserved": float(row["reserved"])} if row else {"usd": 0.0, "reserved": 0.0}

    def limit_reached(self, link: sqlite3.Row, estimate: float = 0.0) -> Refusal | None:
        """The first limit a turn of ``estimate`` dollars would pass. Asked before anything is spent.

        A direct link has no price of its own, its turn runs on the chat's model and is priced only
        afterwards, so it counts as priced: a free stand-in model is the only thing the dollars skip.
        A limit of :data:`NO_LIMIT` is skipped too: the owner chose not to count it.
        """
        priced = link["mode"] == DIRECT or float(link["price_in"]) + float(link["price_out"]) > 0
        if priced and float(link["usd_limit"]) != NO_LIMIT:
            spent = float(link["spent_usd"]) + float(link["reserved_usd"])
            if spent >= float(link["usd_limit"]) or spent + estimate > float(link["usd_limit"]):
                return Refusal("usd_per_link", None)
        if int(link["turns_total"]) != NO_LIMIT and int(link["turns_used"]) >= int(link["turns_total"]):
            return Refusal("turns_total", None)
        today = int(link["turns_today"]) if link["turns_day"] == self._day() else 0
        if int(link["turns_per_day"]) != NO_LIMIT and today >= int(link["turns_per_day"]):
            return Refusal("turns_per_day", self._tomorrow())
        if priced and self.usd_per_day() != NO_LIMIT:
            day = self.spend_today()
            total = day["usd"] + day["reserved"]
            if total >= self.usd_per_day() or total + estimate > self.usd_per_day():
                return Refusal("usd_per_day", self._tomorrow())
        return None

    def allowance(self, link: sqlite3.Row) -> float | None:
        """What one more turn of this link may spend before a limit: a direct turn's own cap. ``None`` when
        neither the link nor the day has a limit in dollars."""
        day = self.spend_today()
        left = []
        if float(link["usd_limit"]) != NO_LIMIT:
            left.append(float(link["usd_limit"]) - float(link["spent_usd"]) - float(link["reserved_usd"]))
        if self.usd_per_day() != NO_LIMIT:
            left.append(self.usd_per_day() - day["usd"] - day["reserved"])
        return max(0.0, min(left)) if left else None

    def reserve_turn(self, message_id: int, link: sqlite3.Row, ceiling: float | None) -> tuple[Refusal | None, float | None]:
        """Put aside what a direct turn may spend: ``ceiling``, or what is left if that is less.

        Atomic, and what it takes shrinks the next turn's share, so turns running at once cannot each
        be told they have the whole of what is left. ``None`` for a share is a turn nothing bounds in
        dollars: it is counted, nothing is put aside, and the host holds it to its own cap on a run.
        """
        self._write("BEGIN IMMEDIATE")
        try:
            fresh = self.link(link["id"])
            assert fresh is not None
            left = self.allowance(fresh)
            share = left if ceiling is None else ceiling if left is None else min(ceiling, left)
            refusal = self.limit_reached(fresh, share or 0.0)
            if refusal is not None:
                self._write("ROLLBACK")
                return refusal, 0.0
            self._take(link["id"], message_id, share or 0.0)
            self._write("COMMIT")
        except BaseException:
            self._write("ROLLBACK")
            raise
        return None, share

    def _take(self, link_id: str, message_id: int, amount: float) -> None:
        day = self._day()
        self._write(
            "UPDATE links SET reserved_usd = reserved_usd + ?, turns_used = turns_used + 1,"
            " turns_today = CASE WHEN turns_day = ? THEN turns_today + 1 ELSE 1 END, turns_day = ? WHERE id = ?",
            amount, day, day, link_id,
        )
        self._write("INSERT INTO spend_days(day, reserved) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET reserved = reserved + excluded.reserved", day, amount)
        self._write("UPDATE messages SET state = ?, reserved_usd = ? WHERE id = ?", CLAIMED, amount, message_id)

    def reserve(self, message_id: int, link: sqlite3.Row, estimate: float) -> Refusal | None:
        """Count a turn and put its worst-case cost aside, or say which limit stops it.

        In one transaction with the check: two conversations reserving at once must not each see the
        last of the day's money.
        """
        self._write("BEGIN IMMEDIATE")
        try:
            fresh = self.link(link["id"])
            assert fresh is not None
            refusal = self.limit_reached(fresh, estimate)
            if refusal is not None:
                self._write("ROLLBACK")
                return refusal
            self._take(link["id"], message_id, estimate)
            self._write("COMMIT")
        except BaseException:
            self._write("ROLLBACK")
            raise
        return None

    def _refund(self, link_id: str, amount: float) -> None:
        if amount:
            self._write("UPDATE links SET reserved_usd = max(0, reserved_usd - ?) WHERE id = ?", amount, link_id)
            self._write("UPDATE spend_days SET reserved = max(0, reserved - ?) WHERE day = ?", amount, self._day())

    def settle(self, message_id: int, *, cost: float, state: str = ANSWERED) -> None:
        """A turn is over: its reservation is replaced by what it really cost."""
        message = self.message(message_id)
        if message is None or message["state"] != CLAIMED:
            return
        conversation = self.conversation(message["conversation_id"])
        assert conversation is not None
        link_id = conversation["link_id"]
        day = self._day()
        self._write("BEGIN IMMEDIATE")
        try:
            self._refund(link_id, float(message["reserved_usd"]))
            self._write("UPDATE links SET spent_usd = spent_usd + ? WHERE id = ?", cost, link_id)
            self._write("INSERT INTO spend_days(day, usd) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET usd = usd + excluded.usd", day, cost)
            self._write("UPDATE messages SET state = ?, reserved_usd = 0, cost_usd = ? WHERE id = ?", state, cost, message_id)
            self._write("COMMIT")
        except BaseException:
            self._write("ROLLBACK")
            raise

    def give_back(self, message_id: int) -> None:
        """A turn that never ran: its reservation and its count go back. One that ran keeps its count,
        whatever it cost, or a free model's turns could be had for ever by ending without a word."""
        message = self.message(message_id)
        if message is None or message["state"] != CLAIMED:
            return
        conversation = self.conversation(message["conversation_id"])
        assert conversation is not None
        self._refund(conversation["link_id"], float(message["reserved_usd"]))
        self._write("UPDATE links SET turns_used = max(0, turns_used - 1), turns_today = max(0, turns_today - 1) WHERE id = ?", conversation["link_id"])
        self._write("UPDATE messages SET state = ?, reserved_usd = 0 WHERE id = ?", FAILED, message_id)

    def drop(self, message_id: int) -> None:
        self._write("UPDATE messages SET state = ? WHERE id = ? AND state IN (?, ?)", FAILED, message_id, QUEUED, HELD)

    # -- tasks ------------------------------------------------------------------------

    def create_task(self, conversation_id: str, text: str) -> str:
        task_id = "t_" + secrets.token_hex(6)
        now = self.clock()
        self._write("INSERT INTO tasks(id, conversation_id, state, text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)", task_id, conversation_id, ACCEPTED, text, now, now)
        return task_id

    def task(self, task_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM tasks WHERE id = ?", task_id)

    def tasks_of(self, conversation_id: str) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM tasks WHERE conversation_id = ? ORDER BY created_at", conversation_id)

    def move_task(self, task_id: str, state: str) -> bool:
        """A task's next state. Nothing moves a task that is over: a cancel that raced a result keeps the cancel."""
        cursor = self._write(
            f"UPDATE tasks SET state = ?, updated_at = ? WHERE id = ? AND state IN ({','.join('?' * len(OPEN_TASK_STATES))})",
            state, self.clock(), task_id, *OPEN_TASK_STATES,
        )
        return cursor.rowcount == 1

    def queued_of_task(self, task_id: str) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM messages WHERE task_id = ? AND state IN (?, ?, ?)", task_id, HELD, QUEUED, CLAIMED)

    # -- files ------------------------------------------------------------------------

    def create_file(self, conversation_id: str, origin: str, name: str, size: int, sha256: str) -> str:
        file_id = "f_" + secrets.token_hex(8)
        now = self.clock()
        self._write(
            "INSERT INTO files(id, conversation_id, origin, name, size, sha256, state, created_at, touched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            file_id, conversation_id, origin, name, int(size), sha256, UPLOADING, now, now,
        )
        return file_id

    def file(self, file_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM files WHERE id = ?", file_id)

    def files_of(self, conversation_id: str) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM files WHERE conversation_id = ? ORDER BY created_at", conversation_id)

    def file_received(self, file_id: str, received: int, state: str = UPLOADING) -> None:
        self._write("UPDATE files SET received = ?, state = ?, touched_at = ? WHERE id = ?", received, state, self.clock(), file_id)

    def file_gone(self, file_id: str) -> None:
        self._write("UPDATE files SET state = ? WHERE id = ?", GONE, file_id)

    def file_fetched(self, file_id: str) -> None:
        self._write("UPDATE files SET fetched = 1, state = ? WHERE id = ?", GONE, file_id)

    def bytes_today(self, link_id: str) -> int:
        """What files took on this link since midnight UTC, both ways and counted when announced: a day's
        limit that counted only finished files could be passed by starting many at once."""
        now = self.clock()
        row = self._one(
            "SELECT coalesce(sum(f.size), 0) n FROM files f JOIN conversations c ON c.id = f.conversation_id WHERE c.link_id = ? AND f.created_at >= ?",
            link_id, now - now % 86400,
        )
        return int(row["n"]) if row else 0

    def bytes_held(self) -> int:
        """What every body on the door's disk may grow to: files arriving count at their announced size."""
        row = self._one("SELECT coalesce(sum(size), 0) n FROM files WHERE state IN (?, ?)", UPLOADING, READY)
        return int(row["n"]) if row else 0

    def files_to_forget(self, *, stalled_before: float, kept_before: float) -> list[sqlite3.Row]:
        """Bodies the door no longer keeps: an upload nobody finished, and a file past its time on this disk."""
        return self._all(
            "SELECT * FROM files WHERE (state = ? AND touched_at < ?) OR (state = ? AND created_at < ?)", UPLOADING, stalled_before, READY, kept_before,
        )

    def file_ids(self) -> set[str]:
        return {str(row["id"]) for row in self._all("SELECT id FROM files WHERE state != ?", GONE)}

    # -- the record the owner reads ---------------------------------------------------

    def record(self, link_id: str, kind: str, detail: str = "") -> int:
        cursor = self._write("INSERT INTO events(link_id, kind, at, detail) VALUES (?, ?, ?, ?)", link_id, kind, self.clock(), detail)
        return int(cursor.lastrowid or 0)

    def events_after(self, after: int, limit: int = 100) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", after, limit)

    def events_of(self, link_id: str, limit: int = 100) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM events WHERE link_id = ? ORDER BY id DESC LIMIT ?", link_id, limit)

    def last_event_id(self) -> int:
        row = self._one("SELECT max(id) m FROM events")
        return int(row["m"] or 0) if row else 0

    def client_of(self, link_id: str) -> str:
        """What the guest said it is at the redeem, or nothing. A claim, never a fact: see ``Mailbox.redeem``."""
        row = self._one("SELECT detail FROM events WHERE link_id = ? AND kind = 'client' ORDER BY id DESC LIMIT 1", link_id)
        return str(row["detail"]) if row is not None else ""

    def refused_redeems(self, link_id: str) -> int:
        row = self._one("SELECT count(*) c FROM events WHERE link_id = ? AND kind = 'redeem_refused'", link_id)
        return int(row["c"]) if row else 0

    # -- time passing -----------------------------------------------------------------

    def sweep(self) -> None:
        """What the clock closes, and what the retention period deletes.

        A conversation is not relied on to end itself: about half the agents in the trial never called
        ``end``, and one with nothing left to do simply stops. Only a guest's message counts as activity
        here, so a client left polling forever does not keep a conversation alive.
        """
        now = self.clock()
        for link in self._all("SELECT id FROM links WHERE state = ? AND invite_expires_at > 0 AND invite_expires_at < ?", WAITING, now):
            self._write("UPDATE links SET state = ?, closed_at = ? WHERE id = ?", EXPIRED, now, link["id"])
            self.record(link["id"], "invite_expired")
        for conversation in self._all("SELECT id, link_id FROM conversations WHERE state = ? AND decide_by < ?", PENDING, now):
            self._close_conversation_of(conversation["link_id"], "approval_timeout", now)
            self.record(conversation["link_id"], "approval_timeout")
        rows = self._all(
            "SELECT c.id, c.link_id, c.expires_at, c.last_guest_at, l.idle_hours FROM conversations c JOIN links l ON l.id = c.link_id WHERE c.state = ?", OPEN,
        )
        for row in rows:
            idle = float(row["idle_hours"]) and row["last_guest_at"] + float(row["idle_hours"]) * 3600 < now
            reason = "expired" if ended(row["expires_at"], now) else "idle" if idle else ""
            if reason:
                self._close_conversation_of(row["link_id"], reason, now)
                self.record(row["link_id"], reason)
        for link in self._all("SELECT l.id, l.retention_days, l.closed_at, c.closed_at AS talked FROM links l LEFT JOIN conversations c ON c.link_id = l.id WHERE l.state != ?", WAITING):
            closed = link["talked"] or link["closed_at"]
            if closed is not None and closed + float(link["retention_days"]) * 86400 < now:
                self._write("DELETE FROM links WHERE id = ?", link["id"])
        self._write("DELETE FROM spend_days WHERE day < ?", self._day(now - 40 * 86400))


FIRST_CONTACT_HOURS = 24.0
"""How long a first contact waits for the owner. Past it the answer is no: silence must never admit."""


def rows(items: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(item) for item in items]


__all__ = [
    "ACCEPTED", "AGENT", "ANSWERED", "CANCELLED", "CLAIMED", "CLOSED", "DEFAULT_DAY_MB", "DEFAULT_FILE_MB", "DIRECT", "DONE", "DOOR", "EXPIRED", "FAILED", "FILE",
    "FIRST_CONTACT_HOURS", "GONE", "GUEST", "HELD", "KINDS", "NEEDS_INPUT", "NO_LIMIT", "OPEN", "OPEN_TASK_STATES", "OWNER", "PENDING", "QUESTION", "QUEUED",
    "READY", "REFUSED_TASK", "REVOKED", "SCHEMA_VERSION", "SENT", "SPENT", "STAND_IN", "STATUS", "TASK", "TASK_STATES", "TEXT", "UPLOADING", "WAITING", "WORKING",
    "Refusal", "Store", "digest", "ended", "migrate", "new_secret", "rows", "until",
]
