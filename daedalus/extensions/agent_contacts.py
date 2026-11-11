"""Standing contacts: two installations that may each write to the other whenever they need to.

A link is spent once and its conversation ends. A contact is the pair made to last: a link of this side's door that
only the other installation holds, and a conversation of ours at theirs. Whoever has something to say writes into
the other's door, so either side starts a conversation, and each side decides on its own what the other may do here:
the level and the limits of its link for them, which the operator changes like any link's.

**How a pair is made.** It starts in a conversation that already exists, a link either side gave. One operator offers
a contact from it: this side makes the link it will give them and sends its address, its name and its key, signed, in
a ``contact`` message. Nothing is run on the other side: their host shows the operator the offer and the fingerprint
of the key it came with. Taking it up, their host redeems our link, signing the redeem, makes a link of its own and
sends its address back the same way over the new conversation; this side checks that the answer is signed by the key
that redeemed, and redeems theirs. Each side is then pinned to the key the other proved it holds.

**Keys.** Every message of a contact is signed (:mod:`daedalus.host.link_identity`) and checked against the pinned
key; a message signed by another key, or by none, is refused and the operator is told at once, because that is what
a stolen token in somebody else's hands looks like. Whether the key belongs to who the other side says it does is for
the two operators to settle by reading the fingerprints to each other.

**Ending it.** Either side breaks a contact: its link for the other is revoked and its conversation at theirs is ended,
and the other side, seeing either, breaks it too. A contact's conversation is never ended for being idle: it is the
point of one that it waits.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from daedalus import public_address
from daedalus.extensions.guest_door import DoorRefused, DoorUnavailable, bridge_of, chat_link, identity_of
from daedalus.extensions.notifications import Draft
from daedalus.host import link_identity
from daedalus.host.notify_text import render
from daedalus.security.untrusted import inert

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

EXTENSION_KEY = "agent_contacts"
NAME_KV = "agent_contacts.self_name"
DEFAULT_SELF_NAME = "Daedalus"
OFFERED, INCOMING, ACCEPTING, ACTIVE, BROKEN = "offered", "incoming", "accepting", "active", "broken"
LIVE = (OFFERED, ACCEPTING, ACTIVE)
"""A contact whose link and conversation this side keeps going. ``incoming`` has neither yet: it is an offer."""
LEVELS = ("read", "all", "trusted")
"""The tools of this side's link for a contact, as for any direct link. A stand-in is set on the link itself, in the
chat's links, since it needs a model and a briefing; a contact's own row offers the direct levels."""
NAME_CHARS = 80
INVITE_HOURS = 72.0
"""How long the other side has to take up an offer, or to redeem the link it was answered with."""
LIMITS = {"usd_limit": 1.0, "turns_per_day": 40, "turns_total": 0, "file_mb": 50.0, "day_mb": 500.0}
"""A contact's limits when the operator chose none: what each of its turns here costs is the owner's, so a day's worth.
No lifetime: a contact lasts until somebody breaks it. A trusted contact starts with none at all, as a trusted link does."""
TRUSTED_LIMITS = {"usd_limit": 0.0, "turns_per_day": 0, "turns_total": 0, "file_mb": 0.0, "day_mb": 0.0}
SEEN_KEPT = 512


class ContactError(Exception):
    """Why a contact cannot be offered, taken up or kept, in words the operator reads."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def clean_name(value: Any) -> str:
    return " ".join(inert(str(value or "")).split())[:NAME_CHARS]


class Contacts:
    def __init__(self, app: Application | Any) -> None:
        self.app = app
        self._seen: dict[str, set[str]] = {}
        """Signatures already taken from each contact, against a message carried twice."""

    @property
    def db(self) -> Any:
        return self.app.db

    @property
    def bridge(self) -> Any:
        bridge = bridge_of(self.app)
        if bridge is None or not bridge.client.configured:
            raise ContactError("this installation has no guest door, and a contact needs one on both sides")
        return bridge

    @property
    def talks(self) -> Any:
        from daedalus.extensions.agent_talk import talks_of  # Lazy: agent_talk imports this module for its hooks.

        talks = talks_of(self.app)
        if talks is None:
            raise ContactError("agent links are not available in this installation")
        return talks

    def identity(self) -> link_identity.Identity:
        identity = identity_of(self.app)
        if identity is None:
            raise ContactError("this installation's key for agent links cannot be read")
        return identity

    # -- rows -----------------------------------------------------------------------------

    async def contact(self, contact_id: str) -> Any:
        return await self.db.fetchone("SELECT * FROM link_contacts WHERE id = ?", (contact_id,))

    async def of_link(self, link_id: str) -> Any:
        return await self.db.fetchone("SELECT * FROM link_contacts WHERE link_id = ? AND link_id != '' ORDER BY created_at DESC", (link_id,))

    async def of_talk(self, talk_id: str) -> Any:
        return await self.db.fetchone("SELECT * FROM link_contacts WHERE talk_id = ? AND talk_id != '' ORDER BY created_at DESC", (talk_id,))

    async def every(self) -> list[Any]:
        return list(await self.db.fetchall("SELECT * FROM link_contacts ORDER BY CASE state WHEN 'incoming' THEN 0 WHEN 'broken' THEN 2 ELSE 1 END, last_at DESC"))

    async def _update(self, contact_id: str, **fields: Any) -> None:
        names = ", ".join(f"{name} = ?" for name in fields)
        await self.db.execute(f"UPDATE link_contacts SET {names}, last_at = ? WHERE id = ?", (*fields.values(), _now(), contact_id))

    async def self_name(self) -> str:
        return clean_name(await self.db.kv_get(NAME_KV, DEFAULT_SELF_NAME)) or DEFAULT_SELF_NAME

    async def by_name(self, name: str) -> Any:
        """A live contact by the name the operator knows it by, ignoring case; ``None`` for none or several."""
        wanted = " ".join(name.lower().split())
        found = [row for row in await self.every() if row["state"] in LIVE and " ".join(str(row["name"]).lower().split()) == wanted]
        return found[0] if len(found) == 1 else None

    # -- this side's link for a contact ----------------------------------------------------

    async def _make_link(self, session_id: str, label: str, tools: str) -> tuple[str, str, str]:
        """A link of this door for one installation: no lifetime, no approval of a first contact (the operator
        decided that by offering or accepting), the level asked for, and this side's key with a proof on its card."""
        if tools not in LEVELS:
            raise ContactError("a contact's level is read, all or trusted")
        manager = self.app.manager
        if manager is None or await manager.get_state(session_id) is None:
            raise ContactError("no such chat")
        refused = await manager.guest_ready(session_id, tools)
        if refused:
            raise ContactError(f"this chat cannot answer a contact at that level ({refused})")
        base = public_address.guest_base(self.app.settings)
        if not base:
            raise ContactError("the guest door has no public address, so the other side could not reach this one")
        limits = dict(TRUSTED_LIMITS if tools == "trusted" else LIMITS)
        if limits["usd_limit"] and await manager.guest_price(session_id) is None:
            limits["usd_limit"] = 0.0  # a model nobody can price is held by its turns, as a link on it would be
        fields = {
            "label": label, "topic": "A standing contact.", "mode": "direct", "briefing": "", "model_path": "", "model_name": "", "price_in": 0.0, "price_out": 0.0,
            "guest_tools": tools, "approval": False, "invite_hours": INVITE_HOURS, "conversation_hours": 0.0, "idle_hours": 0.0, "retention_days": 30.0, "max_guests": 1,
            "total_usd": 0.0, **limits,
        }
        bridge = self.bridge
        try:
            made = await bridge.client.call("POST", "/control/links", json=fields)
            link = made["link"]
            await bridge.client.call("PUT", f"/control/links/{link['id']}/identity", json={"key": self.identity().public, "proof": self.identity().door_proof(link["invite_hash"])})
        except (DoorUnavailable, DoorRefused, KeyError) as exc:
            raise ContactError(f"the guest door did not make the link: {exc}") from None
        await bridge.remember_link(link["id"], session_id, label, "direct", tools, float(limits["usd_limit"]), topic=fields["topic"])
        return link["id"], f"{base}/a/{made['invite']}", str(link["invite_hash"])

    async def _drop_link(self, link_id: str) -> None:
        if not link_id:
            return
        bridge = bridge_of(self.app)
        if bridge is None or await bridge.known_link(link_id) is None:
            return
        await bridge.revoked(link_id, door_told=False)
        try:
            await bridge.client.call("POST", f"/control/links/{link_id}/revoke")
            await bridge.revoked(link_id)
        except (DoorUnavailable, DoorRefused):
            logger.warning("the door was out of reach to revoke contact link %s; the next connection does it", link_id)

    # -- offering -------------------------------------------------------------------------

    async def offer(self, *, session_id: str, via: str, name: str, tools: str, text: str = "", as_name: str = "") -> Any:
        """Offer a contact over a conversation that exists: ``talk:<id>``, ours at their door, or ``link:<link>:<conversation>``,
        a guest's at ours. The row is this side's until they answer."""
        kind, _, rest = via.partition(":")
        if kind not in ("talk", "link") or not rest:
            raise ContactError("an offer travels over a conversation: talk:<id> or link:<link>:<conversation>")
        identity = self.identity()
        if as_name.strip():
            await self.db.kv_set(NAME_KV, clean_name(as_name))
        mine = await self.self_name()
        label = clean_name(name) or "contact"
        if kind == "talk":
            talk = await self.talks.talk(rest)
            if talk is None or talk["state"] == "closed":
                raise ContactError("that conversation is over")
            session_id = str(talk["session_id"]) if not session_id else session_id
        else:
            link_id, _, conversation = rest.partition(":")
            known = await self.bridge.known_link(link_id)
            if known is None or known["revoked_at"] or not re.fullmatch(r"[0-9a-f]{16}", conversation):
                raise ContactError("no such conversation of a link")
            session_id = session_id or str(known["session_id"])
        link_id, url, hashed = await self._make_link(session_id, label, tools)
        contact_id = secrets.token_hex(6)
        now = _now()
        await self.db.execute(
            "INSERT INTO link_contacts(id, name, session_id, state, link_id, our_invite_hash, via, created_at, last_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (contact_id, label, session_id, OFFERED, link_id, hashed, via, now, now),
        )
        payload = identity.contact(invite=url, name=mine, answer="offer")
        words = " ".join(text.split())[:600]
        try:
            if kind == "talk":
                await self.talks.post_contact(rest, payload, words)
            else:
                conversation = rest.partition(":")[2]
                await self.bridge.client.call("POST", f"/control/conversations/{conversation}/messages", json={"kind": "contact", "contact": payload, "text": words, "from": "owner"})
        except (ContactError, DoorUnavailable, DoorRefused) as exc:
            await self._drop_link(link_id)
            await self.db.execute("DELETE FROM link_contacts WHERE id = ?", (contact_id,))
            raise ContactError(f"the offer did not reach the other side: {exc}") from None
        return await self.contact(contact_id)

    # -- an offer or an answer that came ------------------------------------------------------

    async def on_door_contact(self, link_id: str, detail: str) -> None:
        """A ``contact`` message a guest of one of our links sent: an offer, or the answer to one of ours. The guest is
        whoever redeemed that link, so the key it signed with must be the key that signed the redeem."""
        try:
            found = json.loads(detail)
            conversation, offer, text = str(found["conversation"]), found["contact"], str(found.get("text") or "")
        except (ValueError, KeyError, TypeError):
            return
        if not re.fullmatch(r"[0-9a-f]{16}", conversation) or not link_identity.check_contact(offer):
            logger.warning("a contact message on link %s was not signed by the key it names; left out", link_id)
            return
        bridge = self.bridge
        if await bridge.known_link(link_id) is None:
            return
        guest = await self._guest_of(link_id, conversation)
        if guest is None or guest["key"] != offer["key"]:
            contact = await self.of_link(link_id)
            await self._mismatch(contact, link_id, "the offer was signed by another key than the redeem")
            return
        contact = await self.of_link(link_id)
        if offer["answer"] == "accept":
            if contact is None or contact["state"] != OFFERED:
                return
            if contact["key"] and contact["key"] != offer["key"]:
                await self._mismatch(contact, link_id, "the answer was signed by another key than the one that redeemed")
                return
            await self._complete(contact, offer)
            return
        if offer["answer"] != "offer" or contact is not None:
            return  # an offer over a contact's own link is nothing new
        row = await bridge.known_link(link_id)
        await self._incoming(str(row["session_id"]), offer, f"link:{link_id}:{conversation}", text, str(row["label"] or ""))

    async def on_talk_contact(self, talk: Any, offer: Any, text: str) -> None:
        """An offer the owner of a door we talk to made us, in a reply. The key is theirs if their card proved it."""
        if not link_identity.check_contact(offer) or offer.get("answer") != "offer":
            return
        if talk["peer_key"] and talk["peer_key"] != offer["key"]:
            await self._mismatch(await self.of_talk(talk["id"]), "", "an offer came signed by another key than the door's own")
            return
        if await self.of_talk(talk["id"]) is not None:
            return
        await self._incoming(str(talk["session_id"]), offer, f"talk:{talk['id']}", text, str(talk["host"]))

    async def _incoming(self, session_id: str, offer: dict[str, Any], via: str, text: str, known_as: str) -> None:
        """An offer that waits for the operator. ``known_as`` is what this side already calls the conversation it came
        over, the link's label or the door's host: the notice says that, never the name the other side chose."""
        known = await self.db.fetchone("SELECT 1 FROM link_contacts WHERE state = ? AND key = ? AND invite = ?", (INCOMING, offer["key"], offer["invite"]))
        if known is not None:
            return
        contact_id = secrets.token_hex(6)
        now = _now()
        await self.db.execute(
            "INSERT INTO link_contacts(id, name, session_id, state, key, invite, via, offer_text, created_at, last_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (contact_id, clean_name(offer.get("name")) or "contact", session_id, INCOMING, offer["key"], str(offer["invite"])[:400], via, inert(text)[:600], now, now),
        )
        await self._tell("offer", session_id, contact_id, name=known_as)

    async def _guest_of(self, link_id: str, conversation: str) -> dict[str, str] | None:
        """Who redeemed ``conversation`` of our link: the key, when its proof over the invite holds."""
        try:
            view = (await self.bridge.client.call("GET", f"/control/links/{link_id}?conversation={conversation}")).get("link") or {}
        except (DoorUnavailable, DoorRefused):
            return None
        found = next((c for c in view.get("conversations") or [] if c.get("id") == conversation), None)
        if found is None:
            return None
        key, proof = str(found.get("client_key") or ""), str(found.get("client_proof") or "")
        if not key or not link_identity.check_redeem(key, proof, str(view.get("invite_hash") or "")):
            return {"key": ""}
        return {"key": key}

    # -- taking an offer up ---------------------------------------------------------------------

    async def accept(self, contact_id: str, *, session_id: str = "", tools: str = "read", name: str = "") -> Any:
        """Take up an offer: our link for them, their link redeemed, and the address of ours sent back over it.
        ``session_id`` empty is a chat of its own for this contact."""
        contact = await self.contact(contact_id)
        if contact is None or contact["state"] != INCOMING:
            raise ContactError("that offer is not waiting")
        identity = self.identity()
        label = clean_name(name) or str(contact["name"])
        manager = self.app.manager
        if not session_id:
            state = await manager.create_session(label, metadata={"telegram_detached": True})
            session_id = state.session.id
        link_id, url, hashed = await self._make_link(session_id, label, tools)
        try:
            talk_id = await self.talks.open_contact(session_id, str(contact["invite"]), str(contact["key"]))
            await self.talks.post_contact(talk_id, identity.contact(invite=url, name=await self.self_name(), answer="accept"), "")
        except ContactError:
            await self._drop_link(link_id)
            raise
        await self._update(contact_id, state=ACCEPTING, name=label, session_id=session_id, link_id=link_id, talk_id=talk_id, our_invite_hash=hashed, invite="")
        return await self.contact(contact_id)

    async def decline(self, contact_id: str) -> None:
        """An offer the operator does not take up: forgotten here. The other side's link runs out unused."""
        await self.db.execute("DELETE FROM link_contacts WHERE id = ? AND state = ?", (contact_id, INCOMING))

    async def _complete(self, contact: Any, answer: dict[str, Any]) -> None:
        """Our offer was taken up: their link redeemed, and the contact pinned to the key that answered."""
        try:
            talk_id = await self.talks.open_contact(str(contact["session_id"]), str(answer["invite"]), str(answer["key"]))
        except ContactError as exc:
            logger.warning("could not take up the answer of contact %s: %s", contact["id"], exc)
            await self._broken(contact, "unreachable", tell=True)
            return
        await self._update(contact["id"], state=ACTIVE, key=answer["key"], talk_id=talk_id, name=str(contact["name"]) or clean_name(answer.get("name")))
        await self._tell("active", str(contact["session_id"]), contact["id"], name=str(contact["name"]))

    async def on_link_redeemed(self, link_id: str, conversation: str) -> None:
        """Somebody redeemed a contact's link of ours: the key it signed the redeem with is the one we pin, or must be
        the one we pinned already."""
        contact = await self.of_link(link_id)
        if contact is None or contact["state"] not in (OFFERED, ACCEPTING):
            return
        guest = await self._guest_of(link_id, conversation)
        key = guest["key"] if guest is not None else ""
        if not key or (contact["key"] and key != contact["key"]):
            await self._mismatch(contact, link_id, "the link was redeemed with another key, or with none")
            return
        if contact["state"] == ACCEPTING:
            await self._update(contact["id"], state=ACTIVE)
            await self._tell("active", str(contact["session_id"]), contact["id"], name=str(contact["name"]))
        elif not contact["key"]:
            await self._update(contact["id"], key=key)

    # -- what crosses a contact ------------------------------------------------------------------

    async def check_delivery(self, delivery: Any) -> str:
        """Why a guest's message on one of our links may not start a turn, or "". Only a contact's link is checked:
        its messages must be signed by the key the contact is pinned to, for this side, and not seen before."""
        contact = await self.of_link(delivery.link_id)
        if contact is None:
            return ""
        if contact["state"] not in LIVE:
            return "closed"
        key = str(contact["key"] or "")
        if not key or delivery.client_key != key:
            await self._mismatch(contact, delivery.link_id, "a message came over a token redeemed with another key")
            return "key"
        signed = delivery.signed
        if not link_identity.check_message(signed, key, text=delivery.text, kind=delivery.kind, task=delivery.task, by=delivery.by, to=self.identity().public, card=delivery.card):
            await self._mismatch(contact, delivery.link_id, "a message was not signed by the contact's key")
            return "signature"
        if not await self._fresh(contact, signed):
            return "replay"
        await self._update(contact["id"], last_in_at=max(int(contact["last_in_at"]), int(signed["at"])))
        return ""

    async def check_reply(self, talk: Any, item: dict[str, Any], *, text: str, kind: str, task: str, by: str, card: dict[str, Any] | None = None) -> str:
        """``ok``, ``bad`` or "" for a reply on a conversation of ours: "" where there is no key to check it by. A contact's
        reply that is not ``ok`` is refused by the caller; another conversation only shows how it went."""
        contact = await self.of_talk(talk["id"])
        key = str(contact["key"]) if contact is not None and contact["key"] else str(talk["peer_key"] or "")
        if not key:
            return ""
        signed = item.get("signed")
        identity = identity_of(self.app)
        if identity is None or not link_identity.check_message(signed, key, text=text, kind=kind, task=task, by=by, to=identity.public, card=card):
            if contact is not None and contact["state"] in LIVE:
                await self._mismatch(contact, "", "a reply was not signed by the contact's key")
            return "bad"
        if contact is not None:
            if not await self._fresh(contact, signed):
                return "bad"
            await self._update(contact["id"], last_in_at=max(int(contact["last_in_at"]), int(signed["at"])))
        return "ok"

    async def _fresh(self, contact: Any, signed: dict[str, Any]) -> bool:
        seen = self._seen.setdefault(contact["id"], set())
        if signed["sig"] in seen or int(signed["at"]) < int(contact["last_in_at"]) - link_identity.REPLAY_SKEW_MS:
            logger.warning("contact %s sent a signed message this side has had before; left out", contact["id"])
            return False
        if len(seen) > SEEN_KEPT:
            seen.clear()
        seen.add(signed["sig"])
        return True

    async def _mismatch(self, contact: Any, link_id: str, why: str) -> None:
        """A contact's traffic under another key: refused by the caller, and the operator hears at once."""
        logger.warning("agent link %s: %s", (contact["id"] if contact is not None else link_id) or "?", why)
        if contact is None:
            return
        await self._update(contact["id"], reason="key")
        await self._tell("key", str(contact["session_id"]), contact["id"], name=str(contact["name"]))

    async def configure(self, contact_id: str, *, name: str | None = None, tools: str | None = None, usd_limit: float | None = None, turns_per_day: int | None = None) -> None:
        """The operator's settings for a contact: its name here, and what its link of ours lets it do and spend. The link is
        edited at the door as any link is, its other settings as they were."""
        contact = await self.contact(contact_id)
        if contact is None:
            raise ContactError("no such contact")
        if name is not None and clean_name(name):
            await self._update(contact_id, name=clean_name(name))
        if tools is None and usd_limit is None and turns_per_day is None:
            return
        if contact["state"] not in LIVE or not contact["link_id"]:
            raise ContactError("this contact has no link of ours to change")
        if tools is not None and tools not in LEVELS:
            raise ContactError("a contact's level is read, all or trusted")
        bridge = self.bridge
        known = await bridge.known_link(str(contact["link_id"]))
        try:
            link = (await bridge.client.call("GET", f"/control/links/{contact['link_id']}")).get("link") or {}
        except (DoorUnavailable, DoorRefused) as exc:
            raise ContactError(str(exc)) from None
        level = tools or (str(known["tools"]) if known is not None else "read")
        if tools is not None:
            refused = await self.app.manager.guest_ready(str(contact["session_id"]), level)
            if refused:
                raise ContactError(f"this chat cannot answer a contact at that level ({refused})")
        invite_hours = max(0.1, (float(link["invite_expires_at"]) - float(link["created_at"])) / 3600) if link.get("invite_expires_at") else 0.0
        fields = {
            name: link[name] for name in (
                "label", "topic", "model_path", "model_name", "approval", "turns_total", "conversation_hours", "idle_hours", "retention_days", "file_mb", "day_mb", "max_guests", "total_usd",
            )
        }
        fields.update(
            mode="direct", briefing="", guest_tools=level, price_in=0.0, price_out=0.0, invite_hours=invite_hours,
            usd_limit=float(link["usd_limit"]) if usd_limit is None else float(usd_limit), turns_per_day=int(link["turns_per_day"]) if turns_per_day is None else int(turns_per_day),
        )
        try:
            await bridge.client.call("PUT", f"/control/links/{contact['link_id']}", json=fields)
        except (DoorUnavailable, DoorRefused) as exc:
            raise ContactError(str(exc)) from None
        await bridge.relabel_link(
            str(contact["link_id"]), label=str(fields["label"]), mode="direct", tools=level, usd_limit=float(fields["usd_limit"]), topic=str(fields["topic"]),
            instructions=str(known["instructions"] or "") if known is not None else "",
        )

    # -- ending -------------------------------------------------------------------------------

    async def break_contact(self, contact_id: str) -> None:
        contact = await self.contact(contact_id)
        if contact is None:
            raise ContactError("no such contact")
        if contact["state"] == INCOMING:
            await self.decline(contact_id)
            return
        await self._broken(contact, "ours")

    async def on_link_closed(self, link_id: str) -> None:
        """The other side ended its conversation at our door: the contact is over for both."""
        contact = await self.of_link(link_id)
        if contact is not None and contact["state"] in LIVE:
            await self._broken(contact, "theirs", tell=True)

    async def on_talk_closed(self, talk_id: str, reason: str) -> None:
        """Our conversation at their door closed: revoked by them, or its token refused. The contact is over."""
        contact = await self.of_talk(talk_id)
        if contact is not None and contact["state"] in LIVE:
            await self._broken(contact, "theirs" if reason in ("revoked", "ended", "token") else reason, tell=True)

    async def _broken(self, contact: Any, reason: str, *, tell: bool = False) -> None:
        # Marked first: ending the conversation below comes back here through ``on_talk_closed``.
        await self._update(contact["id"], state=BROKEN, reason=reason, closed_at=_now())
        await self._drop_link(str(contact["link_id"]))
        if contact["talk_id"]:
            try:
                await self.talks.end(str(contact["talk_id"]))
            except ContactError:
                pass
        if tell:
            await self._tell("broken", str(contact["session_id"]), contact["id"], name=str(contact["name"]))

    async def on_session_deleted(self, session_id: str) -> None:
        for contact in await self.db.fetchall("SELECT * FROM link_contacts WHERE session_id = ? AND state IN (?, ?, ?)", (session_id, *LIVE)):
            await self._broken(contact, "ours")

    # -- what the operator and the agent are shown -----------------------------------------------------

    async def describe(self, contact: Any, links: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """A contact for the app. The address of their link is not a field of this: it is a credential until redeemed."""
        talks = talks_of_app(self.app)
        talk = await talks.talk(str(contact["talk_id"])) if talks is not None and contact["talk_id"] else None
        link = (links or {}).get(str(contact["link_id"])) or {}
        bridge = bridge_of(self.app)
        known = await bridge.known_link(str(contact["link_id"])) if bridge is not None and contact["link_id"] else None
        spent = float(link.get("spent_usd") or 0.0) + (await talks.spent(talk) if talks is not None and talk is not None else 0.0)
        last = max([str(contact["last_at"]), str(talk["last_at"]) if talk is not None else ""])
        return {
            "id": contact["id"], "name": contact["name"], "state": contact["state"], "reason": contact["reason"], "session_id": contact["session_id"],
            "fingerprint": link_identity.fingerprint(str(contact["key"])), "link_id": contact["link_id"], "talk_id": contact["talk_id"], "offer_text": contact["offer_text"],
            "level": ("stand_in" if known["mode"] == "stand_in" else str(known["tools"])) if known is not None else "", "usd_limit": float(link.get("usd_limit") or 0.0),
            "turns_per_day": int(link.get("turns_per_day") or 0), "spent_usd": round(spent, 6), "created_at": contact["created_at"], "last_at": last,
            "via": str(contact["via"]).partition(":")[0],
        }

    async def turn_notes(self) -> str:
        """The contacts the agent may write to, by name: in an operator's turn, in the turn context, never a word of theirs
        but the name the operator knows them by."""
        live = [row for row in await self.every() if row["state"] == ACTIVE]
        if not live:
            return ""
        names = ", ".join(f"{row['name']!r}" for row in live[:20])
        return f"- Standing contacts with other agents: {names}. Write to one with AgentTalk and contact=<name> when the operator's message calls for it; action='contacts' lists them."

    async def listed(self) -> str:
        rows = await self.every()
        if not rows:
            return "There are no contacts. The operator makes one from a conversation with another agent, in the Agents tab."
        lines = []
        for row in rows:
            fingerprint = link_identity.fingerprint(str(row["key"]))
            lines.append(f"- {row['name']}: {row['state']}" + (f", key {fingerprint}" if fingerprint else ""))
        return "\n".join(lines)

    async def _tell(self, key: str, session_id: str, contact_id: str, *, name: str = "") -> None:
        """A notice in this code's words and the operator's own name for the contact: the other side chose the name an
        offer came with, and a stranger's words do not go to Telegram."""
        notifications = getattr(self.app, "notifications", None)
        if notifications is None:
            return
        lang = notifications.language()
        await notifications.post(Draft(
            "system", render(f"contact.{key}", lang, name=name or "?"), render(f"contact.{key}.body", lang), kind=f"agent_contact_{key}",
            tone="warning" if key in ("key", "broken") else "info", session_id=session_id, link=chat_link(session_id), source="agent_contact",
            dedupe_key=f"agent-contact-{key}:{contact_id}",
        ))


def talks_of_app(app: Any) -> Any:
    from daedalus.extensions.agent_talk import talks_of  # Lazy: agent_talk imports this module for its hooks.

    return talks_of(app)


def contacts_of(app: Application | Any) -> Contacts | None:
    return (getattr(app, "extensions", None) or {}).get(EXTENSION_KEY)


async def install(app: Application) -> list[Any]:
    contacts = Contacts(app)
    app.extensions[EXTENSION_KEY] = contacts
    if app.manager is not None:
        app.manager.delete_hooks.append(contacts.on_session_deleted)
    return []


__all__ = ["ACCEPTING", "ACTIVE", "BROKEN", "EXTENSION_KEY", "INCOMING", "LEVELS", "LIVE", "OFFERED", "ContactError", "Contacts", "contacts_of", "install"]
