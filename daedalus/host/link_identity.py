"""This installation's key for agent links: who is on the other side, checked rather than taken on their word.

Each installation has one Ed25519 key pair, made on first use and kept in the secrets directory beside the key that
seals the operator's secrets. That directory is sealed from the agent's own tools (``Settings.sealed_paths``) and is
not in any sandbox a command runs in, so a turn can neither read the key nor sign with it: only this code signs, and
only what the host itself sends.

What is signed, each with a purpose of its own so that a signature for one thing is never one for another:

* ``redeem``: the invite this installation redeems, so the owner of a door sees which key came in.
* ``door``: an invite of this installation's own door, which the door's card hands a guest with the key.
* ``message``: every message this host sends over a link, so a contact pinned to a key can refuse one that is not.
* ``contact``: an offer of a standing contact, or the answer that takes one up.

A signature says that whoever holds the key wrote this. Whose key it is, is what the operators settle by comparing the
short fingerprint both sides show, out of band; until then the app says "unverified" of a key it has never pinned.

A message's signature carries ``at``, in milliseconds, and names the key it is for (``to``): a signed message copied
into another conversation is for somebody else, and the receiving host refuses one older than the newest it has
taken from that contact by more than :data:`REPLAY_SKEW_MS`, and one it has already seen. The door between them keeps
messages for days, so a window on the clock alone would refuse a contact who reads late.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

KEY_FILE = "agent-link-identity.key"
DOMAIN = "daedalus-agent-link-v1"
REPLAY_SKEW_MS = 10 * 60 * 1000
"""How far behind the newest signed message from a contact another may be and still be taken: two messages the host
signed a moment apart can reach the door in the other order."""
FINGERPRINT_GROUPS = 5


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(purpose: str, fields: dict[str, Any]) -> bytes:
    """What is signed: the purpose and the fields as one JSON text with sorted keys, so both sides build the same bytes."""
    return f"{DOMAIN}\n{purpose}\n".encode() + json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def invite_hash(invite: str) -> str:
    """An invite as both sides name it in a signature: its hash, as the door keeps it. A whole address is cut to its code."""
    code = invite.rstrip("/").rsplit("/", 1)[-1]
    return hashlib.sha256(code.encode()).hexdigest()


def fingerprint(key: str) -> str:
    """A key in a form two people can read to each other: five groups of four, from its hash. Empty for no key."""
    if not key:
        return ""
    try:
        digest = hashlib.sha256(unb64(key)).hexdigest().upper()
    except (binascii.Error, ValueError):
        return ""
    return "-".join(digest[i * 4:(i + 1) * 4] for i in range(FINGERPRINT_GROUPS))


def verify(key: str, signature: str, purpose: str, fields: dict[str, Any]) -> bool:
    """Whether ``signature`` is ``key``'s over ``fields`` for ``purpose``. Anything malformed is simply not."""
    if not key or not signature:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(unb64(key)).verify(unb64(signature), canonical(purpose, fields))
    except (InvalidSignature, ValueError, binascii.Error, TypeError):
        return False
    return True


def message_fields(*, text: str, kind: str, task: str, by: str, to: str, at: int, card: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a message's signature covers. Its files are not in it: each is checked against the sha256 it was announced
    with, and the door that carries a file is one of the two sides'. A card is, as its digest, and only where there is
    one: a door could otherwise put a meeting or a summary of its own beside a contact's signed words."""
    fields: dict[str, Any] = {"text": text, "kind": kind or "text", "task": task or "", "by": by or "", "to": to or "", "at": at}
    if card is not None:
        fields["card"] = card_digest(card)
    return fields


def card_digest(card: dict[str, Any]) -> str:
    """A card as a signature names it. Both sides pass the card as ``link_flows.card_view`` keeps it, which is stricter
    than the door's own cleaning, so what the door trims and cleans on the way does not change it."""
    return hashlib.sha256(json.dumps(card, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def contact_fields(*, invite: str, name: str, answer: str, at: int) -> dict[str, Any]:
    return {"invite": invite, "name": name, "answer": answer, "at": at}


def check_redeem(key: str, proof: str, hashed_invite: str) -> bool:
    """A redeem's proof, ``<at>.<signature>``, over the invite's hash."""
    at, _, signature = proof.partition(".")
    return at.isdigit() and verify(key, signature, "redeem", {"invite": hashed_invite, "at": int(at)})


def check_message(signed: Any, expected_key: str, *, text: str, kind: str, task: str, by: str, to: str, card: dict[str, Any] | None = None) -> bool:
    """A message's signature, by the key it must be by. ``to`` is this side's own key: a message signed for somebody
    else is not one for here, wherever it was copied."""
    if not isinstance(signed, dict) or signed.get("key") != expected_key:
        return False
    at = signed.get("at")
    if isinstance(at, bool) or not isinstance(at, int):
        return False
    return verify(expected_key, str(signed.get("sig") or ""), "message", message_fields(text=text, kind=kind, task=task, by=by, to=to, at=at, card=card))


@dataclass(frozen=True, slots=True)
class Identity:
    private: Ed25519PrivateKey

    @property
    def public(self) -> str:
        return b64(self.private.public_key().public_bytes_raw())

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public)

    def sign(self, purpose: str, fields: dict[str, Any]) -> str:
        return b64(self.private.sign(canonical(purpose, fields)))

    def redeem_proof(self, invite: str) -> str:
        at = now_ms()
        return f"{at}.{self.sign('redeem', {'invite': invite_hash(invite), 'at': at})}"

    def door_proof(self, hashed_invite: str) -> str:
        return self.sign("door", {"invite": hashed_invite})

    def signed_message(self, *, text: str, kind: str = "text", task: str = "", by: str = "", to: str = "", card: dict[str, Any] | None = None) -> dict[str, Any]:
        """The ``signed`` field a message carries: this key, when, and the signature."""
        at = now_ms()
        return {"key": self.public, "at": at, "sig": self.sign("message", message_fields(text=text, kind=kind, task=task, by=by, to=to, at=at, card=card))}

    def contact(self, *, invite: str, name: str, answer: str) -> dict[str, Any]:
        """An offer of a contact, or the answer to one, signed: what the ``contact`` field of such a message holds."""
        at = now_ms()
        return {"key": self.public, "name": name, "invite": invite, "answer": answer, "at": at, "sig": self.sign("contact", contact_fields(invite=invite, name=name, answer=answer, at=at))}


def check_contact(offer: Any) -> bool:
    """Whether an offer or an answer is signed by the key it names. Which key that should be is the caller's to say."""
    if not isinstance(offer, dict):
        return False
    at = offer.get("at")
    if isinstance(at, bool) or not isinstance(at, int):
        return False
    fields = contact_fields(invite=str(offer.get("invite") or ""), name=str(offer.get("name") or ""), answer=str(offer.get("answer") or ""), at=at)
    return verify(str(offer.get("key") or ""), str(offer.get("sig") or ""), "contact", fields)


_clock_lock = threading.Lock()
_last_ms = [0]


def now_ms() -> int:
    """Milliseconds, never the same twice in this process and never going back: the receiving side orders by them."""
    with _clock_lock:
        _last_ms[0] = max(int(time.time() * 1000), _last_ms[0] + 1)
        return _last_ms[0]


def load(path: Path) -> Identity:
    """The installation's key, made on first use. Written owner-only, in a directory only the owner reads, before a
    byte of it lands on disk; a file that is not a key is an error rather than a new identity nobody pinned."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b""
    if len(data) == 32:
        return Identity(Ed25519PrivateKey.from_private_bytes(data))
    if data:
        raise ValueError(f"{path.name} is not an Ed25519 key; move it aside to make a new one (every contact pinned to the old key is lost with it)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # a filesystem without modes: Windows keeps the folder in the user's profile
        path.parent.chmod(0o700)
    private = Ed25519PrivateKey.generate()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(private.private_bytes_raw())
        handle.flush()
        os.fsync(handle.fileno())
    return Identity(private)


__all__ = [
    "DOMAIN", "KEY_FILE", "REPLAY_SKEW_MS", "Identity", "canonical", "card_digest", "check_contact", "check_message", "check_redeem", "contact_fields", "fingerprint", "invite_hash", "load",
    "message_fields", "now_ms", "verify",
]
