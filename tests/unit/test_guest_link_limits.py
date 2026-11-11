"""A link with no limits, and a link changed after it was made, at the door itself.

0 is no limit for every number and lifetime of a link and for the day's budget: the app offers it as a
choice of its own, and the door must then never stop a guest on a limit nobody set. An edit changes the
settings in place: the invite and the conversation stay, and so does what was spent and counted.
"""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

from typing import Any

from tests.unit.test_guest_door import CONTROL, Rig, direct_link, rig  # noqa: F401 — the fixture is used by name

EDITABLE = (
    "label", "topic", "mode", "briefing", "model_path", "model_name", "guest_tools", "price_in", "price_out", "approval", "usd_limit", "turns_per_day", "turns_total",
    "conversation_hours", "idle_hours", "retention_days",
)


def settings_of(rig: Rig, link_id: str) -> dict[str, Any]:
    link = rig.store.link(link_id)
    return {name: link[name] for name in EDITABLE} | {"invite_hours": 72}


def test_a_link_with_no_limits_is_never_stopped_by_one(rig: Rig) -> None:
    rig.control.put("/control/settings", json={"usd_per_day": 0}, headers=CONTROL)
    link_id, invite = direct_link(rig, usd_limit=0, turns_per_day=0, turns_total=0, invite_hours=0, conversation_hours=0, idle_hours=0)
    rig.clock.now += 400 * 86400
    rig.store.sweep()
    assert rig.store.link(link_id)["state"] == "waiting", "an invite with no lifetime does not run out"
    token = rig.redeem(invite)
    rig.store.conn.execute("UPDATE links SET spent_usd = 5000, turns_used = 999999, turns_today = 99999, turns_day = ? WHERE id = ?", (rig.store._day(), link_id))
    rig.say(token, "port?")
    offered = rig.control.get("/control/feed?after=0&wait=0", headers=CONTROL).json()["deliveries"]
    assert [d["text"] for d in offered] == ["port?"]
    assert offered[0]["allowance_usd"] is None, "nothing bounds the turn, so the door puts nothing aside and says so"
    rig.clock.now += 400 * 86400
    rig.store.sweep()
    assert rig.read(token).status_code == 200, "neither a lifetime nor idleness closes the conversation"
    assert rig.public.get("/a/status", headers={"Authorization": f"Bearer {token}"}).json()["turns_left_today"] is None


def test_a_link_with_no_limit_of_its_own_still_keeps_to_the_day(rig: Rig) -> None:
    _, invite = direct_link(rig, usd_limit=0)
    rig.control.put("/control/settings", json={"usd_per_day": 0.1}, headers=CONTROL)
    rig.say(rig.redeem(invite), "port?")
    assert rig.control.get("/control/feed?after=0&wait=0", headers=CONTROL).json()["deliveries"][0]["allowance_usd"] == 0.1


def test_a_stand_in_with_no_dollar_limit_answers_past_any_spend(rig: Rig) -> None:
    link_id, invite = rig.link(usd_limit=0)
    rig.store.conn.execute("UPDATE links SET spent_usd = 9999 WHERE id = ?", (link_id,))
    assert rig.store.limit_reached(rig.store.link(link_id), 50.0).limit == "usd_per_day", "the day's budget still holds"
    rig.control.put("/control/settings", json={"usd_per_day": 0}, headers=CONTROL)
    assert rig.store.limit_reached(rig.store.link(link_id), 50.0) is None


def test_every_setting_of_a_link_can_be_changed_and_its_counts_stay(rig: Rig) -> None:
    link_id, invite = rig.link(invite_hours=1)
    rig.clock.now += 3601
    rig.store.sweep()
    assert rig.store.link(link_id)["state"] == "expired"
    rig.store.conn.execute("UPDATE links SET spent_usd = 0.3, turns_used = 4 WHERE id = ?", (link_id,))
    fields = {
        "label": "renamed", "topic": "new topic", "mode": "direct", "briefing": "", "model_path": "", "model_name": "", "guest_tools": "trusted", "price_in": 0, "price_out": 0,
        "approval": False, "usd_limit": 0, "turns_per_day": 0, "turns_total": 0, "invite_hours": 0, "conversation_hours": 0, "idle_hours": 0, "retention_days": 7,
    }
    changed = rig.control.put(f"/control/links/{link_id}", json=fields, headers=CONTROL)
    assert changed.status_code == 200, changed.text
    link = rig.store.link(link_id)
    assert (link["label"], link["topic"], link["mode"], link["guest_tools"], link["usd_limit"], link["invite_expires_at"]) == ("renamed", "new topic", "direct", "trusted", 0, 0)
    assert (link["spent_usd"], link["turns_used"]) == (0.3, 4), "an edit is not a fresh start of the counts"
    assert link["state"] == "waiting", "an invite that ran out comes back when its new lifetime has not"
    assert rig.public.post(f"/a/{invite}/redeem").status_code == 201
    assert "edited" in rig.events(link_id)
    assert rig.control.put(f"/control/links/{link_id}", json={**fields, "turns_per_day": -1}, headers=CONTROL).status_code == 422


def test_an_edit_moves_an_open_conversations_end_from_when_it_began(rig: Rig) -> None:
    link_id, invite = rig.link(conversation_hours=1)
    token = rig.redeem(invite)
    began = rig.store.conversation_of_link(link_id)["created_at"]
    assert rig.control.put(f"/control/links/{link_id}", json={**settings_of(rig, link_id), "conversation_hours": 5}, headers=CONTROL).status_code == 200
    assert rig.store.conversation_of_link(link_id)["expires_at"] == began + 5 * 3600
    rig.clock.now += 3 * 3600
    rig.store.sweep()
    assert rig.read(token).status_code == 200


def test_a_revoked_link_is_not_brought_back_by_an_edit(rig: Rig) -> None:
    link_id, _ = rig.link()
    rig.control.post(f"/control/links/{link_id}/revoke", headers=CONTROL)
    rig.control.put(f"/control/links/{link_id}", json=settings_of(rig, link_id), headers=CONTROL)
    assert rig.store.link(link_id)["state"] == "revoked"
