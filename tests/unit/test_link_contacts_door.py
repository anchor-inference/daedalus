"""The door's part of standing contacts, several guests on one link, and words an owner wrote themselves.

The door holds no key and checks no signature: it keeps what a host signed, hands it on, and keeps each guest of a
link in a conversation of its own with its own count. These tests are about that much, against the real door with
no network; what a host makes of a signature is in ``test_link_contacts_host``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from guestd.control import ControlDoor
from guestd.limits import Rate
from guestd.mailbox import CONTACTS_PER_CONVERSATION, Mailbox
from guestd.public import PublicDoor
from guestd.store import SCHEMA_VERSION, Store
from starlette.testclient import TestClient

CONTROL = {"authorization": "Bearer control-token"}
KEY = "A" * 43
SIG = "s" * 86


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = Clock()
        self.store = Store(tmp_path / "guest.sqlite", clock=self.clock)
        self.mailbox = Mailbox(self.store, public_base=lambda: "https://agents.example.test", clock=self.clock)
        self.public = TestClient(PublicDoor(self.mailbox, sends=Rate(1000)), base_url="https://agents.example.test")
        self.control = TestClient(ControlDoor(self.store, self.mailbox, "control-token"), base_url="http://guestd")

    def link(self, **overrides: Any) -> tuple[str, str]:
        fields = {
            "label": "a partner", "topic": "Shipping.", "mode": "direct", "briefing": "", "model_path": "", "model_name": "", "guest_tools": "read", "price_in": 0.0,
            "price_out": 0.0, "approval": False, "usd_limit": 1.0, "turns_per_day": 40, "turns_total": 200, "invite_hours": 72, "conversation_hours": 0, "idle_hours": 0,
            "retention_days": 30, **overrides,
        }
        made = self.control.post("/control/links", json=fields, headers=CONTROL)
        assert made.status_code == 200, made.text
        return made.json()["link"]["id"], made.json()["invite"]

    def redeem(self, invite: str, **headers: str) -> TestClient:
        return self.public.post(f"/a/{invite}/redeem", headers=headers)

    def token(self, invite: str) -> str:
        answer = self.redeem(invite)
        assert answer.status_code == 201, answer.text
        return answer.json()["token"]

    def send(self, token: str, body: dict[str, Any]) -> Any:
        return self.public.post("/a/messages", json=body, headers={"Authorization": f"Bearer {token}"})

    def read(self, token: str) -> list[dict[str, Any]]:
        return self.public.get("/a/messages?after=0&wait=0", headers={"Authorization": f"Bearer {token}"}).json()["messages"]

    def feed(self) -> dict[str, Any]:
        return self.control.get("/control/feed?after=0&wait=0", headers=CONTROL).json()

    def answer(self, delivery: dict[str, Any], cost: float, **extra: Any) -> None:
        self.control.post(f"/control/deliveries/{delivery['message_id']}/ack", headers=CONTROL)
        assert self.control.post(f"/control/deliveries/{delivery['message_id']}/reply", json={"ok": True, "text": "done", "cost_usd": cost, **extra}, headers=CONTROL).status_code == 200


@pytest.fixture
def rig(tmp_path: Path) -> Any:
    made = Rig(tmp_path)
    yield made
    made.store.close()


# -- several guests on one link --------------------------------------------------------------------


def test_a_link_for_two_guests_gives_each_its_own_conversation_and_token_and_then_closes(rig: Rig) -> None:
    link_id, invite = rig.link(max_guests=2)
    first, second = rig.token(invite), rig.token(invite)
    assert first != second
    assert rig.redeem(invite).status_code == 410, "the third guest finds the link spent"
    conversations = rig.store.conversations_of(link_id)
    assert len(conversations) == 2 and conversations[0]["id"] != conversations[1]["id"]
    assert rig.store.link(link_id)["state"] == "spent"
    rig.send(first, {"text": "from the first"})
    rig.send(second, {"text": "from the second"})
    deliveries = rig.feed()["deliveries"]
    assert {d["text"]: d["conversation_id"] for d in deliveries} == {"from the first": conversations[0]["id"], "from the second": conversations[1]["id"]}
    for delivery in deliveries:
        rig.answer(delivery, 0.1)
    assert [m["text"] for m in rig.read(first)] == ["done"] and [m["text"] for m in rig.read(second)] == ["done"], "each guest reads only its own conversation"
    listed = rig.control.get(f"/control/links/{link_id}", headers=CONTROL).json()["link"]
    assert [c["id"] for c in listed["conversations"]] == [c["id"] for c in conversations]
    one = rig.control.get(f"/control/links/{link_id}?conversation={conversations[0]['id']}", headers=CONTROL).json()["link"]
    assert [m["text"] for m in one["messages"]] == ["from the first", "done"]


def test_a_link_with_no_limit_on_guests_stays_open_for_the_next_one(rig: Rig) -> None:
    link_id, invite = rig.link(max_guests=0)
    for _ in range(5):
        rig.token(invite)
    assert rig.store.link(link_id)["state"] == "waiting" and rig.store.guests_of(link_id) == 5
    assert rig.public.get(f"/a/{invite}").status_code == 200, "the page stays up while more guests may come"


def test_each_guest_has_the_links_limits_to_itself_and_all_of_them_share_the_total(rig: Rig) -> None:
    link_id, invite = rig.link(max_guests=3, usd_limit=1.0, total_usd=1.5, turns_total=0, turns_per_day=0)
    first, second, third = rig.token(invite), rig.token(invite), rig.token(invite)
    rig.send(first, {"text": "one"})
    rig.answer(rig.feed()["deliveries"][0], 1.0)
    spent = rig.send(first, {"text": "again"})
    assert spent.status_code == 403 and spent.json()["limit"] == "usd_per_link", "the first guest has had its own dollar"
    assert rig.send(second, {"text": "two"}).status_code == 202, "the second guest's own limit is untouched"
    rig.answer(rig.feed()["deliveries"][0], 0.5)
    shared = rig.send(third, {"text": "three"})
    assert shared.status_code == 403 and shared.json()["limit"] == "usd_all_guests", "the three together have spent what the link allows them"
    link = rig.control.get(f"/control/links/{link_id}", headers=CONTROL).json()["link"]
    assert link["spent_usd"] == pytest.approx(1.5) and [c["spent_usd"] for c in link["conversations"]] == pytest.approx([1.0, 0.5, 0.0])


def test_a_guests_turn_count_is_its_own(rig: Rig) -> None:
    _, invite = rig.link(max_guests=2, turns_total=1, usd_limit=0)
    first, second = rig.token(invite), rig.token(invite)
    rig.send(first, {"text": "one"})
    rig.answer(rig.feed()["deliveries"][0], 0.0)
    assert rig.send(first, {"text": "two"}).json()["limit"] == "turns_total"
    assert rig.send(second, {"text": "one"}).status_code == 202


def test_revoking_a_link_closes_every_guests_conversation(rig: Rig) -> None:
    link_id, invite = rig.link(max_guests=2)
    tokens = [rig.token(invite), rig.token(invite)]
    rig.control.post(f"/control/links/{link_id}/revoke", headers=CONTROL)
    for token in tokens:
        assert rig.send(token, {"text": "hi"}).status_code == 410


# -- keys and signatures, carried and not checked ---------------------------------------------------


def test_a_redeem_keeps_the_key_and_proof_a_daedalus_signed_it_with_and_drops_a_malformed_pair(rig: Rig) -> None:
    link_id, invite = rig.link(max_guests=2)
    assert rig.redeem(invite, **{"x-agent-client": "daedalus", "x-agent-key": KEY, "x-agent-proof": f"1800000000000.{SIG}"}).status_code == 201
    assert rig.redeem(invite, **{"x-agent-key": "not a key", "x-agent-proof": "nonsense"}).status_code == 201
    first, second = rig.control.get(f"/control/links/{link_id}", headers=CONTROL).json()["link"]["conversations"]
    assert (first["client"], first["client_key"], first["client_proof"]) == ("daedalus", KEY, f"1800000000000.{SIG}")
    assert (second["client"], second["client_key"], second["client_proof"]) == ("", "", "")


def test_the_card_hands_out_the_owners_key_and_proof_once_the_host_has_set_them(rig: Rig) -> None:
    link_id, invite = rig.link()
    card = rig.public.get(f"/a/{invite}", headers={"accept": "application/json"}).json()
    assert "key" not in card
    assert rig.control.put(f"/control/links/{link_id}/identity", json={"key": KEY, "proof": "bad"}, headers=CONTROL).status_code == 422
    assert rig.control.put(f"/control/links/{link_id}/identity", json={"key": KEY, "proof": SIG}, headers=CONTROL).status_code == 200
    card = rig.public.get(f"/a/{invite}", headers={"accept": "application/json"}).json()
    assert (card["key"], card["proof"]) == (KEY, SIG)


def test_a_signed_message_carries_its_signature_both_ways_and_a_malformed_one_is_left_out(rig: Rig) -> None:
    _, invite = rig.link()
    token = rig.token(invite)
    signed = {"key": KEY, "sig": SIG, "at": 1_800_000_000_123}
    rig.send(token, {"text": "signed", "signed": signed})
    rig.send(token, {"text": "garbled", "signed": {"key": KEY, "sig": "short", "at": 1}})
    first = rig.feed()["deliveries"][0]
    assert first["signed"] == signed
    rig.answer(first, 0.0, signed={"key": KEY, "sig": SIG, "at": 1_800_000_000_456})
    assert rig.read(token)[0]["signed"] == {"key": KEY, "sig": SIG, "at": 1_800_000_000_456}
    assert rig.feed()["deliveries"][0]["signed"] is None


# -- the owner's own words ---------------------------------------------------------------------------


def test_a_message_the_guests_owner_wrote_reaches_the_host_marked_as_theirs(rig: Rig) -> None:
    _, invite = rig.link()
    token = rig.token(invite)
    rig.send(token, {"text": "it is me, not my agent", "from": "owner"})
    rig.send(token, {"text": "an agent pretending", "from": "someone else"})
    first = rig.feed()["deliveries"][0]
    assert first["by"] == "owner"
    rig.answer(first, 0.0)
    assert rig.feed()["deliveries"][0]["by"] == ""


def test_the_owners_own_message_from_this_side_reads_as_from_the_owner(rig: Rig) -> None:
    link_id, invite = rig.link()
    token = rig.token(invite)
    conversation = rig.store.conversation_of_link(link_id)["id"]
    said = rig.control.post(f"/control/conversations/{conversation}/messages", json={"text": "hello, this is the owner", "from": "owner"}, headers=CONTROL)
    assert said.status_code == 200
    assert [(m["from"], m["text"]) for m in rig.read(token)] == [("owner", "hello, this is the owner")]
    view = rig.control.get(f"/control/links/{link_id}", headers=CONTROL).json()["link"]
    assert view["messages"][-1]["by"] == "owner"


# -- contacts ------------------------------------------------------------------------------------------


def offer(answer: str = "offer", **extra: Any) -> dict[str, Any]:
    return {"key": KEY, "name": "Someone's Daedalus", "invite": "https://agents.example.test/a/" + "x" * 43, "answer": answer, "at": 1_800_000_000_000, "sig": SIG, **extra}


def test_an_offer_of_a_contact_is_for_the_owner_and_costs_no_turn(rig: Rig) -> None:
    link_id, invite = rig.link(turns_total=1)
    token = rig.token(invite)
    sent = rig.send(token, {"kind": "contact", "text": "shall we stay in touch?", "contact": offer()})
    assert sent.status_code == 202
    feed = rig.feed()
    assert feed["deliveries"] == [], "no turn answers an offer"
    contact = [e for e in feed["events"] if e["kind"] == "contact"]
    assert len(contact) == 1
    detail = json.loads(contact[0]["detail"])
    assert detail["conversation"] == rig.store.conversation_of_link(link_id)["id"] and detail["contact"]["key"] == KEY and detail["text"] == "shall we stay in touch?"
    assert rig.send(token, {"text": "and a question"}).status_code == 202, "the offer did not use the only turn"


def test_a_malformed_offer_is_refused_and_offers_are_bounded(rig: Rig) -> None:
    _, invite = rig.link()
    token = rig.token(invite)
    assert rig.send(token, {"kind": "contact", "contact": offer(key="short")}).status_code == 400
    assert rig.send(token, {"kind": "contact", "contact": offer(answer="whatever")}).status_code == 400
    for _ in range(CONTACTS_PER_CONVERSATION):
        assert rig.send(token, {"kind": "contact", "contact": offer()}).status_code == 202
    assert rig.send(token, {"kind": "contact", "contact": offer()}).status_code == 429


def test_the_host_can_offer_a_contact_to_its_guest_who_reads_it_as_data(rig: Rig) -> None:
    link_id, invite = rig.link()
    token = rig.token(invite)
    conversation = rig.store.conversation_of_link(link_id)["id"]
    made = rig.control.post(f"/control/conversations/{conversation}/messages", json={"kind": "contact", "contact": offer(), "from": "owner"}, headers=CONTROL)
    assert made.status_code == 200, made.text
    shown = rig.read(token)[0]
    assert shown["kind"] == "contact" and shown["contact"]["key"] == KEY and shown["from"] == "owner"


# -- the door's own file, from before -------------------------------------------------------------------

V2 = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE links (
    id TEXT PRIMARY KEY, invite_hash TEXT NOT NULL UNIQUE, state TEXT NOT NULL, label TEXT NOT NULL, topic TEXT NOT NULL, mode TEXT NOT NULL, briefing TEXT NOT NULL,
    model_path TEXT NOT NULL, model_name TEXT NOT NULL, price_in REAL NOT NULL, price_out REAL NOT NULL, approval INTEGER NOT NULL, usd_limit REAL NOT NULL,
    turns_per_day INTEGER NOT NULL, turns_total INTEGER NOT NULL, invite_expires_at REAL NOT NULL, conversation_hours REAL NOT NULL, idle_hours REAL NOT NULL,
    retention_days REAL NOT NULL, guest_tools TEXT NOT NULL, created_at REAL NOT NULL, closed_at REAL, spent_usd REAL NOT NULL DEFAULT 0, reserved_usd REAL NOT NULL DEFAULT 0,
    turns_used INTEGER NOT NULL DEFAULT 0, turns_today INTEGER NOT NULL DEFAULT 0, turns_day TEXT NOT NULL DEFAULT '', file_mb REAL NOT NULL DEFAULT 50, day_mb REAL NOT NULL DEFAULT 500
);
CREATE TABLE conversations (
    id TEXT PRIMARY KEY, link_id TEXT NOT NULL UNIQUE REFERENCES links(id) ON DELETE CASCADE, token_hash TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, decide_by REAL NOT NULL, expires_at REAL NOT NULL, last_guest_at REAL NOT NULL, closed_at REAL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, role TEXT NOT NULL, text TEXT NOT NULL,
    at REAL NOT NULL, state TEXT NOT NULL, reserved_usd REAL NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, acknowledged INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'text', task_id TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT ''
);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, link_id TEXT NOT NULL REFERENCES links(id) ON DELETE CASCADE, kind TEXT NOT NULL, at REAL NOT NULL, detail TEXT NOT NULL DEFAULT '');
CREATE TABLE files (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, origin TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL,
    sha256 TEXT NOT NULL, received INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, created_at REAL NOT NULL, touched_at REAL NOT NULL, fetched INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, state TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
INSERT INTO links VALUES ('l1', 'h1', 'spent', 'old', 't', 'direct', '', '', '', 0, 0, 0, 2.0, 40, 200, 0, 0, 0, 30, 'read', 1, NULL, 0.75, 0.25, 7, 2, '2027-01-15', 50, 500);
INSERT INTO conversations VALUES ('c1', 'l1', 'th1', 'open', '', 1, 2, 0, 3, NULL);
INSERT INTO messages(conversation_id, role, text, at, state) VALUES ('c1', 'guest', 'kept', 4, 'answered');
INSERT INTO files VALUES ('f_0000000000000001', 'c1', 'guest', 'a.txt', 3, 'x', 3, 'ready', 5, 5, 0);
INSERT INTO tasks VALUES ('t_000000000001', 'c1', 'done', 'task', 6, 6);
INSERT INTO events(link_id, kind, at, detail) VALUES ('l1', 'client', 7, 'daedalus');
PRAGMA user_version = 2;
"""


def test_a_door_file_of_the_version_before_keeps_every_row_and_its_guest_keeps_its_count(tmp_path: Path) -> None:
    path = tmp_path / "guest.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(V2)
    store = Store(path)
    try:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        conversation = store.conversation("c1")
        assert (conversation["spent_usd"], conversation["reserved_usd"], conversation["turns_used"], conversation["client"]) == (0.75, 0.25, 7, "daedalus")
        assert [row[0] for row in store.conn.execute("SELECT count(*) FROM messages UNION ALL SELECT count(*) FROM files UNION ALL SELECT count(*) FROM tasks")] == [1, 1, 1]
        assert store.link("l1")["max_guests"] == 1 and store.link("l1")["total_usd"] == 0
        assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == [] and store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "foreign keys are on again once the rebuild is done"
    finally:
        store.close()
    again = Store(path)
    try:
        assert again.conversation("c1")["turns_used"] == 7, "a second start changes nothing"
        again.conn.execute("DELETE FROM conversations WHERE id = 'c1'")
        assert again.conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0, "the rebuilt table still cascades to its messages"
    finally:
        again.close()
