"""Pairing links: the way into the app on an installation that has no Telegram to vouch for a browser.

The server mints one code at every start that finds no Telegram bot and writes the link it belongs
to next to the database (``<state_dir>/pairing-url``, readable by the operator only); the log names
the file, never the link. Opening that link once turns the browser into a signed-in one (after the
two-factor code, when that is on). ``daedalus auth pair`` makes another at any time.

The desktop launcher mints its own with ``daedalus auth pair --window`` for the window it runs beside.
Such a code is marked, lives two minutes, is never written to the file, and skips the two-factor step:
minting one takes running a command inside the installation, which is more than a code would guard.

A code is stored as its SHA-256 only: whoever reads the database reads no usable link. It lives for
half an hour, is spent the first time it is opened, and spending one revokes every other code that is
still outstanding — a link that did its job leaves no second copy working in a log or a paste buffer.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from daedalus.stores.database import Database

TTL_MINUTES = 30
WINDOW_TTL_MINUTES = 2
"""A launcher's link for its window is opened the moment it is printed; nothing else should find it alive."""
URL_FILE = "pairing-url"


@dataclass(frozen=True, slots=True)
class Redeemed:
    """A code that was just spent, and whether the launcher minted it for its own window."""

    for_window: bool


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def pairing_url(base: str, code: str) -> str:
    return f"{base.rstrip('/')}/api/auth/pair?code={quote(code)}"


async def mint(db: Database, *, note: str = "", ttl_minutes: int = TTL_MINUTES, for_window: bool = False) -> str:
    """A fresh code, returned once — only its digest is kept."""
    now = datetime.now(UTC)
    await db.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (now.isoformat(),))
    code = secrets.token_urlsafe(24)
    await db.execute(
        "INSERT INTO pairing_codes(code_hash, note, created_at, expires_at, for_window) VALUES (?, ?, ?, ?, ?)",
        (_digest(code), note, now.isoformat(), (now + timedelta(minutes=ttl_minutes)).isoformat(), int(for_window)),
    )
    return code


async def redeem(db: Database, code: str, *, state_dir: Path | None = None) -> Redeemed | None:
    """Spend a code: an answer once, for one that exists and has not expired, and None ever after.

    The link file goes with it: what it held is no longer a way in, and a launcher that opens the
    file's link on every start must not send the operator to a spent one."""
    row = await db.fetchone("SELECT id, expires_at, used_at, for_window FROM pairing_codes WHERE code_hash = ?", (_digest(code or ""),))
    if row is None or row["used_at"] is not None:
        return None
    now = datetime.now(UTC)
    if datetime.fromisoformat(row["expires_at"]) <= now:
        return None
    await db.execute("UPDATE pairing_codes SET used_at = ? WHERE id = ?", (now.isoformat(), row["id"]))
    await db.execute("DELETE FROM pairing_codes WHERE used_at IS NULL")
    if state_dir is not None:
        (state_dir / URL_FILE).unlink(missing_ok=True)
    return Redeemed(for_window=bool(row["for_window"]))


async def outstanding(db: Database) -> int:
    """How many codes could still be opened; what the login page means by "pairing is on"."""
    row = await db.fetchone(
        "SELECT count(*) c FROM pairing_codes WHERE used_at IS NULL AND expires_at > ?", (datetime.now(UTC).isoformat(),)
    )
    return int(row["c"]) if row else 0


async def announce(db: Database, state_dir: Path, base: str, *, note: str = "server start") -> str:
    """Mint a code and leave the link it belongs to where only the operator can read it."""
    url = pairing_url(base, await mint(db, note=note))
    path = state_dir / URL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)  # touch leaves an existing file's mode alone
    path.write_text(url + "\n", encoding="utf-8")
    return url


__all__ = ["TTL_MINUTES", "URL_FILE", "WINDOW_TTL_MINUTES", "Redeemed", "announce", "mint", "outstanding", "pairing_url", "redeem"]
